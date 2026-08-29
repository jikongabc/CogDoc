import asyncio
import json
import logging
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from typing import Callable
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from cogdoc.api.error_mapping import classify_error_code, status_for_code
from cogdoc.api.claim_verification_observability import (
    record_claim_verification_observation,
)
from cogdoc.api.ha_chat_authority import (
    HAChatAuthorityChanged,
    capture_ha_chat_epoch,
    ha_authority_guard,
    ha_chat_authority_guard,
)
from cogdoc.api.offload import run_sync
from cogdoc.api.connector_scope import (
    capture_kb_epoch,
)
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
from cogdoc.api.tenancy import Permission
from cogdoc.config.settings import get_settings
from cogdoc.ha.index_generation import StaleIndexFence
from cogdoc.ha.index_replica import IndexReplicaError
from cogdoc.ha.chat_execution import ha_retrieval_scope
from cogdoc.ha.session_store import SessionBusy, StaleSessionLease
from cogdoc.observability.trace import delete_trace_files
from cogdoc.observability.logger import log_event
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
    409: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
    504: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


def _ha_chat_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, SessionBusy):
        error = build_error_response(
            ErrorCode.CHAT_SESSION_CONFLICT,
            "该会话已有请求正在处理，请等待完成后重试",
        )
        return JSONResponse(status_code=409, content=error.model_dump())
    if isinstance(exc, (HAChatAuthorityChanged, StaleSessionLease)):
        error = build_error_response(
            ErrorCode.CHAT_SESSION_CONFLICT,
            "会话权限或状态已变化，请重试",
        )
        return JSONResponse(status_code=409, content=error.model_dump())
    if isinstance(exc, (IndexReplicaError, StaleIndexFence)):
        error = build_error_response(
            ErrorCode.MODEL_UNAVAILABLE,
            "索引快照暂不可用，请稍后重试",
        )
        return JSONResponse(status_code=503, content=error.model_dump())
    raise exc


def _epoch_unavailable_error(exc: Exception, storage_id: str) -> JSONResponse:
    log_event(
        "cogdoc.chat",
        "chat_epoch_unavailable",
        level=logging.ERROR,
        storage_id=storage_id,
        error_class=type(exc).__name__,
    )
    error = build_error_response(
        ErrorCode.MODEL_UNAVAILABLE,
        "知识库状态暂不可用，请稍后重试",
    )
    return JSONResponse(status_code=503, content=error.model_dump())


