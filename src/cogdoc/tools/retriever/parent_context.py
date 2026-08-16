from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from cogdoc.graph.state import RetrievedDoc


REASON_SELECTED = "selected"
REASON_ANCHOR_NOT_FOUND = "anchor_not_found"
REASON_MISSING_ANCHOR_CHUNK_ID = "missing_anchor_chunk_id"
REASON_MISSING_PARENT_CHUNK_ID = "missing_parent_chunk_id"
REASON_INCOMPLETE_PARENT_STRUCTURE = "incomplete_parent_structure"


@dataclass(frozen=True)
class ParentContextSelection:
    docs: list[RetrievedDoc]
    fallback_required: bool
    reason: str


def _meta(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    value = doc.get("meta")
    return value if isinstance(value, Mapping) else {}


def _chunk_id(doc: Mapping[str, Any]) -> str:
    return str(_meta(doc).get("chunk_id") or "").strip()


def _parent_chunk_id(doc: Mapping[str, Any]) -> str:
    return str(_meta(doc).get("parent_chunk_id") or "").strip()


def _order_value(doc: Mapping[str, Any], key: str) -> int | None:
    value = _meta(doc).get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _fallback(reason: str) -> ParentContextSelection:
    return ParentContextSelection([], True, reason)


def _ordered_parent_children(
    source_chunks: Sequence[RetrievedDoc], parent_chunk_id: str
) -> list[RetrievedDoc] | None:
    children = [
        doc for doc in source_chunks if _parent_chunk_id(doc) == parent_chunk_id
    ]
    if not children:
        return None

    chunk_ids = [_chunk_id(doc) for doc in children]
    if any(not chunk_id for chunk_id in chunk_ids) or len(set(chunk_ids)) != len(
        chunk_ids
    ):
        return None

    declared_counts = [
        _order_value(doc, "parent_child_count")
        for doc in children
        if _meta(doc).get("parent_child_count") is not None
    ]
    if declared_counts:
        if len(declared_counts) != len(children):
            return None
        if len(set(declared_counts)) != 1 or declared_counts[0] != len(children):
            return None

    child_orders = [_order_value(doc, "child_index_in_parent") for doc in children]
    chunk_orders = [_order_value(doc, "chunk_index") for doc in children]
    if all(value is not None for value in child_orders):
        ordered = sorted(
            children,
            key=lambda doc: (
                cast(int, _order_value(doc, "child_index_in_parent")),
                _order_value(doc, "chunk_index")
                if _order_value(doc, "chunk_index") is not None
                else -1,
                _chunk_id(doc),
            ),
        )
        # A structured parent is valid only when every child is present exactly
        # once.  Gaps usually mean a mixed/partially rebuilt index; treating the
        # surviving rows as a complete section would silently skip evidence.
        normalized_orders = [
            cast(int, _order_value(doc, "child_index_in_parent")) for doc in ordered
        ]
        return ordered if normalized_orders == list(range(len(ordered))) else None
    if all(value is not None for value in chunk_orders):
        ordered = sorted(
            children,
            key=lambda doc: (
                cast(int, _order_value(doc, "chunk_index")),
                _chunk_id(doc),
            ),
        )
        normalized_orders = [
            cast(int, _order_value(doc, "chunk_index")) for doc in ordered
        ]
        expected = list(
            range(normalized_orders[0], normalized_orders[0] + len(ordered))
        )
        return ordered if normalized_orders == expected else None
    return None


def _text_chars(doc: Mapping[str, Any]) -> int:
    meta = _meta(doc)
    # Budget what the generator actually receives, not only the child body.
    # Labels are fixed-size prompt overhead; including them keeps a configured
    # window from growing unexpectedly when locator context is present.
    body_chars = len(str(doc.get("text") or ""))
    context = str(meta.get("context") or "").strip()
    section_path = str(meta.get("section_path") or "").strip()
    if context:
        body_chars += len(context) + len("定位上下文：\n\n正文：\n")
    if section_path:
        body_chars += len(section_path) + len("章节路径：\n\n")
    return body_chars


def _balanced_window(
    docs: Sequence[RetrievedDoc],
    anchor_index: int,
    *,
    max_chunks: int,
    max_chars: int,
) -> tuple[int, int]:
    # Anchor 是硬约束；max_chars 是附加 siblings 的软预算。
    best_start = anchor_index
    best_end = anchor_index + 1
    best_score = (1, 0, _text_chars(docs[anchor_index]), -anchor_index)

    for start in range(anchor_index, -1, -1):
        for end in range(anchor_index + 1, len(docs) + 1):
            count = end - start
            if count > max_chunks:
                break
            total_chars = sum(_text_chars(doc) for doc in docs[start:end])
            if count > 1 and total_chars > max_chars:
                break
            left_count = anchor_index - start
            right_count = end - anchor_index - 1
            score = (
                count,
                -abs(left_count - right_count),
                total_chars,
                -start,
            )
            if score > best_score:
                best_start, best_end, best_score = start, end, score
    return best_start, best_end


def select_parent_context(
    source_chunks: Sequence[RetrievedDoc],
    anchor: RetrievedDoc | str,
    *,
    max_chunks: int,
    max_chars: int,
) -> ParentContextSelection:
    """Select a bounded, contiguous sibling window around a structured child."""

    if max_chunks < 1:
        raise ValueError("max_chunks must be at least 1")
    if max_chars < 0:
        raise ValueError("max_chars must be non-negative")

    source_by_id: dict[str, RetrievedDoc] = {}
    duplicate_ids: set[str] = set()
    for doc in source_chunks:
        chunk_id = _chunk_id(doc)
        if not chunk_id:
            continue
        if chunk_id in source_by_id:
            duplicate_ids.add(chunk_id)
        else:
            source_by_id[chunk_id] = doc

    if isinstance(anchor, str):
        anchor_chunk_id = anchor.strip()
        if not anchor_chunk_id:
            return _fallback(REASON_MISSING_ANCHOR_CHUNK_ID)
        anchor_doc = source_by_id.get(anchor_chunk_id)
        if anchor_doc is None or anchor_chunk_id in duplicate_ids:
            return _fallback(REASON_ANCHOR_NOT_FOUND)
        output_anchor = anchor_doc
    else:
        anchor_chunk_id = _chunk_id(anchor)
        if not anchor_chunk_id:
            return _fallback(REASON_MISSING_ANCHOR_CHUNK_ID)
        anchor_doc = source_by_id.get(anchor_chunk_id)
        if anchor_doc is None or anchor_chunk_id in duplicate_ids:
            return _fallback(REASON_ANCHOR_NOT_FOUND)
        output_anchor = anchor

    parent_chunk_id = _parent_chunk_id(output_anchor)
    if not parent_chunk_id:
        return _fallback(REASON_MISSING_PARENT_CHUNK_ID)

    ordered = _ordered_parent_children(source_chunks, parent_chunk_id)
    if ordered is None:
        return _fallback(REASON_INCOMPLETE_PARENT_STRUCTURE)
    anchor_indexes = [
        index for index, doc in enumerate(ordered) if _chunk_id(doc) == anchor_chunk_id
    ]
    if len(anchor_indexes) != 1:
        return _fallback(REASON_INCOMPLETE_PARENT_STRUCTURE)

    anchor_index = anchor_indexes[0]
    ordered = list(ordered)
    ordered[anchor_index] = output_anchor
    start, end = _balanced_window(
        ordered,
        anchor_index,
        max_chunks=max_chunks,
        max_chars=max_chars,
    )
    return ParentContextSelection(
        docs=[copy.deepcopy(doc) for doc in ordered[start:end]],
        fallback_required=False,
        reason=REASON_SELECTED,
    )
