from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
import json
import math
import sqlite3
import time
from threading import RLock
from typing import Any

from cogdoc.api.persistence import connect_sqlite
from cogdoc.tools.eval.claim_verification_eval import CLAIM_VERDICTS


_STATUSES = frozenset({"pending", "reviewed"})
_TASK_TYPES = frozenset({"qa", "summary", "compare"})
_SUMMARY_VERDICTS = ("supported", "unsupported", "insufficient", "not_factual")


class ClaimReviewRevisionConflictError(ValueError):
    """A reviewer attempted to label a stale revision."""


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _clone(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _cursor(timestamp: float, review_id: str) -> str:
    payload = json.dumps([timestamp, review_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str | None) -> tuple[float, str] | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
        if (
            not isinstance(decoded, list)
            or len(decoded) != 2
            or not isinstance(decoded[1], str)
        ):
            return None
        timestamp = float(decoded[0])
        review_id = decoded[1]
        if (
            not math.isfinite(timestamp)
            or len(review_id) != 32
            or any(character not in "0123456789abcdef" for character in review_id)
        ):
            return None
        return timestamp, review_id
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError):
        return None


def _bounded_float(value: Any, *, minimum: float, maximum: float) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number < minimum or number > maximum:
        return None
    return round(number, 4)


def _bounded_ids(value: Any, *, maximum: int = 12) -> list[str] | None:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        return None
    result: list[str] = []
    for item in value[:maximum]:
        normalized = str(item or "")[:256]
        if normalized:
            result.append(normalized)
    return result


def _normalize_candidate(
    tenant_id: str,
    candidate: Mapping[str, Any],
    *,
    observed_at: float,
) -> dict[str, Any] | None:
    tenant = str(tenant_id or "").strip()
    review_id = str(candidate.get("review_id") or "")
    kb_id = str(candidate.get("kb_id") or "").strip()
    task_type = str(candidate.get("task_type") or "")
    policy_id = str(candidate.get("policy_id") or "")
    mode = str(candidate.get("effective_mode") or "")
    claim_id = str(candidate.get("claim_id") or "").strip()
    claim = str(candidate.get("claim") or "").strip()
    actual = str(candidate.get("actual_verdict") or "")
    if (
        not tenant
        or len(tenant) > 160
        or any(ord(character) < 32 or ord(character) == 127 for character in tenant)
        or len(review_id) != 32
        or any(character not in "0123456789abcdef" for character in review_id)
        or task_type not in _TASK_TYPES
        or not kb_id
        or len(kb_id) > 512
        or len(policy_id) != 16
        or any(character not in "0123456789abcdef" for character in policy_id)
        or mode not in {"shadow", "enforce"}
        or not claim_id
        or len(claim_id) > 64
        or not claim
        or len(claim) > 8_000
        or actual not in CLAIM_VERDICTS
    ):
        return None
    evidence_value = candidate.get("evidence")
    if not isinstance(evidence_value, Sequence) or isinstance(
        evidence_value, (str, bytes, bytearray)
    ):
        return None
    evidence: list[dict[str, Any]] = []
    for item in evidence_value[:12]:
        if not isinstance(item, Mapping):
            return None
        chunk_id = str(item.get("chunk_id") or "")
        text = str(item.get("text") or "")
        if not chunk_id or len(chunk_id) > 256 or len(text) > 8_000:
            return None
        evidence.append(
            {
                "chunk_id": chunk_id,
                "source": str(item.get("source") or "")[:512],
                "authorization_source": str(
                    item.get("authorization_source") or item.get("source") or ""
                )[:512],
                "page": item.get("page") if isinstance(item.get("page"), int) else None,
                "page_start": (
                    item.get("page_start")
                    if isinstance(item.get("page_start"), int)
                    else None
                ),
                "page_end": (
                    item.get("page_end")
                    if isinstance(item.get("page_end"), int)
                    else None
                ),
                "text": text,
                "text_truncated": bool(item.get("text_truncated")),
            }
        )
    confidence = _bounded_float(candidate.get("confidence"), minimum=0, maximum=1)
    duration_ms = _bounded_float(
        candidate.get("duration_ms"), minimum=0, maximum=86_400_000
    )
    cited_chunk_ids = _bounded_ids(candidate.get("cited_chunk_ids"))
    supporting_chunk_ids = _bounded_ids(candidate.get("supporting_chunk_ids"))
    if cited_chunk_ids is None or supporting_chunk_ids is None:
        return None
    return {
        "review_id": review_id,
        "tenant_id": tenant,
        "kb_id": kb_id,
        "observed_at": float(observed_at),
        "task_type": task_type,
        "policy_id": policy_id,
        "effective_mode": mode,
        "decision": str(candidate.get("decision") or "")[:32],
        "claim_id": claim_id,
        "claim": claim,
        "actual_verdict": actual,
        "reason": str(candidate.get("reason") or "")[:1_000],
        "confidence": confidence,
        "duration_ms": duration_ms,
        "cited_chunk_ids": cited_chunk_ids,
        "supporting_chunk_ids": supporting_chunk_ids,
        "evidence": evidence,
        "evidence_complete": bool(candidate.get("evidence_complete")),
        "status": "pending",
        "expected_verdict": None,
        "reviewer": "",
        "review_note": "",
        "reviewed_at": None,
        "revision": 1,
    }


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = _clone(row)
    result["_cursor"] = _cursor(
        float(row["observed_at"]), str(row["review_id"])
    )
    result["observed_at"] = _utc_iso(float(row["observed_at"]))
    reviewed_at = row.get("reviewed_at")
    result["reviewed_at"] = (
        _utc_iso(float(reviewed_at)) if reviewed_at is not None else None
    )
    return result


def _authorization_sources(evidence: object) -> list[str] | None:
    if not isinstance(evidence, Sequence) or isinstance(
        evidence, (str, bytes, bytearray)
    ):
        return None
    sources: list[str] = []
    seen: set[str] = set()
    for item in evidence:
        if not isinstance(item, Mapping):
            return None
        source = str(
            item.get("authorization_source") or item.get("source") or ""
        )
        if source not in seen:
            seen.add(source)
            sources.append(source)
    return sources


def _decode_authorization_sources(value: object) -> list[str] | None:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, list) or not all(
        isinstance(item, str) for item in decoded
    ):
        return None
    return decoded


