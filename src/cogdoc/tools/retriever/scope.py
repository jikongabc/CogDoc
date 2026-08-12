from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any


class RetrievalAccessMode(str, Enum):
    """Whether a retrieval request may see all, some, or no source documents."""

    ALL = "all"
    SUBSET = "subset"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """Task-independent retrieval boundaries applied before channel top-k.

    ``access_mode`` is deliberately explicit: security callers must be able to
    represent an empty authorization result without accidentally turning it
    into whole-KB access.  For source compatibility, passing a non-empty
    ``allowed_sources`` value promotes the default ``ALL`` mode to ``SUBSET``.
    Derived knowledge is an independent channel switch and, when source-scoped,
    is matched through its ``related_source`` binding rather than its synthetic
    ``knowledge:*`` source.
    """

    allowed_sources: tuple[str, ...] = ()
    include_derived_knowledge: bool = True
    access_mode: RetrievalAccessMode = RetrievalAccessMode.ALL

    def __post_init__(self) -> None:
        raw_sources: Sequence[Any] = self.allowed_sources
        if isinstance(raw_sources, (str, bytes, bytearray)):
            raise TypeError("allowed_sources must be a sequence of source names")
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_source in raw_sources:
            # Source names are document identities and therefore use exact
            # spelling.  Do not case-fold or trim a valid filesystem name.
            source = "" if raw_source is None else str(raw_source)
            if source and source not in seen:
                seen.add(source)
                normalized.append(source)
        if not isinstance(self.include_derived_knowledge, bool):
            raise TypeError("include_derived_knowledge must be a boolean")
        try:
            mode = (
                self.access_mode
                if isinstance(self.access_mode, RetrievalAccessMode)
                else RetrievalAccessMode(self.access_mode)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported retrieval access mode: {self.access_mode!r}") from exc
        if normalized and mode is RetrievalAccessMode.ALL:
            mode = RetrievalAccessMode.SUBSET
        if mode is RetrievalAccessMode.SUBSET and not normalized:
            raise ValueError("subset retrieval access requires at least one source")
        if mode is RetrievalAccessMode.DENY and normalized:
            raise ValueError("deny retrieval access cannot include allowed sources")
        object.__setattr__(self, "allowed_sources", tuple(normalized))
        object.__setattr__(self, "access_mode", mode)

    @classmethod
    def deny(cls) -> "RetrievalScope":
        return cls(
            include_derived_knowledge=False,
            access_mode=RetrievalAccessMode.DENY,
        )

    @property
    def denies_all(self) -> bool:
        return self.access_mode is RetrievalAccessMode.DENY

    @property
    def allows_all_sources(self) -> bool:
        return self.access_mode is RetrievalAccessMode.ALL

    @property
    def is_source_restricted(self) -> bool:
        return self.access_mode is RetrievalAccessMode.SUBSET

    def allows_source(self, source: Any) -> bool:
        if self.denies_all:
            return False
        if self.allows_all_sources:
            return True
        normalized = "" if source is None else str(source)
        return normalized in self.allowed_sources

    def allows_document(self, doc: Mapping[str, Any]) -> bool:
        meta_value = doc.get("meta")
        meta = meta_value if isinstance(meta_value, Mapping) else {}
        if meta.get("source_type") == "derived_knowledge":
            return self.include_derived_knowledge and self.allows_source(
                meta.get("related_source")
            )
        return self.allows_source(meta.get("source"))

    def intersect(self, other: "RetrievalScope") -> "RetrievalScope":
        """Return the fail-closed intersection of task and authorization scopes."""

        if not isinstance(other, RetrievalScope):
            raise TypeError("other must be a RetrievalScope")
        include_derived = (
            self.include_derived_knowledge and other.include_derived_knowledge
        )
        if self.denies_all or other.denies_all:
            return RetrievalScope.deny()
        if self.allows_all_sources and other.allows_all_sources:
            return RetrievalScope(include_derived_knowledge=include_derived)
        if self.allows_all_sources:
            sources = other.allowed_sources
        elif other.allows_all_sources:
            sources = self.allowed_sources
        else:
            other_sources = set(other.allowed_sources)
            sources = tuple(
                source for source in self.allowed_sources if source in other_sources
            )
        if not sources:
            return RetrievalScope.deny()
        return RetrievalScope(
            allowed_sources=sources,
            include_derived_knowledge=include_derived,
            access_mode=RetrievalAccessMode.SUBSET,
        )


__all__ = ["RetrievalAccessMode", "RetrievalScope"]
