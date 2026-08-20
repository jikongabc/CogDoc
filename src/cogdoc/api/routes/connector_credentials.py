from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from cogdoc.api.offload import run_sync
from cogdoc.api.connector_scope import (
    KBIncarnationChanged,
    capture_kb_epoch,
    guarded_kb_mutation,
)
from cogdoc.api.schemas import (
    ConnectorCredential,
    ConnectorCredentialCreate,
    ConnectorCredentialEvent,
    ConnectorCredentialEventList,
    ConnectorCredentialList,
    ConnectorCredentialRotate,
    ErrorCode,
    build_error_response,
)
from cogdoc.api.tenant_scope import (
    request_principal,
    resource_access_decision,
    resolve_kb_scope,
)
from cogdoc.api.tenancy import Permission
from cogdoc.connectors.connection_store import (
    ConnectionRevisionConflict as ConnectionReferenceRevisionConflict,
    connector_provider_matches,
    validate_connector_secret_fields,
)
from cogdoc.connectors.credential_store import CredentialRevisionConflict
from cogdoc.connectors.oauth import OAuthError, OAuthProviderError


router = APIRouter(
    prefix="/v1/knowledge-bases/{kb_id}/connector-credentials",
    tags=["connector-credentials"],
)


def _error(code: ErrorCode, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status, content=build_error_response(code, message).model_dump()
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


def _vault(request: Request):
    return getattr(request.app.state, "connector_credential_vault", None)


def _is_internal_credential(row: Mapping | None) -> bool:
    return row is not None and row.get("credential_kind") == "oauth-session"


def _public_credential(row: Mapping, external_kb_id: str) -> ConnectorCredential:
    values = {key: row.get(key) for key in ConnectorCredential.model_fields}
    values["kb_id"] = external_kb_id
    return ConnectorCredential.model_validate(values)


def _public_event(row: Mapping, external_kb_id: str) -> ConnectorCredentialEvent:
    values = {key: row.get(key) for key in ConnectorCredentialEvent.model_fields}
    values["kb_id"] = external_kb_id
    return ConnectorCredentialEvent.model_validate(values)


def _owned_connection(request: Request, scope, connection_id: str):
    row = request.app.state.connection_store.get(connection_id)
    if (
        row is None
        or row["tenant_id"] != scope.tenant_id
        or row["kb_id"] != scope.storage_id
    ):
        return None
    return row


def _validate_references(
    request: Request,
    scope,
    credential_id: str,
    provider: str,
    secret_fields: list[str],
) -> list[Mapping]:
    references = request.app.state.connection_store.credential_references(
        scope.tenant_id, scope.storage_id, credential_id
    )
    rows: list[Mapping] = []
    for connection_id in references:
        row = _owned_connection(request, scope, connection_id)
        if row is None:
            continue
        if not connector_provider_matches(str(row["connector_type"]), provider):
            raise ValueError("credential provider does not match its connection")
        # ``set_credential`` is the single connector-contract validator. It is
        # safe to invoke only after all references have been checked; callers
        # update rows after the encrypted rotation succeeds.
        validate_connector_secret_fields(str(row["connector_type"]), secret_fields)
        rows.append(row)
    return rows


@router.get("", response_model=ConnectorCredentialList)
async def list_credentials(kb_id: str, request: Request):
    scope = _scope(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    vault = _vault(request)
    if vault is None:
        return _error(ErrorCode.CREDENTIAL_UNAVAILABLE, "连接凭据库未配置", 503)
    rows = await run_sync(
        request.app.state.offload_executor,
        vault.list_metadata,
        scope.tenant_id,
        scope.storage_id,
    )
    return ConnectorCredentialList(
        credentials=[
            _public_credential(row, scope.external_id)
            for row in rows
            if not _is_internal_credential(row)
        ]
    )


@router.post("", response_model=ConnectorCredential, status_code=201)
async def create_credential(
    kb_id: str, body: ConnectorCredentialCreate, request: Request
):
    scope = _scope(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    kb_epoch = capture_kb_epoch(scope.storage_id)
    vault = _vault(request)
    if vault is None:
        return _error(ErrorCode.CREDENTIAL_UNAVAILABLE, "连接凭据库未配置", 503)
    if body.credential_kind.casefold() == "oauth-session":
        return _error(ErrorCode.BAD_REQUEST, "该凭据类型仅供系统内部使用", 400)
    principal = request_principal(request)
    async with request.app.state.connector_credential_reference_lock:
        connection = None
        if body.connection_id is not None:
            connection = _owned_connection(request, scope, body.connection_id)
            if connection is None:
                return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
            if not connector_provider_matches(
                str(connection["connector_type"]), body.provider
            ):
                return _error(ErrorCode.BAD_REQUEST, "凭据提供方与连接类型不匹配", 400)
            try:
                # Validate the target before persisting encrypted data.
                validate_connector_secret_fields(
                    str(connection["connector_type"]), body.secret_values
                )
            except ValueError as exc:
                return _error(ErrorCode.BAD_REQUEST, str(exc), 400)
        row = None
        try:
            row = await run_sync(
                request.app.state.offload_executor,
                guarded_kb_mutation,
                request.app.state.kb_registry,
                scope.tenant_id,
                scope.storage_id,
                kb_epoch,
                vault.create,
                tenant_id=scope.tenant_id,
                kb_id=scope.storage_id,
                connection_id=body.connection_id,
                provider=body.provider,
                credential_kind=body.credential_kind,
                label=body.label,
                secret_values=body.secret_values,
                actor_id=principal.subject_id,
                subject=body.subject,
                scopes=body.scopes,
                expires_at=body.expires_at,
            )
            if connection is not None:
                await run_sync(
                    request.app.state.offload_executor,
                    guarded_kb_mutation,
                    request.app.state.kb_registry,
                    scope.tenant_id,
                    scope.storage_id,
                    kb_epoch,
                    request.app.state.connection_store.set_credential,
                    str(connection["connection_id"]),
                    str(row["credential_id"]),
                    row["secret_fields"],
                )
        except KBIncarnationChanged:
            if row is not None:
                try:
                    await run_sync(
                        request.app.state.offload_executor,
                        vault.delete,
                        row["credential_id"],
                        tenant_id=scope.tenant_id,
                        kb_id=scope.storage_id,
                        connection_id=body.connection_id,
                        actor_id=principal.subject_id,
                    )
                except Exception:
                    pass
            return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
        except (KeyError, TypeError, ValueError) as exc:
            if row is not None and connection is not None:
                try:
                    await run_sync(
                        request.app.state.offload_executor,
                        vault.delete,
                        row["credential_id"],
                        tenant_id=scope.tenant_id,
                        kb_id=scope.storage_id,
                        connection_id=body.connection_id,
                        actor_id=principal.subject_id,
                    )
                except Exception:
                    pass
            return _error(ErrorCode.BAD_REQUEST, str(exc), 400)
        except Exception:
            if row is not None and connection is not None:
                try:
                    await run_sync(
                        request.app.state.offload_executor,
                        vault.delete,
                        row["credential_id"],
                        tenant_id=scope.tenant_id,
                        kb_id=scope.storage_id,
                        connection_id=body.connection_id,
                        actor_id=principal.subject_id,
                    )
                except Exception:
                    pass
            return _error(
                ErrorCode.CREDENTIAL_UNAVAILABLE,
                "凭据持久化成功但无法绑定连接，已执行回滚",
                503,
            )
        return _public_credential(row, scope.external_id)


@router.patch("/{credential_id}", response_model=ConnectorCredential)
async def rotate_credential(
    kb_id: str,
    credential_id: str,
    body: ConnectorCredentialRotate,
    request: Request,
):
    scope = _scope(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    kb_epoch = capture_kb_epoch(scope.storage_id)
    vault = _vault(request)
    if vault is None:
        return _error(ErrorCode.CREDENTIAL_UNAVAILABLE, "连接凭据库未配置", 503)
    async with request.app.state.connector_credential_reference_lock:
        metadata = await run_sync(
            request.app.state.offload_executor,
            vault.get_metadata,
            credential_id,
            tenant_id=scope.tenant_id,
            kb_id=scope.storage_id,
        )
        if metadata is None or _is_internal_credential(metadata):
            return _error(ErrorCode.CREDENTIAL_NOT_FOUND, "连接凭据不存在", 404)
        next_fields = (
            sorted(body.secret_values)
            if body.secret_values is not None
            else list(metadata["secret_fields"])
        )
        try:
            references = _validate_references(
                request,
                scope,
                credential_id,
                str(metadata["provider"]),
                next_fields,
            )
            kwargs = {
                "tenant_id": scope.tenant_id,
                "kb_id": scope.storage_id,
                "connection_id": metadata.get("connection_id"),
                "actor_id": request_principal(request).subject_id,
                "secret_values": body.secret_values,
                "expected_revision": body.expected_revision,
            }
            if "expires_at" in body.model_fields_set:
                kwargs["expires_at"] = body.expires_at
            row = await run_sync(
                request.app.state.offload_executor,
                guarded_kb_mutation,
                request.app.state.kb_registry,
                scope.tenant_id,
                scope.storage_id,
                kb_epoch,
                vault.rotate,
                credential_id,
                **kwargs,
            )
            for connection in references:
                await run_sync(
                    request.app.state.offload_executor,
                    guarded_kb_mutation,
                    request.app.state.kb_registry,
                    scope.tenant_id,
                    scope.storage_id,
                    kb_epoch,
                    request.app.state.connection_store.set_credential,
                    str(connection["connection_id"]),
                    credential_id,
                    row["secret_fields"],
                )
        except KBIncarnationChanged:
            return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
        except CredentialRevisionConflict:
            return _error(
                ErrorCode.CREDENTIAL_REVISION_CONFLICT,
                "凭据已被其他操作更新，请刷新后重试",
                409,
            )
        except (TypeError, ValueError) as exc:
            return _error(ErrorCode.BAD_REQUEST, str(exc), 400)
        return _public_credential(row, scope.external_id)


@router.post("/{credential_id}/refresh", response_model=ConnectorCredential)
async def refresh_oauth_credential(
    kb_id: str,
    credential_id: str,
    request: Request,
    expected_revision: int | None = Query(default=None, ge=1),
):
    scope = _scope(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    kb_epoch = capture_kb_epoch(scope.storage_id)
    vault = _vault(request)
    coordinator = getattr(request.app.state, "connector_oauth", None)
    if vault is None or coordinator is None:
        return _error(ErrorCode.OAUTH_PROVIDER_UNAVAILABLE, "OAuth 提供方未配置", 503)
    principal = request_principal(request)
    authority_checker = getattr(
        request.app.state, "connector_oauth_authorization_checker", None
    )
    try:
        # Snapshot references and authority under the short cross-store lock.
        # The provider network call below deliberately runs outside it so one
        # slow tenant cannot serialize every tenant's credential mutations.
        async with request.app.state.connector_credential_reference_lock:
            metadata = await run_sync(
                request.app.state.offload_executor,
                vault.get_metadata,
                credential_id,
                tenant_id=scope.tenant_id,
                kb_id=scope.storage_id,
            )
            if metadata is None or _is_internal_credential(metadata):
                return _error(ErrorCode.CREDENTIAL_NOT_FOUND, "连接凭据不存在", 404)
            if metadata.get("credential_kind") != "oauth":
                return _error(ErrorCode.BAD_REQUEST, "该凭据不支持 OAuth 刷新", 409)
            _validate_references(
                request,
                scope,
                credential_id,
                str(metadata["provider"]),
                list(metadata["secret_fields"]),
            )
            snapshot_revision = int(metadata["revision"])
            if (
                expected_revision is not None
                and expected_revision != snapshot_revision
            ):
                raise CredentialRevisionConflict("credential revision has changed")
            bound_connection_id = metadata.get("connection_id")
            bound_connection = (
                request.app.state.connection_store.get(str(bound_connection_id))
                if bound_connection_id is not None
                else None
            )
            if bound_connection_id is not None and bound_connection is None:
                raise ValueError("credential connection binding is unavailable")
            authority_evidence = SimpleNamespace(
                tenant_id=scope.tenant_id,
                kb_id=scope.storage_id,
                kb_epoch=kb_epoch,
                user_id=principal.subject_id,
                membership_id=principal.membership_id,
                principal_fingerprint=principal.key_fingerprint,
                connection_id=bound_connection_id,
                connection_revision=(
                    int(bound_connection["revision"])
                    if bound_connection is not None
                    else None
                ),
            )

        def refresh_authority_is_current() -> bool:
            try:
                return bool(
                    callable(authority_checker)
                    and authority_checker(authority_evidence) is True
                )
            except Exception:
                return False

        if not refresh_authority_is_current():
            return _error(
                ErrorCode.OAUTH_SESSION_INVALID,
                "OAuth 刷新授权已失效",
                409,
            )
        row = await run_sync(
            request.app.state.offload_executor,
            coordinator.refresh_credential,
            credential_id,
            tenant_id=scope.tenant_id,
            kb_id=scope.storage_id,
            connection_id=bound_connection_id,
            user_id=principal.subject_id,
            expected_revision=snapshot_revision,
            kb_epoch=kb_epoch,
            authority_checker=refresh_authority_is_current,
        )

        async with request.app.state.connector_credential_reference_lock:
            if not refresh_authority_is_current():
                return _error(
                    ErrorCode.OAUTH_SESSION_INVALID,
                    "OAuth 刷新授权已失效",
                    409,
                )
            latest = await run_sync(
                request.app.state.offload_executor,
                vault.get_metadata,
                credential_id,
                tenant_id=scope.tenant_id,
                kb_id=scope.storage_id,
            )
            if latest is None or int(latest["revision"]) != int(row["revision"]):
                raise CredentialRevisionConflict("credential revision has changed")
            references = _validate_references(
                request,
                scope,
                credential_id,
                str(row["provider"]),
                list(row["secret_fields"]),
            )
            for connection in references:
                await run_sync(
                    request.app.state.offload_executor,
                    guarded_kb_mutation,
                    request.app.state.kb_registry,
                    scope.tenant_id,
                    scope.storage_id,
                    kb_epoch,
                    request.app.state.connection_store.set_credential,
                    str(connection["connection_id"]),
                    credential_id,
                    row["secret_fields"],
                    expected_revision=int(connection["revision"]),
                )
    except KBIncarnationChanged:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
    except (CredentialRevisionConflict, ConnectionReferenceRevisionConflict):
        return _error(
            ErrorCode.CREDENTIAL_REVISION_CONFLICT,
            "凭据已被其他操作更新，请刷新后重试",
            409,
        )
    except OAuthProviderError:
        return _error(
            ErrorCode.OAUTH_PROVIDER_UNAVAILABLE,
            "OAuth 提供方令牌刷新失败",
            502,
        )
    except (OAuthError, TypeError, ValueError) as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 409)
    return _public_credential(row, scope.external_id)


@router.delete("/{credential_id}", status_code=204)
async def delete_credential(
    kb_id: str,
    credential_id: str,
    request: Request,
    expected_revision: int | None = Query(default=None, ge=1),
):
    scope = _scope(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    kb_epoch = capture_kb_epoch(scope.storage_id)
    vault = _vault(request)
    if vault is None:
        return _error(ErrorCode.CREDENTIAL_UNAVAILABLE, "连接凭据库未配置", 503)
    async with request.app.state.connector_credential_reference_lock:
        metadata = await run_sync(
            request.app.state.offload_executor,
            vault.get_metadata,
            credential_id,
            tenant_id=scope.tenant_id,
            kb_id=scope.storage_id,
        )
        if metadata is None or _is_internal_credential(metadata):
            return _error(ErrorCode.CREDENTIAL_NOT_FOUND, "连接凭据不存在", 404)
        references = request.app.state.connection_store.credential_references(
            scope.tenant_id, scope.storage_id, credential_id
        )
        if references:
            return _error(ErrorCode.BAD_REQUEST, "凭据仍被连接使用，不能删除", 409)
        try:
            await run_sync(
                request.app.state.offload_executor,
                guarded_kb_mutation,
                request.app.state.kb_registry,
                scope.tenant_id,
                scope.storage_id,
                kb_epoch,
                vault.delete,
                credential_id,
                tenant_id=scope.tenant_id,
                kb_id=scope.storage_id,
                connection_id=metadata.get("connection_id"),
                actor_id=request_principal(request).subject_id,
                expected_revision=expected_revision,
            )
        except KBIncarnationChanged:
            return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
        except CredentialRevisionConflict:
            return _error(
                ErrorCode.CREDENTIAL_REVISION_CONFLICT,
                "凭据已被其他操作更新，请刷新后重试",
                409,
            )
        return Response(status_code=204)


@router.get("/audit/events", response_model=ConnectorCredentialEventList)
async def list_credential_events(
    kb_id: str,
    request: Request,
    credential_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
):
    scope = _scope(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    vault = _vault(request)
    if vault is None:
        return _error(ErrorCode.CREDENTIAL_UNAVAILABLE, "连接凭据库未配置", 503)
    internal_ids: set[str] = set()
    session_store = getattr(request.app.state, "connector_oauth_session_store", None)
    if session_store is not None:
        internal_ids.update(
            await run_sync(
                request.app.state.offload_executor,
                session_store.internal_credential_ids,
                scope.tenant_id,
                scope.storage_id,
            )
        )
    metadata_rows = await run_sync(
        request.app.state.offload_executor,
        vault.list_metadata,
        scope.tenant_id,
        scope.storage_id,
    )
    internal_ids.update(
        str(row["credential_id"])
        for row in metadata_rows
        if _is_internal_credential(row)
    )
    if credential_id in internal_ids:
        return ConnectorCredentialEventList(events=[])
    rows = await run_sync(
        request.app.state.offload_executor,
        vault.audit_events,
        scope.tenant_id,
        scope.storage_id,
        credential_id=credential_id,
        # Fetch the vault's bounded maximum before filtering internal verifier
        # records, then restore the caller's requested public limit.
        limit=1000,
    )
    return ConnectorCredentialEventList(
        events=[
            _public_event(row, scope.external_id)
            for row in rows
            if str(row["credential_id"]) not in internal_ids
        ][:limit]
    )
