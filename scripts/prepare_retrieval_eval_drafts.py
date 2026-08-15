#!/usr/bin/env python3
"""Create provenance-bound pending drafts and a reviewer candidate checklist."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cogdoc.api.retrieval_eval_draft_store import RetrievalEvalDraftStore  # noqa: E402
from cogdoc.config.settings import get_settings  # noqa: E402
from cogdoc.service.index_provenance import current_index_provenance  # noqa: E402
from cogdoc.service.kb_readers import kb_read_lease  # noqa: E402
from cogdoc.service.retrieval_pipeline import (  # noqa: E402
    build_retrieval_queries,
    retrieve_candidate_pool,
)
from cogdoc.service.retriever_factory import RetrieverFactory  # noqa: E402
from cogdoc.state_runtime import default_state_runtime  # noqa: E402
from cogdoc.tools.eval.retrieval_eval_drafts import (  # noqa: E402
    DatasetPartition,
    EvidenceUnitDraft,
    EvidenceUnitTask,
    create_pending_draft,
)


SCHEMA_VERSION = 1


def load_cases(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}") from exc
            if not isinstance(row, dict) or not str(row.get("query") or "").strip():
                raise ValueError(f"evaluation case at line {line_number} needs query")
            rows.append(row)
    return rows


def draft_units(case: Mapping[str, Any]) -> list[EvidenceUnitDraft]:
    query = str(case.get("query") or "").strip()
    no_answer = not list(case.get("expected_sources") or [])
    raw_requirements = case.get("evidence_requirements")
    requirements = raw_requirements if isinstance(raw_requirements, list) else []
    units: list[EvidenceUnitDraft] = []
    for position, raw in enumerate(requirements[:3]):
        requirement = raw if isinstance(raw, Mapping) else {}
        unit_id = str(requirement.get("requirement_id") or f"r{position + 1}").strip()
        label = str(requirement.get("question") or query).strip()
        retrieval_query = str(requirement.get("retrieval_query") or label).strip()
        recovery_query = str(
            requirement.get("recovery_query") or retrieval_query
        ).strip()
        units.append(
            EvidenceUnitDraft(
                unit_id=unit_id,
                task_kind=EvidenceUnitTask.QA_REQUIREMENT,
                label=label,
                retrieval_query=retrieval_query,
                recovery_query=recovery_query,
                expected_status="no_evidence" if no_answer else None,
            )
        )
    if units:
        return units
    expected_sources = [
        str(source).strip()
        for source in list(case.get("expected_sources") or [])
        if str(source).strip()
    ]
    if len(expected_sources) > 1:
        return [
            EvidenceUnitDraft(
                unit_id=f"r{position + 1}",
                task_kind=EvidenceUnitTask.QA_REQUIREMENT,
                label=f"{source}：{query}",
                retrieval_query=f"{Path(source).stem} {query}",
                recovery_query=f"{query} {Path(source).stem}",
                source=source,
            )
            for position, source in enumerate(expected_sources[:3])
        ]
    return [
        EvidenceUnitDraft(
            unit_id="r1",
            task_kind=EvidenceUnitTask.QA_REQUIREMENT,
            label=query,
            retrieval_query=query,
            recovery_query=query,
            expected_status="no_evidence" if no_answer else None,
        )
    ]


def _score_snapshot(retrieval: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "distance",
        "bm25_score",
        "query_fusion_score",
        "query_hit_count",
        "matched_requirement_ids",
        "matched_channels",
        "search_channel",
    )
    return {key: retrieval[key] for key in keys if key in retrieval}


def candidate_checklist(
    docs: Sequence[Mapping[str, Any]],
    *,
    expected_sources: Sequence[str],
    source_versions: Mapping[str, str],
) -> list[dict[str, Any]]:
    expected = {str(source).strip() for source in expected_sources}
    rows: list[dict[str, Any]] = []
    for rank, doc in enumerate(docs, 1):
        meta = doc.get("meta") if isinstance(doc.get("meta"), Mapping) else {}
        retrieval = (
            doc.get("retrieval") if isinstance(doc.get("retrieval"), Mapping) else {}
        )
        source = str(meta.get("source") or "")
        text = " ".join(str(doc.get("text") or "").split())
        rows.append(
            {
                "rank": rank,
                "chunk_id": str(meta.get("chunk_id") or ""),
                "parent_chunk_id": str(meta.get("parent_chunk_id") or ""),
                "source": source,
                "source_sha256": str(
                    meta.get("source_sha256") or source_versions.get(source, "")
                ),
                "page": meta.get("page"),
                "text_preview": text[:500],
                "retrieval": _score_snapshot(retrieval),
                "expected_source_hint": source in expected,
                "review_decision": "",
                "review_span_start": None,
                "review_span_end": None,
            }
        )
    return rows


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--eval-set", type=Path, default=Path("eval/retrieval_eval.jsonl")
    )
    parser.add_argument(
        "--drafts-jsonl",
        type=Path,
        default=Path("artifacts/reliability/retrieval_eval_drafts.jsonl"),
    )
    parser.add_argument(
        "--checklist",
        type=Path,
        default=Path("artifacts/reliability/retrieval_eval_review_checklist.json"),
    )
    parser.add_argument(
        "--planned-eval-jsonl",
        type=Path,
        default=Path("artifacts/reliability/retrieval_eval_pending_plans.jsonl"),
    )
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument(
        "--partition",
        choices=[partition.value for partition in DatasetPartition],
        default=DatasetPartition.RELEASE_GATE.value,
    )
    parser.add_argument("--no-retrieve", action="store_true")
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("top-k must be positive")

    cases = load_cases(args.eval_set)
    settings = get_settings()
    runtime = default_state_runtime()
    store = RetrievalEvalDraftStore(str(args.drafts_jsonl))
    checklist_rows: list[dict[str, Any]] = []
    planned_eval_rows: list[dict[str, Any]] = []
    created = 0
    reused = 0
    try:
        for case in cases:
            kb_id = str(case.get("doc_id") or settings.cogdoc_default_doc_id)
            query = str(case["query"]).strip()
            provenance = current_index_provenance(kb_id)
            source_versions = {
                str(item.get("source") or ""): str(item.get("sha256") or "")
                for item in provenance.get("source_versions", [])
                if isinstance(item, Mapping)
            }
            units = draft_units(case)
            planned_case = copy.deepcopy(case)
            planned_case["evidence_requirements"] = [
                {
                    "requirement_id": unit.unit_id,
                    "question": unit.label,
                    "retrieval_query": unit.retrieval_query,
                    "recovery_query": unit.recovery_query,
                }
                for unit in units
            ]
            planned_case["annotation_status"] = "pending_machine_plan_not_gold"
            planned_eval_rows.append(planned_case)
            draft = create_pending_draft(
                kb_id=kb_id,
                query=query,
                units=units,
                dataset_partition=args.partition,
                no_answer=not list(case.get("expected_sources") or []),
                layer=str(case.get("layer") or ""),
                index_generation=str(provenance.get("index_generation") or ""),
                index_build_version=str(provenance.get("index_build_version") or ""),
                chunk_identity_version=str(
                    provenance.get("chunk_identity_version") or ""
                ),
                source_versions=list(provenance.get("source_versions") or []),
            )
            existed = store.get(draft.draft_id) is not None
            saved = store.ensure(draft)
            created += int(not existed)
            reused += int(existed)

            docs: list[dict[str, Any]] = []
            if not args.no_retrieve:
                requirements = [unit.model_dump(mode="json") for unit in units]
                queries = build_retrieval_queries(
                    query,
                    evidence_requirements=requirements,
                    max_queries=settings.qa_retrieval_max_queries,
                )
                with kb_read_lease(kb_id):
                    engine = RetrieverFactory.get_engine(kb_id)
                    result = retrieve_candidate_pool(
                        engine,
                        runtime.derived_knowledge_retriever,
                        runtime.retrieval_feedback_store,
                        kb_id=kb_id,
                        original_query=query,
                        queries=queries,
                        top_k=args.top_k,
                        rrf_k=float(settings.hybrid_rrf_k),
                        fusion_top_n=args.top_k,
                    )
                docs = list(result.docs)
            checklist_rows.append(
                {
                    "draft_id": saved["draft_id"],
                    "kb_id": kb_id,
                    "query": query,
                    "layer": str(case.get("layer") or ""),
                    "expected_sources_hint": list(case.get("expected_sources") or []),
                    "candidate_chunks": candidate_checklist(
                        docs,
                        expected_sources=list(case.get("expected_sources") or []),
                        source_versions=source_versions,
                    ),
                    "reviewer_notes": "",
                }
            )
    finally:
        store.close()

    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "suggestion_only": True,
        "warning": (
            "Candidate chunks and expected-source hints are reviewer aids only; "
            "they are not gold labels and were not copied into acceptable_evidence."
        ),
        "drafts_jsonl": str(args.drafts_jsonl),
        "planned_eval_jsonl": str(args.planned_eval_jsonl),
        "case_count": len(cases),
        "created_or_existing_count": created,
        "reused_count": reused,
        "cases": checklist_rows,
    }
    _write_jsonl(args.planned_eval_jsonl, planned_eval_rows)
    _write_json(args.checklist, payload)
    print(json.dumps({key: payload[key] for key in payload if key != "cases"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
