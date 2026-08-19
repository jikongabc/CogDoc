from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cogdoc.tools.citation_ledger import (
    display_citation_for_entry,
    is_valid_evidence_id,
)

_DISPLAY_CITATION_RE = re.compile(
    r"\[(?:knowledge:[^\]\r\n]+|[^\]\r\n]+(?::P[0-9]+(?:-[0-9]+)?|"
    r"@(?:slide|sheet|lines|image|section|anchor|region)-[^\]\r\n]+))\]"
)
_CANONICAL_LIKE_EVIDENCE_ID_RE = re.compile(
    r"(?<![A-Za-z0-9])e[0-9]{3,}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_CANONICAL_EVIDENCE_TOKEN_RE = re.compile(
    r"\s*e[0-9]{3,}(?:\s*[,，;；]\s*e[0-9]{3,})*"
    r"\s*[.,;:!?。！？；：]?\s*\Z",
    re.IGNORECASE,
)
_INVALID_EID_PAGE_TOKEN_RE = re.compile(
    r"\s*e[0-9]{3,}\s*:\s*p\s*[0-9]+\s*[.,;!?。！？；]?\s*\Z",
    re.IGNORECASE,
)
_MALFORMED_EVIDENCE_ID_FRAGMENT = (
    r"(?:e(?:[\s:_-]*i[\s:_-]*d)?[\s:_-]+|"
    r"e[\s:_-]*i[\s:_-]*d[\s:_-]*|"
    r"evidence[\s:_-]*i[\s:_-]*d[\s:_-]*)[0-9]{3,}"
)
_MALFORMED_EVIDENCE_TOKEN_RE = re.compile(
    rf"\s*{_MALFORMED_EVIDENCE_ID_FRAGMENT}\s*[.,;:!?。！？；：]?\s*\Z",
    re.IGNORECASE,
)
_MALFORMED_EVIDENCE_TEXT_RE = re.compile(
    rf"(?<![A-Za-z0-9]){_MALFORMED_EVIDENCE_ID_FRAGMENT}(?![A-Za-z0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PublicCitationLedgerValidation:
    """Validation result for a public, already-rendered citation ledger.

    Answer offsets are Python/Unicode code-point offsets and use half-open ranges.
    ``occurrence_total`` is the larger of declared and visible citation counts so a
    missing or extra occurrence lowers the mapping rate instead of disappearing.
    """

    observable: bool
    is_valid: bool
    reason: str
    entries: tuple[Mapping[str, Any], ...]
    targets: tuple[dict[str, str], ...]
    occurrence_mapped: int
    occurrence_total: int
    physical_total: int


def _strict_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _meaningful(value: Any) -> bool:
    if value in (None, ""):
        return False
    if isinstance(value, Mapping | Sequence) and not isinstance(value, str | bytes):
        return bool(value)
    return True


def display_citation(entry: Mapping[str, Any]) -> str:
    return display_citation_for_entry(entry)


def public_citation_occurrences(answer: str) -> list[tuple[int, int, str]]:
    """Return exact public citation tokens with Unicode code-point offsets."""

    return [
        (match.start(), match.end(), match.group(0))
        for match in _DISPLAY_CITATION_RE.finditer(answer)
    ]


def contains_internal_evidence_reference(answer: str) -> bool:
    """Detect canonical or malformed internal EID tokens in a public answer."""

    # Only exact public syntax in the original answer is exempt.  Normalizing first
    # would turn full-width or unclosed lookalikes into something that the physical
    # citation grammar accepts, letting an internal EID bypass the release gate.
    public_citations_masked = _DISPLAY_CITATION_RE.sub(
        lambda match: " " * len(match.group(0)),
        answer,
    )
    normalized = unicodedata.normalize("NFKC", public_citations_masked)
    cursor = 0
    while cursor < len(normalized):
        opening = normalized.find("[", cursor)
        if opening < 0:
            return False
        closing = normalized.find("]", opening + 1)
        line_end = normalized.find("\n", opening + 1)
        if closing >= 0 and (line_end < 0 or closing < line_end):
            end = closing
            cursor = closing + 1
        else:
            end = len(normalized) if line_end < 0 else line_end
            cursor = max(end, opening + 1)
        token = normalized[opening + 1 : end]
        # Inspect the innermost token too; an extra opening bracket must not turn
        # ``[[E001]]`` into an undetected internal reference.
        token = token.rsplit("[", 1)[-1].strip()
        if (
            _CANONICAL_EVIDENCE_TOKEN_RE.fullmatch(token)
            or _INVALID_EID_PAGE_TOKEN_RE.fullmatch(token)
            or _MALFORMED_EVIDENCE_TOKEN_RE.fullmatch(token)
        ):
            return True
    return False


def contains_internal_evidence_identifier(text: str) -> bool:
    """Detect EIDs in untrusted diagnostic text, including unbracketed echoes.

    Public answers use the narrower bracket-aware detector above to avoid treating
    natural prose such as ``[Evidence 2024]`` as an internal citation. Diagnostic
    model reasons have no user-visible EID use case, so hiding a bare echo is safe.
    """

    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return bool(
        contains_internal_evidence_reference(normalized)
        or _CANONICAL_LIKE_EVIDENCE_ID_RE.search(normalized)
        or _MALFORMED_EVIDENCE_TEXT_RE.search(normalized)
    )


def _evidence_rows(value: Any) -> list[Mapping[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _expected_evidence_ids(evidence: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    top_level = evidence.get("evidence_id")
    if _meaningful(top_level):
        values.add(str(top_level))
    retrieval = evidence.get("retrieval")
    if isinstance(retrieval, Mapping) and _meaningful(retrieval.get("evidence_id")):
        values.add(str(retrieval["evidence_id"]))
    return values


def _evidence_matches(entry: Mapping[str, Any], evidence: Mapping[str, Any]) -> bool:
    if evidence.get("_metadata_conflict"):
        return False
    if (
        str(evidence.get("chunk_id") or "").strip()
        != str(entry.get("chunk_id") or "").strip()
    ):
        return False

    entry_source_type = str(entry.get("source_type") or "document")
    evidence_source_type = str(evidence.get("source_type") or "document")
    if entry_source_type != evidence_source_type:
        return False

    expected_ids = _expected_evidence_ids(evidence)
    if expected_ids and expected_ids != {str(entry.get("evidence_id") or "")}:
        return False

    for key in (
        "source",
        "source_id",
        "source_version_id",
        "media_type",
        "location",
        "knowledge_id",
        "page",
        "page_start",
        "page_end",
    ):
        expected = evidence.get(key)
        if _meaningful(expected) and entry.get(key) != expected:
            return False

    retrieval = evidence.get("retrieval")
    retrieval = retrieval if isinstance(retrieval, Mapping) else {}
    expected_start = retrieval.get("evidence_text_start")
    expected_end = retrieval.get("evidence_text_end")
    if not _meaningful(expected_start):
        expected_start = evidence.get("span_start")
    if not _meaningful(expected_end):
        expected_end = evidence.get("span_end")
    if _meaningful(expected_start) and entry.get("span_start") != expected_start:
        return False
    if _meaningful(expected_end) and entry.get("span_end") != expected_end:
        return False
    return True


def _target(evidence: Mapping[str, Any]) -> dict[str, str]:
    return {
        "chunk_id": str(evidence.get("chunk_id") or "").strip(),
        "source": str(evidence.get("source") or "").strip(),
        "source_type": str(evidence.get("source_type") or "document").strip(),
    }


def validate_public_citation_ledger(
    answer: Any,
    ledger: Any,
    *,
    evidence: Any = None,
    ledger_present: bool = True,
    require_evidence: bool = False,
) -> PublicCitationLedgerValidation:
    """Validate one public citation ledger as an all-or-nothing table.

    When evidence is supplied, every ledger entry must bind to one evidence row.
    Available EID, source/page and visible-span coordinates are compared exactly.
    """

    answer_text = answer if isinstance(answer, str) else ""
    physical = public_citation_occurrences(answer_text)
    if not ledger_present:
        return PublicCitationLedgerValidation(
            observable=False,
            is_valid=not physical,
            reason="citation_ledger_missing" if physical else "",
            entries=(),
            targets=(),
            occurrence_mapped=0,
            occurrence_total=len(physical),
            physical_total=len(physical),
        )
    if not isinstance(answer, str):
        return PublicCitationLedgerValidation(
            observable=True,
            is_valid=False,
            reason="answer_not_string",
            entries=(),
            targets=(),
            occurrence_mapped=0,
            occurrence_total=len(physical),
            physical_total=len(physical),
        )
    if not isinstance(ledger, list):
        return PublicCitationLedgerValidation(
            observable=True,
            is_valid=False,
            reason="citation_ledger_not_list",
            entries=(),
            targets=(),
            occurrence_mapped=0,
            occurrence_total=len(physical),
            physical_total=len(physical),
        )

    evidence_rows = _evidence_rows(evidence)
    errors: list[str] = []
    if contains_internal_evidence_reference(answer_text):
        errors.append("internal_evidence_reference_exposed")
    if evidence is not None and evidence_rows == [] and ledger:
        errors.append("evidence_not_available")
    if require_evidence and ledger and not evidence_rows:
        errors.append("evidence_required")

    entries: list[Mapping[str, Any]] = []
    declared: list[tuple[int, int, int, str]] = []
    declared_count = 0
    mapped_count = 0
    evidence_ids: set[str] = set()
    identities: set[tuple[str, int, int]] = set()
    targets: list[dict[str, str]] = []
    seen_target_chunks: set[str] = set()

    for raw_entry in ledger:
        if not isinstance(raw_entry, Mapping):
            errors.append("ledger_entry_not_mapping")
            continue
        entry = raw_entry
        entries.append(entry)
        raw_evidence_id = entry.get("evidence_id")
        evidence_id = str(raw_evidence_id or "")
        if not is_valid_evidence_id(raw_evidence_id) or evidence_id in evidence_ids:
            errors.append("invalid_or_duplicate_evidence_id")
        evidence_ids.add(evidence_id)

        chunk_id = str(entry.get("chunk_id") or "").strip()
        if not chunk_id:
            errors.append("missing_chunk_id")
        span_start = _strict_nonnegative_int(entry.get("span_start"))
        span_end = _strict_nonnegative_int(entry.get("span_end"))
        if span_start is None or span_end is None or span_end <= span_start:
            errors.append("invalid_span")
        elif chunk_id:
            identity = (chunk_id, span_start, span_end)
            if identity in identities:
                errors.append("duplicate_evidence_view")
            identities.add(identity)

        page_start = entry.get("page_start")
        page_end = entry.get("page_end")
        normalized_page_start = _strict_nonnegative_int(page_start)
        normalized_page_end = _strict_nonnegative_int(page_end)
        if page_start is not None and normalized_page_start is None:
            errors.append("invalid_page_start")
        if page_end is not None and normalized_page_end is None:
            errors.append("invalid_page_end")
        if (
            normalized_page_start is not None
            and normalized_page_end is not None
            and normalized_page_end < normalized_page_start
        ):
            errors.append("invalid_page_range")

        display = display_citation(entry)
        if not display:
            errors.append("invalid_display_citation")
        if (
            entry.get("location")
            and not str(entry.get("source_version_id") or "").strip()
        ):
            errors.append("unversioned_source_location")

        matched_evidence: Mapping[str, Any] | None = None
        if evidence_rows is not None:
            matched_evidence = next(
                (row for row in evidence_rows if _evidence_matches(entry, row)),
                None,
            )
            if matched_evidence is None:
                errors.append("evidence_binding_failed")
            else:
                target = _target(matched_evidence)
                if target["chunk_id"] not in seen_target_chunks:
                    seen_target_chunks.add(target["chunk_id"])
                    targets.append(target)

        occurrences = entry.get("occurrences")
        if not isinstance(occurrences, list) or not occurrences:
            errors.append("occurrences_missing")
            continue
        for occurrence in occurrences:
            declared_count += 1
            if not isinstance(occurrence, Mapping):
                errors.append("occurrence_not_mapping")
                continue
            index = _strict_nonnegative_int(occurrence.get("index"))
            start = _strict_nonnegative_int(occurrence.get("answer_start"))
            end = _strict_nonnegative_int(occurrence.get("answer_end"))
            if (
                index is None
                or start is None
                or end is None
                or end <= start
                or end > len(answer_text)
            ):
                errors.append("invalid_occurrence_range")
                continue
            declared.append((start, end, index, display))
            if display and answer_text[start:end] == display:
                mapped_count += 1
            else:
                errors.append("occurrence_slice_mismatch")

    ordered = sorted(declared, key=lambda item: (item[0], item[1], item[2], item[3]))
    if [item[2] for item in ordered] != list(range(len(ordered))):
        errors.append("occurrence_indices_not_unique_contiguous")
    if any(
        current[0] < previous[1]
        for previous, current in zip(ordered, ordered[1:], strict=False)
    ):
        errors.append("occurrences_overlap")
    declared_physical = [(start, end, display) for start, end, _, display in ordered]
    if declared_physical != physical:
        errors.append("physical_occurrence_coverage_mismatch")

    occurrence_total = max(declared_count, len(physical))
    return PublicCitationLedgerValidation(
        observable=True,
        is_valid=not errors,
        reason=errors[0] if errors else "",
        entries=tuple(entries),
        targets=tuple(targets),
        occurrence_mapped=mapped_count,
        occurrence_total=occurrence_total,
        physical_total=len(physical),
    )
