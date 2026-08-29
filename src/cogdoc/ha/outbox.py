from __future__ import annotations

import hashlib
import hmac
import logging
import json
import math
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from cogdoc.connectors.http_transport import HttpTransport
from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection, execute_script


OUTBOX_PENDING: Final = "pending"
OUTBOX_DELIVERING: Final = "delivering"
OUTBOX_DELIVERED: Final = "delivered"
OUTBOX_DEAD_LETTER: Final = "dead_letter"
_MAX_JSON_BYTES = 1024 * 1024


class OutboxError(RuntimeError):
    pass


class OutboxConflict(OutboxError):
    pass


class StaleOutboxLease(OutboxError):
    pass


class EventHandler(Protocol):
    def __call__(
        self,
        topic: str,
        payload: Any,
        headers: Mapping[str, Any],
        event_id: str,
    ) -> None: ...


class WebhookOutboxHandler:
    """Signed webhook delivery that preserves the durable outbox event ID."""

    def __init__(
        self,
        url: str,
        *,
        secret: str = "",
        timeout_seconds: float = 10,
        client: Any | None = None,
        transport: Any | None = None,
        allow_private_hosts: bool = False,
        max_response_bytes: int = _MAX_JSON_BYTES,
        max_redirects: int = 2,
    ) -> None:
        if not isinstance(url, str):
            raise ValueError("outbox webhook URL must use HTTPS")
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError("outbox webhook URL is invalid") from exc
        host = str(parts.hostname or "").casefold()
        if (
            parts.scheme != "https"
            or not host
            or port not in {None, 443}
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError(
                "outbox webhook URL must be a credential-free HTTPS URL"
            )
        if not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= 60:
            raise ValueError(
                "outbox webhook timeout must be between 0.1 and 60 seconds"
            )
        if client is not None and transport is not None:
            raise ValueError("outbox webhook client and transport are mutually exclusive")
        self.url = url
        self._secret = secret.encode()
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._transport = transport
        if self._client is None and self._transport is None:
            self._transport = HttpTransport(
                allowed_hosts={host},
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
                max_redirects=max_redirects,
                allow_private_hosts=allow_private_hosts,
            )

    def __call__(
        self,
        topic: str,
        payload: Any,
        headers: Mapping[str, Any],
        event_id: str,
    ) -> None:
        body = _json(
            {
                "schema_version": "v1",
                "event_id": event_id,
                "event": topic,
                "payload": payload,
                "metadata": dict(headers),
            },
            "webhook",
        ).encode()
        request_headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": event_id,
            "X-CogDoc-Event-Id": event_id,
        }
        if self._secret:
            signature = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
            request_headers["X-CogDoc-Signature"] = f"sha256={signature}"
        if self._client is not None:
            response = self._client.post(
                self.url,
                content=body,
                headers=request_headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        else:
            assert self._transport is not None
            self._transport.request(
                "POST",
                self.url,
                headers=request_headers,
                body=body,
            )


def _clean(value: str, field: str, maximum: int = 255) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _json(value: Any, field: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"outbox {field} must be JSON serializable") from exc
    if len(encoded.encode()) > _MAX_JSON_BYTES:
        raise ValueError(f"outbox {field} exceeds 1 MiB")
    return encoded


class OutboxStore:
    """Transactional events delivered at-least-once with stable event IDs."""

    def __init__(
        self, backend: DatabaseBackend, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.backend = backend
        self._clock = clock
        execute_script(
            backend,
            [
                backend.sql(
                    sqlite="""CREATE TABLE IF NOT EXISTS ha_outbox (
                    event_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,topic TEXT NOT NULL,
                    aggregate_type TEXT NOT NULL,aggregate_id TEXT NOT NULL,
                    aggregate_revision INTEGER NOT NULL,payload_json TEXT NOT NULL,
                    headers_json TEXT NOT NULL,status TEXT NOT NULL,available_at REAL NOT NULL,
                    lease_owner TEXT,lease_token TEXT,lease_expires_at REAL,attempt INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,last_error TEXT,idempotency_key TEXT,
                    created_at REAL NOT NULL,updated_at REAL NOT NULL,delivered_at REAL,
                    UNIQUE(tenant_id,topic,idempotency_key))""",
                    postgres="""CREATE TABLE IF NOT EXISTS ha_outbox (
                    event_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,topic TEXT NOT NULL,
                    aggregate_type TEXT NOT NULL,aggregate_id TEXT NOT NULL,
                    aggregate_revision BIGINT NOT NULL,payload_json TEXT NOT NULL,
                    headers_json TEXT NOT NULL,status TEXT NOT NULL,
                    available_at DOUBLE PRECISION NOT NULL,lease_owner TEXT,lease_token TEXT,
                    lease_expires_at DOUBLE PRECISION,attempt INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,last_error TEXT,idempotency_key TEXT,
                    created_at DOUBLE PRECISION NOT NULL,updated_at DOUBLE PRECISION NOT NULL,
                    delivered_at DOUBLE PRECISION,UNIQUE(tenant_id,topic,idempotency_key))""",
                ),
                """CREATE TABLE IF NOT EXISTS ha_outbox_keys (
                tenant_id TEXT NOT NULL,topic TEXT NOT NULL,idempotency_key TEXT NOT NULL,
                event_id TEXT NOT NULL,fingerprint TEXT NOT NULL,created_at REAL NOT NULL,
                PRIMARY KEY(tenant_id,topic,idempotency_key))""",
                "CREATE INDEX IF NOT EXISTS idx_ha_outbox_claim ON ha_outbox(status,available_at,created_at,event_id)",
                "CREATE INDEX IF NOT EXISTS idx_ha_outbox_lease ON ha_outbox(status,lease_expires_at)",
                "CREATE INDEX IF NOT EXISTS idx_ha_outbox_aggregate ON ha_outbox(tenant_id,aggregate_type,aggregate_id,aggregate_revision)",
            ],
        )

    @staticmethod
    def _row(row: Any | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(str(result.pop("payload_json")))
        result["headers"] = json.loads(str(result.pop("headers_json")))
        return result

    def append(
        self,
        connection: DatabaseConnection,
        *,
        tenant_id: str,
        topic: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_revision: int,
        payload: Any,
        headers: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        max_attempts: int = 10,
        available_at: float | None = None,
    ) -> dict[str, Any]:
        """Append using the caller's transaction; never opens a nested transaction."""

        tenant_id = _clean(tenant_id, "tenant_id")
        topic = _clean(topic, "topic", 200)
        aggregate_type = _clean(aggregate_type, "aggregate_type", 128)
        aggregate_id = _clean(aggregate_id, "aggregate_id", 512)
        if type(aggregate_revision) is not int or aggregate_revision < 0:
            raise ValueError("aggregate_revision is invalid")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        if idempotency_key is not None:
            idempotency_key = _clean(idempotency_key, "idempotency_key", 512)
        payload_json = _json(payload, "payload")
        headers_json = _json(dict(headers or {}), "headers")
        now = self._clock()
        ready = now if available_at is None else float(available_at)
        if not math.isfinite(ready) or ready < 0:
            raise ValueError("available_at is invalid")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        if idempotency_key is not None:
            fingerprint = self._fingerprint(
                aggregate_type,
                aggregate_id,
                aggregate_revision,
                payload_json,
                headers_json,
            )
            key_row = connection.execute(
                f"SELECT event_id,fingerprint FROM ha_outbox_keys WHERE tenant_id={marker} "
                f"AND topic={marker} AND idempotency_key={marker}",
                (tenant_id, topic, idempotency_key),
            ).fetchone()
            if key_row is not None:
                stored_fingerprint = (
                    key_row["fingerprint"]
                    if isinstance(key_row, Mapping)
                    else key_row[1]
                )
                if str(stored_fingerprint) != fingerprint:
                    raise OutboxConflict(
                        "outbox idempotency key was reused with different data"
                    )
                prior_event_id = str(
                    key_row["event_id"] if isinstance(key_row, Mapping) else key_row[0]
                )
                existing = connection.execute(
                    f"SELECT * FROM ha_outbox WHERE event_id={marker}",
                    (prior_event_id,),
                ).fetchone()
                if existing is None:
                    # The retention window ended and the delivered row was
                    # compacted. Remove the matching tombstone atomically so
                    # this key may begin a new idempotency window.
                    connection.execute(
                        f"DELETE FROM ha_outbox_keys WHERE tenant_id={marker} "
                        f"AND topic={marker} AND idempotency_key={marker} "
                        f"AND event_id={marker} AND fingerprint={marker}",
                        (
                            tenant_id,
                            topic,
                            idempotency_key,
                            prior_event_id,
                            fingerprint,
                        ),
                    )
                else:
                    current = self._row(existing)
                    assert current is not None
                    return current
        event_id = f"evt-{uuid.uuid4().hex}"
        insert = self.backend.sql(sqlite="INSERT OR IGNORE", postgres="INSERT")
        suffix = self.backend.sql(
            sqlite="",
            postgres=" ON CONFLICT(tenant_id,topic,idempotency_key) DO NOTHING",
        )
        placeholders = self.backend.sql(
            sqlite="?,?,?,?,?,?,?,?,?,?,?,?,?,?,?",
            postgres="%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s",
        )
        changed = connection.execute(
            f"{insert} INTO ha_outbox(event_id,tenant_id,topic,aggregate_type,aggregate_id,"
            "aggregate_revision,payload_json,headers_json,status,available_at,attempt,max_attempts,"
            f"idempotency_key,created_at,updated_at) VALUES({placeholders}){suffix}",
            (
                event_id,
                tenant_id,
                topic,
                aggregate_type,
                aggregate_id,
                aggregate_revision,
                payload_json,
                headers_json,
                OUTBOX_PENDING,
                ready,
                0,
                max_attempts,
                idempotency_key,
                now,
                now,
            ),
        )
        if changed.rowcount == 1:
            if idempotency_key is not None:
                key_placeholders = self.backend.sql(
                    sqlite="?,?,?,?,?,?", postgres="%s,%s,%s,%s,%s,%s"
                )
                connection.execute(
                    "INSERT INTO ha_outbox_keys(tenant_id,topic,idempotency_key,event_id,"
                    f"fingerprint,created_at) VALUES({key_placeholders})",
                    (
                        tenant_id,
                        topic,
                        idempotency_key,
                        event_id,
                        self._fingerprint(
                            aggregate_type,
                            aggregate_id,
                            aggregate_revision,
                            payload_json,
                            headers_json,
                        ),
                        now,
                    ),
                )
            return (
                self._row(
                    connection.execute(
                        f"SELECT * FROM ha_outbox WHERE event_id={marker}", (event_id,)
                    ).fetchone()
                )
                or {}
            )
        if idempotency_key is None:  # pragma: no cover - UUID collision
            raise OutboxConflict("outbox event identifier collision")
        existing = connection.execute(
            f"SELECT * FROM ha_outbox WHERE tenant_id={marker} AND topic={marker} "
            f"AND idempotency_key={marker}",
            (tenant_id, topic, idempotency_key),
        ).fetchone()
        if existing is None:
            raise OutboxConflict("idempotent outbox event disappeared")
        return self._same_or_conflict(
            existing,
            aggregate_type,
            aggregate_id,
            aggregate_revision,
            payload_json,
            headers_json,
        )

    @staticmethod
    def _fingerprint(
        aggregate_type: str,
        aggregate_id: str,
        revision: int,
        payload_json: str,
        headers_json: str,
    ) -> str:
        encoded = "\0".join(
            (aggregate_type, aggregate_id, str(revision), payload_json, headers_json)
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def enqueue(self, **event: Any) -> dict[str, Any]:
        with self.backend.transaction(write=True) as connection:
            return self.append(connection, **event)

    def _same_or_conflict(
        self,
        row: Any,
        aggregate_type: str,
        aggregate_id: str,
        revision: int,
        payload_json: str,
        headers_json: str,
    ) -> dict[str, Any]:
        current = self._row(row)
        assert current is not None
        if (
            current["aggregate_type"] != aggregate_type
            or current["aggregate_id"] != aggregate_id
            or int(current["aggregate_revision"]) != revision
            or current["payload"] != json.loads(payload_json)
            or current["headers"] != json.loads(headers_json)
        ):
            raise OutboxConflict(
                "outbox idempotency key was reused with different data"
            )
        return current

    def claim(
        self, worker_id: str, *, lease_seconds: float = 60.0
    ) -> dict[str, Any] | None:
        worker_id = _clean(worker_id, "worker_id")
        if not math.isfinite(lease_seconds) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        now = self._clock()
        token = secrets.token_urlsafe(32)
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                f"UPDATE ha_outbox SET status='{OUTBOX_DEAD_LETTER}',lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,last_error='LEASE_EXPIRED',"
                f"updated_at={marker} WHERE status='{OUTBOX_DELIVERING}' "
                f"AND lease_expires_at<={marker} AND attempt>=max_attempts",
                (now, now),
            )
            if self.backend.kind == "postgres":
                row = connection.execute(
                    "WITH candidate AS (SELECT event_id FROM ha_outbox AS events "
                    "WHERE attempt<max_attempts AND NOT EXISTS (SELECT 1 FROM ha_outbox AS prior "
                    "WHERE prior.tenant_id=events.tenant_id AND prior.topic=events.topic "
                    "AND prior.aggregate_type=events.aggregate_type "
                    "AND prior.aggregate_id=events.aggregate_id "
                    "AND prior.status IN ('pending','delivering','dead_letter') AND ("
                    "prior.aggregate_revision<events.aggregate_revision OR "
                    "(prior.aggregate_revision=events.aggregate_revision AND "
                    "(prior.created_at<events.created_at OR (prior.created_at=events.created_at "
                    "AND prior.event_id<events.event_id))))) AND "
                    f"((events.status='{OUTBOX_PENDING}' AND events.available_at<={marker}) OR "
                    f"(events.status='{OUTBOX_DELIVERING}' AND events.lease_expires_at<={marker})) "
                    "ORDER BY events.available_at,events.created_at,events.event_id "
                    "FOR UPDATE SKIP LOCKED LIMIT 1) "
                    f"UPDATE ha_outbox AS events SET status='{OUTBOX_DELIVERING}',lease_owner=%s,"
                    "lease_token=%s,lease_expires_at=%s,attempt=attempt+1,updated_at=%s "
                    "FROM candidate WHERE events.event_id=candidate.event_id RETURNING events.*",
                    (now, now, worker_id, token, now + lease_seconds, now),
                ).fetchone()
            else:
                candidate = connection.execute(
                    "SELECT event_id FROM ha_outbox AS events WHERE attempt<max_attempts "
                    "AND NOT EXISTS (SELECT 1 FROM ha_outbox AS prior WHERE "
                    "prior.tenant_id=events.tenant_id AND prior.topic=events.topic "
                    "AND prior.aggregate_type=events.aggregate_type "
                    "AND prior.aggregate_id=events.aggregate_id "
                    "AND prior.status IN ('pending','delivering','dead_letter') AND ("
                    "prior.aggregate_revision<events.aggregate_revision OR "
                    "(prior.aggregate_revision=events.aggregate_revision AND "
                    "(prior.created_at<events.created_at OR (prior.created_at=events.created_at "
                    "AND prior.event_id<events.event_id))))) AND "
                    "((events.status='pending' AND events.available_at<=?) OR "
                    "(events.status='delivering' AND events.lease_expires_at<=?)) "
                    "ORDER BY events.available_at,events.created_at,events.event_id LIMIT 1",
                    (now, now),
                ).fetchone()
                if candidate is None:
                    return None
                event_id = str(candidate[0])
                changed = connection.execute(
                    "UPDATE ha_outbox SET status='delivering',lease_owner=?,lease_token=?,"
                    "lease_expires_at=?,attempt=attempt+1,updated_at=? WHERE event_id=? "
                    "AND attempt<max_attempts AND ((status='pending' AND available_at<=?) "
                    "OR (status='delivering' AND lease_expires_at<=?))",
                    (worker_id, token, now + lease_seconds, now, event_id, now, now),
                )
                if changed.rowcount != 1:
                    return None
                row = connection.execute(
                    "SELECT * FROM ha_outbox WHERE event_id=?", (event_id,)
                ).fetchone()
            return self._row(row)

    def delivered(self, event_id: str, lease_token: str) -> dict[str, Any]:
        return self._finish(event_id, lease_token, error=None, retry_delay_seconds=0)

    def heartbeat(
        self, event_id: str, lease_token: str, *, lease_seconds: float = 60.0
    ) -> None:
        event_id = _clean(event_id, "event_id")
        lease_token = _clean(lease_token, "lease_token", 512)
        if not math.isfinite(lease_seconds) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                f"UPDATE ha_outbox SET lease_expires_at={marker},updated_at={marker} "
                f"WHERE event_id={marker} AND status='{OUTBOX_DELIVERING}' "
                f"AND lease_token={marker} AND lease_expires_at>{marker}",
                (now + lease_seconds, now, event_id, lease_token, now),
            )
            if changed.rowcount != 1:
                raise StaleOutboxLease("outbox delivery lease is stale or expired")

    def failed(
        self,
        event_id: str,
        lease_token: str,
        error: str,
        *,
        retry_delay_seconds: float,
    ) -> dict[str, Any]:
        error = _clean(error, "error", 512)
        if not math.isfinite(retry_delay_seconds) or retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds is invalid")
        return self._finish(event_id, lease_token, error, retry_delay_seconds)

    def _finish(
        self,
        event_id: str,
        lease_token: str,
        error: str | None,
        retry_delay_seconds: float,
    ) -> dict[str, Any]:
        event_id = _clean(event_id, "event_id")
        lease_token = _clean(lease_token, "lease_token", 512)
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
            row = connection.execute(
                f"SELECT * FROM ha_outbox WHERE event_id={marker}{lock}", (event_id,)
            ).fetchone()
            current = self._row(row)
            if (
                current is None
                or current["status"] != OUTBOX_DELIVERING
                or current["lease_token"] != lease_token
                or float(current["lease_expires_at"] or 0) <= now
            ):
                raise StaleOutboxLease("outbox delivery lease is stale or expired")
            if error is None:
                status = OUTBOX_DELIVERED
            elif int(current["attempt"]) >= int(current["max_attempts"]):
                status = OUTBOX_DEAD_LETTER
            else:
                status = OUTBOX_PENDING
            changed = connection.execute(
                f"UPDATE ha_outbox SET status={marker},available_at={marker},lease_owner=NULL,"
                f"lease_token=NULL,lease_expires_at=NULL,last_error={marker},delivered_at={marker},"
                f"updated_at={marker} WHERE event_id={marker} AND status='{OUTBOX_DELIVERING}' "
                f"AND lease_token={marker}",
                (
                    status,
                    now + retry_delay_seconds if status == OUTBOX_PENDING else now,
                    error,
                    now if status == OUTBOX_DELIVERED else None,
                    now,
                    event_id,
                    lease_token,
                ),
            )
            if changed.rowcount != 1:
                raise StaleOutboxLease("outbox delivery was superseded")
            return (
                self._row(
                    connection.execute(
                        f"SELECT * FROM ha_outbox WHERE event_id={marker}", (event_id,)
                    ).fetchone()
                )
                or {}
            )

    def get(self, event_id: str) -> dict[str, Any] | None:
        event_id = _clean(event_id, "event_id")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            return self._row(
                connection.execute(
                    f"SELECT * FROM ha_outbox WHERE event_id={marker}", (event_id,)
                ).fetchone()
            )

    def prune_delivered(self, *, before: float, limit: int = 1000) -> int:
        if not math.isfinite(before):
            raise ValueError("prune cutoff must be finite")
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            rows = connection.execute(
                f"SELECT event_id FROM ha_outbox WHERE status='{OUTBOX_DELIVERED}' "
                f"AND delivered_at<={marker} ORDER BY delivered_at,event_id LIMIT {limit}",
                (before,),
            ).fetchall()
            for row in rows:
                event_id = row["event_id"] if isinstance(row, Mapping) else row[0]
                connection.execute(
                    f"DELETE FROM ha_outbox_keys WHERE event_id={marker}",
                    (event_id,),
                )
                connection.execute(
                    f"DELETE FROM ha_outbox WHERE event_id={marker} "
                    f"AND status='{OUTBOX_DELIVERED}' AND delivered_at<={marker}",
                    (event_id, before),
                )
            return len(rows)


class OutboxDispatcher:
    def __init__(
        self,
        store: OutboxStore,
        handler: EventHandler,
        *,
        worker_id: str,
        lease_seconds: float = 60,
        idle_seconds: float = 0.5,
        retry_delay: Callable[[int], float] | None = None,
    ) -> None:
        self.store = store
        self.handler = handler
        self.worker_id = _clean(worker_id, "worker_id")
        if not math.isfinite(lease_seconds) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        if not math.isfinite(idle_seconds) or not 0.01 <= idle_seconds <= 60:
            raise ValueError("idle_seconds must be between 0.01 and 60")
        self.lease_seconds = lease_seconds
        self.idle_seconds = idle_seconds
        self.retry_delay = retry_delay or (
            lambda attempt: min(300.0, 2 ** min(attempt, 8))
        )
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: BaseException | None = None
        self._lock = threading.Lock()

    def run_once(self) -> bool:
        event = self.store.claim(self.worker_id, lease_seconds=self.lease_seconds)
        if event is None:
            return False
        finished = threading.Event()
        lease_lost = threading.Event()

        def keep_lease() -> None:
            interval = max(0.25, self.lease_seconds / 3)
            while not finished.wait(interval):
                try:
                    self.store.heartbeat(
                        str(event["event_id"]),
                        str(event["lease_token"]),
                        lease_seconds=self.lease_seconds,
                    )
                except Exception:
                    lease_lost.set()
                    return

        keeper = threading.Thread(
            target=keep_lease,
            name=f"outbox-heartbeat-{event['event_id']}",
            daemon=True,
        )
        keeper.start()
        try:
            self.handler(
                str(event["topic"]),
                event["payload"],
                event["headers"],
                str(event["event_id"]),
            )
        except Exception as exc:
            finished.set()
            keeper.join()
            if lease_lost.is_set():
                return True
            delay = float(self.retry_delay(int(event["attempt"])))
            if not math.isfinite(delay) or delay < 0:
                delay = 300.0
            self.store.failed(
                str(event["event_id"]),
                str(event["lease_token"]),
                type(exc).__name__.upper(),
                retry_delay_seconds=delay,
            )
        else:
            finished.set()
            keeper.join()
            if lease_lost.is_set():
                return True
            self.store.delivered(str(event["event_id"]), str(event["lease_token"]))
        finally:
            finished.set()
        return True

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name=f"outbox-{self.worker_id}", daemon=True
            )
            self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("outbox dispatcher did not stop")
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
                self._last_error = None
            except Exception as exc:
                self._last_error = exc
                logging.getLogger(__name__).exception("HA outbox cycle failed")
                worked = False
            if not worked:
                self._wake.wait(self.idle_seconds)
                self._wake.clear()

    def check(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and self._last_error is None


__all__ = [
    "OUTBOX_DEAD_LETTER",
    "OUTBOX_DELIVERED",
    "OUTBOX_DELIVERING",
    "OUTBOX_PENDING",
    "EventHandler",
    "OutboxConflict",
    "OutboxDispatcher",
    "OutboxError",
    "OutboxStore",
    "StaleOutboxLease",
    "WebhookOutboxHandler",
]
