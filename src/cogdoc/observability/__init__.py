from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from cogdoc.observability.logger import (
        configure_logging,
        get_logger,
        log_event,
        new_trace_id,
    )
    from cogdoc.observability.trace import (
        build_trace_payload,
        build_trace_step,
        export_trace,
        monotonic_ms,
        summarize_trace_steps,
    )

__all__ = [
    "build_trace_payload",
    "build_trace_step",
    "configure_logging",
    "export_trace",
    "get_logger",
    "log_event",
    "monotonic_ms",
    "new_trace_id",
    "summarize_trace_steps",
]

_LOGGER_EXPORTS = frozenset(
    {"configure_logging", "get_logger", "log_event", "new_trace_id"}
)


def __getattr__(name: str) -> Any:
    """Keep logger imports independent from the heavier trace dependency graph."""

    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name = (
        "cogdoc.observability.logger"
        if name in _LOGGER_EXPORTS
        else "cogdoc.observability.trace"
    )
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
