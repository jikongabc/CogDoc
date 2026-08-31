from __future__ import annotations

import copy
import logging
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Any, cast

from cogdoc.graph.state import RetrievedDoc
from cogdoc.tools.rust_core_loader import ensure_rust_core
from cogdoc.tools.tokenizer import tokenize_mixed_text


logger = logging.getLogger(__name__)
_rust_core: ModuleType | None = None
_rust_core_lock = threading.Lock()


def _get_rust_core() -> ModuleType:
    global _rust_core
    if _rust_core is None:
        with _rust_core_lock:
            if _rust_core is None:
                _rust_core = ensure_rust_core("select_evidence_span_native")
    assert _rust_core is not None
    return _rust_core

REASON_WITHIN_BUDGET = "within_budget"
REASON_QUERY_SPAN = "query_span"
REASON_LONG_SENTENCE_WINDOW = "long_sentence_window"
REASON_FALLBACK_NO_TERMS = "fallback_no_terms"
REASON_FALLBACK_NO_MATCH = "fallback_no_match"

_PACK_SOURCE_TEXT_KEY = "_evidence_source_text"
_PACK_SOURCE_START_KEY = "_evidence_source_start"
_PACK_SOURCE_END_KEY = "_evidence_source_end"
_PACK_SOURCE_OVERLAP_KEY = "_evidence_source_overlap_chars"
_PACK_SOURCE_KEYS = (
    _PACK_SOURCE_TEXT_KEY,
    _PACK_SOURCE_START_KEY,
    _PACK_SOURCE_END_KEY,
    _PACK_SOURCE_OVERLAP_KEY,
)

# Evidence Pack deliberately does not know these keys.  They let a later
# adaptive round select a different span from the locally available source,
# without allowing a pack/repack operation to restore text outside the span.
_SPAN_SOURCE_TEXT_KEY = "_evidence_span_source_text"
_SPAN_SOURCE_START_KEY = "_evidence_span_source_start"
_SPAN_SOURCE_END_KEY = "_evidence_span_source_end"
_SPAN_SOURCE_OVERLAP_KEY = "_evidence_span_source_overlap_chars"

_REQUIREMENT_TEXT_KEYS = (
    "question",
    "retrieval_query",
    "recovery_query",
    "text",
)
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[_.-][A-Za-z0-9]+)*")
_CJK_CHAR_RE = re.compile(r"[\u3400-\u9fff]")


@dataclass(frozen=True, slots=True)
class EvidenceSpanBatch:
    """Isolated, query-aware document views plus aggregate compression data."""

    docs: tuple[RetrievedDoc, ...]
    input_count: int
    compressed_count: int
    fallback_count: int
    input_chars: int
    selected_chars: int
    reason_counts: dict[str, int]

    @property
    def output_count(self) -> int:
        return len(self.docs)


@dataclass(frozen=True, slots=True)
class _SourceView:
    text: str
    start: int
    end: int
    overlap_chars: int


@dataclass(frozen=True, slots=True)
class _RequirementTerms:
    requirement_id: str
    terms: tuple[str, ...]
    match_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Selection:
    start: int
    end: int
    score: float
    matched_terms: tuple[str, ...]
    reason: str
    fallback: bool = False


def _non_negative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _source_view(doc: Mapping[str, Any]) -> _SourceView:
    """Return the fullest locally available view and its ultimate offsets.

    Span-private text wins across adaptive rounds.  Pack-private text is only
    promoted when receiving an older packed document for the first time.  It
    is removed from every output snapshot, so Evidence Pack cannot restore the
    full text after this selector has established the evidence boundary.
    """

    span_text = doc.get(_SPAN_SOURCE_TEXT_KEY)
    pack_text = doc.get(_PACK_SOURCE_TEXT_KEY)
    retrieval = _mapping(doc.get("retrieval"))
    if isinstance(span_text, str):
        text = span_text
        start = _non_negative_int(doc.get(_SPAN_SOURCE_START_KEY)) or 0
        stored_end = _non_negative_int(doc.get(_SPAN_SOURCE_END_KEY))
        overlap = _non_negative_int(doc.get(_SPAN_SOURCE_OVERLAP_KEY)) or 0
    elif isinstance(pack_text, str):
        text = pack_text
        start = _non_negative_int(doc.get(_PACK_SOURCE_START_KEY)) or 0
        stored_end = _non_negative_int(doc.get(_PACK_SOURCE_END_KEY))
        overlap = _non_negative_int(doc.get(_PACK_SOURCE_OVERLAP_KEY)) or 0
    else:
        text = str(doc.get("text") or "")
        start = _non_negative_int(retrieval.get("evidence_text_start")) or 0
        stored_end = _non_negative_int(retrieval.get("evidence_text_end"))
        overlap = (
            _non_negative_int(retrieval.get("evidence_trimmed_overlap_chars")) or 0
        )
    minimum_end = start + len(text)
    if stored_end is not None and stored_end != minimum_end:
        meta = _mapping(doc.get("meta"))
        logger.warning(
            "evidence_span_source_end_mismatch chunk_id=%s stored_end=%s "
            "text_derived_end=%s",
            str(meta.get("chunk_id") or ""),
            stored_end,
            minimum_end,
        )
    end = stored_end if stored_end == minimum_end else minimum_end
    return _SourceView(text=text, start=start, end=end, overlap_chars=overlap)


