from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from cogdoc.api.connector_scope import KBIncarnationChanged, guarded_kb_mutation
from cogdoc.ha.session_store import DistributedSessionStore, StaleSessionLease
from cogdoc.service.retriever_factory import RetrieverFactory
from cogdoc.tools.retriever.scope import RetrievalScope


class _NoAuxiliaryRetriever:
    def search(self, _kb_id: str, _query: str, *, top_k: int, **_kwargs: Any) -> list:
        del top_k
        return []


class HAChatStateRuntimeView:
    """Hide node-local auxiliary retrieval state from HA chat execution.

    The document retriever is supplied by the pinned HA index provider.  Derived
    knowledge and retrieval-feedback stores are still node-local today, so
    consulting them would make identical requests depend on the serving node.
    """

    def __init__(self, runtime: Any, *, shared_auxiliary: bool = False) -> None:
        self._runtime = runtime
        self.retrieval_feedback_store = (
            runtime.retrieval_feedback_store if shared_auxiliary else None
        )
        self.derived_knowledge_retriever = (
            runtime.derived_knowledge_retriever
            if shared_auxiliary
            else _NoAuxiliaryRetriever()
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)


def ha_retrieval_scope(
    scope: RetrievalScope, *, include_shared_auxiliary: bool = False
) -> RetrievalScope:
    """Keep source ACLs and exclude only node-local auxiliary channels."""

    return (
        scope
        if include_shared_auxiliary
        else scope.intersect(RetrievalScope(include_derived_knowledge=False))
    )