def _summary_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kb_id": str(row.get("kb_id") or ""),
        "observed_at": _utc_iso(float(row["observed_at"])),
        "effective_mode": str(row.get("effective_mode") or ""),
        "evidence_complete": bool(row.get("evidence_complete")),
        "status": str(row.get("status") or ""),
        "actual_verdict": row.get("actual_verdict"),
        "expected_verdict": row.get("expected_verdict"),
        "authorization_sources": _authorization_sources(row.get("evidence")),
    }


def _summary_buckets(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, tuple[str, ...] | None], dict[str, Any]] = {}
    for source_row in rows:
        row = _summary_projection(source_row)
        sources = row["authorization_sources"]
        key = (
            str(row["kb_id"]),
            tuple(sources) if isinstance(sources, list) else None,
        )
        bucket = buckets.setdefault(
            key,
            {
                "kb_id": row["kb_id"],
                "authorization_sources": sources,
                "total_count": 0,
                "pending_count": 0,
                "reviewed_count": 0,
                "shadow_count": 0,
                "enforce_count": 0,
                "evidence_incomplete_count": 0,
                "agreement_count": 0,
                "disagreement_count": 0,
                "oldest_pending_at": None,
                "actual_verdict_counts": {
                    verdict: 0 for verdict in _SUMMARY_VERDICTS
                },
                "expected_verdict_counts": {
                    verdict: 0 for verdict in _SUMMARY_VERDICTS
                },
            },
        )
        bucket["total_count"] += 1
        status = str(row["status"])
        bucket["pending_count"] += int(status == "pending")
        bucket["reviewed_count"] += int(status == "reviewed")
        bucket["shadow_count"] += int(row["effective_mode"] == "shadow")
        bucket["enforce_count"] += int(row["effective_mode"] == "enforce")
        bucket["evidence_incomplete_count"] += int(
            not bool(row["evidence_complete"])
        )
        if status == "pending":
            observed_at = str(row["observed_at"])
            oldest = bucket["oldest_pending_at"]
            if oldest is None or observed_at < oldest:
                bucket["oldest_pending_at"] = observed_at
        actual = str(row["actual_verdict"] or "")
        expected = str(row["expected_verdict"] or "")
        if actual in _SUMMARY_VERDICTS:
            bucket["actual_verdict_counts"][actual] += 1
        if status == "reviewed" and expected in _SUMMARY_VERDICTS:
            bucket["expected_verdict_counts"][expected] += 1
            if actual == expected:
                bucket["agreement_count"] += 1
            else:
                bucket["disagreement_count"] += 1
    return list(buckets.values())


