import asyncio
import json
from typing import Callable
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from cogdoc.api.error_mapping import classify_error_code, status_for_code
from cogdoc.api.claim_verification_observability import (
    record_claim_verification_observation,
)
from cogdoc.api.offload import run_sync
from cogdoc.api.runners import run_with_optional_session
from cogdoc.api.schemas import (
    ChatRequest,
    ChatResponse,
    ErrorCode,
    ErrorResponse,
    MemorySnapshotResponse,
    SessionHistoryResponse,
    SessionListResponse,
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
from cogdoc.observability.trace import delete_trace_files
from cogdoc.service.chat_service import (
    ChatEvent,
    ChatResult,
    ChatServiceError,
    run_chat,
    run_chat_sync,
)


ChatRunner = Callable[..., ChatResult]

router = APIRouter(prefix="/v1", tags=["chat"])

# OpenAPI 错误响应契约，让前端按稳定 schema 处理失败。
_ERROR_RESPONSES = {
    429: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
    504: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


# 完成 chat 处理。
@router.post("/chat", response_model=ChatResponse, responses=_ERROR_RESPONSES)
async def chat(request_body: ChatRequest, request: Request, response: Response):
    # 同步问答：offload 跑图 → 写会话 → 映射结构化响应；服务层异常转稳定错误码。
    scope = resolve_kb_scope(request, request_body.doc_id, allow_legacy_default=True)
    if scope is None:
        error = build_error_response(
            ErrorCode.KB_NOT_FOUND, "知识库不存在"
        )
        return JSONResponse(status_code=404, content=error.model_dump())
    external_doc_id = request_body.doc_id
    external_chat_session_id = request_body.session_id
    retrieval_scope = retrieval_scope_for_request(request, scope)
    request_body = request_body.model_copy(update={"doc_id": scope.storage_id})
    runner: ChatRunner = getattr(request.app.state, "chat_runner", run_chat_sync)
    session_store = request.app.state.session_store
    chat_history = session_store.get_history(
        session_store_doc_id(request, request_body.doc_id),
        external_chat_session_id,
        request_body.query,
    )

    try:
        # 用 app 级有界线程池 offload 同步图：不阻塞事件循环、不无界起线程、不走 anyio。
        result = await run_sync(
            request.app.state.offload_executor,
            run_with_optional_session,
            runner,
            request_body.doc_id,
            request_body.query,
            request_body.is_local,
            chat_history,
            request_body.forced_task,
            internal_session_id(request, external_chat_session_id),
            retrieval_scope,
        )
    except ChatServiceError as exc:
        error_code = classify_error_code(exc.stage, exc.error_class, exc.message)
        error = build_error_response(
            error_code,
            exc.message,
            request_id=exc.trace_id,
            trace_id=exc.trace_id,
            details={"error_class": exc.error_class, "stage": exc.stage},
        )
        return JSONResponse(
            status_code=status_for_code(error_code),
            content=error.model_dump(),
            headers={"X-Trace-Id": exc.trace_id or ""},
        )

    # 记忆走门控后的 chat_messages；展示存「用户问题 + 实际答案」，切对话时能看到内容。
    session_store.record(
        session_store_doc_id(request, request_body.doc_id),
        external_chat_session_id,
        result.chat_messages,
        [
            {"role": "user", "content": request_body.query},
            {
                "role": "assistant",
                "content": result.answer,
                "trace_id": result.trace_id,
                "query": request_body.query,
                "task_type": result.task_type,
            },
        ],
    )
    request.app.state.metrics.chat_results.labels(
        result.task_type, str(result.is_valid).lower()
    ).inc()
    request.app.state.metrics.observe_claim_audit(
        result.task_type,
        result.raw_output.get("claim_audit"),
    )
    request.app.state.metrics.observe_claim_verification_rollout(
        result.task_type,
        result.raw_output.get("claim_verification_rollout"),
    )
    record_claim_verification_observation(
        request, result, kb_id=request_body.doc_id
    )
    request.app.state.metrics.observe_retrieval(result.task_type, result.raw_output)
    chat_response = chat_result_to_response(
        result,
        doc_id=external_doc_id,
        session_id=external_chat_session_id,
    )
    response.headers["X-Trace-Id"] = chat_response.trace_id
    return chat_response


# 列出 sessions。
@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(request: Request, doc_id: str = Query(default="")):
    # 列出某库下的全部对话，供前端多对话列表。
    kb_id = doc_id or get_settings().cogdoc_default_doc_id
    scope = resolve_kb_scope(request, kb_id, allow_legacy_default=True)
    if scope is None:
        return SessionListResponse(doc_id=kb_id, sessions=[])
    sessions = request.app.state.session_store.list_sessions(
        session_store_doc_id(request, scope.storage_id)
    )
    return SessionListResponse(doc_id=kb_id, sessions=sessions)


# 完成 会话历史记录 处理。
@router.get("/sessions/{session_id}/history", response_model=SessionHistoryResponse)
async def session_history(
    session_id: str, request: Request, doc_id: str = Query(default="")
):
    # 前端刷新后凭 URL 里的 session_id 拉回多轮历史；会话态仍在内存（服务存活期内）。
    kb_id = doc_id or get_settings().cogdoc_default_doc_id
    scope = resolve_kb_scope(request, kb_id, allow_legacy_default=True)
    messages = (
        request.app.state.session_store.get_display(
            session_store_doc_id(request, scope.storage_id), session_id
        )
        if scope is not None
        else []
    )
    return SessionHistoryResponse(
        session_id=session_id, doc_id=kb_id, messages=messages
    )


# 返回会话三层记忆快照。
@router.get("/sessions/{session_id}/memory", response_model=MemorySnapshotResponse)
async def session_memory(
    session_id: str, request: Request, doc_id: str = Query(default="")
):
    kb_id = doc_id or get_settings().cogdoc_default_doc_id
    scope = resolve_kb_scope(request, kb_id, allow_legacy_default=True)
    snapshot = (
        request.app.state.session_store.get_memory_snapshot(
            session_store_doc_id(request, scope.storage_id), session_id
        )
        if scope is not None
        else {"short_term": [], "mid_term": {}, "long_term": []}
    )
    return MemorySnapshotResponse(
        session_id=session_id,
        doc_id=kb_id,
        **snapshot,
    )


# 清除知识库长期记忆。
@router.delete("/memory/long-term", status_code=204)
async def delete_long_term_memory(request: Request, doc_id: str = Query(default="")):
    kb_id = doc_id or get_settings().cogdoc_default_doc_id
    scope = resolve_kb_scope(request, kb_id, allow_legacy_default=True)
    if scope is not None:
        request.app.state.session_store.clear_long_term(
            session_store_doc_id(request, scope.storage_id)
        )
    return Response(status_code=204)


# 删除 session。
@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str, request: Request, doc_id: str = Query(default="")
):
    # 删除一个对话的多轮历史（幂等，不存在也返回 204）。
    kb_id = doc_id or get_settings().cogdoc_default_doc_id
    scope = resolve_kb_scope(request, kb_id, allow_legacy_default=True)
    if scope is None:
        return Response(status_code=204)
    request.app.state.session_store.clear(
        session_store_doc_id(request, scope.storage_id), session_id
    )
    await run_sync(
        request.app.state.offload_executor,
        delete_trace_files,
        scope.storage_id,
        internal_session_id(request, session_id),
    )
    return Response(status_code=204)


# 流式进度事件直接转发；token/final/error 单独成结构化帧。
_SSE_PROGRESS_TYPES = {
    "router_decided",
    "rewrite_queries",
    "retrieval_abstained",
    "retrieval_retry",
    "evidence_verified",
    "evidence_rejected",
    "citation_passed",
    "citation_rejected",
    "compare_citation_passed",
    "compare_citation_rejected",
    "claim_audit",
    "claim_repair",
    "claim_rejected",
}
_STREAM_DONE = object()
_STREAM_QUEUE_WATCHDOG_SECONDS = 0.05


# 封装SSE 帧。
def _sse_frame(event_name: str, data: dict) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# 转换toSSE 帧。
def _event_to_frame(
    event: ChatEvent, *, doc_id: str, session_id: str | None
) -> str | None:
    # 把服务层 ChatEvent 转成 SSE 帧；final 发结构化响应、error 转稳定错误码、其余为进度。
    if event.type == "request_started":
        return _sse_frame(
            "start",
            {"trace_id": event.payload.get("trace_id"), "doc_id": doc_id},
        )
    if event.type == "token":
        return _sse_frame("token", {"content": event.payload.get("content", "")})
    if event.type in _SSE_PROGRESS_TYPES:
        # round_answer 是 CLI 展示用的整段模型回答，不进流式帧。
        data = {k: v for k, v in event.payload.items() if k != "round_answer"}
        data["stage"] = event.type
        return _sse_frame("node", data)
    if event.type == "final":
        chat_response = chat_result_to_response(
            event.payload["result"], doc_id=doc_id, session_id=session_id
        )
        return _sse_frame("final", chat_response.model_dump())
    if event.type == "error":
        error_code = classify_error_code(
            event.payload.get("stage", ""),
            event.payload.get("error_class", ""),
            event.payload.get("message", ""),
        )
        error = build_error_response(
            error_code,
            event.payload.get("message", ""),
            request_id=event.payload.get("trace_id"),
            trace_id=event.payload.get("trace_id"),
            details={
                "error_class": event.payload.get("error_class"),
                "stage": event.payload.get("stage"),
            },
        )
        return _sse_frame("error", error.model_dump())
    return None


# 完成 chat流式响应 处理。
@router.post("/chat/stream", responses=_ERROR_RESPONSES)
async def chat_stream(request_body: ChatRequest, request: Request):
    # SSE 流式问答：worker 线程跑事件流 → 队列桥到事件循环 → 逐帧输出。
    scope = resolve_kb_scope(request, request_body.doc_id, allow_legacy_default=True)
    if scope is None:
        error = build_error_response(
            ErrorCode.KB_NOT_FOUND, "知识库不存在"
        )
        return JSONResponse(status_code=404, content=error.model_dump())
    external_doc_id = request_body.doc_id
    external_chat_session_id = request_body.session_id
    retrieval_scope = retrieval_scope_for_request(request, scope)
    request_body = request_body.model_copy(update={"doc_id": scope.storage_id})
    stream_runner = getattr(request.app.state, "chat_stream_runner", run_chat)
    session_store = request.app.state.session_store
    doc_id = request_body.doc_id
    session_id = external_chat_session_id
    chat_history = session_store.get_history(
        session_store_doc_id(request, doc_id), session_id, request_body.query
    )

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    # 生产结果。
    def produce() -> None:
        # 同步事件流跑在有界线程池里，逐事件回投到事件循环的队列。
        try:
            for event in run_with_optional_session(
                stream_runner,
                doc_id,
                request_body.query,
                request_body.is_local,
                chat_history,
                request_body.forced_task,
                internal_session_id(request, session_id),
                retrieval_scope,
            ):
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:
            loop.call_soon_threadsafe(
                queue.put_nowait,
                ChatEvent(
                    "error",
                    {
                        "error_class": type(exc).__name__,
                        "message": str(exc),
                        "stage": "runtime",
                    },
                ),
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _STREAM_DONE)

    try:
        producer_future = request.app.state.offload_executor.submit(produce)
    except RuntimeError:
        error = build_error_response(
            ErrorCode.MODEL_UNAVAILABLE,
            "服务正在关闭，请重试",
        )
        return JSONResponse(status_code=503, content=error.model_dump())

    idle_timeout_seconds = get_settings().cogdoc_chat_stream_idle_timeout_seconds

    # 转换来源。
    async def event_source():
        final_result: ChatResult | None = None
        recorded = False
        last_activity = loop.time()

        # 记录最终结果。
        def record_final(result: ChatResult) -> None:
            nonlocal recorded
            if recorded:
                return
            request.app.state.metrics.chat_results.labels(
                result.task_type, str(result.is_valid).lower()
            ).inc()
            request.app.state.metrics.observe_claim_audit(
                result.task_type,
                result.raw_output.get("claim_audit"),
            )
            request.app.state.metrics.observe_claim_verification_rollout(
                result.task_type,
                result.raw_output.get("claim_verification_rollout"),
            )
            record_claim_verification_observation(request, result, kb_id=doc_id)
            request.app.state.metrics.observe_retrieval(
                result.task_type, result.raw_output
            )
            session_store.record(
                session_store_doc_id(request, doc_id),
                session_id,
                result.chat_messages,
                [
                    {"role": "user", "content": request_body.query},
                    {
                        "role": "assistant",
                        "content": result.answer,
                        "trace_id": result.trace_id,
                        "query": request_body.query,
                        "task_type": result.task_type,
                    },
                ],
            )
            recorded = True

        try:
            while True:
                remaining = idle_timeout_seconds - (loop.time() - last_activity)
                if remaining <= 0:
                    producer_future.cancel()
                    timeout_event = ChatEvent(
                        "error",
                        {
                            "error_class": "TimeoutError",
                            "message": "流式响应等待超时",
                            "stage": "stream",
                        },
                    )
                    yield _event_to_frame(
                        timeout_event,
                        doc_id=external_doc_id,
                        session_id=session_id,
                    )
                    break
                try:
                    # ``call_soon_threadsafe`` normally wakes the selector, but
                    # a lost/late self-pipe notification must not strand this
                    # request after the producer has already finished.  The
                    # short timer also lets us observe the idle deadline.
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=min(_STREAM_QUEUE_WATCHDOG_SECONDS, remaining),
                    )
                except TimeoutError:
                    if producer_future.done():
                        # Give callbacks already placed on the loop's ready
                        # queue one turn before treating a completed producer
                        # with no sentinel as exhausted.
                        await asyncio.sleep(0)
                        try:
                            event = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            producer_error = producer_future.exception()
                            if producer_error is not None:
                                failure_event = ChatEvent(
                                    "error",
                                    {
                                        "error_class": type(producer_error).__name__,
                                        "message": str(producer_error),
                                        "stage": "runtime",
                                    },
                                )
                                yield _event_to_frame(
                                    failure_event,
                                    doc_id=external_doc_id,
                                    session_id=session_id,
                                )
                            break
                    else:
                        continue
                last_activity = loop.time()
                if event is _STREAM_DONE:
                    break
                if event.type == "final":
                    final_result = event.payload["result"]
                    record_final(final_result)
                frame = _event_to_frame(
                    event, doc_id=external_doc_id, session_id=session_id
                )
                if frame is not None:
                    yield frame
            # 兜底：理论上 final 事件已即时记录；保留防止未来事件处理顺序变化。
            if final_result is not None:
                record_final(final_result)
        finally:
            # Cancellation succeeds for queued work.  A running synchronous
            # provider must still obey its own configured transport timeout.
            if not producer_future.done():
                producer_future.cancel()

    return StreamingResponse(event_source(), media_type="text/event-stream")