class HAChatCoordinator:
    """One-node-at-a-time chat execution over a frozen HA index generation."""

    def __init__(
        self,
        session_store: DistributedSessionStore,
        index_provider: Any,
        kb_registry: Any,
        *,
        worker_id: str,
        session_lease_seconds: float = 300.0,
    ) -> None:
        if getattr(index_provider, "registry", None) is not kb_registry:
            raise ValueError("HA chat coordinator dependencies disagree on KB registry")
        if not callable(index_provider) or not callable(
            getattr(index_provider, "pin", None)
        ):
            raise ValueError("HA chat index provider contract is invalid")
        if not worker_id or worker_id != worker_id.strip():
            raise ValueError("HA chat worker_id is invalid")
        if not 5 <= session_lease_seconds <= 3600:
            raise ValueError("HA chat session lease is invalid")
        self.session_store = session_store
        self.index_provider = index_provider
        self.kb_registry = kb_registry
        self.worker_id = worker_id
        self.session_lease_seconds = float(session_lease_seconds)

    @staticmethod
    def _display_messages(
        query: str,
        result: Any,
        *,
        index_generation_id: str,
    ) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": query},
            {
                "role": "assistant",
                "content": str(result.answer),
                "trace_id": str(result.trace_id),
                "query": query,
                "task_type": str(result.task_type),
                "index_generation_id": index_generation_id,
            },
        ]

    def _commit(
        self,
        *,
        tenant_id: str,
        storage_id: str,
        expected_epoch: int,
        session_scope_id: str,
        session_id: str | None,
        query: str,
        result: Any,
        index_generation_id: str,
        authority_guard: Callable[[], None],
    ) -> None:
        def record() -> None:
            authority_guard()
            self.session_store.record(
                session_scope_id,
                session_id,
                list(result.chat_messages),
                self._display_messages(
                    query,
                    result,
                    index_generation_id=index_generation_id,
                ),
                authority=getattr(authority_guard, "evidence", None),
            )

        authority = getattr(authority_guard, "evidence", None)
        if authority is not None:
            # DistributedSessionStore checks KB/ACL/login authority again inside
            # the exact database transaction that appends the turn.
            record()
            return
        try:
            guarded_kb_mutation(
                self.kb_registry,
                tenant_id,
                storage_id,
                expected_epoch,
                record,
            )
        except KBIncarnationChanged as exc:
            raise StaleSessionLease("chat knowledge base changed") from exc

    def run(
        self,
        *,
        tenant_id: str,
        storage_id: str,
        expected_epoch: int,
        session_scope_id: str,
        session_id: str | None,
        query: str,
        authority_guard: Callable[[], None],
        runner: Callable[[list[dict[str, Any]]], Any],
    ) -> Any:
        with self.session_store.execution(
            session_scope_id,
            session_id,
            self.worker_id,
            lease_seconds=self.session_lease_seconds,
            authority=getattr(authority_guard, "evidence", None),
            storage_id=storage_id,
        ):
            history = self.session_store.get_history(
                session_scope_id, session_id, query
            )
            authority_guard()
            with (
                self.index_provider.pin(storage_id) as snapshot,
                RetrieverFactory.provider_context(self.index_provider),
            ):
                check_reader = snapshot.get("check", lambda: None)
                result = runner(history)
                check_reader()
                self._commit(
                    tenant_id=tenant_id,
                    storage_id=storage_id,
                    expected_epoch=expected_epoch,
                    session_scope_id=session_scope_id,
                    session_id=session_id,
                    query=query,
                    result=result,
                    index_generation_id=str(snapshot["generation_id"]),
                    authority_guard=authority_guard,
                )
            return result

    def stream(
        self,
        *,
        tenant_id: str,
        storage_id: str,
        expected_epoch: int,
        session_scope_id: str,
        session_id: str | None,
        query: str,
        authority_guard: Callable[[], None],
        runner: Callable[[list[dict[str, Any]]], Iterator[Any]],
        stop_requested: Callable[[], bool] = lambda: False,
    ) -> Iterator[Any]:
        with self.session_store.execution(
            session_scope_id,
            session_id,
            self.worker_id,
            lease_seconds=self.session_lease_seconds,
            authority=getattr(authority_guard, "evidence", None),
            storage_id=storage_id,
        ):
            history = self.session_store.get_history(
                session_scope_id, session_id, query
            )
            authority_guard()
            committed = False
            with (
                self.index_provider.pin(storage_id) as snapshot,
                RetrieverFactory.provider_context(self.index_provider),
            ):
                check_reader = snapshot.get("check", lambda: None)
                for event in runner(history):
                    if stop_requested():
                        raise StaleSessionLease("chat stream was abandoned")
                    self.session_store.assert_execution(session_scope_id, session_id)
                    # Streaming is an authorization boundary: never release a
                    # frame after session/ACL/KB authority has changed.
                    authority_guard()
                    check_reader()
                    final_result = (
                        event.payload["result"]
                        if getattr(event, "type", "") == "final"
                        else None
                    )
                    authority_guard()
                    yield event
                    # Commit only after the consumer resumes the generator.  In
                    # the HTTP path that happens after the final SSE frame was
                    # handed to ASGI.  A disconnect/idle timeout closes the
                    # generator at the yield and therefore cannot create a
                    # conversation turn the client never received.
                    if final_result is not None:
                        if stop_requested():
                            raise StaleSessionLease("chat stream was abandoned")
                        authority_guard()
                        self.session_store.assert_execution(
                            session_scope_id, session_id
                        )
                        check_reader()
                        self._commit(
                            tenant_id=tenant_id,
                            storage_id=storage_id,
                            expected_epoch=expected_epoch,
                            session_scope_id=session_scope_id,
                            session_id=session_id,
                            query=query,
                            result=final_result,
                            index_generation_id=str(snapshot["generation_id"]),
                            authority_guard=authority_guard,
                        )
                        committed = True
            if not committed and not stop_requested():
                # Error/aborted streams do not mutate conversation memory.
                authority_guard()

    def check(self) -> bool:
        provider_check = getattr(self.index_provider, "check", None)
        return bool(
            self.session_store.check()
            and callable(getattr(self.index_provider, "pin", None))
            and (not callable(provider_check) or provider_check())
        )


__all__ = ["HAChatCoordinator", "HAChatStateRuntimeView", "ha_retrieval_scope"]
