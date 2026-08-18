from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from cogdoc.service.chat_service import (
        ChatEvent,
        ChatResult,
        run_chat,
        run_chat_sync,
    )

__all__ = ["ChatEvent", "ChatResult", "run_chat", "run_chat_sync"]


def __getattr__(name: str) -> Any:
    """Load public chat helpers without eagerly importing the service graph."""

    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("cogdoc.service.chat_service"), name)
    globals()[name] = value
    return value
