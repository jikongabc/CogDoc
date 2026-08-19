import bisect
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, List, cast

from cogdoc.graph.state import DocMeta, ParsedPage, RetrievedDoc
from cogdoc.tools.chunk_identity import (
    CHUNKING_STRATEGY_VERSION,
    DEFAULT_CHUNK_CONTEXT_CHARS,
    DEFAULT_CHUNK_CHAR_OVERLAP,
    DEFAULT_CHUNK_CHAR_SIZE,
    MIN_CHUNK_CHARS,
    build_chunk_id,
    build_document_id,
    build_parent_chunk_id,
)
from cogdoc.tools.section_structure import SectionSpan, detect_section_spans


_BLANK_LINE_RE = re.compile(r"\n\s*\n")
_SENTENCE_END_RE = re.compile(r"[。！？!?；;]+[\"'”’）】》」』]*|[.!?;]+(?=\s|$)")
_SOFT_BREAK_RE = re.compile(r"\n+")
_FENCE_START_RE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})")
_LIST_ITEM_RE = re.compile(
    r"^[ \t]*(?:[-+*•‣▪◦]|\d{1,4}[.)、．]|[（(]?[一二三四五六七八九十]+[）)、.．])\s+\S"
)
_TABLE_SEPARATOR_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$"
)
_MEANINGFUL_CHAR_RE = re.compile(r"[A-Za-z0-9\u3400-\u9fff]")
_CJK_CHAR_RE = re.compile(r"[\u3400-\u9fff]")
_WHITESPACE_RE = re.compile(r"\s+")

CHUNK_TYPE_PROSE = "prose"
CHUNK_TYPE_LIST = "list"
CHUNK_TYPE_TABLE = "table"
CHUNK_TYPE_CODE = "code"


# 文本片段用全局字符下标表示闭开区间。
@dataclass(frozen=True)
class TextSpan:
    start: int
    end: int
    kind: str = CHUNK_TYPE_PROSE
    block_id: int = 0


@dataclass(frozen=True)
class ChunkingStats:
    chunk_count: int
    char_count: int
    min_chunk_chars: int
    max_chunk_chars: int
    average_chunk_chars: float
    chunk_type_counts: dict[str, int]


# 修剪 span 两端空白并丢弃空片段。
def _trim_span(
    text: str,
    start: int,
    end: int,
    *,
    kind: str = CHUNK_TYPE_PROSE,
    block_id: int = 0,
) -> TextSpan | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return None
    return TextSpan(start, end, kind, block_id)


def _line_spans(text: str) -> list[tuple[int, int, str]]:
    lines: list[tuple[int, int, str]] = []
    position = 0
    for raw_line in text.splitlines(keepends=True):
        end = position + len(raw_line)
        lines.append((position, end, raw_line.rstrip("\r\n")))
        position = end
    if position < len(text):
        lines.append((position, len(text), text[position:]))
    return lines


def _is_table_line(value: str) -> bool:
    stripped = value.strip()
    return bool(stripped) and stripped.count("|") >= 2


