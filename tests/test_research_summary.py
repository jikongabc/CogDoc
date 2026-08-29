import base64
import json

import pytest
from pydantic import ValidationError

from cogdoc.api.schemas import (
    ResearchJobSectionCounts,
    ResearchJobSummary,
    ResearchJobSummaryPage,
)
from cogdoc.service.research_summary import (
    RESEARCH_SUMMARY_CURSOR_VERSION,
    ResearchSummaryCursorError,
    ResearchSummaryProjectionError,
    compact_research_job_summary,
    decode_research_summary_cursor,
    encode_research_summary_cursor,
    if_none_match_matches,
    paginate_research_job_summaries,
    project_research_job_summaries,
    project_research_job_summary,
    research_summary_page_etag,
)


def _row(**overrides):
    row = {
        "job_id": "rj_1",
        "kb_id": "kb",
        "title": "赛事比较案卷",
        "objective": "比较赛事资格、时间成本与评分规则，形成有证据的建议。",
        "is_local": False,
        "status": "running",
        "revision": 4,
        "created_at": "2026-08-10T01:00:00+00:00",
        "updated_at": "2026-08-10T02:00:00+00:00",
        "sections": [
            {"status": "completed", "content": "不可进入摘要的章节正文"},
            {"status": "running", "evidence": [{"text_preview": "私有证据"}]},
            {"status": "pending"},
            {"status": "failed"},
        ],
        "report_status": "ready",
        "report_version": 2,
        "review_status": "pending",
        "report": {"content": "不可进入摘要的报告正文"},
        "report_history": [{"report": {"content": "不可进入摘要的历史正文"}}],
        "published_report": None,
        "published_at": None,
        "error": "",
    }
    row.update(overrides)
    return row


def test_research_summary_projection_is_bounded_and_excludes_heavy_artifacts():
    row = _row()
    summary = project_research_job_summary(
        row,
        {
            "status": "stale",
            "stale_reasons": [
                "index_generation_changed",
                "source_sha256_changed:clients/acme/private.pdf",
            ],
            "captured": {"source_versions": ["private.pdf"]},
        },
    )

    assert summary["section_counts"] == {
        "total": 4,
        "pending": 1,
        "running": 1,
        "completed": 1,
        "failed": 1,
    }
    assert summary["has_report"] is True
    assert summary["report_size_bytes"] == len(
        row["report"]["content"].encode("utf-8")
    )
    assert summary["report_history_count"] == 1
    assert summary["provenance_status"] == "stale"
    assert summary["provenance_stale_reasons"] == [
        "index_generation_changed",
        "source_sha256_changed",
    ]
    encoded = json.dumps(summary, ensure_ascii=False)
    for private_value in ("章节正文", "私有证据", "报告正文", "历史正文", "private.pdf"):
        assert private_value not in encoded
    assert "sections" not in summary
    assert "report" not in summary
    assert "report_history" not in summary
    assert "published_report" not in summary


def test_research_summary_projection_truncates_public_text_without_copying_body_size():
    first = project_research_job_summary(_row(objective="目标" * 500))
    second = project_research_job_summary(
        _row(objective="目标" * 500, report={"content": "正文" * 100_000})
    )

    assert len(first["objective_preview"]) == 240
    assert first["objective_preview"].endswith("…")
    assert set(first) == set(second)
    assert second["report_size_bytes"] > first["report_size_bytes"]
    first.pop("report_size_bytes")
    second.pop("report_size_bytes")
    assert first == second


def test_research_compact_storage_round_trip_never_contains_artifact_bodies():
    row = _row(
        evidence_provenance={
            "index_generation": "generation-1",
            "source_versions": [{"source": "private.pdf", "sha256": "abc"}],
        }
    )
    compact = compact_research_job_summary(row)
    projected = project_research_job_summary(
        compact,
        {"status": "current", "stale_reasons": []},
    )

    assert projected == project_research_job_summary(
        row,
        {"status": "current", "stale_reasons": []},
    )
    encoded = json.dumps(compact, ensure_ascii=False)
    for forbidden in ("章节正文", "私有证据", "报告正文", "历史正文"):
        assert forbidden not in encoded
    assert compact["evidence_provenance"]["index_generation"] == "generation-1"


@pytest.mark.parametrize(
    "overrides",
    [
        {"revision": True},
        {"status": "mystery"},
        {"sections": "not-a-list"},
        {"report_history": {}},
        {"is_local": 1},
    ],
)
def test_research_summary_projection_rejects_ambiguous_durable_rows(overrides):
    with pytest.raises(ResearchSummaryProjectionError):
        project_research_job_summary(_row(**overrides))


def test_research_summary_projection_requires_provenance_cardinality_alignment():
    with pytest.raises(ResearchSummaryProjectionError, match="cardinality"):
        project_research_job_summaries([_row()], [])


