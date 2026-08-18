import json

from cogdoc.frontend.app import (
    _claim_review_export_jsonl,
    _claim_review_queue_label,
    _eval_candidate_identity,
)


def test_eval_candidate_identity_keeps_exact_half_open_span():
    candidate = {
        "chunk_id": "c1",
        "parent_chunk_id": "p1",
        "source": "policy.pdf",
        "source_sha256": "sha-1",
        "_selected_start": 4,
        "_selected_end": 11,
    }

    assert _eval_candidate_identity(candidate, include_span=True) == {
        "chunk_id": "c1",
        "parent_chunk_id": "p1",
        "source": "policy.pdf",
        "source_sha256": "sha-1",
        "start": 4,
        "end": 11,
    }


def test_eval_candidate_identity_omits_empty_parent_and_optional_span():
    candidate = {
        "chunk_id": "c1",
        "parent_chunk_id": "",
        "source": "policy.pdf",
        "source_sha256": "sha-1",
    }

    assert _eval_candidate_identity(candidate, include_span=False) == {
        "chunk_id": "c1",
        "source": "policy.pdf",
        "source_sha256": "sha-1",
    }


def test_claim_review_queue_label_is_compact_and_localized():
    label = _claim_review_queue_label(
        {
            "status": "pending",
            "actual_verdict": "insufficient",
            "claim": "这是一条需要人工核对、内容很长并且应当在队列选择器中安全截断的声明。" * 3,
        }
    )

    assert label.startswith("待审 · 证据不足 · ")
    assert label.endswith("…")
    assert len(label) < 80


def test_claim_review_export_jsonl_preserves_unicode_and_one_row_per_line():
    output = _claim_review_export_jsonl(
        [
            {"id": "a", "claim": "中文声明"},
            {"id": "b", "claim": "second"},
        ]
    )

    lines = output.splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["a", "b"]
    assert "中文声明" in output
    assert output.endswith("\n")
