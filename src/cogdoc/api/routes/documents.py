from collections.abc import Callable, Mapping
import mimetypes
import os
from fastapi import APIRouter, File, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from cogdoc.api.ingest import KBExistsError
from cogdoc.api.ha_chat_authority import (
    HAChatAuthorityChanged,
    capture_ha_chat_epoch,
    ha_authority_guard,
)
from cogdoc.api.offload import run_sync
from cogdoc.api.tenant_quota import (
    TenantMutationInProgress,
    TenantQuotaExceeded,
)
from cogdoc.api.tenant_scope import (
    externalize_kb_fields,
    is_user_session_principal,
    request_principal,
    resource_access_decision,
    resolve_kb_scope,
    scope_for_storage_id,
    source_is_authorized,
    tenant_kb_scopes,
)
from cogdoc.api.tenancy import Permission, Principal, Role
from cogdoc.api.schemas import (
    Document,
    ErrorCode,
    ErrorResponse,
    IndexJob,
    KnowledgeBase,
    KnowledgeBaseCreate,
    SourceChunksResponse,
    SourceListResponse,
    build_error_response,
)
from cogdoc.config.settings import get_settings
from cogdoc.ha.session_store import StaleSessionLease
from cogdoc.observability.trace import delete_trace_files
from cogdoc.service.ingest_service import (
    KBCleanupError,
    delete_kb_index_transactional,
    mark_kb_deleted,
)
from cogdoc.service.source_chunks import (
    chunk_preview,
    source_chunks as read_source_chunks,
)
from cogdoc.service.kb_locks import kb_write_lock
from cogdoc.service.kb_epoch import shared_epoch_store
from cogdoc.service.kb_lifecycle import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_DELETING,
    shared_lifecycle_store,
)
from cogdoc.service.kb_state import KBState
from cogdoc.source_model import build_source_id, build_version_id
from cogdoc.tools.manifest import load_index_manifest
from cogdoc.tools.chunk_identity import build_document_id
from cogdoc.tools.source_parser import (
    CONNECTOR_MATERIALIZED_PREFIX,
    SUPPORTED_EXTENSIONS,
)

router = APIRouter(prefix="/v1", tags=["documents"])


# 创建 kb。
def _create_kb(
    kb_id,
    tenant_id,
    owner_id,
    registry,
    resource_access_store=None,
    access_policy="workspace",
    owner_membership_id=None,
):
    # 与删库尾部互斥：create 与 delete 都持 kb_write_lock，杜绝"删库已删 registry、未落 tombstone" 之间并发 create 把 lifecycle 切 active、随后旧删库又写 deleted 把新 KB 标删的竞态。
    storage_id_for = getattr(registry, "storage_id_for", None)
    storage_id = storage_id_for(kb_id, tenant_id) if callable(storage_id_for) else kb_id
    with kb_write_lock(storage_id):
        record = registry.create(kb_id, tenant_id=tenant_id, owner_id=owner_id)
        if resource_access_store is not None:
            try:
                resource_access_store.set_kb_policy(
                    tenant_id,
                    str(record.get("storage_id") or storage_id),
                    owner_id,
                    access_policy,
                    owner_membership_id=owner_membership_id,
                )
            except Exception:
                # A registered KB without an ACL is unusable in account mode and
                # would also make a same-slug retry impossible. Compensate the
                # empty create before surfacing the persistence failure.
                registry.delete(str(record.get("storage_id") or storage_id))
                raise
        return record


# 删除 kb。
def _clear_kb_review_state(kb_id, stores) -> None:
    for store in stores:
        clear_kb = getattr(store, "clear_kb", None)
        if clear_kb is not None:
            clear_kb(kb_id)


def _begin_connector_kb_delete(
    kb_id: str,
    registry,
    authorization_guard: Callable[..., None] | None,
) -> str:
    """Persist the incarnation fence before draining connector workers."""

    with kb_write_lock(kb_id):
        if authorization_guard is not None:
            authorization_guard()
        if registry.get_by_storage_id(kb_id) is None:
            raise KeyError(kb_id)
        lifecycle = shared_lifecycle_store()
        previous = lifecycle.status(kb_id)
        lifecycle.set(kb_id, LIFECYCLE_DELETING)
        return previous


