from __future__ import annotations

import copy
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Literal, Protocol

from cogdoc.graph.state import EvidenceLedgerEntry, RetrievedDoc
from cogdoc.service.evidence_units import EvidenceUnit, EvidenceUnitBudget
from cogdoc.service.retrieval_pipeline import (
    RetrievalQuery,
    retrieve_candidate_pool,
)
from cogdoc.tools.citation_ledger import assign_evidence_ids
from cogdoc.tools.evidence_rendering import (
    EVIDENCE_BLOCK_SEPARATOR,
    evidence_block_char_count,
)
from cogdoc.tools.reranker import BGEReranker, skipped_cpu_rerank_docs
from cogdoc.tools.retriever.evidence_pack import build_evidence_pack_from_sources
from cogdoc.tools.retriever.evidence_spans import EvidenceSpanSelector
from cogdoc.tools.retriever.parent_context import select_parent_context
from cogdoc.tools.retriever.retrieval_text import retrieval_text
from cogdoc.tools.retriever.scope import RetrievalScope
from cogdoc.tools.tokenizer import tokenize_mixed_text


class EvidenceUnitExecutionStatus(str, Enum):
    """Pre-generation execution state, deliberately distinct from verification."""

    READY = "ready"
    NO_EVIDENCE = "no_evidence"
    RETRIEVAL_ERROR = "retrieval_error"
    BUDGET_EXHAUSTED = "budget_exhausted"


def evidence_unit_plan_state(unit: EvidenceUnit) -> dict[str, Any]:
    """Return the stable, text-bounded runtime plan exposed to state and trace."""

    return {
        "unit_id": unit.unit_id,
        "task_kind": unit.task_kind.value,
        "label": unit.label,
        "instruction": unit.instruction,
        "retrieval_query": unit.retrieval_query,
        "recovery_query": unit.recovery_query,
        "allowed_sources": list(unit.scope.allowed_sources),
        "allow_derived_knowledge": unit.scope.allow_derived_knowledge,
        "admission_group": unit.policy.admission_group,
        "required": unit.policy.required,
        "priority": unit.policy.priority,
        "max_retrieval_retries": unit.policy.max_retrieval_retries,
        "binding": unit.binding_metadata,
    }


