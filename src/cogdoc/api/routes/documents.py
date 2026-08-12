from collections.abc import Callable, Mapping
import os
from fastapi import APIRouter, File, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from cogdoc.api.ingest import KBExistsError
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
from cogdoc.service.kb_state import KBState
from cogdoc.tools.manifest import load_index_manifest
from cogdoc.tools.chunk_identity import build_document_id

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
    authorization_guard: Callable[[], None] | None = None,
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
            registry.delete(kb_id)
            if resource_access_store is not None:
                try:
                    resource_access_store.clear_kb(tenant_id, kb_id)
                except Exception as exc:
                    raise KBCleanupError(f"KB ACL 状态删除失败: {kb_id}") from exc
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
def _kb_documents(kb_id: str) -> list[Document]:
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
        )
        for doc in documents
        if doc.get("name")
    ]


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
) -> Callable[[], None] | None:
    """Capture a human membership incarnation for an asynchronous mutation."""

    principal = request_principal(request)
    if not is_user_session_principal(principal):
        return None
    auth_store = getattr(request.app.state, "auth_store", None)
    access_store = getattr(request.app.state, "resource_access_store", None)
    captured_membership_id = principal.membership_id
    tenant_id = scope.tenant_id
    storage_id = scope.storage_id
    subject_id = principal.subject_id
    key_fingerprint = principal.key_fingerprint

    def authorize_commit() -> None:
        try:
            if not captured_membership_id or auth_store is None or access_store is None:
                raise PermissionError("authorization state is unavailable")
            membership = auth_store.membership(tenant_id, subject_id)
            if not isinstance(membership, Mapping):
                raise PermissionError("workspace membership was removed")
            live_membership_id = str(
                membership.get("member_id") or membership.get("membership_id") or ""
            )
            if live_membership_id != captured_membership_id:
                raise PermissionError("workspace membership incarnation changed")
            live_principal = Principal(
                tenant_id=tenant_id,
                subject_id=subject_id,
                role=Role(str(membership.get("role") or "")),
                key_fingerprint=key_fingerprint,
                membership_id=live_membership_id,
            )
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
        documents = _kb_documents(scope.storage_id)
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
    documents = _kb_documents(scope.storage_id)
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
    scope = resolve_kb_scope(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    storage_id = scope.storage_id
    authorization_guard = _live_session_authorization_guard(
        request,
        scope,
        permission=Permission.DELETE,
    )
    index_jobs = request.app.state.index_jobs
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
            authorization_guard,
        )
    except PermissionError:
        # A queued delete whose authority disappeared is indistinguishable from
        # an inaccessible KB, including across membership reincarnations.
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
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
    documents = _kb_documents(scope.storage_id)
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
    if not filename.lower().endswith(".pdf"):
        return _error(ErrorCode.INVALID_PDF, "只接受 .pdf 文件", 400)

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
    if not content.startswith(_PDF_MAGIC):
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
    if not source_is_authorized(
        request, scope, safe_name, permission=Permission.DELETE
    ):
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在", 404)
    storage_id = scope.storage_id
    path = os.path.join(registry.source_dir(storage_id), safe_name)
    access_store = getattr(request.app.state, "resource_access_store", None)
    on_succeeded: Callable[[], None] | None = None
    if access_store is not None:
        document_id = build_document_id(safe_name)

        def clear_document_access() -> None:
            # IndexJobManager invokes this only after the new index generation
            # without the source has committed, and before publishing a
            # succeeded job. delete_document_policy atomically removes both
            # the document policy and its document-scoped grants.
            access_store.delete_document_policy(
                scope.tenant_id,
                storage_id,
                document_id,
            )

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
    )
    return _public_job(job, request)


# 返回索引任务。
@router.get("/index-jobs/{job_id}", responses=_ERROR_RESPONSES)
async def get_index_job(job_id: str, request: Request):
    job = request.app.state.index_jobs.get(job_id)
    if job is None:
        return _error(ErrorCode.JOB_NOT_FOUND, f"任务不存在: {job_id}", 404)
    if scope_for_storage_id(request, str(job.get("kb_id") or "")) is None:
        return _error(ErrorCode.JOB_NOT_FOUND, f"任务不存在: {job_id}", 404)
    return _public_job(job, request)