def _authorize_connector_kb_cleanup(
    kb_id: str,
    registry,
    authorization_guard: Callable[..., None] | None,
) -> None:
    """Revalidate live authority immediately before irreversible cleanup."""

    with kb_write_lock(kb_id):
        if authorization_guard is not None:
            authorization_guard(require_resource_acl=True)
        if registry.get_by_storage_id(kb_id) is None:
            raise KeyError(kb_id)
        if shared_lifecycle_store().status(kb_id) != LIFECYCLE_DELETING:
            raise PermissionError("knowledge-base deletion fence changed")
        # Lifecycle=deleting already blocks control-plane writes while workers
        # drain. Invalidate OAuth/callback incarnations only after this second
        # authorization check, so a revoked delete can roll back cleanly.
        shared_epoch_store().bump(kb_id)


def _restore_connector_kb_delete(kb_id: str, registry, previous: str) -> None:
    """Undo only a conflict fence that performed no connector cancellation."""

    if previous != LIFECYCLE_ACTIVE:
        return
    with kb_write_lock(kb_id):
        if registry.get_by_storage_id(kb_id) is not None:
            shared_lifecycle_store().set(kb_id, LIFECYCLE_ACTIVE)


def _cleanup_connector_kb_state(
    tenant_id: str,
    kb_id: str,
    *,
    sync_manager,
    oauth_session_store=None,
    credential_vault=None,
    source_catalog=None,
    source_artifact_store=None,
    external_acl_sync_store=None,
) -> None:
    """Idempotently erase every connector capability for a fenced KB."""

    try:
        if oauth_session_store is not None:
            oauth_session_store.delete_scope(tenant_id, kb_id)
        sync_manager.purge_knowledge_base(tenant_id, kb_id)
        if source_artifact_store is not None:
            source_artifact_store.delete_scope(tenant_id, kb_id)
        if source_catalog is not None:
            source_catalog.delete_scope(tenant_id, kb_id)
        if external_acl_sync_store is not None:
            external_acl_sync_store.delete_scope(tenant_id, kb_id)
        if credential_vault is not None:
            credential_vault.delete_scope(tenant_id, kb_id)
    except Exception as exc:
        raise KBCleanupError(
            f"KB connector control-plane cleanup failed: {kb_id}"
        ) from exc


# 删除 kb。
def _delete_kb(
    kb_id,
    registry,
    index_jobs,
    session_store=None,
    knowledge_store=None,
    feedback_store=None,
    feedback_analysis_store=None,
    retrieval_feedback_store=None,
    retrieval_eval_draft_store=None,
    research_job_store=None,
    resource_access_store=None,
    tenant_id="default",
    authorization_guard: Callable[..., None] | None = None,
):
    # registry 删除与落 tombstone 必须与 create 在同一把锁内原子完成。
    authorized = False
    try:
        with kb_write_lock(kb_id):
            # This command may have waited behind earlier index work. Re-check
            # the exact live membership and ACL under the mutation lock, before
            # the first destructive operation or trace cleanup.
            if authorization_guard is not None:
                authorization_guard()
            authorized = True
            delete_kb_index_transactional(kb_id)  # 内部同一把锁，可重入
            # 先持久化 deleted，再删 registry。后者失败时 KB 记录仍在、读写被 tombstone 拦住， DELETE 可重试；反过来会出现 registry 已消失但 tombstone 未落、无法重试的半删除态。
            mark_kb_deleted(kb_id)
            try:
                _clear_kb_review_state(
                    kb_id,
                    (
                        knowledge_store,
                        feedback_store,
                        feedback_analysis_store,
                        retrieval_feedback_store,
                        retrieval_eval_draft_store,
                        research_job_store,
                    ),
                )
            except Exception as exc:
                raise KBCleanupError(f"KB 派生/反馈状态删除失败: {kb_id}") from exc
            # 连带清掉该库的会话历史，否则同名新库复用 kb_id 会捡到旧对话。
            try:
                if session_store is not None:
                    session_store.clear_kb(kb_id)
            except Exception as exc:
                # registry 保留到所有幂等状态清理完成后再删，失败时 DELETE 可重试。
                raise KBCleanupError(f"KB 会话状态删除失败: {kb_id}") from exc
            if resource_access_store is not None:
                try:
                    resource_access_store.clear_kb(tenant_id, kb_id)
                except Exception as exc:
                    raise KBCleanupError(f"KB ACL 状态删除失败: {kb_id}") from exc
            # Registry deletion is the final commit point. Every state keyed
            # by the deterministic storage ID is gone first, so a same-slug
            # create can never inherit old documents, grants, or capabilities.
            registry.delete(kb_id)
    finally:
        try:
            if authorized:
                delete_trace_files(doc_id=kb_id)
        finally:
            # 释放 executor 槽位，允许 KB 重建时创建新 executor，防止 256 上限耗尽。
            index_jobs.release_executor(kb_id)