def _special_block_spans(text: str) -> list[TextSpan]:
    """Detect fenced code, Markdown tables and contiguous list blocks."""

    lines = _line_spans(text)
    blocks: list[TextSpan] = []
    block_id = 1
    index = 0
    while index < len(lines):
        start, _, value = lines[index]
        fence = _FENCE_START_RE.match(value)
        if fence:
            marker = fence.group("fence")
            end_index = index + 1
            closing_re = re.compile(
                rf"^[ \t]*{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$"
            )
            while end_index < len(lines):
                if closing_re.match(lines[end_index][2]):
                    end_index += 1
                    break
                end_index += 1
            blocks.append(
                TextSpan(
                    start,
                    lines[end_index - 1][1],
                    CHUNK_TYPE_CODE,
                    block_id,
                )
            )
            block_id += 1
            index = end_index
            continue

        if _is_table_line(value):
            end_index = index + 1
            while end_index < len(lines) and _is_table_line(lines[end_index][2]):
                end_index += 1
            table_lines = [line[2] for line in lines[index:end_index]]
            if len(table_lines) >= 2 and (
                any(_TABLE_SEPARATOR_RE.match(line) for line in table_lines)
                or all(line.count("|") >= 2 for line in table_lines)
            ):
                blocks.append(
                    TextSpan(
                        start,
                        lines[end_index - 1][1],
                        CHUNK_TYPE_TABLE,
                        block_id,
                    )
                )
                block_id += 1
                index = end_index
                continue

        if _LIST_ITEM_RE.match(value):
            list_start_index = index
            if index > 0:
                previous = lines[index - 1][2].strip()
                if previous.endswith((":", "：")):
                    list_start_index = index - 1
            end_index = index + 1
            item_count = 1
            while end_index < len(lines):
                candidate = lines[end_index][2]
                if _LIST_ITEM_RE.match(candidate):
                    item_count += 1
                    end_index += 1
                    continue
                if candidate.strip() and candidate[:1].isspace():
                    end_index += 1
                    continue
                break
            if item_count >= 1:
                blocks.append(
                    TextSpan(
                        lines[list_start_index][0],
                        lines[end_index - 1][1],
                        CHUNK_TYPE_LIST,
                        block_id,
                    )
                )
                block_id += 1
                index = end_index
                continue

        index += 1
    return blocks


# 按空行提取段落级文本片段。
def _paragraph_spans(text: str) -> List[TextSpan]:
    spans: List[TextSpan] = []
    pos = 0
    while pos < len(text):
        match = _BLANK_LINE_RE.search(text, pos)
        raw_end = match.start() if match else len(text)
        span = _trim_span(text, pos, raw_end)
        if span:
            spans.append(span)
        if not match:
            break
        pos = match.end()
    return spans


