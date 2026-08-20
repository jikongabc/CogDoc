from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from cogdoc.api.offload import run_sync
from cogdoc.api.connector_scope import (
    KBIncarnationChanged,
    capture_kb_epoch,
    guarded_kb_mutation,
)
from cogdoc.api.schemas import (
    ConnectorOAuthAuthorization,
    ConnectorOAuthCallback,
    ConnectorOAuthStart,
    ErrorCode,
    build_error_response,
)
from cogdoc.api.tenant_scope import (
    request_principal,
    resource_access_decision,
    resolve_kb_scope,
)
from cogdoc.api.tenancy import Permission
from cogdoc.connectors.oauth import (
    OAuthError,
    OAuthProviderError,
    OAuthReplayError,
    OAuthSessionExpired,
    OAuthStateMismatch,
)
from cogdoc.connectors.connection_store import ConnectionRevisionConflict
from cogdoc.service.kb_epoch import shared_epoch_store


router = APIRouter(tags=["connector-oauth"])


def _error(code: ErrorCode, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=build_error_response(code, message).model_dump(),
        headers={"Cache-Control": "no-store"},
    )


def _scope(request: Request, kb_id: str):
    if not request_principal(request).allows(Permission.MANAGE_ACCESS):
        return None
    scope = resolve_kb_scope(request, kb_id)
    if scope is None:
        return None
    decision = resource_access_decision(
        request, scope, permission=Permission.MANAGE_ACCESS
    )
    return (
        scope
        if decision is None
        or (decision is not False and getattr(decision, "is_allowed", False))
        else None
    )


def _owned_connection(request: Request, scope, connection_id: str):
    row = request.app.state.connection_store.get(connection_id)
    if (
        row is None
        or row["tenant_id"] != scope.tenant_id
        or row["kb_id"] != scope.storage_id
    ):
        return None
    return row


def _provider_matches(connector_type: str, provider: str) -> bool:
    return provider in {
        "notion": {"notion"},
        "confluence": {"atlassian"},
        "sharepoint": {"microsoft"},
    }.get(connector_type, set())


@router.post(
    "/v1/knowledge-bases/{kb_id}/connector-oauth/authorize",
    response_model=ConnectorOAuthAuthorization,
    status_code=201,
)
async def authorize_connector_oauth(
    kb_id: str,
    body: ConnectorOAuthStart,
    request: Request,
    response: Response,
):
    scope = _scope(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    kb_epoch = capture_kb_epoch(scope.storage_id)
    coordinator = getattr(request.app.state, "connector_oauth", None)
    redirect_uris = getattr(request.app.state, "connector_oauth_redirect_uris", {})
    if coordinator is None or body.provider not in redirect_uris:
        return _error(ErrorCode.OAUTH_PROVIDER_UNAVAILABLE, "OAuth 提供方未配置", 503)
    connection_revision = None
    if body.connection_id is not None:
        connection = _owned_connection(request, scope, body.connection_id)
        if connection is None:
            return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
        if not _provider_matches(str(connection["connector_type"]), body.provider):
            return _error(ErrorCode.BAD_REQUEST, "OAuth 提供方与连接类型不匹配", 400)
        connection_revision = int(connection["revision"])
    principal = request_principal(request)
    try:
        start = await run_sync(
            request.app.state.offload_executor,
            guarded_kb_mutation,
            request.app.state.kb_registry,
            scope.tenant_id,
            scope.storage_id,
            kb_epoch,
            coordinator.begin,
            provider=body.provider,
            tenant_id=scope.tenant_id,
            kb_id=scope.storage_id,
            connection_id=body.connection_id,
            user_id=principal.subject_id,
            membership_id=principal.membership_id,
            principal_fingerprint=principal.key_fingerprint,
            connection_revision=connection_revision,
            ttl_seconds=(request.app.state.connector_oauth_session_ttl_seconds),
        )
    except KBIncarnationChanged:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
    except (TypeError, ValueError) as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 400)
    response.headers["Cache-Control"] = "no-store"
    return ConnectorOAuthAuthorization(
        session_id=start.session_id,
        provider=start.provider,
        authorization_url=start.authorization_url,
        redirect_uri=redirect_uris[body.provider],
        expires_at=start.expires_at,
    )


