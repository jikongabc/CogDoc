from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from typing import Any


_SEGMENT_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class ChromaSegmentCleanupError(RuntimeError):
    pass


def _database_path(persist_directory: str | os.PathLike[str]) -> Path:
    return Path(persist_directory) / "chroma.sqlite3"


def _readonly_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def collection_segment_ids(
    persist_directory: str | os.PathLike[str], collection_name: str
) -> tuple[str, ...]:
    """Return persistent segment directories owned by one live collection."""

    database = _database_path(persist_directory)
    if not database.is_file():
        return ()
    try:
        with closing(_readonly_connection(database)) as connection:
            rows = connection.execute(
                "SELECT s.id FROM segments s JOIN collections c "
                "ON c.id=s.collection WHERE c.name=?",
                (collection_name,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise ChromaSegmentCleanupError(
            "Chroma segment metadata could not be read"
        ) from exc
    return tuple(
        sorted(
            {
                str(row[0])
                for row in rows
                if row and _SEGMENT_ID.fullmatch(str(row[0]))
            }
        )
    )


def remove_segment_directories(
    persist_directory: str | os.PathLike[str], segment_ids: Iterable[str]
) -> int:
    """Remove only validated, direct children of the Chroma persistence root."""

    root = Path(persist_directory)
    removed = 0
    for raw_segment_id in sorted(set(segment_ids)):
        segment_id = str(raw_segment_id)
        if not _SEGMENT_ID.fullmatch(segment_id):
            raise ChromaSegmentCleanupError("invalid Chroma segment id")
        target = root / segment_id
        if target.is_symlink():
            raise ChromaSegmentCleanupError("refusing to remove a segment symlink")
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ChromaSegmentCleanupError(
                f"Chroma segment directory could not be removed: {segment_id}"
            ) from exc
        removed += 1
    return removed


def delete_collection_and_segments(
    client: Any,
    persist_directory: str | os.PathLike[str],
    collection_name: str,
    *,
    retained_segment_ids: Iterable[str] = (),
) -> int:
    """Delete a collection and its now-unreferenced persistent segment files."""

    from chromadb.errors import NotFoundError

    segment_ids = set(retained_segment_ids)
    segment_ids.update(collection_segment_ids(persist_directory, collection_name))
    try:
        client.delete_collection(collection_name)
    except (ValueError, NotFoundError):
        pass
    return remove_segment_directories(persist_directory, segment_ids)


def sweep_orphan_segment_directories(
    persist_directory: str | os.PathLike[str],
) -> dict[str, int]:
    """Reclaim old UUID segment directories absent from Chroma's live catalog.

    This is intended for startup, after CogDoc owns its single-instance lock and
    before mutation workers are admitted. It must not run concurrently with a
    Chroma collection build.
    """

    root = Path(persist_directory)
    database = _database_path(root)
    if not root.is_dir() or not database.is_file():
        return {"scanned": 0, "removed": 0, "bytes_reclaimed": 0}
    try:
        with closing(_readonly_connection(database)) as connection:
            live = {
                str(row[0])
                for row in connection.execute("SELECT id FROM segments").fetchall()
                if row and _SEGMENT_ID.fullmatch(str(row[0]))
            }
    except sqlite3.Error as exc:
        raise ChromaSegmentCleanupError(
            "Chroma segment metadata could not be read"
        ) from exc

    candidates = [
        entry
        for entry in root.iterdir()
        if entry.name not in live
        and _SEGMENT_ID.fullmatch(entry.name)
        and entry.is_dir()
        and not entry.is_symlink()
    ]
    reclaimed = sum(_directory_size(entry) for entry in candidates)
    removed = remove_segment_directories(root, (entry.name for entry in candidates))
    return {
        "scanned": len(candidates),
        "removed": removed,
        "bytes_reclaimed": reclaimed,
    }


def _directory_size(path: Path) -> int:
    total = 0
    for root, _directories, files in os.walk(path):
        for filename in files:
            try:
                total += (Path(root) / filename).stat().st_size
            except FileNotFoundError:
                continue
    return total
