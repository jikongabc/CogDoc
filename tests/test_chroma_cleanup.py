import sqlite3
from pathlib import Path

from cogdoc.service.chroma_cleanup import (
    delete_collection_and_segments,
    sweep_orphan_segment_directories,
)


LIVE_SEGMENT = "11111111-1111-1111-1111-111111111111"
OTHER_SEGMENT = "22222222-2222-2222-2222-222222222222"
ORPHAN_SEGMENT = "33333333-3333-3333-3333-333333333333"


def _database(root: Path) -> Path:
    path = root / "chroma.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE collections(id TEXT PRIMARY KEY,name TEXT)")
        connection.execute("CREATE TABLE segments(id TEXT PRIMARY KEY,collection TEXT)")
        connection.executemany(
            "INSERT INTO collections(id,name) VALUES(?,?)",
            (("collection-a", "col-a"), ("collection-b", "col-b")),
        )
        connection.executemany(
            "INSERT INTO segments(id,collection) VALUES(?,?)",
            (
                (LIVE_SEGMENT, "collection-a"),
                (OTHER_SEGMENT, "collection-b"),
            ),
        )
    return path


class _Client:
    def __init__(self, database: Path):
        self.database = database

    def delete_collection(self, name: str) -> None:
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT id FROM collections WHERE name=?", (name,)
            ).fetchone()
            if row is None:
                raise ValueError(name)
            connection.execute("DELETE FROM segments WHERE collection=?", (row[0],))
            connection.execute("DELETE FROM collections WHERE id=?", (row[0],))


def test_delete_collection_removes_only_its_segment_directories(tmp_path):
    database = _database(tmp_path)
    live = tmp_path / LIVE_SEGMENT
    other = tmp_path / OTHER_SEGMENT
    live.mkdir()
    other.mkdir()
    (live / "data.bin").write_bytes(b"old")
    (other / "data.bin").write_bytes(b"current")

    removed = delete_collection_and_segments(_Client(database), tmp_path, "col-a")

    assert removed == 1
    assert not live.exists()
    assert other.is_dir()


def test_startup_sweep_removes_only_unreferenced_uuid_directories(tmp_path):
    _database(tmp_path)
    live = tmp_path / LIVE_SEGMENT
    other = tmp_path / OTHER_SEGMENT
    orphan = tmp_path / ORPHAN_SEGMENT
    unrelated = tmp_path / "do-not-delete"
    for directory in (live, other, orphan, unrelated):
        directory.mkdir()
    (orphan / "data.bin").write_bytes(b"orphan-data")

    result = sweep_orphan_segment_directories(tmp_path)

    assert result == {
        "scanned": 1,
        "removed": 1,
        "bytes_reclaimed": len(b"orphan-data"),
    }
    assert live.is_dir()
    assert other.is_dir()
    assert unrelated.is_dir()
    assert not orphan.exists()


class _MissingClient:
    def delete_collection(self, name: str) -> None:
        from chromadb.errors import NotFoundError

        raise NotFoundError(f"Collection {name} does not exist")


def test_delete_missing_collection_still_removes_retained_segments(tmp_path):
    segment = tmp_path / LIVE_SEGMENT
    segment.mkdir()
    (segment / "data.bin").write_bytes(b"retained")

    removed = delete_collection_and_segments(
        _MissingClient(),
        tmp_path,
        "missing",
        retained_segment_ids=(LIVE_SEGMENT,),
    )

    assert removed == 1
    assert not segment.exists()