def _stable_tokens(text: str) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokenize_mixed_text(text):
        normalized = str(token).strip().casefold()
        if normalized and normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return tuple(ordered)


def _requirement_text(requirement: Mapping[str, Any] | str) -> str:
    if isinstance(requirement, str):
        return requirement
    return " ".join(
        str(requirement.get(key) or "").strip()
        for key in _REQUIREMENT_TEXT_KEYS
        if str(requirement.get(key) or "").strip()
    )


def _fallback_entity_terms(text: str) -> tuple[str, ...]:
    """Keep exact single-character entities discarded by the corpus tokenizer.

    These terms are only used when two or more requirements have no distinctive
    word-level token.  Cross-requirement uniqueness removes shared function
    characters, while retaining labels such as ``A``/``B`` or ``甲``/``乙``.
    """

    seen: set[str] = set()
    ordered: list[str] = []
    for match in _WORD_RE.finditer(text):
        value = match.group(0).casefold()
        if len(value) == 1 and value not in seen:
            seen.add(value)
            ordered.append(value)
    for value in _CJK_CHAR_RE.findall(text):
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


def _term_plan(
    query: str,
    evidence_requirements: Sequence[Mapping[str, Any] | str],
) -> tuple[tuple[str, ...], tuple[_RequirementTerms, ...]]:
    query_terms = _stable_tokens(query)
    raw_requirements: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    for requirement in evidence_requirements:
        text = _requirement_text(requirement)
        terms = _stable_tokens(text)
        entity_terms = _fallback_entity_terms(text)
        if not terms and not entity_terms:
            continue
        requirement_id = (
            str(requirement.get("requirement_id") or "").strip()
            if isinstance(requirement, Mapping)
            else ""
        )
        raw_requirements.append((requirement_id, terms, entity_terms))
    term_requirement_counts: dict[str, int] = {}
    entity_requirement_counts: dict[str, int] = {}
    for _, terms, entity_terms in raw_requirements:
        for term in set(terms):
            term_requirement_counts[term] = term_requirement_counts.get(term, 0) + 1
        for term in set(entity_terms):
            entity_requirement_counts[term] = entity_requirement_counts.get(term, 0) + 1
    requirements = tuple(
        _RequirementTerms(
            requirement_id=requirement_id,
            terms=terms,
            match_terms=(
                distinctive
                if (
                    distinctive := tuple(
                        term
                        for term in terms
                        if term_requirement_counts.get(term, 0) == 1
                    )
                )
                else tuple(
                    term
                    for term in entity_terms
                    if entity_requirement_counts.get(term, 0) == 1
                )
            ),
        )
        for requirement_id, terms, entity_terms in raw_requirements
    )
    return query_terms, requirements


def _target_terms(
    query_terms: tuple[str, ...], requirements: Sequence[_RequirementTerms]
) -> tuple[str, ...]:
    ordered = list(query_terms)
    seen = set(ordered)
    for requirement in requirements:
        for term in (*requirement.match_terms, *requirement.terms):
            if term not in seen:
                seen.add(term)
                ordered.append(term)
    return tuple(ordered)