_PDF_MAGIC = b"%PDF"
_ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


# 完成 错误 处理。
def _error(code: ErrorCode, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status, content=build_error_response(code, message).model_dump()
    )


# 完成 公开视图任务 处理。
def _public_job(job: dict, request: Request | None = None) -> IndexJob:
    # committed_generation_id 是崩溃对账证据，只存内部 job record，不进入严格 API schema。
    payload = {k: v for k, v in job.items() if k != "committed_generation_id"}
    # Internal exceptions may contain the physical tenant storage ID, absolute
    # source paths, provider details, or mutation-journal state.  The stable
    # error code is the public contract; never project the raw exception text.
    if payload.get("status") == "failed":
        payload["message"] = (
            "文档不存在"
            if payload.get("error_code") == ErrorCode.DOCUMENT_NOT_FOUND.value
            else "索引任务执行失败"
        )
    if request is not None:
        payload = externalize_kb_fields(payload, request)
    return IndexJob(**payload)


# 完成 知识库documents 处理。
def _kb_documents(
    kb_id: str, manifest_reader: Callable[[str], Mapping | None] | None = None
) -> list[Document]:
    if manifest_reader is not None:
        manifest = manifest_reader(kb_id)
        files = manifest.get("files", []) if isinstance(manifest, Mapping) else []
        documents: list[Document] = []
        for item in files:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("path") or "")
            sha256 = str(item.get("sha256") or "")
            if os.path.splitext(name)[1].casefold() not in SUPPORTED_EXTENSIONS:
                continue
            source_id = build_source_id("legacy-upload", name)
            documents.append(
                Document(
                    name=name,
                    sha256=sha256,
                    document_id=build_document_id(name),
                    source_id=source_id,
                    version_id=build_version_id(source_id, sha256),
                    connector_type="legacy-upload",
                    media_type=mimetypes.guess_type(name)[0]
                    or "application/octet-stream",
                    kind="file",
                )
            )
        return documents
    # generation state 是事务提交指针且内含 documents；manifest 是提交后的派生缓存，写失败时可能滞后。
    active = KBState(kb_id).active()
    documents = (
        active.get("documents", [])
        if active is not None
        else load_index_manifest(kb_id).get("documents", [])
    )
    return [
        Document(
            name=doc.get("name", ""),
            sha256=doc.get("sha256", ""),
            document_id=build_document_id(str(doc.get("name", ""))),
            source_id=str(doc.get("source_id") or ""),
            version_id=str(doc.get("version_id") or ""),
            connector_type=str(doc.get("connector_type") or "legacy-upload"),
            media_type=str(doc.get("media_type") or "application/pdf"),
            kind=str(doc.get("kind") or "file"),
            origin_uri=(str(doc.get("origin_uri")) if doc.get("origin_uri") else None),
        )
        for doc in documents
        if doc.get("name")
    ]


async def _request_kb_documents(request: Request, kb_id: str) -> list[Document]:
    manifest_reader = getattr(request.app.state, "ha_source_manifest_reader", None)
    if manifest_reader is None:
        return _kb_documents(kb_id)
    return await run_sync(
        request.app.state.offload_executor,
        _kb_documents,
        kb_id,
        manifest_reader,
    )


def _allowed_sources_for_scope(request: Request, scope, sources) -> list[str]:
    decision = resource_access_decision(request, scope, permission=Permission.READ)
    if decision is None:
        return [str(source) for source in sources]
    if decision is False or not getattr(decision, "is_allowed", False):
        return []
    allows = getattr(decision, "allows_source", None)
    if not callable(allows):
        return []
    return [str(source) for source in sources if allows(source)]


