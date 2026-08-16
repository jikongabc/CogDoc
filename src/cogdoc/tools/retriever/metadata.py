from collections.abc import Mapping
from typing import Any


SAFE_RETRIEVAL_METADATA_KEYS = {
    "evidence_id",
    "search_channel",
    "context_anchor_chunk_id",
    "context_expansion",
    "knowledge_score",
    "retrieval_score",
    "rerank_score",
    "feedback_boost",
    "match_coverage",
    "match_density",
    "matched_terms",
    "query_term_count",
    "knowledge_term_count",
    "status_filter",
    "rewrite_query",
    "query_fusion_score",
    "query_hit_count",
    "matched_queries",
    "matched_channels",
    "matched_requirement_ids",
    "matched_unit_ids",
    "best_query_rank",
    "original_query_hit",
    "retrieval_round",
    "evidence_text_start",
    "evidence_text_end",
    "evidence_trimmed_overlap_chars",
    "evidence_span_selected",
    "evidence_span_input_start",
    "evidence_span_input_end",
    "evidence_span_start",
    "evidence_span_end",
    "evidence_span_original_chars",
    "evidence_span_selected_chars",
    "evidence_span_score",
    "evidence_span_matched_terms",
    "evidence_span_matched_requirement_ids",
    "evidence_span_matched_unit_ids",
    "evidence_span_reason",
}

_STRUCTURE_STRING_META_FIELDS = (
    "parent_chunk_id",
    "section_title",
    "section_path",
    "chunk_type",
    "document_profile",
    "chunking_strategy_version",
)
_STRUCTURE_INT_META_FIELDS = (
    "section_level",
    "child_index_in_parent",
    "parent_child_count",
    "parent_char_count",
    "chunk_char_count",
)
_STRUCTURE_FLOAT_META_FIELDS = ("chunk_quality_score",)


def copy_optional_structure_metadata(
    source: Mapping[str, Any], target: dict[str, Any]
) -> None:
    """Copy explicitly present chunk-structure fields into persisted metadata."""

    for field in _STRUCTURE_STRING_META_FIELDS:
        value = source.get(field)
        if value is not None:
            target[field] = str(value)
    for field in _STRUCTURE_INT_META_FIELDS:
        value = source.get(field)
        if value is not None:
            target[field] = int(value)
    for field in _STRUCTURE_FLOAT_META_FIELDS:
        value = source.get(field)
        if value is not None:
            target[field] = float(value)


# 提取安全检索元数据。
def safe_retrieval_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {key: value[key] for key in SAFE_RETRIEVAL_METADATA_KEYS if key in value}
