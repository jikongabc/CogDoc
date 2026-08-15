#!/usr/bin/env python3
"""Build a deterministic, isolated knowledge base for retrieval evaluation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Iterable

from cogdoc.api.ingest import KnowledgeBaseRegistry
from cogdoc.config.settings import get_settings
from cogdoc.service.ingest_service import (
    build_kb_index_transactional,
    cancel_all_timers,
)
from cogdoc.service.process_lock import (
    acquire_single_instance_lock,
    release_single_instance_lock,
)


SCHEMA_VERSION = 1
MARKER_NAME = ".cogdoc-reliability-corpus.json"
KB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,55}$")
ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_expected_sources(eval_set: Path) -> set[str]:
    expected: set[str] = set()
    with eval_set.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}") from exc
            for value in item.get("expected_sources", []):
                name = str(value).strip()
                if not name:
                    continue
                if Path(name).name != name or not name.lower().endswith(".pdf"):
                    raise ValueError(
                        f"unsafe or unsupported expected source at line {line_number}"
                    )
                expected.add(name)
    if not expected:
        raise ValueError("evaluation set does not declare any expected PDF sources")
    return expected


def resolve_corpus(source_dir: Path, expected: Iterable[str]) -> list[Path]:
    if not source_dir.is_dir():
        raise ValueError(f"corpus directory does not exist: {source_dir}")
    paths = [source_dir / name for name in sorted(set(expected))]
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise ValueError("corpus is missing required PDFs: " + ", ".join(missing))
    return paths


def sync_managed_corpus(
    source_paths: list[Path], destination: Path, kb_id: str, *, newly_created: bool
) -> dict[str, str]:
    destination.mkdir(parents=True, exist_ok=True)
    marker = destination / MARKER_NAME
    existing_entries = [path for path in destination.iterdir() if path.name != MARKER_NAME]
    if existing_entries and not newly_created and not marker.is_file():
        raise RuntimeError(
            "refusing to replace an existing knowledge-base corpus not created by the reliability gate"
        )

    expected_names = {path.name for path in source_paths}
    if marker.is_file():
        try:
            marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("reliability corpus marker is invalid") from exc
        if marker_payload.get("kb_id") != kb_id:
            raise RuntimeError("reliability corpus marker belongs to another knowledge base")

    for current in destination.glob("*.pdf"):
        if current.name not in expected_names:
            current.unlink()

    fingerprints: dict[str, str] = {}
    for source in source_paths:
        target = destination / source.name
        temporary = target.with_name(target.name + ".tmp")
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
        fingerprints[source.name] = _sha256(target)

    marker_payload = {
        "schema_version": SCHEMA_VERSION,
        "kb_id": kb_id,
        "sources": fingerprints,
    }
    temporary_marker = marker.with_name(marker.name + ".tmp")
    temporary_marker.write_text(
        json.dumps(marker_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary_marker, marker)
    return fingerprints


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def portable_project_path(value: str | Path) -> str:
    """Keep repository-local metadata portable while preserving external paths."""

    path = Path(value).expanduser()
    resolved = path.resolve() if path.is_absolute() else (ROOT / path).resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-id", default="arch_blueprint_2026")
    parser.add_argument("--source-dir", type=Path, default=Path("your_documents"))
    parser.add_argument(
        "--eval-set", type=Path, default=Path("eval/retrieval_eval.jsonl")
    )
    parser.add_argument(
        "--json", type=Path, default=Path("artifacts/reliability/eval-index.json")
    )
    args = parser.parse_args()

    if not KB_ID_PATTERN.fullmatch(args.kb_id):
        parser.error("kb-id must be a safe 1-56 character identifier")

    try:
        expected = load_expected_sources(args.eval_set)
        corpus = resolve_corpus(args.source_dir, expected)
        lock_handle = acquire_single_instance_lock()
        if lock_handle is None:
            raise RuntimeError("could not acquire the evaluation data-directory lock")
        try:
            registry = KnowledgeBaseRegistry()
            newly_created = not registry.exists(args.kb_id)
            if newly_created:
                registry.create(args.kb_id)
            fingerprints = sync_managed_corpus(
                corpus,
                Path(registry.source_dir(args.kb_id)),
                args.kb_id,
                newly_created=newly_created,
            )
            result = build_kb_index_transactional(
                args.kb_id, registry.source_dir(args.kb_id)
            )
        finally:
            if cancel_all_timers():
                release_single_instance_lock(lock_handle)

        indexed_names = {document.name for document in result.documents}
        missing_from_index = sorted(expected - indexed_names)
        if result.document_count <= 0 or result.chunk_count <= 0 or missing_from_index:
            detail = ", ".join(missing_from_index) or "empty index"
            raise RuntimeError(f"evaluation index is incomplete: {detail}")

        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": "ready",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "kb_id": args.kb_id,
            "data_dir": portable_project_path(get_settings().cogdoc_data_dir),
            "document_count": result.document_count,
            "chunk_count": result.chunk_count,
            "sources": fingerprints,
        }
        _write_json(args.json, payload)
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"evaluation index preparation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