def _live_session_authorization_guard(
    request: Request,
    scope,
    *,
    permission: Permission,
    source: str | None = None,
) -> Callable[..., None] | None:
    """Capture live authority for an asynchronous mutation.

    Human sessions additionally freeze the membership incarnation.  Static API
    principals cannot change without replacing the running application, but
    their resource grants are mutable and therefore still need a commit-time
    ACL check.
    """

    principal = request_principal(request)
    user_session = is_user_session_principal(principal)
    auth_store = getattr(request.app.state, "auth_store", None)
    access_store = getattr(request.app.state, "resource_access_store", None)
    if not user_session and (
        access_store is None or principal.key_fingerprint == "auth-disabled"
    ):
        # Legacy/local mode has no live authorization state to revalidate.
        return None
    captured_membership_id = principal.membership_id
    tenant_id = scope.tenant_id
    storage_id = scope.storage_id
    subject_id = principal.subject_id
    key_fingerprint = principal.key_fingerprint

    def authorize_commit(*, require_resource_acl: bool = False) -> None:
        try:
            if access_store is None:
                raise PermissionError("authorization state is unavailable")
            if user_session:
                if not captured_membership_id or auth_store is None:
                    raise PermissionError("authorization state is unavailable")
                membership = auth_store.membership(tenant_id, subject_id)
                if not isinstance(membership, Mapping):
                    raise PermissionError("workspace membership was removed")
                live_membership_id = str(
                    membership.get("member_id") or membership.get("membership_id") or ""
                )
                if live_membership_id != captured_membership_id:
                    raise PermissionError("workspace membership incarnation changed")
                session_is_active = getattr(auth_store, "session_is_active", None)
                session_prefix = "session:"
                if not callable(session_is_active) or not key_fingerprint.startswith(
                    session_prefix
                ):
                    raise PermissionError("session authority is unavailable")
                if not session_is_active(
                    session_id=key_fingerprint[len(session_prefix) :],
                    user_id=subject_id,
                    workspace_id=tenant_id,
                ):
                    raise PermissionError("session authority expired or was revoked")
                live_principal = Principal(
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    role=Role(str(membership.get("role") or "")),
                    key_fingerprint=key_fingerprint,
                    membership_id=live_membership_id,
                )
            else:
                live_principal = principal
            if not live_principal.allows(permission):
                raise PermissionError("principal permission was revoked")
            try:
                lifecycle_active = (
                    shared_lifecycle_store().status(storage_id) == LIFECYCLE_ACTIVE
                )
            except Exception:
                lifecycle_active = False
            if not lifecycle_active:
                # Cleanup retries can legitimately run after the KB ACL row
                # was already erased. Revalidate the live tenant membership,
                # role and physical registry record instead of reopening an
                # ACL-less resource to ordinary reads or writes.
                record = request.app.state.kb_registry.get_by_storage_id(storage_id)
                policy_reader = getattr(access_store, "get_kb_policy", None)
                policy = (
                    policy_reader(tenant_id, storage_id)
                    if require_resource_acl and callable(policy_reader)
                    else None
                )
                valid_record = (
                    isinstance(record, Mapping)
                    and str(record.get("tenant_id") or "default") == tenant_id
                )
                cleanup_authority = live_principal.allows(permission) or (
                    valid_record and str(record.get("owner_id") or "") == subject_id
                )
                if not valid_record or (policy is None and not cleanup_authority):
                    raise PermissionError("knowledge-base cleanup authority changed")
                if not require_resource_acl or policy is None:
                    return
            decision = access_store.allowed_sources(
                live_principal,
                storage_id,
                tenant_id=tenant_id,
                permission=permission,
            )
            if not getattr(decision, "is_allowed", False):
                raise PermissionError("resource authorization was revoked")
            if source is not None:
                allows_source = getattr(decision, "allows_source", None)
                if not callable(allows_source) or not allows_source(source):
                    raise PermissionError("document authorization was revoked")
        except PermissionError:
            raise
        except Exception as exc:
            # Authorization backend failures deny commit; they never become a
            # legacy-workspace fallback at this mutation boundary.
            raise PermissionError("authorization state is unavailable") from exc

    return authorize_commit


# 读取知识库来源文件列表。
def _kb_sources(kb_id: str) -> list[str]:
    from cogdoc.service.retriever_factory import RetrieverFactory
    from cogdoc.service.kb_readers import kb_read_lease

    with kb_read_lease(kb_id):
        return RetrieverFactory.get_engine(kb_id).list_sources()


