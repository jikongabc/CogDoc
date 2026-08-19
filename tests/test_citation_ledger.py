from __future__ import annotations

import copy
import json

import pytest

from cogdoc.agents.citation_validator import CitationValidatorAgent
from cogdoc.graph.state import RetrievedDoc
from cogdoc.tools.citation_ledger import (
    CitationLedgerError,
    assign_evidence_ids,
    format_evidence_id,
    render_display_citations,
)
from cogdoc.tools.public_citation_ledger import validate_public_citation_ledger


def _doc(
    chunk_id: str,
    *,
    start: int = 0,
    end: int = 12,
    source: str = "guide.pdf",
    page: int = 2,
    text: str = "sensitive evidence body",
    chunk_index: int = 0,
) -> RetrievedDoc:
    return {
        "text": text,
        "meta": {
            "chunk_id": chunk_id,
            "source_sha256": "sha256:test",
            "local_chunk_index": chunk_index,
            "chunk_index": chunk_index,
            "source": source,
            "page": page,
            "page_start": page,
            "page_end": page,
            "score": 1.0,
            "origin": "test",
        },
        "retrieval": {
            "evidence_text_start": start,
            "evidence_text_end": end,
        },
    }


def test_assign_evidence_ids_preserves_input_and_presentation_order() -> None:
    docs = [
        _doc("chunk-b", start=8, end=20, chunk_index=1),
        _doc("chunk-a", start=0, end=8, chunk_index=0),
    ]
    original = copy.deepcopy(docs)

    annotated, ledger = assign_evidence_ids(docs)

    assert docs == original
    assert annotated is not docs
    assert [doc["meta"]["chunk_id"] for doc in annotated] == [
        "chunk-b",
        "chunk-a",
    ]
    assert [doc["retrieval"]["evidence_id"] for doc in annotated] == [
        "E001",
        "E002",
    ]
    assert [entry["chunk_id"] for entry in ledger] == ["chunk-b", "chunk-a"]


def test_evidence_ids_expand_beyond_three_digits() -> None:
    docs = [_doc(f"chunk-{index}", chunk_index=index) for index in range(1000)]

    annotated, ledger = assign_evidence_ids(docs)

    assert format_evidence_id(999) == "E999"
    assert format_evidence_id(1000) == "E1000"
    assert annotated[-1]["retrieval"]["evidence_id"] == "E1000"
    assert ledger[-1]["evidence_id"] == "E1000"


def test_same_chunk_with_different_final_offsets_gets_distinct_ids() -> None:
    docs = [
        _doc("shared-chunk", start=0, end=10, text="0123456789"),
        _doc("shared-chunk", start=10, end=20, text="abcdefghij"),
        _doc("shared-chunk", start=0, end=10, text="0123456789"),
    ]

    annotated, ledger = assign_evidence_ids(docs)

    assert [doc["retrieval"]["evidence_id"] for doc in annotated] == [
        "E001",
        "E002",
        "E001",
    ]
    assert [(entry["span_start"], entry["span_end"]) for entry in ledger] == [
        (0, 10),
        (10, 20),
    ]


@pytest.mark.parametrize(
    "answer",
    [
        "伪造证据。[E999]",
        "一半正确[E001]，一半伪造[E999]。",
        "过早暴露物理引用。[guide.pdf:P2]",
        "小写 ID 不被接受。[e001]",
        "全角括号不被接受。［E001］",
        "全角字符不被接受。[Ｅ００１]",
        "合法引用[E001]，但不能混入全角冒号页码。[guide.pdf：P2]",
        "合法引用[E001]，但不能混入连字符 ID。[E-002]",
        "合法引用[E001]，但不能混入拆分 ID。[E-ID:002]",
        "合法引用[E001]，但不能混入组合 ID。[E001,E002]",
        "合法引用[E001]，但不能混入带句点 ID。[E001.]",
        "合法引用[E001]，但不能留下未闭合 ID。[E-002",
        "合法引用[E001]，但不能留下超长标签。[" + "a" * 161 + ":P2]",
        "合法引用[E001]，但不能混入变体页码。[guide.pdf / P-2]",
    ],
)
def test_strict_validator_rejects_non_registry_or_noncanonical_citations(
    answer: str,
) -> None:
    _, ledger = assign_evidence_ids([_doc("chunk-a")])

    result = CitationValidatorAgent.validate_evidence_citations(answer, ledger)

    assert result["is_valid"] is False


