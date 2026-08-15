import json

from scripts.calibrate_retrieval_confidence import calibrate
from scripts.prepare_retrieval_eval_drafts import candidate_checklist, draft_units


def test_draft_units_never_promote_candidates_to_gold():
    units = draft_units(
        {
            "query": "A 和 B 分别是什么？",
            "expected_sources": ["a.pdf", "b.pdf"],
            "evidence_requirements": [
                {
                    "requirement_id": "r1",
                    "question": "A 是什么？",
                    "retrieval_query": "A",
                    "recovery_query": "A definition",
                }
            ],
        }
    )

    assert units[0].unit_id == "r1"
    assert units[0].acceptable_evidence == []
    assert units[0].hard_negative_chunks == []
    assert units[0].expected_status is None


def test_draft_units_split_multi_source_cases_for_human_review():
    units = draft_units(
        {
            "query": "比较两份规则",
            "expected_sources": ["a.pdf", "b.pdf"],
        }
    )

    assert [unit.unit_id for unit in units] == ["r1", "r2"]
    assert [unit.source for unit in units] == ["a.pdf", "b.pdf"]
    assert all(unit.acceptable_evidence == [] for unit in units)


def test_candidate_checklist_marks_hints_without_gold_annotations():
    docs = [
        {
            "text": " evidence  text ",
            "meta": {"chunk_id": "c1", "source": "a.pdf", "page": 2},
            "retrieval": {"distance": 0.3},
        }
    ]

    rows = candidate_checklist(
        docs, expected_sources=["a.pdf"], source_versions={"a.pdf": "sha"}
    )

    assert rows[0]["expected_source_hint"] is True
    assert rows[0]["source_sha256"] == "sha"
    assert rows[0]["review_decision"] == ""
    assert "acceptable_evidence" not in json.dumps(rows)


def test_confidence_calibration_is_deterministic_and_does_not_mutate_config():
    rows = []
    for index in range(20):
        answerable = index % 2 == 0
        rows.append(
            {
                "query": f"q-{index}",
                "expected_sources": ["a.pdf"] if answerable else [],
                "retrieval_signals": {
                    "distance": 0.3 if answerable else 1.2,
                    "bm25_score": 15.0 if answerable else 1.0,
                },
            }
        )

    first = calibrate(rows)
    second = calibrate(rows)

    assert first == second
    assert first["overall"]["balanced_accuracy"] == 1.0
    assert set(first["recommended"]) == {
        "QA_ABSTAIN_MAX_VECTOR_DISTANCE",
        "QA_ABSTAIN_MIN_BM25_SCORE",
    }
