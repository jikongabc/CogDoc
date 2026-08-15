import json

import pytest

from scripts.prepare_eval_index import (
    MARKER_NAME,
    load_expected_sources,
    portable_project_path,
    resolve_corpus,
    sync_managed_corpus,
)


def test_portable_project_path_relativizes_only_repository_paths(tmp_path):
    assert portable_project_path("artifacts/reliability/eval-data") == (
        "artifacts/reliability/eval-data"
    )
    assert portable_project_path(tmp_path) == str(tmp_path.resolve())


def test_load_expected_sources_and_resolve_corpus(tmp_path):
    eval_set = tmp_path / "eval.jsonl"
    eval_set.write_text(
        json.dumps({"query": "q", "expected_sources": ["a.pdf", "b.pdf"]})
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "a.pdf").write_bytes(b"a")
    (tmp_path / "b.pdf").write_bytes(b"b")

    expected = load_expected_sources(eval_set)
    corpus = resolve_corpus(tmp_path, expected)

    assert expected == {"a.pdf", "b.pdf"}
    assert [path.name for path in corpus] == ["a.pdf", "b.pdf"]


def test_load_expected_sources_rejects_path_traversal(tmp_path):
    eval_set = tmp_path / "eval.jsonl"
    eval_set.write_text(
        json.dumps({"expected_sources": ["../secret.pdf"]}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsafe"):
        load_expected_sources(eval_set)


def test_resolve_corpus_rejects_missing_pdf(tmp_path):
    with pytest.raises(ValueError, match="missing required PDFs"):
        resolve_corpus(tmp_path, {"missing.pdf"})


def test_sync_managed_corpus_is_repeatable_and_removes_stale_pdf(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "a.pdf").write_bytes(b"version-one")

    first = sync_managed_corpus(
        [source / "a.pdf"], destination, "eval-kb", newly_created=True
    )
    (destination / "stale.pdf").write_bytes(b"stale")
    (source / "a.pdf").write_bytes(b"version-two")
    second = sync_managed_corpus(
        [source / "a.pdf"], destination, "eval-kb", newly_created=False
    )

    assert first["a.pdf"] != second["a.pdf"]
    assert (destination / "a.pdf").read_bytes() == b"version-two"
    assert not (destination / "stale.pdf").exists()
    assert (destination / MARKER_NAME).is_file()


def test_sync_managed_corpus_refuses_unmanaged_existing_target(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "a.pdf").write_bytes(b"a")
    (destination / "existing.pdf").write_bytes(b"existing")

    with pytest.raises(RuntimeError, match="refusing to replace"):
        sync_managed_corpus(
            [source / "a.pdf"], destination, "eval-kb", newly_created=False
        )