def test_strict_validator_accepts_only_frozen_evidence_ids() -> None:
    _, ledger = assign_evidence_ids([_doc("chunk-a")])

    result = CitationValidatorAgent.validate_evidence_citations(
        "结论由精确证据支持。[E001]", ledger
    )

    assert result == {
        "is_valid": True,
        "critique": "",
        "evidence_ids": ["E001"],
    }


def test_strict_validator_ignores_long_non_citation_brackets() -> None:
    _, ledger = assign_evidence_ids([_doc("chunk-a")])
    answer = f"数组说明[{'x' * 200}]，结论有证据。[E001]"

    result = CitationValidatorAgent.validate_evidence_citations(answer, ledger)

    assert result["is_valid"] is True


def test_strict_validator_accepts_canonical_four_digit_id() -> None:
    _, ledger = assign_evidence_ids([_doc("chunk-a")])
    ledger[0]["evidence_id"] = "E1000"

    result = CitationValidatorAgent.validate_evidence_citations(
        "结论由精确证据支持。[E1000]", ledger
    )

    assert result["is_valid"] is True
    assert result["evidence_ids"] == ["E1000"]


def test_same_page_siblings_remain_independent_ledger_entries() -> None:
    _, ledger = assign_evidence_ids(
        [
            _doc("same-page-left", start=0, end=20),
            _doc("same-page-right", start=20, end=40),
        ]
    )

    assert [entry["evidence_id"] for entry in ledger] == ["E001", "E002"]
    assert [entry["chunk_id"] for entry in ledger] == [
        "same-page-left",
        "same-page-right",
    ]
    assert [entry["display_citation"] for entry in ledger] == [
        "[guide.pdf:P2]",
        "[guide.pdf:P2]",
    ]
    restricted_result = CitationValidatorAgent.validate_evidence_citations(
        "不能借用同页 sibling。[E002]", ledger[:1]
    )
    assert restricted_result["is_valid"] is False
    assert restricted_result["invalid_evidence_ids"] == ["E002"]


def test_render_display_citations_tracks_exact_occurrences_for_same_page() -> None:
    _, ledger = assign_evidence_ids(
        [
            _doc("same-page-left", start=0, end=20),
            _doc("same-page-right", start=20, end=40),
        ]
    )

    rendered = render_display_citations("甲[E001]；乙[E002]；再证[E001]。", ledger)

    expected = "甲[guide.pdf:P2]；乙[guide.pdf:P2]；再证[guide.pdf:P2]。"
    assert rendered.answer == expected
    assert [entry["evidence_id"] for entry in rendered.entries] == [
        "E001",
        "E002",
    ]
    occurrences = {
        entry["evidence_id"]: entry["occurrences"] for entry in rendered.entries
    }
    assert [item["index"] for item in occurrences["E001"]] == [0, 2]
    assert [item["index"] for item in occurrences["E002"]] == [1]
    for entry_occurrences in occurrences.values():
        for occurrence in entry_occurrences:
            assert (
                expected[occurrence["answer_start"] : occurrence["answer_end"]]
                == "[guide.pdf:P2]"
            )


def test_display_citation_escapes_filename_syntax_characters() -> None:
    _, ledger = assign_evidence_ids(
        [_doc("syntax-source", source="policy].pdf", page=2)]
    )

    rendered = render_display_citations("结论。[E001]", ledger)

    assert rendered.answer == "结论。[policy%5D.pdf:P2]"
    assert rendered.entries[0]["source"] == "policy].pdf"
    validation = validate_public_citation_ledger(
        rendered.answer, list(rendered.entries)
    )
    assert validation.is_valid is True