# 把超长片段继续拆到最大长度以内。
def _split_long_span(text: str, span: TextSpan, max_chars: int) -> List[TextSpan]:
    pieces: List[TextSpan] = []
    start = span.start
    while start < span.end:
        hard_end = min(start + max_chars, span.end)
        if hard_end >= span.end:
            piece = _trim_span(
                text,
                start,
                span.end,
                kind=span.kind,
                block_id=span.block_id,
            )
            if piece:
                pieces.append(piece)
            break

        search_start = start + max(max_chars // 2, 1)
        boundary = -1
        patterns = (
            (_SOFT_BREAK_RE, re.compile(r"\s+"))
            if span.kind != CHUNK_TYPE_PROSE
            else (_SENTENCE_END_RE, _SOFT_BREAK_RE, re.compile(r"\s+"))
        )
        for pattern in patterns:
            for match in pattern.finditer(text, search_start, hard_end):
                boundary = match.end()
            if boundary != -1:
                break
        if boundary <= start:
            boundary = hard_end

        piece = _trim_span(
            text,
            start,
            boundary,
            kind=span.kind,
            block_id=span.block_id,
        )
        if piece:
            pieces.append(piece)
        start = boundary
    return pieces


# 按句末标点或软换行拆分段落。
def _sentence_spans(text: str, paragraph: TextSpan, max_chars: int) -> List[TextSpan]:
    spans: List[TextSpan] = []
    start = paragraph.start
    matches = sorted(
        list(_SENTENCE_END_RE.finditer(text, paragraph.start, paragraph.end))
        + list(_SOFT_BREAK_RE.finditer(text, paragraph.start, paragraph.end)),
        key=lambda match: match.end(),
    )
    for match in matches:
        end = match.end()
        span = _trim_span(text, start, end)
        if span:
            spans.extend(_split_long_span(text, span, max_chars))
        start = end

    tail = _trim_span(text, start, paragraph.end)
    if tail:
        spans.extend(_split_long_span(text, tail, max_chars))
    return spans


# 构造语义优先的最小组合单元。
def _prose_semantic_spans(text: str, max_chars: int) -> List[TextSpan]:
    spans: List[TextSpan] = []
    for paragraph in _paragraph_spans(text):
        if paragraph.end - paragraph.start <= max_chars:
            spans.append(paragraph)
        else:
            spans.extend(_sentence_spans(text, paragraph, max_chars))
    return spans


def _semantic_spans(
    text: str,
    max_chars: int,
    *,
    prose_max_chars: int | None = None,
) -> List[TextSpan]:
    """Build semantic units while keeping structured blocks isolated."""

    special_blocks = _special_block_spans(text)
    prose_limit = max_chars if prose_max_chars is None else prose_max_chars
    if not special_blocks:
        return _prose_semantic_spans(text, prose_limit)

    spans: list[TextSpan] = []
    cursor = 0
    for block in special_blocks:
        if block.start > cursor:
            spans.extend(
                TextSpan(span.start + cursor, span.end + cursor)
                for span in _prose_semantic_spans(
                    text[cursor : block.start], prose_limit
                )
            )
        spans.extend(_split_long_span(text, block, max_chars))
        cursor = block.end
    if cursor < len(text):
        spans.extend(
            TextSpan(span.start + cursor, span.end + cursor)
            for span in _prose_semantic_spans(text[cursor:], prose_limit)
        )
    return sorted(spans, key=lambda span: (span.start, span.end))


# 在指定章节范围内构造全局字符坐标的语义单元。
def _section_semantic_spans(
    text: str, section: SectionSpan, max_chars: int
) -> List[TextSpan]:
    section_text = text[section.start : section.end]
    prose_limit = _adaptive_prose_limit(section_text, max_chars)
    return [
        TextSpan(
            section.start + span.start,
            section.start + span.end,
            span.kind,
            span.block_id,
        )
        for span in _semantic_spans(
            section_text,
            max_chars,
            prose_max_chars=prose_limit,
        )
    ]


# 查找下一个 chunk 的完整语义重叠起点。
def _find_overlap_start(
    units: List[TextSpan], start_idx: int, end_idx: int, overlap_chars: int
) -> int:
    # 从当前 chunk 末尾向左找 overlap 起点，优先复用完整语义单元。
    if overlap_chars <= 0 or end_idx <= start_idx + 1:
        return end_idx

    target = units[end_idx - 1].end - overlap_chars
    next_start = end_idx - 1
    while next_start > start_idx and units[next_start - 1].end > target:
        next_start -= 1
    if next_start <= start_idx:
        next_start = end_idx - 1
    return next_start


# 构造 chunk 前后的定位上下文。
def _build_context(
    text: str,
    start: int,
    end: int,
    context_chars: int,
    *,
    section_start: int = 0,
    section_end: int | None = None,
) -> str:
    if context_chars <= 0:
        return ""

    bounded_end = len(text) if section_end is None else section_end
    before = _context_before(text, start, context_chars, section_start)
    after = _context_after(text, end, context_chars, bounded_end)
    parts = []
    if before:
        parts.append(f"前文：{before}")
    if after:
        parts.append(f"后文：{after}")
    return "\n".join(parts)


# 截取 chunk 前方的上下文片段。
def _context_before(
    text: str, start: int, context_chars: int, lower_bound: int = 0
) -> str:
    snippet = text[max(lower_bound, start - context_chars) : start].strip()
    for match in _SENTENCE_END_RE.finditer(snippet):
        candidate = snippet[match.end() :].strip()
        if candidate:
            return candidate
    return snippet


# 截取 chunk 后方的上下文片段。
def _context_after(
    text: str, end: int, context_chars: int, upper_bound: int | None = None
) -> str:
    bounded_end = len(text) if upper_bound is None else upper_bound
    snippet = text[end : min(bounded_end, end + context_chars)].strip()
    boundary = -1
    for match in _SENTENCE_END_RE.finditer(snippet):
        boundary = match.end()
    if boundary > 0:
        return snippet[:boundary].strip()
    return snippet


def _adaptive_prose_limit(sample: str, hard_limit: int) -> int:
    if hard_limit <= 1:
        return max(1, hard_limit)
    meaningful = _MEANINGFUL_CHAR_RE.findall(sample)
    if not meaningful:
        return hard_limit
    cjk_ratio = len(_CJK_CHAR_RE.findall(sample)) / len(meaningful)
    # CJK text generally carries more information per character than spaced
    # alphabetic prose.  A smaller target improves retrieval granularity while
    # keeping the caller-provided size as an absolute ceiling.
    ratio = 0.85 if cjk_ratio >= 0.45 else 1.0
    return max(1, min(hard_limit, math.ceil(hard_limit * ratio)))


def _adaptive_chunk_limit(text: str, unit: TextSpan, hard_limit: int) -> int:
    """Adapt prose size to script density without violating the hard limit."""

    if unit.kind != CHUNK_TYPE_PROSE:
        return max(1, hard_limit)
    sample = text[unit.start : min(len(text), unit.start + hard_limit)]
    return _adaptive_prose_limit(sample, hard_limit)


def _chunk_quality(text: str) -> float:
    non_space = [char for char in text if not char.isspace()]
    if not non_space:
        return 0.0
    meaningful = len(_MEANINGFUL_CHAR_RE.findall(text)) / len(non_space)
    normalized_words = re.findall(r"[A-Za-z0-9]+|[\u3400-\u9fff]", text.casefold())
    diversity = (
        len(set(normalized_words)) / len(normalized_words) if normalized_words else 0.0
    )
    return round(min(1.0, 0.8 * meaningful + 0.2 * diversity), 6)


def _is_informative_chunk(text: str, *, structured_document: bool) -> bool:
    compact = _WHITESPACE_RE.sub("", text)
    if not compact or not _MEANINGFUL_CHAR_RE.search(compact):
        return False
    if structured_document:
        return True
    return len(text) > MIN_CHUNK_CHARS


def _dedupe_key(text: str, section: SectionSpan) -> tuple[int, str]:
    normalized = _WHITESPACE_RE.sub(" ", text).strip().casefold()
    # Deduplicate only inside one structural parent.  Identical clauses in
    # different sections can have different legal or procedural meaning.
    return section.ordinal, normalized


def summarize_chunks(chunks: List[RetrievedDoc]) -> ChunkingStats:
    sizes = [len(str(chunk.get("text") or "")) for chunk in chunks]
    type_counts = Counter(
        str(chunk.get("meta", {}).get("chunk_type") or CHUNK_TYPE_PROSE)
        for chunk in chunks
    )
    return ChunkingStats(
        chunk_count=len(chunks),
        char_count=sum(sizes),
        min_chunk_chars=min(sizes, default=0),
        max_chunk_chars=max(sizes, default=0),
        average_chunk_chars=(round(sum(sizes) / len(sizes), 2) if sizes else 0.0),
        chunk_type_counts=dict(sorted(type_counts.items())),
    )


def chunking_stats_dict(chunks: List[RetrievedDoc]) -> dict[str, Any]:
    stats = summarize_chunks(chunks)
    return {
        "chunk_count": stats.chunk_count,
        "char_count": stats.char_count,
        "min_chunk_chars": stats.min_chunk_chars,
        "max_chunk_chars": stats.max_chunk_chars,
        "average_chunk_chars": stats.average_chunk_chars,
        "chunk_type_counts": stats.chunk_type_counts,
    }


def _document_profile(text: str, units: List[TextSpan], *, structured: bool) -> str:
    kind_chars: Counter[str] = Counter()
    for unit in units:
        kind_chars[unit.kind] += unit.end - unit.start
    meaningful_chars = max(1, sum(kind_chars.values()))
    for kind in (CHUNK_TYPE_CODE, CHUNK_TYPE_TABLE, CHUNK_TYPE_LIST):
        if kind_chars[kind] / meaningful_chars >= 0.35:
            return kind
    if structured:
        return "structured"
    meaningful = _MEANINGFUL_CHAR_RE.findall(text)
    if meaningful and len(_CJK_CHAR_RE.findall(text)) / len(meaningful) >= 0.45:
        return "cjk_prose"
    return CHUNK_TYPE_PROSE


# 切分 paper。
def chunk_paper(
    parsed_pages: List[ParsedPage],
    source_sha256: str = "",
    chunk_char_size: int = DEFAULT_CHUNK_CHAR_SIZE,
    chunk_char_overlap: int = DEFAULT_CHUNK_CHAR_OVERLAP,
    chunk_context_chars: int = DEFAULT_CHUNK_CONTEXT_CHARS,
) -> List[RetrievedDoc]:
    if chunk_char_size <= 0:
        raise ValueError("chunk_char_size must be positive")
    if chunk_char_overlap < 0:
        raise ValueError("chunk_char_overlap must be non-negative")
    if chunk_context_chars < 0:
        raise ValueError("chunk_context_chars must be non-negative")
    if chunk_char_overlap >= chunk_char_size:
        raise ValueError("chunk_char_overlap must be smaller than chunk_char_size")

    if not parsed_pages:
        return []

    source_name = parsed_pages[0]["source"]
    document_id = build_document_id(source_name)
    if not source_sha256:
        raise ValueError("source_sha256 is required for stable chunk identity")

    global_text = ""

    page_starts: List[int] = []

    page_nums: List[int] = []

    current_idx = 0

    for page in parsed_pages:
        p_text = page["text"]

        if global_text and p_text:
            global_text += "\n\n"
            current_idx += 2

        page_starts.append(current_idx)
        page_nums.append(page["page"])

        global_text += p_text
        current_idx += len(p_text)

    chunks: List[RetrievedDoc] = []

    total_len = len(global_text)

    # local_chunk_index 只在单个 PDF 内递增，参与稳定 chunk_id。
    local_chunk_index = 0

    # 完成 find页码bypos 处理。
    def find_page_by_pos(pos: int) -> int:
        idx = bisect.bisect_right(page_starts, pos) - 1
        return page_nums[max(0, idx)]

    if total_len == 0:
        return []

    max_chars = max(1, chunk_char_size)
    sections = detect_section_spans(global_text)
    has_detected_structure = any(section.title for section in sections)
    semantic_units: List[TextSpan] = []
    unit_section_indexes: List[int] = []
    for section_index, section in enumerate(sections):
        section_units: list[TextSpan] = []
        for unit in _section_semantic_spans(global_text, section, max_chars):
            unit_text = global_text[unit.start : unit.end]
            if not _MEANINGFUL_CHAR_RE.search(unit_text):
                continue
            section_units.append(unit)
        semantic_units.extend(section_units)
        unit_section_indexes.extend([section_index] * len(section_units))
    document_profile = _document_profile(
        global_text,
        semantic_units,
        structured=has_detected_structure,
    )
    child_indexes: dict[int, int] = {}
    seen_chunks: set[tuple[int, str]] = set()
    unit_idx = 0

    while unit_idx < len(semantic_units):
        section_index = unit_section_indexes[unit_idx]
        section = sections[section_index]
        active_unit = semantic_units[unit_idx]
        chunk_start = semantic_units[unit_idx].start
        target_chars = _adaptive_chunk_limit(global_text, active_unit, max_chars)
        next_idx = unit_idx
        while (
            next_idx < len(semantic_units)
            and unit_section_indexes[next_idx] == section_index
        ):
            candidate_unit = semantic_units[next_idx]
            if active_unit.kind != candidate_unit.kind:
                break
            if (
                active_unit.kind != CHUNK_TYPE_PROSE
                and active_unit.block_id != candidate_unit.block_id
            ):
                break
            candidate_end = semantic_units[next_idx].end
            if next_idx > unit_idx and candidate_end - chunk_start > target_chars:
                break
            next_idx += 1

        if next_idx == unit_idx:
            next_idx += 1

        while (
            next_idx > unit_idx + 1
            and semantic_units[next_idx - 1].end - chunk_start > target_chars
        ):
            next_idx -= 1

        chunk_end = semantic_units[next_idx - 1].end
        chunk_text = global_text[chunk_start:chunk_end].strip()

        # 章节硬边界使短 section 无法再与相邻正文合并；结构化文档的非空
        # section/preamble 必须至少保留其内容。无结构文档继续沿用旧过滤规则。
        dedupe_key = _dedupe_key(chunk_text, section)
        is_duplicate = dedupe_key in seen_chunks
        if (
            _is_informative_chunk(
                chunk_text,
                structured_document=has_detected_structure,
            )
            and not is_duplicate
        ):
            p_start = find_page_by_pos(chunk_start)
            p_end = find_page_by_pos(chunk_end - 1)
            chunk_id = build_chunk_id(
                source_sha256, source_name, p_start, p_end, local_chunk_index
            )
            context = _build_context(
                global_text,
                chunk_start,
                chunk_end,
                chunk_context_chars,
                section_start=section.start,
                section_end=section.end,
            )

            meta = {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "source_sha256": source_sha256,
                "local_chunk_index": local_chunk_index,
                "chunk_index": local_chunk_index,
                "source": source_name,
                "page": p_start,
                "page_start": p_start,
                "page_end": p_end,
                "origin": "vector",
                "chunk_type": active_unit.kind,
                "document_profile": document_profile,
                "chunking_strategy_version": CHUNKING_STRATEGY_VERSION,
                "chunk_char_count": len(chunk_text),
                "chunk_quality_score": _chunk_quality(chunk_text),
            }
            first_page = parsed_pages[0]
            for source_key in (
                "source_id",
                "source_version_id",
                "media_type",
                "origin_uri",
                "connector_type",
            ):
                if first_page.get(source_key):
                    meta[source_key] = first_page[source_key]
            covered_pages = [
                page for page in parsed_pages if p_start <= int(page["page"]) <= p_end
            ]
            source_locations = [
                dict(page["location"])
                for page in covered_pages
                if isinstance(page.get("location"), dict)
            ]
            if source_locations:
                meta["source_locations"] = source_locations
                if len(source_locations) == 1:
                    meta["source_location"] = source_locations[0]
                    for location_key in (
                        "line_start",
                        "line_end",
                        "slide",
                        "sheet",
                        "cell_range",
                        "image",
                    ):
                        if location_key in source_locations[0]:
                            meta[location_key] = source_locations[0][location_key]
            extraction_methods = {
                str(page.get("extraction_method", "native")) for page in covered_pages
            }
            meta["extraction_method"] = (
                next(iter(extraction_methods))
                if len(extraction_methods) == 1
                else "mixed"
            )
            ocr_providers = {
                str(page.get("ocr_provider"))
                for page in covered_pages
                if page.get("ocr_provider") and page.get("extraction_method") == "ocr"
            }
            if ocr_providers:
                meta["ocr_provider"] = (
                    next(iter(ocr_providers)) if len(ocr_providers) == 1 else "mixed"
                )
            if context:
                meta["context"] = context
            if has_detected_structure:
                child_index = child_indexes.get(section_index, 0)
                meta.update(
                    {
                        "parent_chunk_id": build_parent_chunk_id(
                            source_sha256, source_name, section.ordinal
                        ),
                        "section_level": section.level,
                        "child_index_in_parent": child_index,
                        "parent_char_count": len(
                            global_text[section.start : section.end].strip()
                        ),
                    }
                )
                if section.title:
                    meta["section_title"] = section.title
                    meta["section_path"] = " > ".join(section.path)
                child_indexes[section_index] = child_index + 1

            chunks.append({"text": chunk_text, "meta": cast(DocMeta, meta)})
            seen_chunks.add(dedupe_key)

            local_chunk_index += 1

        if next_idx >= len(semantic_units):
            break

        # 章节边界是硬边界，重叠不能把前一章节内容带进后一章节 child。
        if unit_section_indexes[next_idx] != section_index:
            unit_idx = next_idx
            continue

        # 表格、代码、列表与正文之间也是硬边界；跨类型重叠会把完整正文
        # 再生成一次，或把结构块内容污染到相邻 prose child。
        next_unit = semantic_units[next_idx]
        if active_unit.kind != next_unit.kind or (
            active_unit.kind != CHUNK_TYPE_PROSE
            and active_unit.block_id != next_unit.block_id
        ):
            unit_idx = next_idx
            continue

        unit_idx = _find_overlap_start(
            semantic_units, unit_idx, next_idx, chunk_char_overlap
        )

    parent_counts = Counter(
        str(chunk.get("meta", {}).get("parent_chunk_id") or "") for chunk in chunks
    )
    for chunk in chunks:
        parent_chunk_id = str(chunk.get("meta", {}).get("parent_chunk_id") or "")
        if parent_chunk_id:
            chunk["meta"]["parent_child_count"] = parent_counts[parent_chunk_id]

    return chunks
