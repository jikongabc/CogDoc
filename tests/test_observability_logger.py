import json
import io
import logging
import pytest
from cogdoc.config.settings import Settings
import cogdoc.observability.logger as logger_module
from cogdoc.observability.logger import configure_logging, log_event, new_trace_id


# 重置 CogDoc 日志配置。
@pytest.fixture(autouse=True)
def reset_cogdoc_logging():
    yield
    logger = logging.getLogger("cogdoc")
    for handler in list(logger.handlers):
        if getattr(handler, logger_module._HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()
    logger_module._CONFIGURED_SIGNATURE = None
    access_logger = logging.getLogger("uvicorn.access")
    for active_filter in list(access_logger.filters):
        if getattr(active_filter, logger_module._ACCESS_FILTER_MARKER, False):
            access_logger.removeFilter(active_filter)


# 验证 json logging writes trace fields 场景。
def test_json_logging_writes_trace_fields(tmp_path):
    log_path = tmp_path / "cogdoc.jsonl"
    settings = Settings(cogdoc_log_file=str(log_path), cogdoc_log_to_console=False)
    configure_logging(settings)

    state = {"request_id": "req-1", "trace_id": "trace-1"}
    log_event("test", "demo_event", state, node_name="router", count=2)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "demo_event"
    assert payload["request_id"] == "req-1"
    assert payload["trace_id"] == "trace-1"
    assert payload["node_name"] == "router"
    assert payload["count"] == 2


# 验证 trace id is unique hex 场景。
def test_trace_id_is_unique_hex():
    first = new_trace_id()
    second = new_trace_id()

    assert first != second
    assert len(first) == 32
    int(first, 16)


# 验证 configure logging is idempotent for same settings 场景。
def test_configure_logging_is_idempotent_for_same_settings(tmp_path):
    log_path = tmp_path / "cogdoc.jsonl"
    settings = Settings(cogdoc_log_file=str(log_path), cogdoc_log_to_console=False)
    configure_logging(settings)
    logger = logging.getLogger("cogdoc")
    first_handlers = list(logger.handlers)

    configure_logging(settings)

    assert logger.handlers == first_handlers


@pytest.mark.parametrize(
    "target",
    [
        "/v1/auth/connector-oauth/callback/notion",
        "/v1/auth/oidc/callback",
    ],
)
def test_configure_logging_redacts_oauth_callback_query_from_uvicorn_access_log(
    tmp_path,
    target,
):
    settings = Settings(
        cogdoc_log_file=str(tmp_path / "cogdoc.jsonl"),
        cogdoc_log_to_console=False,
    )
    configure_logging(settings)
    access_logger = logging.getLogger("uvicorn.access")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    previous_handlers = list(access_logger.handlers)
    previous_level = access_logger.level
    previous_propagate = access_logger.propagate
    access_logger.handlers = [handler]
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = False
    try:
        access_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:1",
            "GET",
            f"{target}?state=state-secret-marker&code=code-secret-marker",
            "1.1",
            200,
        )
    finally:
        access_logger.handlers = previous_handlers
        access_logger.setLevel(previous_level)
        access_logger.propagate = previous_propagate

    output = stream.getvalue()
    assert target in output
    assert "state-secret-marker" not in output
    assert "code-secret-marker" not in output


# 验证 log event prefixes reserved extra keys 场景。
def test_log_event_prefixes_reserved_extra_keys(tmp_path):
    log_path = tmp_path / "cogdoc.jsonl"
    settings = Settings(cogdoc_log_file=str(log_path), cogdoc_log_to_console=False)
    configure_logging(settings)

    log_event(
        "test",
        "reserved_event",
        {"trace_id": "trace-1"},
        name="business-name",
        module="business-module",
        message="business-message",
    )

    payload = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["field_name"] == "business-name"
    assert payload["field_module"] == "business-module"
    assert payload["field_message"] == "business-message"