def _stable_ids(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    seen: set[str] = set()
    ordered: list[str] = []
    for item in value:
        normalized = str(item).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return tuple(ordered)


def _active_requirements(
    doc: Mapping[str, Any],
    requirements: tuple[_RequirementTerms, ...],
    matched_requirement_ids: Sequence[str] | None,
) -> tuple[tuple[_RequirementTerms, ...], tuple[str, ...] | None]:
    if matched_requirement_ids is not None:
        selected_ids = _stable_ids(matched_requirement_ids)
        has_attribution = True
    else:
        retrieval = _mapping(doc.get("retrieval"))
        has_attribution = "matched_requirement_ids" in retrieval
        selected_ids = _stable_ids(retrieval.get("matched_requirement_ids"))
    if not has_attribution:
        return requirements, None
    selected = set(selected_ids)
    # Requirements without a stable ID cannot participate in ID attribution,
    # but remain useful for callers that supplied free-form requirement strings.
    return (
        tuple(
            requirement
            for requirement in requirements
            if not requirement.requirement_id or requirement.requirement_id in selected
        ),
        selected_ids,
    )


def _materialize_doc(
    doc: RetrievedDoc,
    source: _SourceView,
    selection: _Selection,
    requirements: Sequence[_RequirementTerms],
    attributed_requirement_ids: tuple[str, ...] | None,
) -> RetrievedDoc:
    snapshot = cast(dict[str, Any], copy.deepcopy(doc))
    for key in _PACK_SOURCE_KEYS:
        snapshot.pop(key, None)
    raw_meta = snapshot.get("meta")
    if isinstance(raw_meta, Mapping):
        meta = dict(raw_meta)
        # ``context`` may contain facts outside the selected body span, while
        # section_path is only a locator.  Keeping context would make the
        # generator's rendered evidence broader than the verifier's closure.
        meta.pop("context", None)
        snapshot["meta"] = meta
    snapshot["text"] = source.text[selection.start : selection.end]
    snapshot[_SPAN_SOURCE_TEXT_KEY] = source.text
    snapshot[_SPAN_SOURCE_START_KEY] = source.start
    snapshot[_SPAN_SOURCE_END_KEY] = source.end
    snapshot[_SPAN_SOURCE_OVERLAP_KEY] = source.overlap_chars
    retrieval = dict(_mapping(snapshot.get("retrieval")))
    ultimate_start = source.start + selection.start
    ultimate_end = source.start + selection.end
    selected_tokens = set(selection.matched_terms)
    detected_requirement_ids = [
        requirement.requirement_id
        for requirement in requirements
        if requirement.requirement_id
        and selected_tokens.intersection(requirement.match_terms)
    ]
    compressed = selection.end - selection.start < len(source.text)
    matched_requirement_ids = (
        detected_requirement_ids
        if compressed or attributed_requirement_ids is None
        else list(attributed_requirement_ids)
    )
    retrieval.update(
        {
            "evidence_span_selected": compressed,
            "evidence_span_input_start": source.start,
            "evidence_span_input_end": source.start + len(source.text),
            "evidence_span_start": ultimate_start,
            "evidence_span_end": ultimate_end,
            "evidence_span_original_chars": len(source.text),
            "evidence_span_selected_chars": selection.end - selection.start,
            "evidence_span_score": selection.score,
            "evidence_span_matched_terms": list(selection.matched_terms),
            "evidence_span_matched_requirement_ids": matched_requirement_ids,
            "evidence_span_reason": selection.reason,
            "evidence_text_start": ultimate_start,
            "evidence_text_end": ultimate_end,
            "evidence_trimmed_overlap_chars": 0,
        }
    )
    if compressed or attributed_requirement_ids is not None:
        # A compressed view must never retain requirement attribution supported
        # only by text outside the selected span.
        retrieval["matched_requirement_ids"] = matched_requirement_ids
    snapshot["retrieval"] = retrieval
    return cast(RetrievedDoc, snapshot)


class EvidenceSpanSelector:
    """Reusable selector with one tokenized query/requirement plan."""

    def __init__(
        self,
        *,
        query: str,
        evidence_requirements: Sequence[Mapping[str, Any] | str] = (),
        max_chars_per_doc: int,
        context_sentences: int = 1,
    ) -> None:
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        if isinstance(evidence_requirements, (str, bytes)) or not isinstance(
            evidence_requirements, Sequence
        ):
            raise ValueError("evidence_requirements must be a sequence")
        if (
            isinstance(max_chars_per_doc, bool)
            or not isinstance(max_chars_per_doc, int)
            or max_chars_per_doc < 1
        ):
            raise ValueError("max_chars_per_doc must be a positive integer")
        if (
            isinstance(context_sentences, bool)
            or not isinstance(context_sentences, int)
            or context_sentences < 0
        ):
            raise ValueError("context_sentences must be a non-negative integer")
        for requirement in evidence_requirements:
            if not isinstance(requirement, (str, Mapping)):
                raise ValueError(
                    "each evidence requirement must be a mapping or string"
                )

        self.query = query
        self.max_chars_per_doc = max_chars_per_doc
        self.context_sentences = context_sentences
        self._query_terms, self._requirements = _term_plan(query, evidence_requirements)

    def _select_with_details(
        self,
        doc: RetrievedDoc,
        *,
        matched_requirement_ids: Sequence[str] | None = None,
    ) -> tuple[RetrievedDoc, _SourceView, _Selection]:
        if isinstance(matched_requirement_ids, (str, bytes)):
            raise ValueError("matched_requirement_ids must be a sequence of strings")
        requirements, attributed_requirement_ids = _active_requirements(
            doc, self._requirements, matched_requirement_ids
        )
        requirement_terms = tuple(
            requirement.match_terms for requirement in requirements
        )
        target_terms = _target_terms(self._query_terms, requirements)
        source = _source_view(doc)
        native_selection = _get_rust_core().select_evidence_span_native(
            source.text,
            list(self._query_terms),
            [list(terms) for terms in requirement_terms],
            list(target_terms),
            self.max_chars_per_doc,
            self.context_sentences,
        )
        selection = _Selection(
            start=int(native_selection["start"]),
            end=int(native_selection["end"]),
            score=float(native_selection["score"]),
            matched_terms=tuple(str(term) for term in native_selection["matched_terms"]),
            reason=str(native_selection["reason"]),
            fallback=bool(native_selection["fallback"]),
        )
        return (
            _materialize_doc(
                doc,
                source,
                selection,
                requirements,
                attributed_requirement_ids,
            ),
            source,
            selection,
        )

    def select(
        self,
        doc: RetrievedDoc,
        *,
        matched_requirement_ids: Sequence[str] | None = None,
    ) -> RetrievedDoc:
        """Return an isolated verbatim span for one canonical document."""

        snapshot, _, _ = self._select_with_details(
            doc, matched_requirement_ids=matched_requirement_ids
        )
        return snapshot

    def select_many(self, docs: Sequence[RetrievedDoc]) -> EvidenceSpanBatch:
        """Select documents and aggregate body-character compression metrics."""

        output: list[RetrievedDoc] = []
        compressed_count = 0
        fallback_count = 0
        input_chars = 0
        selected_chars = 0
        reason_counts: dict[str, int] = {}
        for doc in docs:
            snapshot, source, selection = self._select_with_details(doc)
            output.append(snapshot)
            input_chars += len(source.text)
            selected_chars += selection.end - selection.start
            compressed_count += selection.end - selection.start < len(source.text)
            fallback_count += selection.fallback
            reason_counts[selection.reason] = reason_counts.get(selection.reason, 0) + 1
        return EvidenceSpanBatch(
            docs=tuple(output),
            input_count=len(docs),
            compressed_count=compressed_count,
            fallback_count=fallback_count,
            input_chars=input_chars,
            selected_chars=selected_chars,
            reason_counts=reason_counts,
        )


def select_evidence_span(
    doc: RetrievedDoc,
    *,
    query: str,
    evidence_requirements: Sequence[Mapping[str, Any] | str] = (),
    matched_requirement_ids: Sequence[str] | None = None,
    max_chars_per_doc: int,
    context_sentences: int = 1,
) -> RetrievedDoc:
    """Convenience wrapper for selecting one canonical document."""

    selector = EvidenceSpanSelector(
        query=query,
        evidence_requirements=evidence_requirements,
        max_chars_per_doc=max_chars_per_doc,
        context_sentences=context_sentences,
    )
    return selector.select(doc, matched_requirement_ids=matched_requirement_ids)


def select_evidence_spans(
    docs: Sequence[RetrievedDoc],
    *,
    query: str,
    evidence_requirements: Sequence[Mapping[str, Any] | str] = (),
    max_chars_per_doc: int,
    context_sentences: int = 1,
) -> EvidenceSpanBatch:
    """Select exact spans, failing open to full text when matching is unsafe."""

    selector = EvidenceSpanSelector(
        query=query,
        evidence_requirements=evidence_requirements,
        max_chars_per_doc=max_chars_per_doc,
        context_sentences=context_sentences,
    )
    return selector.select_many(docs)
