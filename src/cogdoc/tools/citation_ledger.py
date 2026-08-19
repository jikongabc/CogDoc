from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from cogdoc.graph.state import (
    CitationLedgerEntry,
    CitationOccurrence,
    EvidenceLedgerEntry,
    RetrievedDoc,
)


EVIDENCE_ID_MIN_WIDTH = 3
EVIDENCE_ID_PLACEHOLDER = "E000"
MAX_CITATION_TOKEN_CHARS = 160

_EVIDENCE_CITATION_RE = re.compile(r"\[(E[0-9]{3,})\]")
_SUSPICIOUS_EVIDENCE_TOKEN_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:e(?:[\W_]*i[\W_]*d)?|"
    r"evidence(?:[\W_]*i[\W_]*d)?)[\W_]*[0-9]",
    re.IGNORECASE,
)
_PAGE_CITATION_RE = re.compile(r"(?:^|[^A-Za-z0-9])P[\W_]*[0-9]+\s*$", re.IGNORECASE)
_LOCATION_CITATION_RE = re.compile(
    r"[^\]\r\n]{1,120}@(slide|sheet|lines|image|section|anchor|region)-",
    re.IGNORECASE,
)
_KNOWLEDGE_CITATION_RE = re.compile(r"^\s*knowledge(?:[\W_]+)\S+", re.IGNORECASE)


