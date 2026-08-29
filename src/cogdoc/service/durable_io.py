from __future__ import annotations

import errno
import json
import os
import tempfile
from threading import get_ident
from typing import Any


_UNSUPPORTED_DIRECTORY_FSYNC = {
    errno.EBADF,
    errno.EINVAL,
    getattr(errno, "ENOTSUP", errno.EINVAL),
}


def fsync_directory(directory: str) -> None:
    """Persist directory-entry changes where the platform supports it."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory or ".", flags)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC:
                raise
    finally:
        os.close(descriptor)


def atomic_write_json(
    path: str,
    data: Any,
    *,
    ensure_ascii: bool = False,
    indent: int | None = None,
    sort_keys: bool = False,
) -> None:
    """Atomically replace JSON and fsync both the file and its rename."""

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.{os.getpid()}.{get_ident()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(
                data,
                handle,
                ensure_ascii=ensure_ascii,
                indent=indent,
                sort_keys=sort_keys,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(directory)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def atomic_write_text(path: str, value: str, *, mode: int | None = None) -> None:
    """Crash-durably replace a small UTF-8 control-plane marker."""

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        if mode is not None:
            os.fchmod(descriptor, mode)
        owned_descriptor = descriptor
        descriptor = -1
        with os.fdopen(owned_descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if mode is not None:
            # The destination may have existed with broader permissions. Rename
            # preserves the temporary mode, and chmod makes that invariant
            # explicit even on platforms/filesystems with unusual defaults.
            os.chmod(path, mode)
        fsync_directory(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def atomic_write_bytes(path: str, value: bytes) -> None:
    """Crash-durably replace a binary state file."""

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.{os.getpid()}.{get_ident()}.tmp"
    try:
        with open(temporary, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(directory)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def durable_remove(path: str) -> None:
    """Remove one file and persist the directory entry deletion."""

    os.remove(path)
    fsync_directory(os.path.dirname(path) or ".")