# 创建 knowledge base。
@router.post("/knowledge-bases", status_code=201, responses=_ERROR_RESPONSES)
async def create_knowledge_base(body: KnowledgeBaseCreate, request: Request):
    index_jobs = request.app.state.index_jobs
    registry = request.app.state.kb_registry
    principal = request_principal(request)
    quota = getattr(request.app.state, "tenant_quota", None)
    reservation = None
    try:
        if quota is not None:
            reservation = quota.reserve_knowledge_base(principal.tenant_id)
        storage_id_for = getattr(registry, "storage_id_for", None)
        storage_id = (
            storage_id_for(body.kb_id, principal.tenant_id)
            if callable(storage_id_for)
            else body.kb_id
        )
        record = await run_sync(
            request.app.state.offload_executor,
            index_jobs.run_blocking,
            storage_id,
            _create_kb,
            body.kb_id,
            principal.tenant_id,
            principal.subject_id,
            registry,
            getattr(request.app.state, "resource_access_store", None),
            getattr(body, "access_policy", "workspace"),
            principal.membership_id,
        )
    except KBExistsError:
        return _error(ErrorCode.KB_EXISTS, f"知识库已存在: {body.kb_id}", 409)
    except TenantQuotaExceeded as exc:
        return _error(ErrorCode.TENANT_QUOTA_EXCEEDED, str(exc), 409)
    finally:
        if quota is not None:
            quota.release(reservation)
    return KnowledgeBase(
        **{key: value for key, value in record.items() if key != "storage_id"},
        document_count=0,
    )


# 列出 knowledge bases。
@router.get("/knowledge-bases")
async def list_knowledge_bases(request: Request):
    result = []
    for scope in tenant_kb_scopes(request):
        documents = await _request_kb_documents(request, scope.storage_id)
        visible_sources = _allowed_sources_for_scope(
            request, scope, (document.name for document in documents)
        )
        result.append(
            KnowledgeBase(
                kb_id=scope.external_id,
                created_at=scope.created_at,
                tenant_id=scope.tenant_id,
                owner_id=scope.owner_id,
                document_count=len(visible_sources),
            )
        )
    return result