def _export_case(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["review_id"]),
        "layer": str(row["task_type"]),
        "claim_id": str(row["claim_id"]),
        "claim": str(row["claim"]),
        "expected_verdict": str(row["expected_verdict"]),
        "actual_verdict": str(row["actual_verdict"]),
        "duration_ms": row.get("duration_ms"),
        "reviewer": str(row.get("reviewer") or ""),
        "notes": str(row.get("review_note") or ""),
        "policy_id": str(row["policy_id"]),
    }


class ClaimVerificationReviewStore:
    """Bounded in-memory review queue used by app factories and tests."""

    def __init__(
        self,
        *,
        retention_days: int = 30,
        max_per_tenant: int = 10_000,
        clock: Callable[[], float] = time.time,
    ):
        self.retention_seconds = max(1, int(retention_days)) * 86400
        self.max_per_tenant = max(1, int(max_per_tenant))
        self._clock = clock
        self._lock = RLock()
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def record_candidates(
        self, tenant_id: str, candidates: Sequence[Mapping[str, Any]]
    ) -> int:
        now = self._clock()
        normalized = [
            row
            for candidate in candidates
            if (row := _normalize_candidate(tenant_id, candidate, observed_at=now))
            is not None
        ]
        inserted = 0
        with self._lock:
            self._purge_locked(now)
            for row in normalized:
                key = (row["tenant_id"], row["review_id"])
                if key not in self._rows:
                    self._rows[key] = row
                    inserted += 1
            tenant_rows = sorted(
                (
                    row
                    for row in self._rows.values()
                    if row["tenant_id"] == tenant_id
                ),
                key=lambda row: (row["observed_at"], row["review_id"]),
                reverse=True,
            )
            for row in tenant_rows[self.max_per_tenant :]:
                self._rows.pop((tenant_id, row["review_id"]), None)
        return inserted

    def list_page(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if status is not None and status not in _STATUSES:
            raise ValueError("invalid review status")
        decoded = _decode_cursor(cursor)
        if cursor and decoded is None:
            raise ValueError("invalid review cursor")
        now = self._clock()
        cutoff = now - self.retention_seconds
        with self._lock:
            rows = [
                _clone(row)
                for row in self._rows.values()
                if row["tenant_id"] == tenant_id
                and float(row["observed_at"]) >= cutoff
                and (status is None or row["status"] == status)
                and (
                    decoded is None
                    or (float(row["observed_at"]), str(row["review_id"])) < decoded
                )
            ]
        rows.sort(
            key=lambda row: (float(row["observed_at"]), str(row["review_id"])),
            reverse=True,
        )
        bounded_limit = max(1, min(200, int(limit)))
        selected = rows[: bounded_limit + 1]
        has_more = len(selected) > bounded_limit
        selected = selected[:bounded_limit]
        next_cursor = (
            _cursor(float(selected[-1]["observed_at"]), selected[-1]["review_id"])
            if has_more and selected
            else None
        )
        return {"items": [_public_row(row) for row in selected], "next_cursor": next_cursor}

    def get(self, tenant_id: str, review_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._rows.get((tenant_id, review_id))
            if row is None or float(row["observed_at"]) < (
                self._clock() - self.retention_seconds
            ):
                return None
            return _public_row(row)

    def label(
        self,
        tenant_id: str,
        review_id: str,
        *,
        expected_verdict: str,
        reviewer: str,
        review_note: str = "",
        expected_revision: int,
    ) -> dict[str, Any]:
        if expected_verdict not in CLAIM_VERDICTS:
            raise ValueError("invalid expected verdict")
        with self._lock:
            row = self._rows.get((tenant_id, review_id))
            if row is None or float(row["observed_at"]) < (
                self._clock() - self.retention_seconds
            ):
                raise KeyError(review_id)
            if int(row["revision"]) != int(expected_revision):
                raise ClaimReviewRevisionConflictError(review_id)
            row.update(
                {
                    "status": "reviewed",
                    "expected_verdict": expected_verdict,
                    "reviewer": str(reviewer or "")[:160],
                    "review_note": str(review_note or "")[:2_000],
                    "reviewed_at": self._clock(),
                    "revision": int(row["revision"]) + 1,
                }
            )
            return _public_row(row)

    def export_reviewed(
        self, tenant_id: str, *, review_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        cutoff = self._clock() - self.retention_seconds
        with self._lock:
            rows = [
                _clone(row)
                for row in self._rows.values()
                if row["tenant_id"] == tenant_id
                and row["status"] == "reviewed"
                and float(row["observed_at"]) >= cutoff
                and (review_ids is None or row["review_id"] in review_ids)
            ]
        rows.sort(key=lambda row: str(row["review_id"]))
        return [_export_case(row) for row in rows]

    def summary_buckets(self, tenant_id: str) -> list[dict[str, Any]]:
        """Aggregate an ACL-filterable summary without cloning evidence text."""
        cutoff = self._clock() - self.retention_seconds
        with self._lock:
            return _summary_buckets(
                (
                    row
                    for row in self._rows.values()
                    if row["tenant_id"] == tenant_id
                    and float(row["observed_at"]) >= cutoff
                )
            )

    def _purge_locked(self, now: float) -> None:
        cutoff = now - self.retention_seconds
        self._rows = {
            key: row
            for key, row in self._rows.items()
            if float(row["observed_at"]) >= cutoff
        }


class SqliteClaimVerificationReviewStore:
    """Durable tenant-scoped review queue containing explicitly sampled text."""

    def __init__(
        self,
        db_path: str,
        *,
        retention_days: int = 30,
        max_per_tenant: int = 10_000,
        clock: Callable[[], float] = time.time,
    ):
        self.retention_seconds = max(1, int(retention_days)) * 86400
        self.max_per_tenant = max(1, int(max_per_tenant))
        self._clock = clock
        self._lock = RLock()
        self._conn = connect_sqlite(db_path, busy_timeout_ms=250)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS claim_verification_reviews ("
            "review_id TEXT NOT NULL, tenant_id TEXT NOT NULL, observed_at REAL NOT NULL, "
            "kb_id TEXT NOT NULL DEFAULT '', "
            "task_type TEXT NOT NULL, policy_id TEXT NOT NULL, effective_mode TEXT NOT NULL, "
            "decision TEXT NOT NULL, claim_id TEXT NOT NULL, claim TEXT NOT NULL, "
            "actual_verdict TEXT NOT NULL, reason TEXT NOT NULL, confidence REAL, "
            "duration_ms REAL, cited_chunk_ids TEXT NOT NULL, "
            "supporting_chunk_ids TEXT NOT NULL, evidence TEXT NOT NULL, "
            "authorization_sources TEXT NOT NULL DEFAULT '[]', "
            "evidence_complete INTEGER NOT NULL, status TEXT NOT NULL, "
            "expected_verdict TEXT, reviewer TEXT NOT NULL, review_note TEXT NOT NULL, "
            "reviewed_at REAL, revision INTEGER NOT NULL, "
            "PRIMARY KEY(tenant_id,review_id))"
        )
        columns = {
            str(row[1])
            for row in self._conn.execute(
                "PRAGMA table_info(claim_verification_reviews)"
            ).fetchall()
        }
        if "kb_id" not in columns:
            self._conn.execute(
                "ALTER TABLE claim_verification_reviews "
                "ADD COLUMN kb_id TEXT NOT NULL DEFAULT ''"
            )
        if "authorization_sources" not in columns:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "ALTER TABLE claim_verification_reviews "
                    "ADD COLUMN authorization_sources TEXT NOT NULL DEFAULT '[]'"
                )
                self._backfill_authorization_sources()
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claim_reviews_tenant_status_time "
            "ON claim_verification_reviews(tenant_id,status,observed_at DESC,review_id DESC)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claim_reviews_observed_at "
            "ON claim_verification_reviews(observed_at)"
        )

    def _backfill_authorization_sources(self) -> None:
        last_rowid = 0
        while True:
            rows = self._conn.execute(
                "SELECT rowid,evidence FROM claim_verification_reviews "
                "WHERE rowid>? ORDER BY rowid LIMIT 200",
                (last_rowid,),
            ).fetchall()
            if not rows:
                return
            updates: list[tuple[str, int]] = []
            for rowid, raw_evidence in rows:
                try:
                    evidence = json.loads(str(raw_evidence))
                except (TypeError, ValueError, json.JSONDecodeError):
                    sources = None
                else:
                    sources = _authorization_sources(evidence)
                updates.append((json.dumps(sources), int(rowid)))
            self._conn.executemany(
                "UPDATE claim_verification_reviews "
                "SET authorization_sources=? WHERE rowid=?",
                updates,
            )
            last_rowid = int(rows[-1][0])

    def record_candidates(
        self, tenant_id: str, candidates: Sequence[Mapping[str, Any]]
    ) -> int:
        now = self._clock()
        rows = [
            row
            for candidate in candidates
            if (row := _normalize_candidate(tenant_id, candidate, observed_at=now))
            is not None
        ]
        inserted = 0
        with self._lock:
            self._conn.execute(
                "DELETE FROM claim_verification_reviews WHERE observed_at<?",
                (now - self.retention_seconds,),
            )
            for row in rows:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO claim_verification_reviews("
                    "review_id,tenant_id,observed_at,task_type,policy_id,effective_mode,"
                    "kb_id,"
                    "decision,claim_id,claim,actual_verdict,reason,confidence,duration_ms,"
                    "cited_chunk_ids,supporting_chunk_ids,evidence,authorization_sources,"
                    "evidence_complete,status,"
                    "expected_verdict,reviewer,review_note,reviewed_at,revision) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["review_id"], row["tenant_id"], row["observed_at"],
                        row["task_type"], row["policy_id"], row["effective_mode"],
                        row["kb_id"],
                        row["decision"], row["claim_id"], row["claim"],
                        row["actual_verdict"], row["reason"], row["confidence"],
                        row["duration_ms"], json.dumps(row["cited_chunk_ids"]),
                        json.dumps(row["supporting_chunk_ids"]),
                        json.dumps(row["evidence"], ensure_ascii=False),
                        json.dumps(_authorization_sources(row["evidence"])),
                        int(row["evidence_complete"]), row["status"], None, "", "",
                        None, row["revision"],
                    ),
                )
                inserted += int(cursor.rowcount > 0)
            self._conn.execute(
                "DELETE FROM claim_verification_reviews WHERE tenant_id=? AND review_id "
                "NOT IN (SELECT review_id FROM claim_verification_reviews WHERE tenant_id=? "
                "ORDER BY observed_at DESC,review_id DESC LIMIT ?)",
                (tenant_id, tenant_id, self.max_per_tenant),
            )
        return inserted

    @staticmethod
    def _from_sql(row: sqlite3.Row | tuple[Any, ...], names: list[str]) -> dict[str, Any]:
        result = dict(zip(names, row, strict=True))
        result.pop("authorization_sources", None)
        for key in ("cited_chunk_ids", "supporting_chunk_ids", "evidence"):
            result[key] = json.loads(result[key])
        result["evidence_complete"] = bool(result["evidence_complete"])
        return result

    def list_page(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if status is not None and status not in _STATUSES:
            raise ValueError("invalid review status")
        decoded = _decode_cursor(cursor)
        if cursor and decoded is None:
            raise ValueError("invalid review cursor")
        clauses = ["tenant_id=?", "observed_at>=?"]
        params: list[Any] = [tenant_id, self._clock() - self.retention_seconds]
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if decoded is not None:
            clauses.append("(observed_at<? OR (observed_at=? AND review_id<?))")
            params.extend([decoded[0], decoded[0], decoded[1]])
        bounded_limit = max(1, min(200, int(limit)))
        params.append(bounded_limit + 1)
        with self._lock:
            cursor_result = self._conn.execute(
                "SELECT * FROM claim_verification_reviews WHERE "
                + " AND ".join(clauses)
                + " ORDER BY observed_at DESC,review_id DESC LIMIT ?",
                params,
            )
            names = [str(item[0]) for item in cursor_result.description]
            rows = [self._from_sql(row, names) for row in cursor_result.fetchall()]
        has_more = len(rows) > bounded_limit
        rows = rows[:bounded_limit]
        next_cursor = (
            _cursor(float(rows[-1]["observed_at"]), str(rows[-1]["review_id"]))
            if has_more and rows
            else None
        )
        return {"items": [_public_row(row) for row in rows], "next_cursor": next_cursor}

    def get(self, tenant_id: str, review_id: str) -> dict[str, Any] | None:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM claim_verification_reviews WHERE tenant_id=? "
                "AND review_id=? AND observed_at>=?",
                (tenant_id, review_id, self._clock() - self.retention_seconds),
            )
            raw = cursor.fetchone()
            if raw is None:
                return None
            names = [str(item[0]) for item in cursor.description]
            return _public_row(self._from_sql(raw, names))

    def label(
        self,
        tenant_id: str,
        review_id: str,
        *,
        expected_verdict: str,
        reviewer: str,
        review_note: str = "",
        expected_revision: int,
    ) -> dict[str, Any]:
        if expected_verdict not in CLAIM_VERDICTS:
            raise ValueError("invalid expected verdict")
        now = self._clock()
        with self._lock:
            existing = self._conn.execute(
                "SELECT revision FROM claim_verification_reviews "
                "WHERE tenant_id=? AND review_id=? AND observed_at>=?",
                (tenant_id, review_id, now - self.retention_seconds),
            ).fetchone()
            if existing is None:
                raise KeyError(review_id)
            if int(existing[0]) != int(expected_revision):
                raise ClaimReviewRevisionConflictError(review_id)
            cursor = self._conn.execute(
                "UPDATE claim_verification_reviews SET status='reviewed',"
                "expected_verdict=?,reviewer=?,review_note=?,reviewed_at=?,"
                "revision=revision+1 WHERE tenant_id=? AND review_id=? AND revision=? "
                "AND observed_at>=?",
                (
                    expected_verdict, str(reviewer or "")[:160],
                    str(review_note or "")[:2_000], now, tenant_id, review_id,
                    int(expected_revision), now - self.retention_seconds,
                ),
            )
            if cursor.rowcount != 1:
                raise ClaimReviewRevisionConflictError(review_id)
        result = self.get(tenant_id, review_id)
        if result is None:
            raise KeyError(review_id)
        return result

    def export_reviewed(
        self, tenant_id: str, *, review_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        if review_ids is not None and not review_ids:
            return []
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM claim_verification_reviews WHERE tenant_id=? "
                "AND status='reviewed' AND observed_at>=? ORDER BY review_id",
                (tenant_id, self._clock() - self.retention_seconds),
            )
            names = [str(item[0]) for item in cursor.description]
            rows = [self._from_sql(row, names) for row in cursor.fetchall()]
        return [
            _export_case(row)
            for row in rows
            if review_ids is None or str(row["review_id"]) in review_ids
        ]

    def summary_buckets(self, tenant_id: str) -> list[dict[str, Any]]:
        """Aggregate queue metrics in SQL by the fields required for ACL checks."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kb_id,authorization_sources,COUNT(*),"
                "SUM(status='pending'),SUM(status='reviewed'),"
                "SUM(effective_mode='shadow'),SUM(effective_mode='enforce'),"
                "SUM(evidence_complete=0),"
                "SUM(status='reviewed' AND expected_verdict IN "
                "('supported','unsupported','insufficient','not_factual') "
                "AND actual_verdict=expected_verdict),"
                "SUM(status='reviewed' AND expected_verdict IN "
                "('supported','unsupported','insufficient','not_factual') "
                "AND actual_verdict<>expected_verdict),"
                "MIN(CASE WHEN status='pending' THEN observed_at END),"
                "SUM(actual_verdict='supported'),"
                "SUM(actual_verdict='unsupported'),"
                "SUM(actual_verdict='insufficient'),"
                "SUM(actual_verdict='not_factual'),"
                "SUM(status='reviewed' AND expected_verdict='supported'),"
                "SUM(status='reviewed' AND expected_verdict='unsupported'),"
                "SUM(status='reviewed' AND expected_verdict='insufficient'),"
                "SUM(status='reviewed' AND expected_verdict='not_factual') "
                "FROM claim_verification_reviews "
                "WHERE tenant_id=? AND observed_at>=? "
                "GROUP BY kb_id,authorization_sources",
                (tenant_id, self._clock() - self.retention_seconds),
            ).fetchall()
        return [
            {
                "kb_id": str(row[0] or ""),
                "authorization_sources": _decode_authorization_sources(row[1]),
                "total_count": int(row[2] or 0),
                "pending_count": int(row[3] or 0),
                "reviewed_count": int(row[4] or 0),
                "shadow_count": int(row[5] or 0),
                "enforce_count": int(row[6] or 0),
                "evidence_incomplete_count": int(row[7] or 0),
                "agreement_count": int(row[8] or 0),
                "disagreement_count": int(row[9] or 0),
                "oldest_pending_at": (
                    _utc_iso(float(row[10])) if row[10] is not None else None
                ),
                "actual_verdict_counts": dict(
                    zip(
                        _SUMMARY_VERDICTS,
                        (int(value or 0) for value in row[11:15]),
                        strict=True,
                    )
                ),
                "expected_verdict_counts": dict(
                    zip(
                        _SUMMARY_VERDICTS,
                        (int(value or 0) for value in row[15:19]),
                        strict=True,
                    )
                ),
            }
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
