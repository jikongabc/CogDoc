import copy

import pytest

from cogdoc.tools.retriever.parent_context import (
    REASON_ANCHOR_NOT_FOUND,
    REASON_INCOMPLETE_PARENT_STRUCTURE,
    REASON_MISSING_PARENT_CHUNK_ID,
    REASON_SELECTED,
    select_parent_context,
)


def _doc(
    index: int,
    *,
    parent: str = "parent-1",
    text: str | None = None,
    child_index: int | None = None,
) -> dict:
    meta = {
        "chunk_id": f"c{index}",
        "parent_chunk_id": parent,
        "chunk_index": 100 + index,
    }
    if child_index is not None:
        meta["child_index_in_parent"] = child_index
    return {
        "text": text if text is not None else f"text-{index}",
        "meta": meta,
    }


def _ids(selection) -> list[str]:
    return [doc["meta"]["chunk_id"] for doc in selection.docs]


def test_selects_contiguous_balanced_window_and_preserves_anchor_metrics():
    source_chunks = [_doc(index, child_index=index) for index in (4, 1, 3, 0, 2)]
    anchor = copy.deepcopy(
        next(doc for doc in source_chunks if _ids_for_doc(doc) == "c2")
    )
    anchor["retrieval"] = {"rerank_score": 0.91}

    selection = select_parent_context(
        source_chunks,
        anchor,
        max_chunks=3,
        max_chars=100,
    )

    assert _ids(selection) == ["c1", "c2", "c3"]
    assert selection.docs[1]["retrieval"]["rerank_score"] == 0.91
    assert selection.fallback_required is False
    assert selection.reason == REASON_SELECTED


def _ids_for_doc(doc) -> str:
    return doc["meta"]["chunk_id"]


@pytest.mark.parametrize(
    ("anchor_id", "expected"),
    [("c0", ["c0", "c1", "c2"]), ("c4", ["c2", "c3", "c4"])],
)
def test_fills_window_at_parent_boundaries(anchor_id, expected):
    source_chunks = [_doc(index, child_index=index) for index in range(5)]

    selection = select_parent_context(
        source_chunks,
        anchor_id,
        max_chunks=3,
        max_chars=100,
    )

    assert _ids(selection) == expected
    assert selection.fallback_required is False


def test_character_budget_limits_only_siblings_and_never_drops_anchor():
    source_chunks = [
        _doc(0, text="LLLL", child_index=0),
        _doc(1, text="AAAA", child_index=1),
        _doc(2, text="RR", child_index=2),
    ]

    within_budget = select_parent_context(
        source_chunks,
        "c1",
        max_chunks=3,
        max_chars=6,
    )
    anchor_over_budget = select_parent_context(
        source_chunks,
        "c1",
        max_chunks=3,
        max_chars=2,
    )

    assert _ids(within_budget) == ["c1", "c2"]
    assert sum(len(doc["text"]) for doc in within_budget.docs) == 6
    assert _ids(anchor_over_budget) == ["c1"]
    assert anchor_over_budget.fallback_required is False


def test_single_structured_child_is_valid_context_without_fallback():
    selection = select_parent_context(
        [_doc(0, child_index=0)],
        "c0",
        max_chunks=4,
        max_chars=100,
    )

    assert _ids(selection) == ["c0"]
    assert selection.fallback_required is False
    assert selection.reason == REASON_SELECTED


def test_missing_structure_returns_explicit_fallback_signal():
    missing_parent = _doc(0, child_index=0)
    del missing_parent["meta"]["parent_chunk_id"]
    result = select_parent_context(
        [missing_parent], missing_parent, max_chunks=3, max_chars=100
    )
    assert result.docs == []
    assert result.fallback_required is True
    assert result.reason == REASON_MISSING_PARENT_CHUNK_ID

    incomplete = [_doc(0), _doc(1)]
    for doc in incomplete:
        del doc["meta"]["chunk_index"]
    result = select_parent_context(incomplete, "c0", max_chunks=3, max_chars=100)
    assert result.docs == []
    assert result.fallback_required is True
    assert result.reason == REASON_INCOMPLETE_PARENT_STRUCTURE

    result = select_parent_context(
        [_doc(0, child_index=0)], "missing", max_chunks=3, max_chars=100
    )
    assert result.docs == []
    assert result.fallback_required is True
    assert result.reason == REASON_ANCHOR_NOT_FOUND


@pytest.mark.parametrize(
    "order_field",
    ["child_index_in_parent", "chunk_index"],
)
def test_partial_or_corrupt_parent_order_requires_neighbor_fallback(order_field):
    source_chunks = [_doc(index, child_index=index) for index in range(3)]
    if order_field == "child_index_in_parent":
        source_chunks[1]["meta"][order_field] = 2
        source_chunks[2]["meta"][order_field] = 3
    else:
        for doc in source_chunks:
            del doc["meta"]["child_index_in_parent"]
        source_chunks[1]["meta"][order_field] += 1
        source_chunks[2]["meta"][order_field] += 1

    selection = select_parent_context(
        source_chunks,
        source_chunks[0],
        max_chunks=3,
        max_chars=100,
    )

    assert selection.fallback_required is True
    assert selection.reason == REASON_INCOMPLETE_PARENT_STRUCTURE


def test_declared_parent_child_count_detects_missing_tail_child():
    source_chunks = [_doc(index, child_index=index) for index in range(2)]
    for doc in source_chunks:
        doc["meta"]["parent_child_count"] = 3

    selection = select_parent_context(
        source_chunks,
        "c0",
        max_chunks=3,
        max_chars=100,
    )

    assert selection.docs == []
    assert selection.fallback_required is True
    assert selection.reason == REASON_INCOMPLETE_PARENT_STRUCTURE


def test_character_budget_includes_section_and_locator_context():
    source_chunks = [
        _doc(0, text="AAAA", child_index=0),
        _doc(1, text="BBBB", child_index=1),
    ]
    source_chunks[0]["meta"].update(
        {"section_path": "Methods > Training", "context": "locator"}
    )

    selection = select_parent_context(
        source_chunks,
        "c1",
        max_chunks=2,
        max_chars=20,
    )

    assert _ids(selection) == ["c1"]


def test_chunk_index_fallback_sort_is_stable_and_does_not_mutate_inputs():
    source_chunks = [_doc(index) for index in (3, 0, 2, 1)]
    before = copy.deepcopy(source_chunks)

    first = select_parent_context(
        source_chunks,
        "c2",
        max_chunks=4,
        max_chars=100,
    )
    again = select_parent_context(
        list(reversed(source_chunks)),
        "c2",
        max_chunks=4,
        max_chars=100,
    )

    assert _ids(first) == ["c0", "c1", "c2", "c3"]
    assert _ids(again) == _ids(first)
    assert source_chunks == before
    assert first.docs[0] is not source_chunks[1]
    assert [_ids_for_doc(doc) for doc in first.docs] == ["c0", "c1", "c2", "c3"]


def test_rejects_impossible_hard_budgets():
    chunks = [_doc(0, child_index=0)]

    with pytest.raises(ValueError, match="max_chunks"):
        select_parent_context(chunks, "c0", max_chunks=0, max_chars=100)
    with pytest.raises(ValueError, match="max_chars"):
        select_parent_context(chunks, "c0", max_chunks=1, max_chars=-1)