@dataclass(frozen=True, slots=True)
class EvidenceUnitPipelinePolicy:
    retrieval_top_k: int = 9
    recovery_top_k: int = 12
    rerank_max_candidates: int = 12
    rerank_top_n: int = 3
    rerank_on_cpu: bool = False
    parent_context_enabled: bool = True
    parent_context_max_chunks: int = 5
    parent_context_max_chars: int = 3600
    neighbor_context_radius: int = 1
    evidence_span_enabled: bool = True
    evidence_span_max_chars_per_doc: int = 420
    evidence_span_context_sentences: int = 1

    def __post_init__(self) -> None:
        positive = (
            "retrieval_top_k",
            "recovery_top_k",
            "rerank_max_candidates",
            "rerank_top_n",
            "parent_context_max_chunks",
            "parent_context_max_chars",
            "evidence_span_max_chars_per_doc",
        )
        for name in positive:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("neighbor_context_radius", "evidence_span_context_sentences"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in (
            "rerank_on_cpu",
            "parent_context_enabled",
            "evidence_span_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if self.rerank_top_n > self.rerank_max_candidates:
            raise ValueError("rerank_top_n cannot exceed rerank_max_candidates")


@dataclass(frozen=True, slots=True)
class EvidenceUnitExecutionResult:
    unit: EvidenceUnit
    status: EvidenceUnitExecutionStatus
    selected_docs: tuple[RetrievedDoc, ...] = ()
    retrieval_round: int = 0
    executed_queries: tuple[str, ...] = ()
    candidate_count: int = 0
    selected_chars: int = 0
    parent_context_count: int = 0
    neighbor_context_count: int = 0
    scope_violation_count: int = 0
    span_input_chars: int = 0
    span_selected_chars: int = 0
    reason_code: str = ""
    error_class: str = ""
    fallback_used: bool = False

    def __post_init__(self) -> None:
        if self.status is EvidenceUnitExecutionStatus.READY:
            if not self.selected_docs:
                raise ValueError("ready evidence unit requires selected_docs")
            if self.reason_code:
                raise ValueError("ready evidence unit cannot carry a failure reason")
        elif self.selected_docs:
            raise ValueError("failed evidence unit must fail closed without documents")

    def to_state(self) -> dict[str, Any]:
        return {
            **evidence_unit_plan_state(self.unit),
            "status": self.status.value,
            "selected_docs": list(self.selected_docs),
            "retrieval_round": self.retrieval_round,
            "executed_queries": list(self.executed_queries),
            "candidate_count": self.candidate_count,
            "selected_count": len(self.selected_docs),
            "selected_chars": self.selected_chars,
            "parent_context_count": self.parent_context_count,
            "neighbor_context_count": self.neighbor_context_count,
            "scope_violation_count": self.scope_violation_count,
            "span_input_chars": self.span_input_chars,
            "span_selected_chars": self.span_selected_chars,
            "reason_code": self.reason_code,
            "error_class": self.error_class,
            "fallback_used": self.fallback_used,
        }


@dataclass(frozen=True, slots=True)
class EvidenceUnitBatchResult:
    results: tuple[EvidenceUnitExecutionResult, ...]
    evidence_ledger: tuple[EvidenceLedgerEntry, ...] = ()
    channel_counts: Mapping[str, int] = field(default_factory=dict)
    ranking_count: int = 0
    feedback_errors: tuple[str, ...] = ()

    @property
    def ready_docs(self) -> tuple[RetrievedDoc, ...]:
        """Return the exact final evidence-view union in frozen EID order."""

        unique: "OrderedDict[str, RetrievedDoc]" = OrderedDict()
        for result in self.results:
            if result.status is not EvidenceUnitExecutionStatus.READY:
                continue
            for doc in result.selected_docs:
                evidence_id = str(doc.get("retrieval", {}).get("evidence_id") or "")
                key = evidence_id or f"{_chunk_id(doc)}:{len(unique)}"
                unique.setdefault(key, doc)
        return tuple(unique.values())

    @property
    def ready_docs_by_source(self) -> dict[str, list[RetrievedDoc]]:
        grouped: dict[str, list[RetrievedDoc]] = {}
        for doc in self.ready_docs:
            grouped.setdefault(_source(doc), []).append(doc)
        return {source: docs for source, docs in grouped.items() if source}

    @property
    def metrics(self) -> dict[str, Any]:
        counts = {status.value: 0 for status in EvidenceUnitExecutionStatus}
        for result in self.results:
            counts[result.status.value] += 1
        ready = counts[EvidenceUnitExecutionStatus.READY.value]
        return {
            "planned_count": len(self.results),
            "ready_count": ready,
            "no_evidence_count": counts[EvidenceUnitExecutionStatus.NO_EVIDENCE.value],
            "retrieval_error_count": counts[
                EvidenceUnitExecutionStatus.RETRIEVAL_ERROR.value
            ],
            "budget_exhausted_count": counts[
                EvidenceUnitExecutionStatus.BUDGET_EXHAUSTED.value
            ],
            "recovered_count": sum(
                result.retrieval_round > 0
                and result.status is EvidenceUnitExecutionStatus.READY
                for result in self.results
            ),
            "fallback_count": sum(result.fallback_used for result in self.results),
            "selected_doc_count": sum(
                len(result.selected_docs) for result in self.results
            ),
            "selected_chars": sum(result.selected_chars for result in self.results),
            "scope_violation_count": sum(
                result.scope_violation_count for result in self.results
            ),
            "parent_context_count": sum(
                result.parent_context_count for result in self.results
            ),
            "neighbor_context_count": sum(
                result.neighbor_context_count for result in self.results
            ),
            "ranking_count": self.ranking_count,
            "channel_counts": dict(self.channel_counts),
            "feedback_error_count": len(self.feedback_errors),
            "coverage_rate": ready / len(self.results) if self.results else 0.0,
        }

    def to_state(self) -> dict[str, Any]:
        return {
            "evidence_units": [
                evidence_unit_plan_state(result.unit) for result in self.results
            ],
            "evidence_unit_results": [result.to_state() for result in self.results],
            "evidence_unit_metrics": self.metrics,
            "evidence_ledger": list(self.evidence_ledger),
        }


class EvidenceUnitRetrievalEngine(Protocol):
    def search(
        self,
        query: str,
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> list[RetrievedDoc]: ...

    def load_source_chunks(self, source: str) -> list[RetrievedDoc]: ...


class EvidenceUnitDerivedRetriever(Protocol):
    def search(
        self,
        kb_id: str,
        query: str,
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> list[RetrievedDoc]: ...


class EvidenceUnitFeedbackStore(Protocol):
    def boosts_for_query(self, kb_id: str, query: str) -> dict[str, float]: ...


def _runtime_scope(unit: EvidenceUnit) -> RetrievalScope:
    return RetrievalScope(
        allowed_sources=unit.scope.allowed_sources,
        include_derived_knowledge=unit.scope.allow_derived_knowledge,
    )


def _scope_key(unit: EvidenceUnit) -> tuple[tuple[str, ...], bool]:
    scope = _runtime_scope(unit)
    return scope.allowed_sources, scope.include_derived_knowledge


def _retriever_scope(scope: RetrievalScope) -> RetrievalScope | None:
    """Keep the legacy unscoped call shape when the scope is a true no-op."""

    if scope.allows_all_sources and scope.include_derived_knowledge:
        return None
    return scope


def _matched_unit_ids(doc: Mapping[str, Any]) -> set[str]:
    retrieval = doc.get("retrieval")
    if not isinstance(retrieval, Mapping):
        return set()
    values = retrieval.get("matched_requirement_ids")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return set()
    return {str(value).strip() for value in values if str(value).strip()}


def _chunk_id(doc: Mapping[str, Any]) -> str:
    meta = doc.get("meta")
    return str(meta.get("chunk_id") or "") if isinstance(meta, Mapping) else ""


def _source(doc: Mapping[str, Any]) -> str:
    meta = doc.get("meta")
    return str(meta.get("source") or "") if isinstance(meta, Mapping) else ""


def _filter_scope(
    docs: Sequence[RetrievedDoc], scope: RetrievalScope
) -> tuple[list[RetrievedDoc], int]:
    allowed: list[RetrievedDoc] = []
    violations = 0
    for doc in docs:
        if scope.allows_document(doc):
            allowed.append(doc)
        else:
            violations += 1
    return allowed, violations


def _annotate_unit_match(doc: RetrievedDoc, unit_id: str) -> RetrievedDoc:
    snapshot = copy.deepcopy(doc)
    retrieval = snapshot.setdefault("retrieval", {})
    current = retrieval.get("matched_requirement_ids")
    matched = [str(value) for value in current] if isinstance(current, list) else []
    if unit_id not in matched:
        matched.append(unit_id)
    retrieval["matched_requirement_ids"] = matched
    retrieval["matched_unit_ids"] = list(matched)
    retrieval.setdefault("search_channel", "source_lexical_fallback")
    return snapshot


def _lexical_fallback(
    unit: EvidenceUnit,
    docs_by_source: Mapping[str, Sequence[RetrievedDoc]],
    *,
    limit: int,
    query: str | None = None,
) -> list[RetrievedDoc]:
    candidates = [
        doc
        for source in unit.scope.allowed_sources
        for doc in docs_by_source.get(source, ())
    ]
    if not candidates:
        return []
    query_tokens = set(tokenize_mixed_text(query or unit.retrieval_query))
    scored: list[tuple[int, int, RetrievedDoc]] = []
    for index, doc in enumerate(candidates):
        score = len(query_tokens & set(tokenize_mixed_text(retrieval_text(doc))))
        scored.append((score, index, doc))
    positives = [item for item in scored if item[0] > 0]
    selected = (
        sorted(positives, key=lambda item: (-item[0], item[1])) if positives else scored
    )[:limit]
    return [_annotate_unit_match(doc, unit.unit_id) for _, _, doc in selected]


def _merge_by_chunk_id(*groups: Sequence[RetrievedDoc]) -> list[RetrievedDoc]:
    merged: "OrderedDict[str, RetrievedDoc]" = OrderedDict()
    missing = 0
    for group in groups:
        for doc in group:
            key = _chunk_id(doc)
            if not key:
                key = f"__missing_{missing}"
                missing += 1
            merged.setdefault(key, doc)
    return list(merged.values())


def _rerank(
    unit: EvidenceUnit,
    docs: Sequence[RetrievedDoc],
    policy: EvidenceUnitPipelinePolicy,
    *,
    query: str | None = None,
) -> tuple[list[RetrievedDoc], str]:
    candidates = list(docs[: policy.rerank_max_candidates])
    target_device = BGEReranker.default_device()
    if target_device == "cpu" and not policy.rerank_on_cpu:
        return (
            skipped_cpu_rerank_docs(candidates, len(candidates), "cpu_disabled"),
            "cpu_disabled",
        )
    try:
        return (
            BGEReranker.rerank(
                query=query or unit.retrieval_query,
                docs=candidates,
                top_n=len(candidates),
                device=target_device,
            ),
            "",
        )
    except Exception as exc:
        return (
            skipped_cpu_rerank_docs(
                candidates, len(candidates), f"error:{type(exc).__name__}"
            ),
            type(exc).__name__,
        )


def _copy_context_doc(
    doc: RetrievedDoc, *, anchor_chunk_id: str, expansion: str
) -> RetrievedDoc:
    snapshot = copy.deepcopy(doc)
    retrieval = snapshot.setdefault("retrieval", {})
    retrieval["search_channel"] = (
        "parent_context" if expansion == "section" else "neighbor"
    )
    retrieval["context_anchor_chunk_id"] = anchor_chunk_id
    retrieval["context_expansion"] = expansion
    return snapshot


def _expand_context(
    anchors: Sequence[RetrievedDoc],
    *,
    engine: EvidenceUnitRetrievalEngine,
    fallback_docs_by_source: Mapping[str, Sequence[RetrievedDoc]],
    scope: RetrievalScope,
    policy: EvidenceUnitPipelinePolicy,
    source_cache: dict[str, list[RetrievedDoc]],
) -> tuple[list[RetrievedDoc], int, int, int]:
    if not anchors or not policy.parent_context_enabled:
        scoped, violations = _filter_scope(anchors, scope)
        return scoped, 0, 0, violations

    expanded: "OrderedDict[str, RetrievedDoc]" = OrderedDict()
    parent_count = 0
    neighbor_count = 0
    scope_violations = 0
    for anchor in anchors:
        anchor_id = _chunk_id(anchor)
        source = _source(anchor)
        if not anchor_id or not source:
            continue
        if source not in source_cache:
            try:
                source_cache[source] = list(engine.load_source_chunks(source))
            except Exception:
                source_cache[source] = list(fallback_docs_by_source.get(source, ()))
        source_chunks, violations = _filter_scope(source_cache[source], scope)
        scope_violations += violations
        selection = select_parent_context(
            source_chunks,
            anchor,
            max_chunks=policy.parent_context_max_chunks,
            max_chars=policy.parent_context_max_chars,
        )
        context_docs = list(selection.docs)
        expansion = "section"
        if selection.fallback_required:
            expansion = "neighbor"
            anchor_index = next(
                (
                    index
                    for index, doc in enumerate(source_chunks)
                    if _chunk_id(doc) == anchor_id
                ),
                -1,
            )
            if anchor_index >= 0:
                start = max(0, anchor_index - policy.neighbor_context_radius)
                end = min(
                    len(source_chunks),
                    anchor_index + policy.neighbor_context_radius + 1,
                )
                context_docs = source_chunks[start:end]
            else:
                context_docs = [anchor]

        for context_doc in context_docs:
            chunk_id = _chunk_id(context_doc)
            if not chunk_id or chunk_id in expanded:
                continue
            if chunk_id == anchor_id:
                expanded[chunk_id] = copy.deepcopy(anchor)
            else:
                expanded[chunk_id] = _copy_context_doc(
                    context_doc,
                    anchor_chunk_id=anchor_id,
                    expansion=expansion,
                )
                if expansion == "section":
                    parent_count += 1
                else:
                    neighbor_count += 1
    scoped, violations = _filter_scope(list(expanded.values()), scope)
    return scoped, parent_count, neighbor_count, scope_violations + violations


def _pack_unit(
    unit: EvidenceUnit,
    *,
    ranked: Sequence[RetrievedDoc],
    expanded: Sequence[RetrievedDoc],
    budget: EvidenceUnitBudget,
    policy: EvidenceUnitPipelinePolicy,
    query: str | None = None,
) -> tuple[list[RetrievedDoc], bool, int, int]:
    anchors = list(ranked[: min(policy.rerank_top_n, budget.max_docs_per_unit)])
    selector = None
    if policy.evidence_span_enabled:
        selector = EvidenceSpanSelector(
            query=query or unit.retrieval_query,
            evidence_requirements=(
                {
                    "requirement_id": unit.unit_id,
                    "question": unit.instruction,
                    "retrieval_query": unit.retrieval_query,
                    "recovery_query": unit.recovery_query,
                },
            ),
            max_chars_per_doc=policy.evidence_span_max_chars_per_doc,
            context_sentences=policy.evidence_span_context_sentences,
        )

    def transform(doc: RetrievedDoc, matched_unit_ids: tuple[str, ...]) -> RetrievedDoc:
        if selector is None:
            return doc
        selected = selector.select(doc, matched_requirement_ids=matched_unit_ids)
        retrieval = selected.setdefault("retrieval", {})
        span_matches = retrieval.get("evidence_span_matched_requirement_ids")
        if isinstance(span_matches, list):
            retrieval["evidence_span_matched_unit_ids"] = list(span_matches)
        return selected

    pack = build_evidence_pack_from_sources(
        anchors=anchors,
        expanded_docs=expanded,
        verification_candidates=ranked,
        requirement_ids=(unit.unit_id,),
        max_docs=budget.max_docs_per_unit,
        max_chars=budget.max_chars_per_unit,
        document_char_cost=evidence_block_char_count,
        separator_chars=len(EVIDENCE_BLOCK_SEPARATOR),
        document_transform=transform if selector is not None else None,
    )
    input_chars = sum(
        int(doc.get("retrieval", {}).get("evidence_span_original_chars") or 0)
        for doc in pack.kept_docs
    )
    selected_chars = sum(
        int(doc.get("retrieval", {}).get("evidence_span_selected_chars") or 0)
        for doc in pack.kept_docs
    )
    return (
        list(pack.kept_docs),
        pack.over_budget_hard_constraints,
        input_chars,
        selected_chars,
    )


def _document_cost(doc: RetrievedDoc) -> int:
    return evidence_block_char_count(doc, str(doc.get("text") or ""))


def _allocate_global_budget(
    results: Sequence[EvidenceUnitExecutionResult],
    budget: EvidenceUnitBudget,
) -> list[EvidenceUnitExecutionResult]:
    """Reserve each admission group first, then distribute spare budget fairly."""

    ready_by_id = {
        result.unit.unit_id: result
        for result in results
        if result.status is EvidenceUnitExecutionStatus.READY
    }
    groups: "OrderedDict[str, list[EvidenceUnitExecutionResult]]" = OrderedDict()
    for result in results:
        if result.unit.unit_id in ready_by_id:
            groups.setdefault(result.unit.policy.admission_group, []).append(result)

    allocated: dict[str, list[RetrievedDoc]] = {}
    exhausted: set[str] = set()
    used_docs = 0
    used_chars = 0
    admitted: list[EvidenceUnitExecutionResult] = []

    for group_results in groups.values():
        required = [result for result in group_results if result.unit.policy.required]
        reservation: dict[str, list[RetrievedDoc]] = {}
        for result in required:
            docs: list[RetrievedDoc] = []
            chars = 0
            for doc in result.selected_docs:
                if (
                    len(docs) >= budget.min_docs_per_required_unit
                    and chars >= budget.min_chars_per_required_unit
                ):
                    break
                docs.append(doc)
                chars += _document_cost(doc)
            reservation[result.unit.unit_id] = docs
        reservation_docs = sum(len(docs) for docs in reservation.values())
        reservation_chars = sum(
            _document_cost(doc) for docs in reservation.values() for doc in docs
        )
        if (
            any(
                len(docs) < budget.min_docs_per_required_unit
                or sum(_document_cost(doc) for doc in docs)
                < budget.min_chars_per_required_unit
                for docs in reservation.values()
            )
            or used_docs + reservation_docs > budget.max_total_docs
            or used_chars + reservation_chars > budget.max_total_chars
        ):
            exhausted.update(result.unit.unit_id for result in group_results)
            continue
        for result in group_results:
            docs = reservation.get(result.unit.unit_id, [])
            allocated[result.unit.unit_id] = docs
            used_docs += len(docs)
            used_chars += sum(_document_cost(doc) for doc in docs)
            admitted.append(result)

    # Round-robin prevents source-major Compare tasks from spending all spare
    # context on the first source or the first long document.
    cursor = {
        result.unit.unit_id: len(allocated.get(result.unit.unit_id, []))
        for result in admitted
    }
    while admitted and used_docs < budget.max_total_docs:
        progressed = False
        for result in admitted:
            unit_id = result.unit.unit_id
            index = cursor[unit_id]
            if index >= len(result.selected_docs):
                continue
            doc = result.selected_docs[index]
            cursor[unit_id] += 1
            cost = _document_cost(doc)
            if used_chars + cost > budget.max_total_chars:
                continue
            allocated[unit_id].append(doc)
            used_docs += 1
            used_chars += cost
            progressed = True
            if used_docs >= budget.max_total_docs:
                break
        if not progressed:
            break

    output: list[EvidenceUnitExecutionResult] = []
    for result in results:
        unit_id = result.unit.unit_id
        if unit_id in exhausted:
            output.append(
                replace(
                    result,
                    status=EvidenceUnitExecutionStatus.BUDGET_EXHAUSTED,
                    selected_docs=(),
                    selected_chars=0,
                    reason_code="batch_budget_exhausted",
                )
            )
            continue
        if result.status is not EvidenceUnitExecutionStatus.READY:
            output.append(result)
            continue
        allocated_docs = tuple(allocated.get(unit_id, ()))
        if not allocated_docs:
            output.append(
                replace(
                    result,
                    status=EvidenceUnitExecutionStatus.BUDGET_EXHAUSTED,
                    selected_docs=(),
                    selected_chars=0,
                    reason_code="batch_budget_exhausted",
                )
            )
            continue
        output.append(
            replace(
                result,
                selected_docs=allocated_docs,
                selected_chars=sum(_document_cost(doc) for doc in allocated_docs),
            )
        )
    return output


def _freeze_evidence_ids(
    results: Sequence[EvidenceUnitExecutionResult],
) -> tuple[list[EvidenceUnitExecutionResult], list[EvidenceLedgerEntry]]:
    flattened = [
        doc
        for result in results
        if result.status is EvidenceUnitExecutionStatus.READY
        for doc in result.selected_docs
    ]
    annotated, ledger = assign_evidence_ids(flattened) if flattened else ([], [])
    cursor = 0
    frozen: list[EvidenceUnitExecutionResult] = []
    for result in results:
        count = (
            len(result.selected_docs)
            if result.status is EvidenceUnitExecutionStatus.READY
            else 0
        )
        docs = tuple(annotated[cursor : cursor + count])
        cursor += count
        frozen.append(replace(result, selected_docs=docs))
    return frozen, ledger


def retrieve_evidence_units(
    units: Sequence[EvidenceUnit],
    *,
    kb_id: str,
    original_query: str,
    engine: EvidenceUnitRetrievalEngine,
    derived_knowledge_retriever: EvidenceUnitDerivedRetriever,
    retrieval_feedback_store: EvidenceUnitFeedbackStore | None,
    budget: EvidenceUnitBudget,
    policy: EvidenceUnitPipelinePolicy,
    rrf_k: float,
    fallback_docs_by_source: Mapping[str, Sequence[RetrievedDoc]] | None = None,
    query_phase: Literal["primary", "recovery"] = "primary",
    retrieval_round: int = 0,
    authorization_scope: RetrievalScope | None = None,
) -> EvidenceUnitBatchResult:
    """Execute one source-safe retrieval/pack plan for any agent's units.

    The function stops at a generation-ready closed evidence set.  It does not
    claim semantic support; a task-neutral verifier can promote ``ready`` units
    to supported/no-evidence in the next stage without changing retrieval.
    """

    if query_phase not in {"primary", "recovery"}:
        raise ValueError("query_phase must be 'primary' or 'recovery'")
    if (
        isinstance(retrieval_round, bool)
        or not isinstance(retrieval_round, int)
        or retrieval_round < 0
    ):
        raise ValueError("retrieval_round must be a non-negative integer")
    if query_phase == "primary" and retrieval_round != 0:
        raise ValueError("primary query phase must start at retrieval_round 0")
    if query_phase == "recovery" and retrieval_round < 1:
        raise ValueError("recovery query phase requires retrieval_round >= 1")

    normalized_units = tuple(units)
    if not normalized_units:
        raise ValueError("at least one evidence unit is required")
    if len({unit.unit_id for unit in normalized_units}) != len(normalized_units):
        raise ValueError("unit_id values must be unique")
    budget.validate_plan_capacity(normalized_units)
    fallback_docs_by_source = fallback_docs_by_source or {}

    grouped: "OrderedDict[tuple[tuple[str, ...], bool], list[EvidenceUnit]]" = (
        OrderedDict()
    )
    for unit in normalized_units:
        grouped.setdefault(_scope_key(unit), []).append(unit)

    candidates_by_unit: dict[str, list[RetrievedDoc]] = {}
    query_names_by_unit: dict[str, list[str]] = {
        unit.unit_id: [] for unit in normalized_units
    }
    group_errors: dict[str, str] = {}
    scope_violations: dict[str, int] = {unit.unit_id: 0 for unit in normalized_units}
    channel_counts: dict[str, int] = {}
    ranking_count = 0
    feedback_errors: list[str] = []

    for group_units in grouped.values():
        scope = _runtime_scope(group_units[0])
        if authorization_scope is not None:
            scope = scope.intersect(authorization_scope)
        retriever_scope = _retriever_scope(scope)
        queries = [
            RetrievalQuery(
                text=(
                    unit.recovery_query
                    if query_phase == "recovery"
                    else unit.retrieval_query
                ),
                requirement_ids=(unit.unit_id,),
            )
            for unit in group_units
        ]
        try:
            batch = retrieve_candidate_pool(
                engine=engine,
                derived_knowledge_retriever=derived_knowledge_retriever,
                retrieval_feedback_store=retrieval_feedback_store,
                kb_id=kb_id,
                original_query=original_query,
                queries=queries,
                top_k=policy.retrieval_top_k,
                rrf_k=rrf_k,
                fusion_top_n=policy.rerank_max_candidates * len(group_units),
                retrieval_round=retrieval_round,
                scope=retriever_scope,
            )
            scoped_docs, violations = _filter_scope(batch.docs, scope)
            ranking_count += batch.ranking_count
            for channel, count in batch.channel_counts.items():
                channel_counts[channel] = channel_counts.get(channel, 0) + count
            if batch.feedback_error:
                feedback_errors.append(batch.feedback_error)
            for unit in group_units:
                active_query = (
                    unit.recovery_query
                    if query_phase == "recovery"
                    else unit.retrieval_query
                )
                query_names_by_unit[unit.unit_id].append(active_query)
                candidates_by_unit[unit.unit_id] = [
                    _annotate_unit_match(doc, unit.unit_id)
                    for doc in scoped_docs
                    if unit.unit_id in _matched_unit_ids(doc)
                ]
                scope_violations[unit.unit_id] += violations
        except Exception as exc:
            for unit in group_units:
                group_errors[unit.unit_id] = type(exc).__name__

    preliminary: list[EvidenceUnitExecutionResult] = []
    source_cache: dict[str, list[RetrievedDoc]] = {}
    for unit in normalized_units:
        scope = _runtime_scope(unit)
        if authorization_scope is not None:
            scope = scope.intersect(authorization_scope)
        candidates = candidates_by_unit.get(unit.unit_id, [])
        unit_retrieval_round = retrieval_round
        ranking_query = (
            unit.recovery_query
            if query_phase == "recovery"
            else unit.retrieval_query
        )
        error_class = group_errors.get(unit.unit_id, "")

        if (
            query_phase == "primary"
            and not candidates
            and unit.policy.max_retrieval_retries > 0
        ):
            query_names_by_unit[unit.unit_id].append(unit.recovery_query)
            try:
                recovery = retrieve_candidate_pool(
                    engine=engine,
                    derived_knowledge_retriever=derived_knowledge_retriever,
                    retrieval_feedback_store=retrieval_feedback_store,
                    kb_id=kb_id,
                    original_query=original_query,
                    queries=(
                        RetrievalQuery(
                            text=unit.recovery_query,
                            requirement_ids=(unit.unit_id,),
                        ),
                    ),
                    top_k=policy.recovery_top_k,
                    rrf_k=rrf_k,
                    fusion_top_n=policy.rerank_max_candidates,
                    retrieval_round=1,
                    scope=_retriever_scope(scope),
                )
                candidates, violations = _filter_scope(recovery.docs, scope)
                scope_violations[unit.unit_id] += violations
                unit_retrieval_round = 1
                ranking_query = unit.recovery_query
                ranking_count += recovery.ranking_count
                for channel, count in recovery.channel_counts.items():
                    channel_counts[channel] = channel_counts.get(channel, 0) + count
                if recovery.feedback_error:
                    feedback_errors.append(recovery.feedback_error)
                error_class = ""
            except Exception as exc:
                error_class = type(exc).__name__

        fallback_used = False
        if not candidates:
            candidates = _lexical_fallback(
                unit,
                fallback_docs_by_source,
                limit=policy.rerank_max_candidates,
                query=ranking_query,
            )
            fallback_used = bool(candidates)
        if not candidates:
            status = (
                EvidenceUnitExecutionStatus.RETRIEVAL_ERROR
                if error_class
                else EvidenceUnitExecutionStatus.NO_EVIDENCE
            )
            preliminary.append(
                EvidenceUnitExecutionResult(
                    unit=unit,
                    status=status,
                    retrieval_round=unit_retrieval_round,
                    executed_queries=tuple(query_names_by_unit[unit.unit_id]),
                    scope_violation_count=scope_violations[unit.unit_id],
                    reason_code=(
                        "retrieval_error" if error_class else "source_scope_exhausted"
                    ),
                    error_class=error_class,
                )
            )
            continue

        ranked, rerank_error = _rerank(
            unit,
            candidates,
            policy,
            query=ranking_query,
        )
        if rerank_error and rerank_error != "cpu_disabled" and not error_class:
            error_class = rerank_error
        expanded, parent_count, neighbor_count, violations = _expand_context(
            ranked[: policy.rerank_top_n],
            engine=engine,
            fallback_docs_by_source=fallback_docs_by_source,
            scope=scope,
            policy=policy,
            source_cache=source_cache,
        )
        scope_violations[unit.unit_id] += violations
        packed, over_budget, span_input_chars, span_selected_chars = _pack_unit(
            unit,
            ranked=ranked,
            expanded=expanded,
            budget=budget,
            policy=policy,
            query=ranking_query,
        )
        packed, violations = _filter_scope(packed, scope)
        scope_violations[unit.unit_id] += violations
        if over_budget or not packed:
            preliminary.append(
                EvidenceUnitExecutionResult(
                    unit=unit,
                    status=EvidenceUnitExecutionStatus.BUDGET_EXHAUSTED,
                    retrieval_round=unit_retrieval_round,
                    executed_queries=tuple(query_names_by_unit[unit.unit_id]),
                    candidate_count=len(candidates),
                    parent_context_count=parent_count,
                    neighbor_context_count=neighbor_count,
                    scope_violation_count=scope_violations[unit.unit_id],
                    span_input_chars=span_input_chars,
                    span_selected_chars=span_selected_chars,
                    reason_code="unit_evidence_budget_exceeded",
                    error_class=error_class,
                    fallback_used=fallback_used,
                )
            )
            continue
        preliminary.append(
            EvidenceUnitExecutionResult(
                unit=unit,
                status=EvidenceUnitExecutionStatus.READY,
                selected_docs=tuple(packed),
                retrieval_round=unit_retrieval_round,
                executed_queries=tuple(query_names_by_unit[unit.unit_id]),
                candidate_count=len(candidates),
                selected_chars=sum(_document_cost(doc) for doc in packed),
                parent_context_count=parent_count,
                neighbor_context_count=neighbor_count,
                scope_violation_count=scope_violations[unit.unit_id],
                span_input_chars=span_input_chars,
                span_selected_chars=span_selected_chars,
                error_class=error_class,
                fallback_used=fallback_used,
            )
        )

    allocated = _allocate_global_budget(preliminary, budget)
    frozen, ledger = _freeze_evidence_ids(allocated)
    return EvidenceUnitBatchResult(
        results=tuple(frozen),
        evidence_ledger=tuple(ledger),
        channel_counts=channel_counts,
        ranking_count=ranking_count,
        feedback_errors=tuple(dict.fromkeys(feedback_errors)),
    )
