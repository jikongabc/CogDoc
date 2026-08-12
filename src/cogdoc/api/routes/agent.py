from collections.abc import Mapping
from functools import partial
import inspect
from typing import Any
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from cogdoc.api.error_mapping import classify_error_code, status_for_code
from cogdoc.api.offload import run_sync
from cogdoc.api.runners import run_with_optional_session
from cogdoc.api.schemas import (
    ChatResponse,
    ErrorCode,
    ErrorResponse,
    RetrieveHit,
    RetrieveRequest,
    RetrieveResponse,
    TaskRequest,
    build_error_response,
    chat_result_to_response,
)
from cogdoc.api.tenant_scope import (
    internal_session_id,
    resolve_kb_scope,
    retrieval_scope_for_request,
    session_store_doc_id,
)
from cogdoc.config.settings import get_settings
from cogdoc.service.chat_service import ChatResult, ChatServiceError, run_chat_sync


router = APIRouter(prefix="/v1", tags=["agent"])

_ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
    504: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}
_RETRIEVE_PREVIEW_CHARS = 240


# 构建稳定错误响应。
def _error(
    code: ErrorCode,
    message: str,
    status: int,
    *,
    request_id: str | None = None,
    trace_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    error = build_error_response(
        code,
        message,
        request_id=request_id,
        trace_id=trace_id,
        details=details,
    )
    return JSONResponse(status_code=status, content=error.model_dump())


# 校验知识库存在。
def _ensure_kb_exists(request: Request, kb_id: str) -> JSONResponse | None:
    if resolve_kb_scope(request, kb_id) is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    return None


# 写入任务会话历史。
def _record_task_session(
    request: Request,
    body: TaskRequest,
    result: ChatResult,
) -> None:
    request.app.state.session_store.record(
        body.doc_id,
        body.session_id,
        result.chat_messages,
        [
            {"role": "user", "content": body.query},
            {
                "role": "assistant",
                "content": result.answer,
                "trace_id": result.trace_id,
                "query": body.query,
                "task_type": result.task_type,
            },
        ],
    )


# 执行摘要或对比。
async def _task_endpoint(
    body: TaskRequest,
    request: Request,
    response: Response,
    forced_task: str,
) -> ChatResponse | JSONResponse:
    scope = resolve_kb_scope(request, body.doc_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    external_doc_id = body.doc_id
    retrieval_scope = retrieval_scope_for_request(request, scope)
    body = body.model_copy(update={"doc_id": scope.storage_id})

    runner = getattr(request.app.state, "chat_runner", run_chat_sync)
    session_store = request.app.state.session_store
    chat_history = session_store.get_history(
        session_store_doc_id(request, body.doc_id), body.session_id, body.query
    )
    try:
        result = await run_sync(
            request.app.state.offload_executor,
            run_with_optional_session,
            runner,
            body.doc_id,
            body.query,
            body.is_local,
            chat_history,
            forced_task,
            internal_session_id(request, body.session_id),
            retrieval_scope,
        )
    except ChatServiceError as exc:
        code = classify_error_code(exc.stage, exc.error_class, exc.message)
        return _error(
            code,
            exc.message,
            status_for_code(code),
            request_id=exc.trace_id,
            trace_id=exc.trace_id,
            details={"error_class": exc.error_class, "stage": exc.stage},
        )

    session_body = body.model_copy(
        update={"doc_id": session_store_doc_id(request, body.doc_id)}
    )
    _record_task_session(request, session_body, result)
    request.app.state.metrics.chat_results.labels(
        result.task_type, str(result.is_valid).lower()
    ).inc()
    request.app.state.metrics.observe_claim_audit(
        result.task_type,
        result.raw_output.get("claim_audit"),
    )
    request.app.state.metrics.observe_retrieval(result.task_type, result.raw_output)
    task_response = chat_result_to_response(
        result, doc_id=external_doc_id, session_id=body.session_id
    )
    response.headers["X-Trace-Id"] = task_response.trace_id
    return task_response


# 运行检索。
def _run_retrieve(
    body: RetrieveRequest, *, state_runtime=None, retrieval_scope=None
) -> list:
    from cogdoc.service.retriever_factory import RetrieverFactory
    from cogdoc.service.kb_readers import kb_read_lease
    from cogdoc.service.retrieval_pipeline import (
        build_retrieval_queries,
        retrieve_candidate_pool,
    )
    from cogdoc.state_runtime import default_state_runtime
    from cogdoc.tools.reranker import BGEReranker, skipped_cpu_rerank_docs

    runtime = state_runtime or default_state_runtime()
    settings = get_settings()

    with kb_read_lease(body.doc_id):
        engine = RetrieverFactory.get_engine(body.doc_id)
        retrieval_result = retrieve_candidate_pool(
            engine,
            runtime.derived_knowledge_retriever,
            runtime.retrieval_feedback_store,
            kb_id=body.doc_id,
            original_query=body.query,
            queries=build_retrieval_queries(body.query, max_queries=1),
            top_k=body.top_k,
            rrf_k=float(settings.hybrid_rrf_k),
            scope=retrieval_scope,
        )
        docs = retrieval_result.docs
        if not body.rerank or not docs:
            return docs
        target_device = BGEReranker.default_device()
        top_n = body.rerank_top_n or body.top_k
        if target_device == "cpu" and not settings.qa_rerank_on_cpu:
            return skipped_cpu_rerank_docs(docs, top_n)
        return BGEReranker.rerank(body.query, docs, top_n=top_n, device=target_device)


def _call_retrieve_runner(runner, body: RetrieveRequest, retrieval_scope):
    """Call legacy test/custom runners while enforcing the final route guard."""

    try:
        parameters = inspect.signature(runner).parameters
        accepts_scope = "retrieval_scope" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
    except (TypeError, ValueError):
        accepts_scope = False
    docs = (
        runner(body, retrieval_scope=retrieval_scope)
        if accepts_scope
        else runner(body)
    )
    return [
        doc
        for doc in docs
        if isinstance(doc, Mapping) and retrieval_scope.allows_document(doc)
    ]


# 截断文本预览。
def _preview(text: Any) -> str:
    return " ".join(str(text or "").split())[:_RETRIEVE_PREVIEW_CHARS]


# 转换检索元数据。
def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _json_safe(item) for key, item in value.items()}


# 转换为可序列化值。
def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


# 构建检索命中项。
def _retrieve_hit(rank: int, doc: Mapping[str, Any]) -> RetrieveHit:
    meta = doc.get("meta") if isinstance(doc.get("meta"), Mapping) else {}
    retrieval = _safe_mapping(doc.get("retrieval"))
    page = meta.get("page")
    return RetrieveHit(
        rank=rank,
        chunk_id=str(meta.get("chunk_id", "")),
        parent_chunk_id=str(meta.get("parent_chunk_id", "") or ""),
        section_title=str(meta.get("section_title", "") or ""),
        section_path=str(meta.get("section_path", "") or ""),
        section_level=meta.get("section_level"),
        child_index_in_parent=meta.get("child_index_in_parent"),
        source_type=str(meta.get("source_type", "document") or "document"),
        knowledge_id=str(meta.get("knowledge_id", "") or ""),
        chunk_index=meta.get("chunk_index"),
        source=str(meta.get("source", "") or ""),
        page=page,
        page_start=meta.get("page_start", page),
        page_end=meta.get("page_end", page),
        rerank_score=retrieval.get("rerank_score"),
        rewrite_query=retrieval.get("rewrite_query"),
        text_preview=_preview(doc.get("text", "")),
        retrieval=retrieval,
    )


# 单文档摘要接口。
@router.post("/summary", response_model=ChatResponse, responses=_ERROR_RESPONSES)
async def summary(body: TaskRequest, request: Request, response: Response):
    return await _task_endpoint(body, request, response, "summary")


# 多文档对比接口。
@router.post("/compare", response_model=ChatResponse, responses=_ERROR_RESPONSES)
async def compare(body: TaskRequest, request: Request, response: Response):
    return await _task_endpoint(body, request, response, "compare")


# 结构化检索接口。
@router.post("/retrieve", response_model=RetrieveResponse, responses=_ERROR_RESPONSES)
async def retrieve(body: RetrieveRequest, request: Request):
    scope = resolve_kb_scope(request, body.doc_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    external_doc_id = body.doc_id
    retrieval_scope = retrieval_scope_for_request(request, scope)
    body = body.model_copy(update={"doc_id": scope.storage_id})
    retrieve_runner = getattr(request.app.state, "retrieve_runner", None) or partial(
        _run_retrieve,
        state_runtime=request.app.state.state_runtime,
    )
    docs = await run_sync(
        request.app.state.offload_executor,
        _call_retrieve_runner,
        retrieve_runner,
        body,
        retrieval_scope,
    )
    hits = [
        _retrieve_hit(rank, doc)
        for rank, doc in enumerate(docs, start=1)
        if isinstance(doc, Mapping)
    ]
    return RetrieveResponse(
        doc_id=external_doc_id,
        query=body.query,
        top_k=body.top_k,
        rerank=body.rerank,
        hits=hits,
    )