@router.get(
    "/v1/auth/connector-oauth/callback/{provider}",
    response_model=ConnectorOAuthCallback,
)
async def complete_connector_oauth(
    provider: Literal["notion", "atlassian", "microsoft"],
    request: Request,
    response: Response,
    state: str | None = Query(default=None, min_length=16, max_length=512),
    code: str | None = Query(default=None, min_length=1, max_length=8192),
    error: str | None = Query(default=None, max_length=256),
):
    response.headers["Cache-Control"] = "no-store"
    coordinator = getattr(request.app.state, "connector_oauth", None)
    redirect_uris = getattr(request.app.state, "connector_oauth_redirect_uris", {})
    if coordinator is None or provider not in redirect_uris:
        return _error(ErrorCode.OAUTH_PROVIDER_UNAVAILABLE, "OAuth 提供方未配置", 503)
    if error is not None or not state or not code:
        if state:
            try:
                await run_sync(
                    request.app.state.offload_executor,
                    coordinator.cancel_callback,
                    provider=provider,
                    state=state,
                )
            except (OAuthError, TypeError, ValueError):
                # The public response deliberately does not reveal whether a
                # callback state existed, was expired, or had already run.
                pass
        return _error(ErrorCode.OAUTH_SESSION_INVALID, "OAuth 授权未完成", 400)
    try:
        row = await run_sync(
            request.app.state.offload_executor,
            coordinator.complete_callback,
            provider=provider,
            state=state,
            code=code,
            label=f"{provider.title()} OAuth",
            defer_activation=True,
        )
    except (OAuthStateMismatch, OAuthReplayError, OAuthSessionExpired):
        return _error(
            ErrorCode.OAUTH_SESSION_INVALID, "OAuth 会话无效、已过期或已使用", 400
        )
    except OAuthProviderError:
        return _error(
            ErrorCode.OAUTH_PROVIDER_UNAVAILABLE, "OAuth 提供方令牌交换失败", 502
        )
    except OAuthError:
        return _error(ErrorCode.OAUTH_SESSION_INVALID, "OAuth 授权失败", 400)
    authority_checker = getattr(
        request.app.state, "connector_oauth_authorization_checker", None
    )
    frozen_kb_epoch = row.get("_kb_epoch")
    authority_evidence = SimpleNamespace(
        tenant_id=str(row["tenant_id"]),
        kb_id=str(row["kb_id"]),
        kb_epoch=frozen_kb_epoch,
        user_id=str(row["created_by"]),
        membership_id=row.get("_membership_id"),
        principal_fingerprint=row.get("_principal_fingerprint"),
        connection_id=row.get("connection_id"),
        connection_revision=row.get("_connection_revision"),
    )

    def callback_authority_is_current() -> bool:
        if (
            not isinstance(frozen_kb_epoch, int)
            or isinstance(frozen_kb_epoch, bool)
            or shared_epoch_store().current(str(row["kb_id"])) != frozen_kb_epoch
        ):
            return False
        try:
            return bool(
                callable(authority_checker)
                and authority_checker(authority_evidence) is True
            )
        except Exception:
            # A public callback has no request principal to fall back to. Any
            # unavailable live-authority dependency therefore fails closed.
            return False

    async def remove_callback_credential(*, reference_lock_held: bool = False) -> None:
        async def remove_original_revision() -> None:
            reconcile = getattr(
                request.app.state, "reconcile_connector_oauth_bindings", None
            )
            if callable(reconcile):
                try:
                    await run_sync(
                        request.app.state.offload_executor,
                        reconcile,
                        credential_id=str(row["credential_id"]),
                    )
                except Exception:
                    # Leave the durable binding journal and pending credential
                    # intact for startup recovery rather than guessing which
                    # cross-store write committed.
                    return
            try:
                await run_sync(
                    request.app.state.offload_executor,
                    request.app.state.connector_credential_vault.delete,
                    row["credential_id"],
                    tenant_id=row["tenant_id"],
                    kb_id=row["kb_id"],
                    connection_id=row.get("connection_id"),
                    actor_id=row["created_by"],
                    expected_revision=int(row["revision"]),
                )
            except Exception:
                # The callback only owns the revision it created. In
                # particular, a concurrent rotation must survive cleanup.
                try:
                    await run_sync(
                        request.app.state.offload_executor,
                        request.app.state.connector_credential_vault.quarantine,
                        row["credential_id"],
                        tenant_id=row["tenant_id"],
                        kb_id=row["kb_id"],
                        connection_id=row.get("connection_id"),
                        actor_id=row["created_by"],
                        expected_revision=int(row["revision"]),
                    )
                except Exception:
                    # OAuth-created rows are pending from their first
                    # transaction, so even a total cleanup outage cannot make
                    # this token listable or decryptable.
                    pass

        if reference_lock_held:
            await remove_original_revision()
            return
        async with request.app.state.connector_credential_reference_lock:
            await remove_original_revision()

    if not callback_authority_is_current():
        await remove_callback_credential()
        return _error(
            ErrorCode.OAUTH_SESSION_INVALID,
            "OAuth 发起者的授权已失效",
            409,
        )
    registry_record = request.app.state.kb_registry.get_by_storage_id(str(row["kb_id"]))
    if (
        registry_record is None
        or str(registry_record.get("tenant_id") or "default") != row["tenant_id"]
        or frozen_kb_epoch != shared_epoch_store().current(str(row["kb_id"]))
    ):
        await remove_callback_credential()
        return _error(
            ErrorCode.OAUTH_SESSION_INVALID,
            "OAuth 会话关联的知识库已不可用",
            409,
        )
    external_kb_id = str(registry_record["kb_id"])
    connection_id = row.get("connection_id")
    deferred_cancellation: asyncio.CancelledError | None = None

    async def complete_critical(operation: Awaitable[Any]) -> Any:
        """Finish one saga step even if the HTTP task is cancelled.

        The child task owns the actual executor future. Shielding it prevents
        ``run_sync`` from cancelling a SQLite mutation that may already be in
        progress. We remember cancellation and deliver it only after the
        binding saga has either activated or durably rolled back.
        """

        nonlocal deferred_cancellation
        task = asyncio.ensure_future(operation)
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                deferred_cancellation = deferred_cancellation or exc
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()

    def finish_callback(value: Any) -> Any:
        if deferred_cancellation is not None:
            raise deferred_cancellation
        return value

    async with request.app.state.connector_credential_reference_lock:
        vault = request.app.state.connector_credential_vault
        if not callback_authority_is_current():
            await complete_critical(
                remove_callback_credential(reference_lock_held=True)
            )
            return finish_callback(
                _error(
                    ErrorCode.OAUTH_SESSION_INVALID,
                    "OAuth 发起者的授权已失效",
                    409,
                )
            )
        metadata = await complete_critical(
            run_sync(
                request.app.state.offload_executor,
                vault.get_metadata,
                str(row["credential_id"]),
                tenant_id=str(row["tenant_id"]),
                kb_id=str(row["kb_id"]),
                include_inactive=True,
            )
        )
        valid_credential = (
            metadata is not None
            and metadata.get("tenant_id") == row.get("tenant_id")
            and metadata.get("kb_id") == row.get("kb_id")
            and metadata.get("connection_id") == connection_id
            and metadata.get("provider") == provider
            and metadata.get("credential_kind") == "oauth"
            and metadata.get("revision") == row.get("revision")
            and metadata.get("secret_fields") == row.get("secret_fields")
        )
        if not valid_credential:
            if metadata is not None:
                await complete_critical(
                    remove_callback_credential(reference_lock_held=True)
                )
            return finish_callback(
                _error(
                    ErrorCode.OAUTH_SESSION_INVALID,
                    "OAuth 凭据在绑定前已发生变化",
                    409,
                )
            )
        # Metadata lookup is deliberately inside the reference lock. Recheck
        # the frozen member permission and KB incarnation immediately before
        # accepting the credential or binding it to a connection.
        if not callback_authority_is_current():
            await complete_critical(
                remove_callback_credential(reference_lock_held=True)
            )
            return finish_callback(
                _error(
                    ErrorCode.OAUTH_SESSION_INVALID,
                    "OAuth 发起者的授权已失效",
                    409,
                )
            )
        if connection_id is not None:
            connection = request.app.state.connection_store.get(
                str(connection_id), include_secret_refs=True
            )
            frozen_connection_revision = row.get("_connection_revision")
            valid_connection = (
                connection is not None
                and connection["tenant_id"] == row["tenant_id"]
                and connection["kb_id"] == row["kb_id"]
                and _provider_matches(str(connection["connector_type"]), provider)
                and isinstance(frozen_connection_revision, int)
                and not isinstance(frozen_connection_revision, bool)
                and int(connection["revision"]) == frozen_connection_revision
            )
            if not valid_connection:
                # The connection may have been deleted or changed while the
                # user was at the provider. Remove the freshly issued bound
                # token rather than leave an unusable orphan.
                await complete_critical(
                    remove_callback_credential(reference_lock_held=True)
                )
                return finish_callback(
                    _error(
                        ErrorCode.OAUTH_SESSION_INVALID,
                        "OAuth 会话关联的连接已不可用",
                        409,
                    )
                )
            try:
                await complete_critical(
                    run_sync(
                        request.app.state.offload_executor,
                        guarded_kb_mutation,
                        request.app.state.kb_registry,
                        str(row["tenant_id"]),
                        str(row["kb_id"]),
                        int(frozen_kb_epoch),
                        vault.prepare_binding,
                        str(row["credential_id"]),
                        tenant_id=str(row["tenant_id"]),
                        kb_id=str(row["kb_id"]),
                        connection_id=str(connection_id),
                        expected_credential_revision=int(row["revision"]),
                        expected_connection_revision=int(frozen_connection_revision),
                        previous_credential_id=connection.get("credential_id"),
                        previous_credential_fields=(
                            connection.get("secret_fields", ())
                            if connection.get("credential_id") is not None
                            else ()
                        ),
                        previous_secret_env=connection.get("secret_env", {}),
                    )
                )
                bound_connection = await complete_critical(
                    run_sync(
                        request.app.state.offload_executor,
                        guarded_kb_mutation,
                        request.app.state.kb_registry,
                        str(row["tenant_id"]),
                        str(row["kb_id"]),
                        int(frozen_kb_epoch),
                        request.app.state.connection_store.set_credential,
                        str(connection_id),
                        str(row["credential_id"]),
                        row["secret_fields"],
                        expected_revision=int(frozen_connection_revision),
                    )
                )
                expected_bound_revision = int(frozen_connection_revision) + 1
                if (
                    bound_connection.get("credential_id") != row["credential_id"]
                    or int(bound_connection.get("revision") or 0)
                    != expected_bound_revision
                ):
                    raise ConnectionRevisionConflict(
                        "OAuth binding did not persist the expected revision"
                    )
                # This exact CAS is the one authorized connection transition
                # in the callback. Advance only the frozen evidence by that
                # single revision so the final live check still detects every
                # unrelated concurrent mutation.
                authority_evidence.connection_revision = expected_bound_revision
            except (ConnectionRevisionConflict, KBIncarnationChanged, KeyError):
                await complete_critical(
                    remove_callback_credential(reference_lock_held=True)
                )
                return finish_callback(
                    _error(
                        ErrorCode.OAUTH_SESSION_INVALID,
                        "OAuth 会话关联的连接已发生变化",
                        409,
                    )
                )
            except Exception:
                await complete_critical(
                    remove_callback_credential(reference_lock_held=True)
                )
                return finish_callback(
                    _error(
                        ErrorCode.OAUTH_PROVIDER_UNAVAILABLE,
                        "OAuth 凭据无法绑定连接，已执行回滚",
                        503,
                    )
                )
        if not callback_authority_is_current():
            await complete_critical(
                remove_callback_credential(reference_lock_held=True)
            )
            return finish_callback(
                _error(
                    ErrorCode.OAUTH_SESSION_INVALID,
                    "OAuth 发起者的授权已失效",
                    409,
                )
            )
        try:
            await complete_critical(
                run_sync(
                    request.app.state.offload_executor,
                    guarded_kb_mutation,
                    request.app.state.kb_registry,
                    str(row["tenant_id"]),
                    str(row["kb_id"]),
                    int(frozen_kb_epoch),
                    vault.activate,
                    str(row["credential_id"]),
                    tenant_id=str(row["tenant_id"]),
                    kb_id=str(row["kb_id"]),
                    connection_id=(
                        str(connection_id) if connection_id is not None else None
                    ),
                    actor_id=str(row["created_by"]),
                    expected_revision=int(row["revision"]),
                )
            )
        except (KBIncarnationChanged, KeyError):
            await complete_critical(
                remove_callback_credential(reference_lock_held=True)
            )
            return finish_callback(
                _error(
                    ErrorCode.OAUTH_SESSION_INVALID,
                    "OAuth 会话在凭据激活前已失效",
                    409,
                )
            )
        except Exception:
            await complete_critical(
                remove_callback_credential(reference_lock_held=True)
            )
            return finish_callback(
                _error(
                    ErrorCode.OAUTH_PROVIDER_UNAVAILABLE,
                    "OAuth 凭据无法安全激活，已保持隔离",
                    503,
                )
            )
    return finish_callback(
        ConnectorOAuthCallback(
            credential_id=str(row["credential_id"]),
            provider=provider,
            connection_id=(str(connection_id) if connection_id is not None else None),
            kb_id=external_kb_id,
        )
    )
