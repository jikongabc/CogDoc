from dataclasses import dataclass

from cogdoc.service.claim_verification_review import (
    build_claim_review_candidates,
)


@dataclass
class _Result:
    task_type: str = "qa"
    trace_id: str = "trace-private"
    evidence: list | None = None
    raw_output: dict | None = None


def _result(*, mode: str = "shadow", executed: bool = True) -> _Result:
    return _Result(
        evidence=[],
        raw_output={
            "query": "must-not-persist",
            "answer": "whole answer must not persist",
            "claim_verification_rollout": {
                "mode": mode,
                "configured_mode": "enforce",
                "policy_id": "1111111111111111",
                "rollout_percent": 25,
                "cohort_selected": False,
                "decision": "would_allow",
                "executed": executed,
            },
            "claim_audit": {
                "status": "passed",
                "verifier": {"duration_ms": 42.5},
                "claims": [
                    {
                        "claim_id": "c1",
                        "text": "报名截止日期是 9 月 30 日。",
                        "verdict": "supported",
                        "reason": "精确匹配",
                        "confidence": 0.95,
                        "cited_chunk_ids": ["chunk-1"],
                        "supporting_chunk_ids": ["chunk-1"],
                    },
                    {
                        "claim_id": "c2",
                        "text": "该标题不是事实声明。",
                        "verdict": "not_factual",
                        "reason": "固定结构",
                        "confidence": 0.8,
                        "cited_chunk_ids": [],
                        "supporting_chunk_ids": [],
                    },
                ],
            },
            "reranked_docs": [
                {
                    "text": "报名截止日期是 9 月 30 日，逾期不再受理。",
                    "meta": {
                "chunk_id": "chunk-1",
                "source": "guide.pdf",
                        "page": 2,
                    },
                },
                {
                    "text": "uncited private source",
                    "meta": {"chunk_id": "chunk-2", "source": "other.pdf"},
                },
            ],
        },
    )


def _candidates(result: _Result, *, percent: float = 100, max_claims: int = 5):
    return build_claim_review_candidates(
        result,
        tenant_id="tenant-a",
        kb_id="kb-a",
        sample_percent=percent,
        sample_seed="review-seed",
        max_claims=max_claims,
        max_evidence_per_claim=6,
        max_chars_per_evidence=12,
    )


def test_review_sampling_is_opt_in_and_requires_executed_non_off_audit():
    assert _candidates(_result(), percent=0) == []
    assert _candidates(_result(mode="off")) == []
    assert _candidates(_result(executed=False)) == []


def test_review_candidates_are_deterministic_minimized_and_evidence_bound():
    first = _candidates(_result())
    second = _candidates(_result())

    assert first == second
    assert len(first) == 2
    supported = next(item for item in first if item["claim_id"] == "c1")
    assert supported["claim"] == "报名截止日期是 9 月 30 日。"
    assert supported["actual_verdict"] == "supported"
    assert supported["evidence"] == [
        {
            "chunk_id": "chunk-1",
            "source": "guide.pdf",
            "authorization_source": "guide.pdf",
            "page": 2,
            "page_start": 2,
            "page_end": 2,
            "text": "报名截止日期是 9 月 ",
            "text_truncated": True,
        }
    ]
    assert supported["evidence_complete"] is True
    serialized = repr(first)
    assert "must-not-persist" not in serialized
    assert "whole answer" not in serialized
    assert "trace-private" not in serialized
    assert "uncited private source" not in serialized


def test_review_sampling_caps_claims_by_stable_hash_rank():
    all_candidates = _candidates(_result(), max_claims=5)
    capped = _candidates(_result(), max_claims=1)

    assert capped == all_candidates[:1]
