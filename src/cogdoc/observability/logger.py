import json
import logging
import re
import sys
import traceback
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from cogdoc.config.settings import Settings, get_settings


_BASE_RECORD_KEYS = set(
    logging.LogRecord(
        name="",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__
)
_RESERVED_EXTRA_KEYS = _BASE_RECORD_KEYS | {"message", "asctime"}
_HANDLER_MARKER = "_cogdoc_handler"
_ACCESS_FILTER_MARKER = "_cogdoc_sensitive_query_filter"
_CONFIGURED_SIGNATURE = None
_OAUTH_CALLBACK_TARGET = re.compile(
    r"(/v1/auth/(?:connector-oauth/callback/[A-Za-z0-9_-]+|oidc/callback))"
    r"\?[^\s\"]*"
)


class OAuthCallbackAccessLogFilter(logging.Filter):
    """Strip OAuth callback query secrets from Uvicorn access records."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            target = str(args[2])
            redacted = _OAUTH_CALLBACK_TARGET.sub(r"\1", target)
            if redacted != target:
                updated = list(args)
                updated[2] = redacted
                record.args = tuple(updated)
        elif isinstance(record.msg, str):
            record.msg = _OAUTH_CALLBACK_TARGET.sub(r"\1", record.msg)
        return True


def _install_sensitive_access_log_filter() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if any(
        getattr(active_filter, _ACCESS_FILTER_MARKER, False)
        for active_filter in access_logger.filters
    ):
        return
    active_filter = OAuthCallbackAccessLogFilter()
    setattr(active_filter, _ACCESS_FILTER_MARKER, True)
    access_logger.addFilter(active_filter)


# 定义 JsonLogFormatter 数据结构。
class JsonLogFormatter(logging.Formatter):
    # 格式化。
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        if record.exc_info:
            payload["error_trace"] = "".join(
                traceback.format_exception(*record.exc_info)
            )
        for key, value in record.__dict__.items():
            if key in _BASE_RECORD_KEYS or key.startswith("_"):
                continue
            payload[key] = _json_safe(value)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# 解析safe。
def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


# 新建traceid。
def new_trace_id() -> str:
    return uuid4().hex


# 获取 logger。
def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"cogdoc.{name}")


# 配置logging。
def configure_logging(settings: Settings | None = None) -> None:
    global _CONFIGURED_SIGNATURE
    # Uvicorn formats the raw request target outside the ASGI application, so
    # route middleware cannot reliably hide OAuth state/code query values.
    _install_sensitive_access_log_filter()
    settings = settings or get_settings()
    logger = logging.getLogger("cogdoc")
    signature = (
        settings.cogdoc_log_level.upper(),
        settings.cogdoc_log_file,
        settings.cogdoc_log_to_console,
    )
    if _CONFIGURED_SIGNATURE == signature and any(
        getattr(handler, _HANDLER_MARKER, False) for handler in logger.handlers
    ):
        return

    logger.setLevel(settings.cogdoc_log_level.upper())
    logger.propagate = False

    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()

    formatter = JsonLogFormatter()
    if settings.cogdoc_log_file:
        log_path = Path(settings.cogdoc_log_file)
        if not log_path.is_absolute():
            log_path = settings.project_root / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(settings.cogdoc_log_level.upper())
        setattr(file_handler, _HANDLER_MARKER, True)
        logger.addHandler(file_handler)

    if settings.cogdoc_log_to_console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(settings.cogdoc_log_level.upper())
        setattr(console_handler, _HANDLER_MARKER, True)
        logger.addHandler(console_handler)
    _CONFIGURED_SIGNATURE = signature


# 完成 log事件 处理。
def log_event(
    logger_name: str,
    event: str,
    state: Mapping[str, Any] | None = None,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    state = state or {}
    extra = {
        "request_id": state.get("request_id"),
        "trace_id": state.get("trace_id"),
        **fields,
    }
    clean_extra = {}
    for key, value in extra.items():
        if value is None:
            continue
        safe_key = f"field_{key}" if key in _RESERVED_EXTRA_KEYS else key
        clean_extra[safe_key] = _json_safe(value)
    get_logger(logger_name).log(level, event, extra=clean_extra)
