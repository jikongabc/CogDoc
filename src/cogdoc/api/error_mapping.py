from cogdoc.api.schemas import ErrorCode


# 上游异常类名/文案里的特征词 -> 稳定错误码；覆盖 openai/httpx/标准库常见命名。
_TIMEOUT_TOKENS = ("timeout", "timed out")
_RATE_LIMIT_TOKENS = ("rate limit", "ratelimit", "429", "too many requests")

# 每个错误码对应的 HTTP 状态：让客户端据码决定退避重试还是直接放弃。
_STATUS_BY_CODE = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.TENANT_QUOTA_EXCEEDED: 409,
    ErrorCode.REQUEST_THROTTLED: 429,
    ErrorCode.STREAM_INTERRUPTED: 502,
    ErrorCode.LLM_TIMEOUT: 504,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.MODEL_UNAVAILABLE: 503,
}


# 归类错误code。
def classify_error_code(stage: str, error_class: str, message: str) -> ErrorCode:
    # stream 阶段维持既有契约（流中断即 STREAM_INTERRUPTED，不论底层成因）。
    if stage == "stream":
        return ErrorCode.STREAM_INTERRUPTED
    # runtime/其余阶段细分上游成因：超时与限流值得不同的客户端处置；类名+文案一起匹配：文案用于召回被包装的异常（如 RuntimeError("...timeout...")），代价是文案本地化时有误判风险。
    haystack = f"{error_class} {message}".lower()
    if any(token in haystack for token in _TIMEOUT_TOKENS):
        return ErrorCode.LLM_TIMEOUT
    if any(token in haystack for token in _RATE_LIMIT_TOKENS):
        return ErrorCode.RATE_LIMITED
    return ErrorCode.MODEL_UNAVAILABLE


# 返回状态forcode。
def status_for_code(code: ErrorCode) -> int:
    return _STATUS_BY_CODE.get(code, 503)