# 返回knowledgebase。
@router.get("/knowledge-bases/{kb_id}", responses=_ERROR_RESPONSES)
async def get_knowledge_base(kb_id: str, request: Request):
    scope = resolve_kb_scope(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    documents = await _request_kb_documents(request, scope.storage_id)
    visible_sources = _allowed_sources_for_scope(
        request, scope, (document.name for document in documents)
    )
    return KnowledgeBase(
        kb_id=scope.external_id,
        created_at=scope.created_at,
        tenant_id=scope.tenant_id,
        owner_id=scope.owner_id,
        document_count=len(visible_sources),
    )


# 删除 knowledge base。
@router.delete("/knowledge-bases/{kb_id}", status_code=204, responses=_ERROR_RESPONSES)
async def delete_knowledge_base(kb_id: str, request: Request):
    registry = request.app.state.kb_registry
    scope = resolve_kb_scope(request, kb_id, allow_inactive=True)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    storage_id = scope.storage_id
    if getattr(request.app.state, "ha_document_multiwriter_mode", False):
        coordinator = getattr(request.app.state.ha_runtime, "api_kb_deletion", None)
        if coordinator is None:
            return _error(
                ErrorCode.KB_CLEANUP_FAILED,
                "HA 知识库删除控制面未就绪",
                503,
            )
        authority_evidence = None
        try:
            deletion = coordinator.get(storage_id)
            if deletion is None:
                expected_epoch = capture_ha_chat_epoch(
                    request.app.state.kb_registry, storage_id
                )
                authority_guard = ha_authority_guard(
                    request,
                    scope,
                    expected_epoch,
                    permission=Permission.DELETE,
                )
                authority_evidence = getattr(authority_guard, "evidence", None)
            # Once the transactional HA deletion fence exists, the original
            # live DELETE authority has already been checked in that exact DB
            # transaction. Cleanup deliberately removes ACL rows before the
            # tombstone commit, so retries must resume the durable saga instead
            # of requiring authority state that may already be gone.
            await run_sync(
                request.app.state.connector_cleanup_executor,
                coordinator.delete,
                scope.tenant_id,
                storage_id,
                authority=authority_evidence,
            )
        except KeyError:
            return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
        except (HAChatAuthorityChanged, StaleSessionLease, PermissionError):
            return _error(
                ErrorCode.KB_CLEANUP_FAILED,
                "知识库删除权限或状态已变化，请重试",
                409,
            )
        except Exception:
            return _error(
                ErrorCode.KB_CLEANUP_FAILED,
                "HA 知识库清理未完成，请重试",
                500,
            )
        return Response(status_code=204)
    authorization_guard = _live_session_authorization_guard(
        request,
        scope,
        permission=Permission.DELETE,
    )
    index_jobs = request.app.state.index_jobs
    activity = await run_sync(
        request.app.state.offload_executor,
        request.app.state.connector_sync_store.scope_activity,
        scope.tenant_id,
        storage_id,
    )
    if activity["committing"]:
        return _error(
            ErrorCode.KB_CLEANUP_FAILED,
            "知识库有来源正在提交，请稍后重试删除",
            409,
        )
    try:
        previous_lifecycle = await run_sync(
            request.app.state.offload_executor,
            _begin_connector_kb_delete,
            storage_id,
            registry,
            authorization_guard,
        )
    except (KeyError, PermissionError):
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    try:
        delete_fence = await run_sync(
            request.app.state.offload_executor,
            request.app.state.sync_manager.prepare_knowledge_base_delete,
            scope.tenant_id,
            storage_id,
        )
    except ValueError:
        await run_sync(
            request.app.state.offload_executor,
            _restore_connector_kb_delete,
            storage_id,
            registry,
            previous_lifecycle,
        )
        return _error(
            ErrorCode.KB_CLEANUP_FAILED,
            "知识库有来源正在提交，请稍后重试删除",
            409,
        )
    except TimeoutError:
        return _error(
            ErrorCode.KB_CLEANUP_FAILED,
            "来源同步尚未安全停止，请重试删除",
            500,
        )
    try:
        await run_sync(
            request.app.state.offload_executor,
            _authorize_connector_kb_cleanup,
            storage_id,
            registry,
            authorization_guard,
        )
    except (KeyError, PermissionError):
        await run_sync(
            request.app.state.offload_executor,
            _restore_connector_kb_delete,
            storage_id,
            registry,
            previous_lifecycle,
        )
        await run_sync(
            request.app.state.offload_executor,
            request.app.state.sync_manager.restore_knowledge_base_delete,
            scope.tenant_id,
            storage_id,
            tuple(delete_fence.get("previously_enabled_connection_ids", ())),
        )
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    try:
        # Lifecycle=deleting and the bumped KB epoch reject every new control-
        # plane mutation. Briefly acquiring the cross-store lock drains any
        # mutation admitted before that fence; the potentially large work-tree
        # and artifact purge then runs outside the global lock so one tenant's
        # deletion cannot freeze credentials/OAuth for every other tenant.
        async with request.app.state.connector_credential_reference_lock:
            pass
        await run_sync(
            request.app.state.connector_cleanup_executor,
            _cleanup_connector_kb_state,
            scope.tenant_id,
            storage_id,
            sync_manager=request.app.state.sync_manager,
            oauth_session_store=getattr(
                request.app.state, "connector_oauth_session_store", None
            ),
            credential_vault=getattr(
                request.app.state, "connector_credential_vault", None
            ),
            source_catalog=getattr(request.app.state, "source_catalog", None),
            source_artifact_store=getattr(
                request.app.state, "source_artifact_store", None
            ),
            external_acl_sync_store=getattr(
                request.app.state, "external_acl_sync_store", None
            ),
        )
    except KBCleanupError:
        return _error(
            ErrorCode.KB_CLEANUP_FAILED,
            f"知识库连接状态清理未完成，请重试: {kb_id}",
            500,
        )
    # 排进该 KB 的序列化 executor，等待前序入库任务完成再执行。
    try:
        await run_sync(
            request.app.state.offload_executor,
            index_jobs.run_blocking,
            storage_id,
            _delete_kb,
            storage_id,
            registry,
            index_jobs,
            request.app.state.session_store,
            request.app.state.knowledge_store,
            request.app.state.feedback_store,
            request.app.state.feedback_analysis_store,
            request.app.state.retrieval_feedback_store,
            request.app.state.retrieval_eval_draft_store,
            request.app.state.research_job_store,
            getattr(request.app.state, "resource_access_store", None),
            scope.tenant_id,
            # Live authority was linearized immediately before connector
            # purge and KB epoch invalidation. From that irreversible commit
            # point onward, a later membership change cannot safely roll back
            # already erased credentials/artifacts; lifecycle fencing prevents
            # any unrelated mutation from entering this queue in between.
            None,
        )
    except KBCleanupError:
        # 清理不完整：registry 与 manifest 均保留，返回可重试错误而非误报删除成功。
        return _error(
            ErrorCode.KB_CLEANUP_FAILED, f"知识库清理未完成，请重试: {kb_id}", 500
        )
    return Response(status_code=204)


# 列出 documents。
@router.get("/knowledge-bases/{kb_id}/documents", responses=_ERROR_RESPONSES)
async def list_documents(kb_id: str, request: Request):
    scope = resolve_kb_scope(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    documents = await _request_kb_documents(request, scope.storage_id)
    allowed = set(
        _allowed_sources_for_scope(
            request, scope, (document.name for document in documents)
        )
    )
    return [document for document in documents if document.name in allowed]


# 列出知识库来源文件。
@router.get(
    "/knowledge-bases/{kb_id}/sources",
    response_model=SourceListResponse,
    responses=_ERROR_RESPONSES,
)
async def list_sources(kb_id: str, request: Request):
    scope = resolve_kb_scope(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    source_reader = getattr(request.app.state, "source_list_reader", _kb_sources)
    sources = await run_sync(
        request.app.state.offload_executor, source_reader, scope.storage_id
    )
    return SourceListResponse(
        kb_id=kb_id,
        sources=_allowed_sources_for_scope(request, scope, sources),
    )


# 查询来源文件 chunks。
@router.get(
    "/knowledge-bases/{kb_id}/sources/{source}/chunks",
    response_model=SourceChunksResponse,
    responses=_ERROR_RESPONSES,
)
async def source_chunks(
    kb_id: str,
    source: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    anchor_text: str | None = None,
):
    scope = resolve_kb_scope(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    if not source_is_authorized(request, scope, source, permission=Permission.READ):
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在", 404)
    chunks_reader = getattr(
        request.app.state, "source_chunks_reader", read_source_chunks
    )
    chunks = await run_sync(
        request.app.state.offload_executor, chunks_reader, scope.storage_id, source
    )
    window = chunks[offset : offset + limit]
    return SourceChunksResponse(
        kb_id=kb_id,
        source=source,
        total_count=len(chunks),
        offset=offset,
        limit=limit,
        chunks=[chunk_preview(chunk, anchor_text) for chunk in window],
    )


# 完成 上传document 处理。
@router.post(
    "/knowledge-bases/{kb_id}/documents", status_code=202, responses=_ERROR_RESPONSES
)
async def upload_document(kb_id: str, request: Request, file: UploadFile = File(...)):
    registry = request.app.state.kb_registry
    scope = resolve_kb_scope(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)

    filename = os.path.basename(file.filename or "")
    suffix = os.path.splitext(filename)[1].casefold()
    if filename.startswith(CONNECTOR_MATERIALIZED_PREFIX):
        return _error(
            ErrorCode.INVALID_PDF,
            "文件名使用了连接器保留命名空间",
            400,
        )
    if suffix not in SUPPORTED_EXTENSIONS:
        return _error(
            ErrorCode.INVALID_PDF,
            "不支持该文件格式；可上传 PDF、Markdown、文本、HTML、Office 文档和图片",
            400,
        )

    principal = request_principal(request)
    access_store = getattr(request.app.state, "resource_access_store", None)
    if access_store is not None and not source_is_authorized(
        request, scope, filename, permission=Permission.WRITE
    ):
        # A document-specific grant does not authorize replacing another
        # private source or adding arbitrary documents to the KB.
        decision = resource_access_decision(request, scope, permission=Permission.WRITE)
        if (
            decision is False
            or str(getattr(getattr(decision, "mode", None), "value", "")) != "all"
        ):
            return _error(ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在", 404)

    settings = get_settings()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    # 分块读并即时熔断，内存占用以上限为界，不被客户端声明的大小拖垮。
    content = bytearray()
    while True:
        block = await file.read(1024 * 1024)
        if not block:
            break
        content.extend(block)
        if len(content) > max_bytes:
            return _error(
                ErrorCode.FILE_TOO_LARGE,
                f"文件超过上限 {settings.max_upload_mb}MB",
                413,
            )
    if suffix != ".pdf" and content.startswith(_PDF_MAGIC):
        return _error(ErrorCode.INVALID_PDF, "文件内容与扩展名不匹配", 400)
    if suffix == ".pdf" and not content.startswith(_PDF_MAGIC):
        return _error(ErrorCode.INVALID_PDF, "文件不是合法 PDF", 400)

    if access_store is not None:
        try:
            existing_policy = access_store.get_document_by_source(
                scope.tenant_id, scope.storage_id, filename
            )
            if existing_policy is None:
                access_store.set_document_policy(
                    scope.tenant_id,
                    scope.storage_id,
                    build_document_id(filename),
                    filename,
                    principal.subject_id,
                    "inherit",
                    owner_membership_id=principal.membership_id,
                )
        except Exception:
            return _error(
                ErrorCode.INTERNAL_ERROR,
                "文档权限状态不可用，未执行上传",
                503,
            )

    storage_id = scope.storage_id
    authorization_guard = _live_session_authorization_guard(
        request,
        scope,
        permission=Permission.WRITE,
        source=filename,
    )
    source_dir = registry.source_dir(storage_id)
    quota = getattr(request.app.state, "tenant_quota", None)
    reservation = None
    if quota is not None:
        try:
            reservation = quota.reserve_upload(
                scope.tenant_id,
                storage_id,
                source_dir,
                filename,
                len(content),
            )
        except TenantQuotaExceeded as exc:
            return _error(ErrorCode.TENANT_QUOTA_EXCEEDED, str(exc), 409)
        except TenantMutationInProgress as exc:
            return _error(ErrorCode.BAD_REQUEST, str(exc), 409)
    # submit_upload 含同步 SQLite 写：放线程池执行，绝不阻塞事件循环（否则 SQLite 锁竞争会冻结整个 API）。
    try:
        job = await run_sync(
            request.app.state.offload_executor,
            request.app.state.index_jobs.submit_upload,
            storage_id,
            source_dir,
            filename,
            bytes(content),
            (lambda: quota.release(reservation)) if quota is not None else None,
            authorization_guard,
        )
    except Exception:
        if quota is not None:
            quota.release(reservation)
        raise
    return _public_job(job, request)


# 删除 document。
@router.delete(
    "/knowledge-bases/{kb_id}/documents/{name}",
    status_code=202,
    responses=_ERROR_RESPONSES,
)
async def delete_document(kb_id: str, name: str, request: Request):
    registry = request.app.state.kb_registry
    scope = resolve_kb_scope(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)

    safe_name = os.path.basename(name)
    if safe_name.startswith(CONNECTOR_MATERIALIZED_PREFIX):
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在", 404)
    if not source_is_authorized(
        request, scope, safe_name, permission=Permission.DELETE
    ):
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在", 404)
    storage_id = scope.storage_id
    path = os.path.join(registry.source_dir(storage_id), safe_name)
    access_store = getattr(request.app.state, "resource_access_store", None)
    on_succeeded: Callable[[], None] | None = None
    on_retiring: Callable[[], None] | None = None
    if access_store is not None:
        document_id = build_document_id(safe_name)
        managed_by = f"document-delete:{document_id}"

        def fence_document_access() -> None:
            access_store.begin_document_retirement(
                scope.tenant_id,
                storage_id,
                managed_by,
                (document_id,),
            )

        def clear_document_access() -> None:
            # IndexJobManager invokes this only after the new index generation
            # without the source has committed and its HA mirror is current.
            # The atomic finish removes policy, grants and the retirement fence.
            access_store.finish_document_retirement(
                scope.tenant_id,
                storage_id,
                managed_by,
                (document_id,),
            )

        on_retiring = fence_document_access
        on_succeeded = clear_document_access
    authorization_guard = _live_session_authorization_guard(
        request,
        scope,
        permission=Permission.DELETE,
        source=safe_name,
    )
    # 同步 SQLite 写下放线程池，不阻塞事件循环；存在性检查仍在 executor command 内完成，路由始终 202。
    job = await run_sync(
        request.app.state.offload_executor,
        request.app.state.index_jobs.submit_delete_doc,
        storage_id,
        path,
        on_succeeded,
        authorization_guard,
        on_retiring,
    )
    return _public_job(job, request)


# 返回索引任务。
@router.get("/index-jobs/{job_id}", responses=_ERROR_RESPONSES)
async def get_index_job(job_id: str, request: Request):
    job = request.app.state.index_jobs.get(job_id)
    if job is None:
        return _error(ErrorCode.JOB_NOT_FOUND, f"任务不存在: {job_id}", 404)
    storage_id = str(job.get("kb_id") or "")
    readable = scope_for_storage_id(request, storage_id)
    # A document retirement deliberately removes READ while an old index may
    # still contain the source. The actor still needs to poll the deletion job;
    # DELETE is not a content permission and remains fenced to authorized
    # operators by ResourceAccessStore.
    deletion_control = (
        None
        if readable is not None
        else scope_for_storage_id(
            request,
            storage_id,
            permission=Permission.DELETE,
        )
    )
    if readable is None and deletion_control is None:
        return _error(ErrorCode.JOB_NOT_FOUND, f"任务不存在: {job_id}", 404)
    return _public_job(job, request)
