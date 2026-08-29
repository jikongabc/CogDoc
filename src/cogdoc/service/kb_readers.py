from contextlib import contextmanager
from threading import Condition
import time


_condition = Condition()
_readers: dict[str, int] = {}
_draining: set[str] = set()


class KBReadUnavailable(RuntimeError):
    """Raised when a destructive KB operation has closed reader admission."""


# 完成 知识库读取读租约 处理。
@contextmanager
def kb_read_lease(kb_id: str):
    with _condition:
        if kb_id in _draining:
            raise KBReadUnavailable("knowledge base readers are draining")
        _readers[kb_id] = _readers.get(kb_id, 0) + 1
    try:
        yield
    finally:
        with _condition:
            remaining = _readers.get(kb_id, 1) - 1
            if remaining <= 0:
                _readers.pop(kb_id, None)
            else:
                _readers[kb_id] = remaining
            _condition.notify_all()


# 判断是否存在 readers。
def has_readers(kb_id: str) -> bool:
    with _condition:
        return _readers.get(kb_id, 0) > 0


def wait_for_no_readers(kb_id: str, timeout_seconds: float = 30.0) -> bool:
    """Wait until every in-process KB read lease has drained."""

    timeout = max(float(timeout_seconds), 0.0)
    deadline = time.monotonic() + timeout
    with _condition:
        while _readers.get(kb_id, 0) > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _condition.wait(remaining)
        return True


@contextmanager
def drain_kb_readers(kb_id: str, timeout_seconds: float = 30.0):
    """Exclusively drain a KB and reject readers until cleanup completes.

    Closing admission and observing the active-reader count happen under the
    same condition lock. This prevents a new reader from entering between a
    successful drain check and the destructive operation.
    """

    timeout = max(float(timeout_seconds), 0.0)
    deadline = time.monotonic() + timeout
    owns_drain = False
    with _condition:
        while kb_id in _draining:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("knowledge base reader drain is already active")
            _condition.wait(remaining)
        _draining.add(kb_id)
        owns_drain = True
        try:
            while _readers.get(kb_id, 0) > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "knowledge base readers did not drain before cleanup"
                    )
                _condition.wait(remaining)
        except BaseException:
            _draining.discard(kb_id)
            owns_drain = False
            _condition.notify_all()
            raise

    try:
        yield
    finally:
        if owns_drain:
            with _condition:
                _draining.discard(kb_id)
                _condition.notify_all()