# 完成 chat 处理。
@router.post("/chat", response_model=ChatResponse, responses=_ERROR_RESPONSES)
async def chat(request_body: ChatRequest, request: Request, response: Response):
    # 同步问答：offload 跑图 → 写会话 → 映射结构化响应；服务层异常转稳定错误码。
    scope = resolve_kb_scope(request, request_body.doc_id, allow_legacy_default=True)
    if scope is None:
        error = build_error_response(ErrorCode.KB_NOT_FOUND, "知识库不存在")
        return JSONResponse(status_code=404, content=error.model_dump())
    external_doc_id = request_body.doc_id
    external_chat_session_id = request_body.session_id
    retrieval_scope = retrieval_scope_for_request(request, scope)
    request_body = request_body.model_copy(update={"doc_id": scope.storage_id})
    runner: ChatRunner = getattr(request.app.state, "chat_runner", run_chat_sync)
    session_store = request.app.state.session_store
    session_scope_id = session_store_doc_id(request, request_body.doc_id)
    ha_coordinator = getattr(request.app.state, "ha_chat_coordinator", None)
    try:
        expected_epoch = capture_ha_chat_epoch(
            request.app.state.kb_registry, scope.storage_id
        )
    except Exception as exc:
        # Epoch is an authorization/deletion fence. Do not continue without it,
        # but keep state-store corruption or temporary failure on a stable API.
        return _epoch_unavailable_error(exc, scope.storage_id)
    if ha_coordinator is not None:
        retrieval_scope = ha_retrieval_scope(
            retrieval_scope,
            include_shared_auxiliary=bool(
                getattr(request.app.state, "ha_auxiliary_retrieval_enabled", False)
            ),
        )

    try:
        # 用 app 级有界线程池 offload 同步图：不阻塞事件循环、不无界起线程、不走 anyio。
        if ha_coordinator is not None:
            authority_guard = ha_chat_authority_guard(request, scope, expected_epoch)
            result = await run_sync(
                request.app.state.offload_executor,
                ha_coordinator.run,
                tenant_id=scope.tenant_id,
                storage_id=scope.storage_id,
                expected_epoch=expected_epoch,
                session_scope_id=session_scope_id,
                session_id=external_chat_session_id,
                query=request_body.query,
                authority_guard=authority_guard,
                runner=lambda history: run_with_optional_session(
                    runner,
                    request_body.doc_id,
                    request_body.query,
                    request_body.is_local,
                    history,
                    request_body.forced_task,
                    internal_session_id(request, external_chat_session_id),
                    retrieval_scope,
                    expected_epoch,
                ),
            )
        else:
            chat_history = await run_sync(
                request.app.state.offload_executor,
                session_store.get_history,
                session_scope_id,
                external_chat_session_id,
                request_body.query,
            )
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
                expected_epoch,
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
    except (
        SessionBusy,
        StaleSessionLease,
        HAChatAuthorityChanged,
        IndexReplicaError,
        StaleIndexFence,
    ) as exc:
        return _ha_chat_error(exc)

    # 记忆走门控后的 chat_messages；展示存「用户问题 + 实际答案」，切对话时能看到内容。
    if ha_coordinator is None:
        await run_sync(
            request.app.state.offload_executor,
            session_store.record,
            session_scope_id,
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
    record_claim_verification_observation(request, result, kb_id=request_body.doc_id)
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
    sessions = await run_sync(
        request.app.state.offload_executor,
        request.app.state.session_store.list_sessions,
        session_store_doc_id(request, scope.storage_id),
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
    messages = []
    if scope is not None:
        messages = await run_sync(
            request.app.state.offload_executor,
            request.app.state.session_store.get_display,
            session_store_doc_id(request, scope.storage_id),
            session_id,
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
    snapshot = {"short_term": [], "mid_term": {}, "long_term": []}
    if scope is not None:
        snapshot = await run_sync(
            request.app.state.offload_executor,
            request.app.state.session_store.get_memory_snapshot,
            session_store_doc_id(request, scope.storage_id),
            session_id,
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
        session_scope_id = session_store_doc_id(request, scope.storage_id)
        if getattr(request.app.state, "ha_chat_coordinator", None) is not None:
            try:
                expected_epoch = capture_ha_chat_epoch(
                    request.app.state.kb_registry, scope.storage_id
                )
                authority_guard = ha_authority_guard(
                    request,
                    scope,
                    expected_epoch,
                    permission=Permission.DELETE,
                )
                authority_guard()
                await run_sync(
                    request.app.state.offload_executor,
                    request.app.state.session_store.clear_long_term,
                    session_scope_id,
                    authority=getattr(authority_guard, "evidence", None),
                )
            except (HAChatAuthorityChanged, StaleSessionLease) as exc:
                return _ha_chat_error(exc)
        else:
            await run_sync(
                request.app.state.offload_executor,
                request.app.state.session_store.clear_long_term,
                session_scope_id,
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
    session_scope_id = session_store_doc_id(request, scope.storage_id)
    if getattr(request.app.state, "ha_chat_coordinator", None) is not None:
        try:
            expected_epoch = capture_ha_chat_epoch(
                request.app.state.kb_registry, scope.storage_id
            )
            authority_guard = ha_authority_guard(
                request,
                scope,
                expected_epoch,
                permission=Permission.DELETE,
            )
            authority_guard()
            await run_sync(
                request.app.state.offload_executor,
                request.app.state.session_store.clear,
                session_scope_id,
                session_id,
                authority=getattr(authority_guard, "evidence", None),
            )
        except (HAChatAuthorityChanged, StaleSessionLease) as exc:
            return _ha_chat_error(exc)
    else:
        await run_sync(
            request.app.state.offload_executor,
            request.app.state.session_store.clear,
            session_scope_id,
            session_id,
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
_STREAM_WORKER_STARTED = object()
_STREAM_QUEUE_WATCHDOG_SECONDS = 0.05
_STREAM_WORKER_START_TIMEOUT_SECONDS = 1.0


# 封装SSE 帧。
def _sse_frame(event_name: str, data: dict) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stream_text_chunks(value: str, max_chars: int = 12) -> list[str]:
    """Split finalized public text into paint-sized Unicode chunks.

    Audited tasks cannot expose model tokens before citation finalization. Once the
    public answer is safe to release, chunking it here preserves that boundary while
    still giving clients a genuine incremental SSE delivery contract.
    """

    characters = list(value)
    return [
        "".join(characters[index : index + max_chars])
        for index in range(0, len(characters), max_chars)
    ]


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
        content = str(event.payload.get("content", "") or "")
        if not content:
            return None
        return "".join(
            _sse_frame("token", {"content": chunk})
            for chunk in _stream_text_chunks(content)
        )
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
        error_class = str(event.payload.get("error_class") or "")
        if error_class in {
            "HAChatAuthorityChanged",
            "SessionBusy",
            "StaleSessionLease",
        }:
            error_code = ErrorCode.CHAT_SESSION_CONFLICT
        elif error_class in {"IndexReplicaError", "StaleIndexFence"}:
            error_code = ErrorCode.MODEL_UNAVAILABLE
        else:
            error_code = classify_error_code(
                event.payload.get("stage", ""),
                error_class,
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
    # SSE 流式问答：每次只在线程池推进一个同步事件。这样客户端背压会
    # 自然传递到 provider，并且断连后不会留下已排队但尚未授权的帧。
    scope = resolve_kb_scope(request, request_body.doc_id, allow_legacy_default=True)
    if scope is None:
        error = build_error_response(ErrorCode.KB_NOT_FOUND, "知识库不存在")
        return JSONResponse(status_code=404, content=error.model_dump())
    external_doc_id = request_body.doc_id
    external_chat_session_id = request_body.session_id
    retrieval_scope = retrieval_scope_for_request(request, scope)
    request_body = request_body.model_copy(update={"doc_id": scope.storage_id})
    stream_runner = getattr(request.app.state, "chat_stream_runner", run_chat)
    session_store = request.app.state.session_store
    doc_id = request_body.doc_id
    session_id = external_chat_session_id
    session_scope_id = session_store_doc_id(request, doc_id)
    ha_coordinator = getattr(request.app.state, "ha_chat_coordinator", None)
    if ha_coordinator is not None:
        retrieval_scope = ha_retrieval_scope(
            retrieval_scope,
            include_shared_auxiliary=bool(
                getattr(request.app.state, "ha_auxiliary_retrieval_enabled", False)
            ),
        )
    try:
        expected_epoch = (
            capture_ha_chat_epoch(request.app.state.kb_registry, scope.storage_id)
            if ha_coordinator is not None
            else capture_kb_epoch(scope.storage_id)
        )
    except Exception as exc:
        return _epoch_unavailable_error(exc, scope.storage_id)
    try:
        authority_guard = (
            ha_chat_authority_guard(request, scope, expected_epoch)
            if ha_coordinator is not None
            else None
        )
    except (HAChatAuthorityChanged, StaleSessionLease) as exc:
        return _ha_chat_error(exc)
    chat_history = []
    if ha_coordinator is None:
        chat_history = await run_sync(
            request.app.state.offload_executor,
            session_store.get_history,
            session_scope_id,
            session_id,
            request_body.query,
        )

    idle_timeout_seconds = get_settings().cogdoc_chat_stream_idle_timeout_seconds
    stop_event = threading.Event()

    def run_stream(history):
        return run_with_optional_session(
            stream_runner,
            doc_id,
            request_body.query,
            request_body.is_local,
            history,
            request_body.forced_task,
            internal_session_id(request, session_id),
            retrieval_scope,
            expected_epoch,
        )

    events = iter(
        ha_coordinator.stream(
            tenant_id=scope.tenant_id,
            storage_id=scope.storage_id,
            expected_epoch=expected_epoch,
            session_scope_id=session_scope_id,
            session_id=session_id,
            query=request_body.query,
            authority_guard=authority_guard,
            runner=run_stream,
            stop_requested=stop_event.is_set,
        )
        if ha_coordinator is not None
        else run_stream(chat_history)
    )
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[object, threading.Event]] = asyncio.Queue(maxsize=1)

    def emit(item: object) -> threading.Event | None:
        acknowledgement = threading.Event()
        put = queue.put((item, acknowledgement))
        try:
            pending = asyncio.run_coroutine_threadsafe(put, loop)
        except RuntimeError:
            # The request loop may close while an abandoned provider call is
            # returning.  Explicitly close the unscheduled coroutine so the
            # process does not leak a warning or retain the frame payload.
            put.close()
            return None
        while not stop_event.is_set():
            try:
                pending.result(timeout=_STREAM_QUEUE_WATCHDOG_SECONDS)
                return acknowledgement
            except FutureTimeoutError:
                continue
        pending.cancel()
        return None

    def produce() -> None:
        """Own and advance the synchronous generator on exactly one thread."""

        try:
            started = emit(_STREAM_WORKER_STARTED)
            if started is None:
                return
            while not started.wait(_STREAM_QUEUE_WATCHDOG_SECONDS):
                if stop_event.is_set():
                    return
            if stop_event.is_set():
                return
            for event in events:
                acknowledgement = emit(event)
                if acknowledgement is None:
                    return
                while not acknowledgement.wait(_STREAM_QUEUE_WATCHDOG_SECONDS):
                    if stop_event.is_set():
                        return
                if stop_event.is_set():
                    return
        except Exception as exc:
            if not stop_event.is_set():
                acknowledgement = emit(
                    ChatEvent(
                        "error",
                        {
                            "error_class": type(exc).__name__,
                            "message": str(exc),
                            "stage": "runtime",
                        },
                    )
                )
                if acknowledgement is not None:
                    while not acknowledgement.wait(_STREAM_QUEUE_WATCHDOG_SECONDS):
                        if stop_event.is_set():
                            return
        finally:
            close = getattr(events, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            if not stop_event.is_set():
                emit(_STREAM_DONE)

    try:
        producer_future = request.app.state.chat_stream_executor.submit(produce)
    except RuntimeError:
        error = build_error_response(
            ErrorCode.MODEL_UNAVAILABLE,
            "服务正在关闭，请重试",
        )
        return JSONResponse(status_code=503, content=error.model_dump())

    # 转换来源。
    async def event_source():
        final_result: ChatResult | None = None
        recorded = False
        worker_started = False
        current_acknowledgement: threading.Event | None = None

        # 记录最终结果。
        async def record_final(result: ChatResult) -> None:
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
            if ha_coordinator is None:
                await run_sync(
                    request.app.state.offload_executor,
                    session_store.record,
                    session_scope_id,
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
                timeout_seconds = (
                    max(
                        idle_timeout_seconds,
                        _STREAM_QUEUE_WATCHDOG_SECONDS * 2,
                    )
                    if worker_started
                    else max(
                        idle_timeout_seconds,
                        _STREAM_WORKER_START_TIMEOUT_SECONDS,
                    )
                )
                deadline = loop.time() + timeout_seconds
                pending_get = asyncio.create_task(queue.get())
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        pending_get.cancel()
                        event = None
                        break
                    try:
                        event, current_acknowledgement = await asyncio.wait_for(
                            asyncio.shield(pending_get),
                            timeout=min(_STREAM_QUEUE_WATCHDOG_SECONDS, remaining),
                        )
                        break
                    except TimeoutError:
                        continue
                if event is None:
                    stop_event.set()
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
                if event is _STREAM_WORKER_STARTED:
                    worker_started = True
                    current_acknowledgement.set()
                    current_acknowledgement = None
                    continue
                if event is _STREAM_DONE:
                    current_acknowledgement.set()
                    current_acknowledgement = None
                    break
                if not isinstance(event, ChatEvent):
                    raise RuntimeError("chat stream produced an invalid event")
                if authority_guard is not None:
                    try:
                        await run_sync(
                            request.app.state.offload_executor,
                            authority_guard,
                        )
                    except Exception as exc:
                        stop_event.set()
                        failure_event = ChatEvent(
                            "error",
                            {
                                "error_class": type(exc).__name__,
                                "message": str(exc),
                                "stage": "authorization",
                            },
                        )
                        yield _event_to_frame(
                            failure_event,
                            doc_id=external_doc_id,
                            session_id=session_id,
                        )
                        break
                if event.type == "final":
                    final_result = event.payload["result"]
                    await record_final(final_result)
                frame = _event_to_frame(
                    event, doc_id=external_doc_id, session_id=session_id
                )
                if frame is not None:
                    yield frame
                current_acknowledgement.set()
                current_acknowledgement = None
            # 兜底：理论上 final 事件已即时记录；保留防止未来事件处理顺序变化。
            if final_result is not None:
                await record_final(final_result)
        finally:
            stop_event.set()
            if current_acknowledgement is not None:
                current_acknowledgement.set()
            if not producer_future.done():
                producer_future.cancel()
            # Responsive generators close on their owning thread before the
            # request loop disappears.  A provider still blocked in a bounded
            # external call is left to the dedicated stream executor.
            with suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(
                    asyncio.shield(asyncio.wrap_future(producer_future)),
                    timeout=_STREAM_QUEUE_WATCHDOG_SECONDS * 2,
                )

    return StreamingResponse(event_source(), media_type="text/event-stream")