def test_universal_spreadsheet_citation_is_version_pinned_and_publicly_valid() -> None:
    doc = _doc("sheet-chunk", source="metrics.xlsx", page=1)
    doc["meta"].update(
        {
            "source_id": "src-123",
            "source_version_id": "sv-456",
            "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "source_location": {"sheet": "FY26", "cell_range": "A1:C8"},
        }
    )
    _, ledger = assign_evidence_ids([doc])
    rendered = render_display_citations("收入增长。[E001]", ledger)

    assert rendered.answer == "收入增长。[metrics.xlsx@sheet-FY26!A1:C8]"
    assert rendered.entries[0]["source_id"] == "src-123"
    assert rendered.entries[0]["source_version_id"] == "sv-456"
    assert rendered.entries[0]["location"] == {
        "sheet": "FY26",
        "cell_range": "A1:C8",
    }
    validation = validate_public_citation_ledger(
        rendered.answer, list(rendered.entries)
    )
    assert validation.is_valid is True


def test_universal_location_without_source_version_is_rejected() -> None:
    doc = _doc("slide-chunk", source="deck.pptx", page=1)
    doc["meta"]["source_location"] = {"slide": 4}
    _, ledger = assign_evidence_ids([doc])

    with pytest.raises(CitationLedgerError, match="not version-pinned"):
        render_display_citations("结论。[E001]", ledger)


def test_public_ledger_never_contains_sensitive_evidence_text() -> None:
    secret = "PRIVATE-RAW-EVIDENCE-DO-NOT-EXPOSE"
    _, ledger = assign_evidence_ids(
        [_doc("secret-chunk", text=secret, start=4, end=21)]
    )

    rendered = render_display_citations("公开结论。[E001]", ledger)
    serialized = json.dumps(rendered.entries, ensure_ascii=False)

    assert secret not in serialized
    assert "text" not in rendered.entries[0]
    assert "display_citation" not in rendered.entries[0]
    assert rendered.entries[0]["span_start"] == 4
    assert rendered.entries[0]["span_end"] == 4 + len(secret)


def test_assignment_normalizes_offsets_to_renderer_visible_body() -> None:
    doc = _doc(
        "trimmed-chunk",
        text="\n  visible body \t",
        start=10,
        end=999,
    )

    annotated, ledger = assign_evidence_ids([doc])

    assert annotated[0]["text"] == "visible body"
    assert annotated[0]["retrieval"]["evidence_text_start"] == 13
    assert annotated[0]["retrieval"]["evidence_text_end"] == 25
    assert ledger[0]["span_start"] == 13
    assert ledger[0]["span_end"] == 25
    reassigned, reassigned_ledger = assign_evidence_ids(annotated)
    assert reassigned[0]["retrieval"]["evidence_text_start"] == 13
    assert reassigned_ledger[0]["span_end"] == 25


def test_assignment_rejects_empty_visible_evidence() -> None:
    with pytest.raises(CitationLedgerError, match="no visible text"):
        assign_evidence_ids([_doc("empty", text=" \n\t")])


def test_render_rejects_display_locator_inconsistent_with_ledger_fields() -> None:
    _, ledger = assign_evidence_ids([_doc("chunk-a")])
    ledger[0]["display_citation"] = "[other.pdf:P9]"

    with pytest.raises(CitationLedgerError, match="invalid display_citation"):
        render_display_citations("结论。[E001]", ledger)


@pytest.mark.parametrize(
    "malformed_ledger",
    [
        [None],
        [
            {
                "evidence_id": "E001",
                "chunk_id": "chunk-a",
                "source_type": "document",
                "source": "guide.pdf",
                "page": 2,
                "span_start": "bad",
                "span_end": 12,
                "display_citation": "[guide.pdf:P2]",
            }
        ],
    ],
)
def test_malformed_ledger_is_always_reported_or_raised_as_ledger_error(
    malformed_ledger,
) -> None:
    result = CitationValidatorAgent.validate_evidence_citations(
        "结论。[E001]", malformed_ledger
    )

    assert result["is_valid"] is False
    with pytest.raises(CitationLedgerError):
        render_display_citations("结论。[E001]", malformed_ledger)