class CitationLedgerError(ValueError):
    """Raised when an internal evidence citation cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class RenderedCitationLedger:
    answer: str
    entries: tuple[CitationLedgerEntry, ...]


def format_evidence_id(index: int) -> str:
    """Return a response-scoped ID with three digits as the minimum width."""

    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("evidence ID index must be an integer")
    if index < 1:
        raise ValueError("evidence ID index must be positive")
    return f"E{index:0{EVIDENCE_ID_MIN_WIDTH}d}"


def is_valid_evidence_id(value: Any) -> bool:
    """Return whether a value is the canonical E001/E1000/... spelling."""

    if (
        not isinstance(value, str)
        or len(value) > MAX_CITATION_TOKEN_CHARS
        or re.fullmatch(r"E[0-9]{3,}", value) is None
    ):
        return False
    try:
        index = int(value[1:])
        return index > 0 and format_evidence_id(index) == value
    except (ValueError, OverflowError):
        return False


def citation_source_label(value: Any) -> str:
    """Escape filename characters that collide with public citation syntax."""

    return (
        str(value or "")
        .strip()
        .replace("%", "%25")
        .replace("[", "%5B")
        .replace("]", "%5D")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _non_negative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _location(value: Any) -> dict[str, Any]:
    """Return a bounded, JSON-safe source locator and discard unknown fields."""

    raw = _mapping(value)
    location: dict[str, Any] = {}
    for field in (
        "page_start",
        "page_end",
        "line_start",
        "line_end",
        "slide",
        "image",
    ):
        normalized = _non_negative_int(raw.get(field))
        if normalized is not None:
            location[field] = normalized
    for field in ("sheet", "cell_range", "anchor"):
        text = str(raw.get(field) or "").strip()
        if text:
            location[field] = text[:512]
    section_path = raw.get("section_path")
    if isinstance(section_path, Sequence) and not isinstance(
        section_path, (str, bytes)
    ):
        values = [str(item).strip()[:256] for item in section_path if str(item).strip()]
        if values:
            location["section_path"] = values[:32]
    elif raw.get("section"):
        location["section_path"] = [str(raw["section"]).strip()[:256]]
    bbox = raw.get("bbox")
    if (
        isinstance(bbox, Sequence)
        and not isinstance(bbox, (str, bytes))
        and len(bbox) == 4
    ):
        try:
            coordinates = [float(item) for item in bbox]
        except (TypeError, ValueError):
            coordinates = []
        if coordinates and all(abs(item) < 1e12 for item in coordinates):
            location["bbox"] = coordinates
    return location


def _locator_label(location: Mapping[str, Any]) -> str:
    page = _non_negative_int(location.get("page_start"))
    if page is not None:
        page_end = _non_negative_int(location.get("page_end"))
        return f"P{page}" if page_end in (None, page) else f"P{page}-{page_end}"
    slide = _non_negative_int(location.get("slide"))
    if slide is not None:
        return f"slide-{slide}"
    sheet = citation_source_label(location.get("sheet"))
    if sheet:
        cell_range = citation_source_label(location.get("cell_range"))
        return f"sheet-{sheet}" + (f"!{cell_range}" if cell_range else "")
    line_start = _non_negative_int(location.get("line_start"))
    if line_start is not None:
        line_end = _non_negative_int(location.get("line_end"))
        return f"lines-{line_start}" + (
            f"-{line_end}" if line_end not in (None, line_start) else ""
        )
    image = _non_negative_int(location.get("image"))
    if image is not None:
        return f"image-{image}"
    section_path = location.get("section_path")
    if isinstance(section_path, Sequence) and not isinstance(
        section_path, (str, bytes)
    ):
        section = citation_source_label("/".join(str(item) for item in section_path))
        if section:
            return f"section-{section}"
    anchor = citation_source_label(location.get("anchor"))
    if anchor:
        return f"anchor-{anchor}"
    if location.get("bbox"):
        return "region-bbox"
    return ""


def display_citation_for_entry(entry: Mapping[str, Any]) -> str:
    """Build the canonical public citation for legacy or universal sources."""

    source_type = str(entry.get("source_type") or "document")
    if source_type == "derived_knowledge":
        knowledge_id = str(entry.get("knowledge_id") or "").strip()
        return f"[knowledge:{knowledge_id}]" if knowledge_id else ""
    if source_type != "document":
        return ""
    source = citation_source_label(entry.get("source"))
    if not source:
        return ""
    locator = _locator_label(_location(entry.get("location")))
    if locator.startswith("P"):
        return f"[{source}:{locator}]"
    if locator:
        return f"[{source}@{locator}]"
    page = _non_negative_int(entry.get("page"))
    return f"[{source}:P{page}]" if page is not None else ""


def _document_location(meta: Mapping[str, Any]) -> dict[str, Any]:
    location = _location(meta.get("source_location") or meta.get("location"))
    if location:
        return location
    locations = meta.get("source_locations")
    if isinstance(locations, Sequence) and not isinstance(locations, (str, bytes)):
        normalized = [
            _location(item) for item in locations if isinstance(item, Mapping)
        ]
        normalized = [item for item in normalized if item]
        if normalized:
            merged = dict(normalized[0])
            last_page = normalized[-1].get("page_end") or normalized[-1].get(
                "page_start"
            )
            if "page_start" in merged and last_page is not None:
                merged["page_end"] = last_page
            return merged
    return {}


def _chunk_id(doc: Mapping[str, Any]) -> str:
    return str(_mapping(doc.get("meta")).get("chunk_id") or "").strip()


def evidence_id_for_doc(doc: Mapping[str, Any]) -> str:
    return str(_mapping(doc.get("retrieval")).get("evidence_id") or "").strip()


def _visible_view(doc: Mapping[str, Any]) -> tuple[str, int, int]:
    """Resolve offsets for the exact body emitted by ``render_evidence_block``."""

    raw_text = str(doc.get("text") or "")
    visible_text = raw_text.strip()
    leading_trim = len(raw_text) - len(raw_text.lstrip())
    retrieval = _mapping(doc.get("retrieval"))
    start = _non_negative_int(retrieval.get("evidence_text_start"))
    if start is None:
        start = 0
    start += leading_trim
    return visible_text, start, start + len(visible_text)


def _view_range(doc: Mapping[str, Any]) -> tuple[int, int]:
    _, start, end = _visible_view(doc)
    return start, end


def _identity(doc: Mapping[str, Any]) -> tuple[str, int, int]:
    chunk_id = _chunk_id(doc)
    if not chunk_id:
        raise CitationLedgerError("evidence document is missing a stable chunk_id")
    start, end = _view_range(doc)
    if end <= start:
        raise CitationLedgerError("evidence document has no visible text")
    return chunk_id, start, end


def _display_citation(doc: Mapping[str, Any]) -> str:
    meta = _mapping(doc.get("meta"))
    if meta.get("source_type") == "derived_knowledge":
        knowledge_id = str(meta.get("knowledge_id") or "").strip()
        if not knowledge_id:
            chunk_id = _chunk_id(doc)
            if chunk_id.startswith("knowledge:"):
                knowledge_id = chunk_id.split(":", 1)[1]
        if not knowledge_id:
            raise CitationLedgerError("derived evidence is missing knowledge_id")
        return f"[knowledge:{knowledge_id}]"

    display = display_citation_for_entry({**meta, "location": _document_location(meta)})
    if not display:
        raise CitationLedgerError("document evidence is missing source or location")
    return display


def _ledger_entry(doc: Mapping[str, Any], evidence_id: str) -> EvidenceLedgerEntry:
    meta = _mapping(doc.get("meta"))
    start, end = _view_range(doc)
    entry: EvidenceLedgerEntry = {
        "evidence_id": evidence_id,
        "chunk_id": _chunk_id(doc),
        "source_type": str(meta.get("source_type") or "document"),
        "source": str(meta.get("source") or ""),
        "span_start": start,
        "span_end": end,
        "display_citation": _display_citation(doc),
    }
    for field in ("source_id", "source_version_id", "media_type"):
        value = str(meta.get(field) or "").strip()
        if value:
            entry[field] = value
    location = _document_location(meta)
    if location:
        entry["location"] = location
    knowledge_id = str(meta.get("knowledge_id") or "").strip()
    if knowledge_id:
        entry["knowledge_id"] = knowledge_id
    page = _non_negative_int(meta.get("page"))
    page_start = _non_negative_int(meta.get("page_start"))
    page_end = _non_negative_int(meta.get("page_end"))
    if page is not None:
        entry["page"] = page
    if page_start is not None:
        entry["page_start"] = page_start
    if page_end is not None:
        entry["page_end"] = page_end
    return entry


def assign_evidence_ids(
    docs: Sequence[RetrievedDoc],
) -> tuple[list[RetrievedDoc], list[EvidenceLedgerEntry]]:
    """Copy documents and freeze stable IDs in their final presentation order.

    Exact duplicate views share an ID.  A different visible range of the same
    chunk receives a different ID, so claim auditing never has to infer a span
    from a page locator.
    """

    annotated: list[RetrievedDoc] = []
    identity_to_id: dict[tuple[str, int, int], str] = {}
    ledger: list[EvidenceLedgerEntry] = []
    for doc in docs:
        if not isinstance(doc, Mapping):
            raise CitationLedgerError("evidence document must be a mapping")
        identity = _identity(doc)
        evidence_id = identity_to_id.get(identity)
        if evidence_id is None:
            evidence_id = format_evidence_id(len(identity_to_id) + 1)
            identity_to_id[identity] = evidence_id
            ledger.append(_ledger_entry(doc, evidence_id))

        snapshot = cast(dict[str, Any], copy.deepcopy(doc))
        visible_text, visible_start, visible_end = _visible_view(doc)
        snapshot["text"] = visible_text
        retrieval = dict(_mapping(snapshot.get("retrieval")))
        retrieval["evidence_id"] = evidence_id
        retrieval["evidence_text_start"] = visible_start
        retrieval["evidence_text_end"] = visible_end
        snapshot["retrieval"] = retrieval
        annotated.append(cast(RetrievedDoc, snapshot))
    return annotated, ledger


def build_evidence_ledger(
    docs: Sequence[Mapping[str, Any]],
) -> list[EvidenceLedgerEntry]:
    """Build and validate a registry from documents that already carry EIDs."""

    by_id: dict[str, tuple[tuple[str, int, int], EvidenceLedgerEntry]] = {}
    identity_to_id: dict[tuple[str, int, int], str] = {}
    order: list[str] = []
    for doc in docs:
        evidence_id = evidence_id_for_doc(doc)
        if not is_valid_evidence_id(evidence_id):
            raise CitationLedgerError(
                "evidence document has no valid frozen evidence_id"
            )
        identity = _identity(doc)
        existing = by_id.get(evidence_id)
        if existing is not None and existing[0] != identity:
            raise CitationLedgerError(
                f"evidence_id {evidence_id} maps to multiple evidence views"
            )
        if existing is None:
            alias = identity_to_id.get(identity)
            if alias is not None:
                raise CitationLedgerError(
                    f"evidence view is mapped by both {alias} and {evidence_id}"
                )
            identity_to_id[identity] = evidence_id
            by_id[evidence_id] = (identity, _ledger_entry(doc, evidence_id))
            order.append(evidence_id)
    return [by_id[evidence_id][1] for evidence_id in order]


def extract_evidence_ids(answer: str) -> list[str]:
    return [
        evidence_id
        for match in _EVIDENCE_CITATION_RE.finditer(str(answer or ""))
        if is_valid_evidence_id(evidence_id := match.group(1))
    ]


def _strict_non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CitationLedgerError(
            f"ledger field {field} must be a non-negative integer"
        )
    return value


def _optional_non_negative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _strict_non_negative_int(value, field)


def _looks_like_physical_citation(token: str) -> bool:
    normalized = unicodedata.normalize("NFKC", token)
    return bool(
        _PAGE_CITATION_RE.search(normalized)
        or _LOCATION_CITATION_RE.search(normalized)
        or _KNOWLEDGE_CITATION_RE.search(normalized)
    )


def _looks_like_evidence_citation(token: str) -> bool:
    normalized = unicodedata.normalize("NFKC", token)
    return bool(_SUSPICIOUS_EVIDENCE_TOKEN_RE.search(normalized))


def _bracket_tokens(answer: str) -> list[tuple[str, str, bool]]:
    """Return bounded or unterminated ASCII/full-width bracket candidates."""

    tokens: list[tuple[str, str, bool]] = []
    cursor = 0
    while cursor < len(answer):
        if answer[cursor] not in "[［":
            cursor += 1
            continue
        close_positions = [
            position
            for closing in "]］"
            if (position := answer.find(closing, cursor + 1)) >= 0
        ]
        if close_positions:
            end = min(close_positions)
            tokens.append((answer[cursor : end + 1], answer[cursor + 1 : end], True))
            cursor = end + 1
            continue
        line_end = answer.find("\n", cursor + 1)
        end = len(answer) if line_end < 0 else line_end
        tokens.append((answer[cursor:end], answer[cursor + 1 : end], False))
        cursor = max(end, cursor + 1)
    return tokens


def _normalized_ledger_entry(raw: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CitationLedgerError(f"ledger entry {index} must be a mapping")
    evidence_id = raw.get("evidence_id")
    if not is_valid_evidence_id(evidence_id):
        raise CitationLedgerError(f"ledger entry {index} has an invalid evidence_id")
    chunk_id = str(raw.get("chunk_id") or "").strip()
    if not chunk_id:
        raise CitationLedgerError(f"ledger entry {index} is missing chunk_id")
    span_start = _strict_non_negative_int(raw.get("span_start"), "span_start")
    span_end = _strict_non_negative_int(raw.get("span_end"), "span_end")
    if span_end <= span_start:
        raise CitationLedgerError("ledger span_end must be greater than span_start")
    display_citation = raw.get("display_citation")
    if not isinstance(display_citation, str) or not display_citation:
        raise CitationLedgerError(f"ledger entry {index} is missing display_citation")
    source_type = str(raw.get("source_type") or "document")
    if source_type == "derived_knowledge":
        knowledge_id = str(raw.get("knowledge_id") or "").strip()
        expected_display = f"[knowledge:{knowledge_id}]" if knowledge_id else ""
    elif source_type == "document":
        expected_display = display_citation_for_entry(raw)
    else:
        expected_display = ""
    if not expected_display or display_citation != expected_display:
        raise CitationLedgerError(
            f"ledger entry {index} has an invalid display_citation"
        )

    entry = dict(raw)
    entry.update(
        {
            "evidence_id": evidence_id,
            "chunk_id": chunk_id,
            "span_start": span_start,
            "span_end": span_end,
            "display_citation": display_citation,
        }
    )
    for field in ("source_id", "source_version_id", "media_type"):
        value = str(raw.get(field) or "").strip()
        if value:
            entry[field] = value
        else:
            entry.pop(field, None)
    location = _location(raw.get("location"))
    if location:
        if not str(raw.get("source_version_id") or "").strip():
            raise CitationLedgerError(
                f"ledger entry {index} universal location is not version-pinned"
            )
        entry["location"] = location
    else:
        entry.pop("location", None)
    for field in ("page", "page_start", "page_end"):
        normalized = _optional_non_negative_int(raw.get(field), field)
        if normalized is None:
            entry.pop(field, None)
        else:
            entry[field] = normalized
    return entry


def _ledger_lookup_unchecked(
    ledger: Any,
) -> dict[str, dict[str, Any]]:
    if not isinstance(ledger, Sequence) or isinstance(ledger, (str, bytes, bytearray)):
        raise CitationLedgerError("evidence ledger must be a sequence")
    lookup: dict[str, dict[str, Any]] = {}
    identity_to_id: dict[tuple[str, int, int], str] = {}
    for index, raw in enumerate(ledger):
        entry = _normalized_ledger_entry(raw, index=index)
        evidence_id = str(entry["evidence_id"])
        if evidence_id in lookup:
            raise CitationLedgerError(f"ledger contains duplicate {evidence_id}")
        identity = (
            str(entry["chunk_id"]),
            int(entry["span_start"]),
            int(entry["span_end"]),
        )
        alias = identity_to_id.get(identity)
        if alias is not None:
            raise CitationLedgerError(
                f"evidence view is mapped by both {alias} and {evidence_id}"
            )
        identity_to_id[identity] = evidence_id
        lookup[evidence_id] = entry
    return lookup


def _ledger_lookup(ledger: Any) -> dict[str, dict[str, Any]]:
    try:
        return _ledger_lookup_unchecked(ledger)
    except CitationLedgerError:
        raise
    except Exception as exc:
        raise CitationLedgerError("evidence ledger is malformed") from exc


def validate_evidence_citations(
    answer: str,
    ledger: Sequence[Mapping[str, Any]],
    *,
    require_citation: bool = True,
) -> dict[str, Any]:
    """Strictly validate internal ASCII EID citations against one registry."""

    try:
        allowed = _ledger_lookup(ledger)
    except CitationLedgerError as exc:
        return {"is_valid": False, "critique": f"【证据引用账本无效】{exc}"}

    normalized_answer = str(answer or "")
    exact_ids = extract_evidence_ids(normalized_answer)
    malformed: list[str] = []
    physical: list[str] = []
    overlong: list[str] = []
    for whole, token, closed in _bracket_tokens(normalized_answer):
        if len(token) > MAX_CITATION_TOKEN_CHARS:
            if _looks_like_evidence_citation(token) or _looks_like_physical_citation(
                token
            ):
                overlong.append(whole[:80] + "…")
            continue
        canonical_match = _EVIDENCE_CITATION_RE.fullmatch(whole)
        if (
            closed
            and canonical_match is not None
            and is_valid_evidence_id(canonical_match.group(1))
        ):
            continue
        if _looks_like_evidence_citation(token):
            malformed.append(whole)
        elif _looks_like_physical_citation(token):
            physical.append(whole)

    if overlong:
        return {
            "is_valid": False,
            "critique": (
                "【证据引用未通过】回答包含超过引用标签长度上限的方括号内容："
                + "，".join(overlong)
            ),
            "evidence_ids": exact_ids,
        }
    if malformed:
        return {
            "is_valid": False,
            "critique": (
                "【证据引用未通过】Evidence ID 必须严格使用 [E001] 格式："
                + "，".join(malformed)
            ),
            "evidence_ids": exact_ids,
        }
    if physical:
        return {
            "is_valid": False,
            "critique": (
                "【证据引用未通过】内部生成阶段不得使用文件页码或 knowledge 引用；"
                "请改用对应的 [E001] Evidence ID：" + "，".join(physical)
            ),
            "evidence_ids": exact_ids,
        }
    invalid = list(dict.fromkeys(item for item in exact_ids if item not in allowed))
    if invalid:
        return {
            "is_valid": False,
            "critique": (
                "【证据引用未通过】引用了不在本次证据账本中的 Evidence ID："
                + "，".join(f"[{item}]" for item in invalid)
            ),
            "evidence_ids": exact_ids,
            "invalid_evidence_ids": invalid,
        }
    if require_citation and not exact_ids:
        return {
            "is_valid": False,
            "critique": (
                "【证据引用未通过】回答中没有 Evidence ID。"
                "每个来自证据的事实句都必须在句尾使用 [E001] 格式引用。"
            ),
            "evidence_ids": [],
        }
    return {"is_valid": True, "critique": "", "evidence_ids": exact_ids}


def render_display_citations(
    answer: str,
    ledger: Sequence[Mapping[str, Any]],
) -> RenderedCitationLedger:
    """Resolve exact EIDs to public locators and occurrence offsets once."""

    try:
        result = validate_evidence_citations(answer, ledger)
        if not result.get("is_valid"):
            raise CitationLedgerError(str(result.get("critique") or "invalid citation"))
        lookup = _ledger_lookup(ledger)
        pieces: list[str] = []
        output_length = 0
        cursor = 0
        occurrence_index = 0
        occurrences_by_id: dict[str, list[CitationOccurrence]] = {}
        for match in _EVIDENCE_CITATION_RE.finditer(str(answer or "")):
            evidence_id = match.group(1)
            if not is_valid_evidence_id(evidence_id):
                continue
            prefix = answer[cursor : match.start()]
            pieces.append(prefix)
            output_length += len(prefix)
            display = str(lookup[evidence_id]["display_citation"])
            start = output_length
            pieces.append(display)
            output_length += len(display)
            occurrences_by_id.setdefault(evidence_id, []).append(
                {
                    "index": occurrence_index,
                    "answer_start": start,
                    "answer_end": output_length,
                }
            )
            occurrence_index += 1
            cursor = match.end()
        pieces.append(answer[cursor:])

        public_entries: list[CitationLedgerEntry] = []
        for evidence_id, raw in lookup.items():
            occurrences = occurrences_by_id.get(evidence_id)
            if not occurrences:
                continue
            entry: CitationLedgerEntry = {
                "evidence_id": evidence_id,
                "chunk_id": str(raw["chunk_id"]),
                "source_type": str(raw.get("source_type") or "document"),
                "source": str(raw.get("source") or ""),
                "span_start": int(raw["span_start"]),
                "span_end": int(raw["span_end"]),
                "occurrences": occurrences,
            }
            if raw.get("source_id"):
                entry["source_id"] = str(raw["source_id"])
            if raw.get("source_version_id"):
                entry["source_version_id"] = str(raw["source_version_id"])
            if raw.get("media_type"):
                entry["media_type"] = str(raw["media_type"])
            if raw.get("location"):
                entry["location"] = dict(raw["location"])
            knowledge_id = str(raw.get("knowledge_id") or "").strip()
            if knowledge_id:
                entry["knowledge_id"] = knowledge_id
            for field in ("page", "page_start", "page_end"):
                if field in raw:
                    entry[field] = int(raw[field])
            public_entries.append(entry)
        return RenderedCitationLedger("".join(pieces), tuple(public_entries))
    except CitationLedgerError:
        raise
    except Exception as exc:
        raise CitationLedgerError("citation ledger rendering failed") from exc


def evidence_ids_for_docs(docs: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return unique frozen IDs in document order."""

    return list(
        dict.fromkeys(
            evidence_id for doc in docs if (evidence_id := evidence_id_for_doc(doc))
        )
    )


def ensure_evidence_ids(
    docs: Sequence[Mapping[str, Any]],
) -> tuple[list[RetrievedDoc], list[EvidenceLedgerEntry]]:
    """Preserve a complete frozen registry or assign one before prompt rendering."""

    if docs and all(evidence_id_for_doc(doc) for doc in docs):
        return cast(list[RetrievedDoc], list(docs)), build_evidence_ledger(docs)
    return assign_evidence_ids(cast(Sequence[RetrievedDoc], docs))