def test_research_summary_cursor_round_trip_is_opaque_and_canonical():
    token = encode_research_summary_cursor(
        updated_at="2026-08-10T02:00:00+00:00", job_id="rj_1"
    )

    assert "2026" not in token
    assert decode_research_summary_cursor(token).updated_at == (
        "2026-08-10T02:00:00+00:00"
    )
    assert decode_research_summary_cursor(token).job_id == "rj_1"
    with pytest.raises(ResearchSummaryCursorError):
        decode_research_summary_cursor(token + "=")

    noncanonical = base64.urlsafe_b64encode(
        json.dumps(
            {
                "version": RESEARCH_SUMMARY_CURSOR_VERSION,
                "updated_at": "2026-08-10T02:00:00+00:00",
                "job_id": "rj_1",
            },
            separators=(",", ":"),
        ).encode()
    ).decode().rstrip("=")
    with pytest.raises(ResearchSummaryCursorError, match="canonical"):
        decode_research_summary_cursor(noncanonical)


@pytest.mark.parametrize(
    "payload",
    [
        '{"job_id":"rj_1","job_id":"rj_2","updated_at":"2026-08-10T02:00:00+00:00","version":"research-summary-cursor-v1"}',
        '{"job_id":"rj_1","updated_at":"2026-08-10T02:00:00","version":"research-summary-cursor-v1"}',
        '{"job_id":"../../bad","updated_at":"2026-08-10T02:00:00+00:00","version":"research-summary-cursor-v1"}',
        '{"extra":1,"job_id":"rj_1","updated_at":"2026-08-10T02:00:00+00:00","version":"research-summary-cursor-v1"}',
    ],
)
def test_research_summary_cursor_rejects_duplicate_unscoped_or_invalid_fields(payload):
    token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    with pytest.raises(ResearchSummaryCursorError):
        decode_research_summary_cursor(token)


def test_research_summary_keyset_pagination_is_stable_for_timestamp_ties():
    rows = [
        {"job_id": "rj_1", "updated_at": "2026-08-10T01:00:00+00:00"},
        {"job_id": "rj_3", "updated_at": "2026-08-10T02:00:00+00:00"},
        {"job_id": "rj_2", "updated_at": "2026-08-10T02:00:00+00:00"},
    ]

    first = paginate_research_job_summaries(rows, limit=2)
    second = paginate_research_job_summaries(
        rows, limit=2, cursor=first["next_cursor"]
    )

    assert [row["job_id"] for row in first["jobs"]] == ["rj_3", "rj_2"]
    assert first["has_more"] is True
    assert [row["job_id"] for row in second["jobs"]] == ["rj_1"]
    assert second == {
        "jobs": [{"job_id": "rj_1", "updated_at": "2026-08-10T01:00:00+00:00"}],
        "next_cursor": None,
        "has_more": False,
    }


def test_research_summary_orders_iso_offsets_by_instant_not_text() -> None:
    rows = [
        {"job_id": "rj_early", "updated_at": "2026-08-10T09:30:00+08:00"},
        {"job_id": "rj_late", "updated_at": "2026-08-10T02:00:00+00:00"},
    ]
    page = paginate_research_job_summaries(rows, limit=2)
    assert [row["job_id"] for row in page["jobs"]] == ["rj_late", "rj_early"]


def test_research_summary_etag_is_deterministic_and_supports_weak_get_matching():
    page = {"jobs": [{"job_id": "rj_1", "revision": 2}], "next_cursor": None}
    etag = research_summary_page_etag(page)

    assert etag == research_summary_page_etag(dict(reversed(list(page.items()))))
    assert etag != research_summary_page_etag(
        {"jobs": [{"job_id": "rj_1", "revision": 3}], "next_cursor": None}
    )
    assert if_none_match_matches(etag, etag) is True
    assert if_none_match_matches(f'W/{etag}, "other"', etag) is True
    assert if_none_match_matches("*", etag) is True
    assert if_none_match_matches('"other"', etag) is False


def test_research_summary_schemas_are_strict_and_cross_field_consistent():
    summary = ResearchJobSummary.model_validate(project_research_job_summary(_row()))
    page = ResearchJobSummaryPage(jobs=[summary])

    assert page.schema_version == "v1"
    with pytest.raises(ValidationError):
        ResearchJobSectionCounts(
            total=2, pending=1, running=0, completed=0, failed=0
        )
    with pytest.raises(ValidationError):
        ResearchJobSummary.model_validate(
            {**summary.model_dump(), "sections": [], "revision": "4"}
        )
    with pytest.raises(ValidationError):
        ResearchJobSummaryPage(jobs=[summary], has_more=True, next_cursor=None)
