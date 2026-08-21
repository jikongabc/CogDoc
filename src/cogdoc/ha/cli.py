from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from cogdoc.config.settings import get_settings
from cogdoc.ha.migration_catalog import REGISTERED_MIGRATIONS
from cogdoc.ha.migrations import MigrationRunner
from cogdoc.ha.outbox import WebhookOutboxHandler
from cogdoc.ha.runtime import HAConfig, HARuntime, manifest_for_directory


def _bounded_number(value: str, *, minimum: float, maximum: float, field: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{field} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(
            f"{field} must be between {minimum:g} and {maximum:g}"
        )
    return parsed


def _lease_seconds(value: str) -> float:
    return _bounded_number(value, minimum=5, maximum=3600, field="lease seconds")


def _retention_seconds(value: str) -> float:
    return _bounded_number(
        value,
        minimum=1,
        maximum=10 * 366 * 86_400,
        field="retention seconds",
    )


def _dimensions(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("dimensions must be an integer") from exc
    if not 1 <= parsed <= 1_000_000:
        raise argparse.ArgumentTypeError("dimensions must be between 1 and 1000000")
    return parsed


def _gc_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("GC limit must be an integer") from exc
    if not 1 <= parsed <= 1000:
        raise argparse.ArgumentTypeError("GC limit must be between 1 and 1000")
    return parsed


def _batch_size(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("batch size must be an integer") from exc
    if not 1 <= parsed <= 100_000:
        raise argparse.ArgumentTypeError("batch size must be between 1 and 100000")
    return parsed


def _schema_version(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("schema version must be an integer") from exc
    if not 1 <= parsed <= 2_147_483_647:
        raise argparse.ArgumentTypeError("schema version is invalid")
    return parsed


def _runtime() -> HARuntime:
    settings = get_settings()
    config = HAConfig.from_settings(settings)
    handler = (
        WebhookOutboxHandler(
            settings.cogdoc_webhook_url,
            secret=settings.cogdoc_webhook_secret,
            timeout_seconds=settings.cogdoc_webhook_timeout_seconds,
        )
        if settings.cogdoc_webhook_url.strip()
        else None
    )
    return HARuntime(config, outbox_handler=handler)


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _doctor(runtime: HARuntime) -> int:
    ready = runtime.check()
    _json(
        {
            "status": "ready" if ready else "not_ready",
            "backend": runtime.backend.kind,
            "object_store": runtime.config.object_store,
            "multi_instance_safe": runtime.multi_instance_safe,
            "worker_id": runtime.config.worker_id,
        }
    )
    return 0 if ready else 1


def _prepare_generation(runtime: HARuntime, args: argparse.Namespace) -> dict[str, Any]:
    directory = Path(args.directory).resolve(strict=True)
    manifest = manifest_for_directory(
        directory,
        contract={
            "chunk_version": args.chunk_version,
            "embedding_model": args.embedding_model,
            "dimensions": args.dimensions,
        },
    )
    generation = runtime.index_generations.begin_build(
        args.tenant,
        args.kb,
        args.build_id,
        runtime.config.worker_id,
        lease_seconds=args.lease_seconds,
    )
    if generation["status"] != "published":
        if generation["status"] != "prepared":
            generation = runtime.index_generations.prepare(
                generation["generation_id"], generation["lease_token"], manifest
            )
        runtime.index_repository.materialize(generation, directory)
    return generation


def _publish(runtime: HARuntime, args: argparse.Namespace) -> int:
    generation = _prepare_generation(runtime, args)
    if generation["status"] != "published":

        def append_event(connection, candidate):
            runtime.outbox.append(
                connection,
                tenant_id=args.tenant,
                topic="index.published",
                aggregate_type="knowledge_base",
                aggregate_id=args.kb,
                aggregate_revision=int(candidate["fencing_token"]),
                payload={
                    "kb_id": args.kb,
                    "generation_id": candidate["generation_id"],
                    "manifest_sha256": candidate["manifest_sha256"],
                },
                idempotency_key=f"index:{candidate['generation_id']}",
            )

        generation = runtime.index_generations.publish(
            generation["generation_id"],
            generation["lease_token"],
            runtime.index_repository.verify,
            on_publish=append_event,
        )
    _json(
        {
            "status": generation["status"],
            "generation_id": generation["generation_id"],
            "manifest_sha256": generation["manifest_sha256"],
        }
    )
    return 0


def _enqueue_index(runtime: HARuntime, args: argparse.Namespace) -> int:
    generation = _prepare_generation(runtime, args)
    if runtime.index_worker is None:
        raise RuntimeError("HA index worker is disabled")
    job = runtime.index_worker.enqueue(
        args.tenant,
        args.kb,
        args.build_id,
        generation_id=str(generation["generation_id"]),
        generation_lease_token=str(generation["lease_token"]),
    )
    _json(
        {
            "generation_id": generation["generation_id"],
            "job_id": job["job_id"],
            "status": job["status"],
        }
    )
    return 0


def _gc(runtime: HARuntime, args: argparse.Namespace) -> int:
    before = time.time() - args.retention_seconds
    candidates = runtime.index_generations.garbage_candidates(
        before=before, limit=args.limit
    )
    removed = 0
    for generation in candidates:
        runtime.index_repository.delete_generation(generation)
        removed += int(
            runtime.index_generations.forget_collectable(
                generation["generation_id"], before=before
            )
        )
    _json({"candidates": len(candidates), "removed": removed})
    return 0


def _scrub(runtime: HARuntime, args: argparse.Namespace) -> int:
    checked = 0
    after: tuple[str, str] | None = None
    while checked < args.limit:
        rows = runtime.index_generations.list_current(
            limit=min(100, args.limit - checked), after=after
        )
        if not rows:
            break
        for generation in rows:
            runtime.index_repository.verify(generation)
            checked += 1
        last = rows[-1]
        after = (str(last["tenant_id"]), str(last["kb_id"]))
    _json({"checked": checked, "status": "verified"})
    return 0


def _serve(runtime: HARuntime) -> int:
    stopping = threading.Event()

    def stop(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    runtime.start()
    try:
        while not stopping.wait(1):
            pass
    finally:
        runtime.shutdown()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cogdoc-ha", description="CogDoc distributed control-plane operations"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="probe PostgreSQL and object storage")
    commands.add_parser("serve", help="run scheduler and outbox dispatch loops")
    commands.add_parser("scheduler-once", help="materialize and dispatch due schedules")
    commands.add_parser("outbox-once", help="deliver one pending outbox event")
    migrate = commands.add_parser("migrate", help="run registered HA schema migrations")
    migrate.add_argument("--batch-size", type=_batch_size, default=1000)
    migrate.add_argument("--max-batches", type=_batch_size)
    migrate.add_argument("--allow-contract", action="store_true")
    migrate.add_argument(
        "--minimum-compatible-version",
        type=_schema_version,
        help="deployment evidence used when no live application heartbeat exists",
    )

    def add_index_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--tenant", required=True)
        command.add_argument("--kb", required=True)
        command.add_argument("--build-id", required=True)
        command.add_argument("--directory", required=True)
        command.add_argument("--chunk-version", required=True)
        command.add_argument("--embedding-model", required=True)
        command.add_argument("--dimensions", required=True, type=_dimensions)
        command.add_argument("--lease-seconds", type=_lease_seconds, default=300.0)

    publish = commands.add_parser(
        "publish-index", help="publish one immutable index directory"
    )
    add_index_arguments(publish)
    enqueue_index = commands.add_parser(
        "enqueue-index", help="prepare an immutable index for distributed publication"
    )
    add_index_arguments(enqueue_index)
    gc = commands.add_parser(
        "gc-index", help="remove non-current immutable generations"
    )
    gc.add_argument("--retention-seconds", type=_retention_seconds, default=7 * 86400.0)
    gc.add_argument("--limit", type=_gc_limit, default=100)
    scrub = commands.add_parser(
        "scrub-index", help="stream-verify authoritative index generations"
    )
    scrub.add_argument("--limit", type=_gc_limit, default=100)
    replay = commands.add_parser("replay-job", help="replay one dead-letter job")
    replay.add_argument("--job-id", required=True)
    replay.add_argument("--replay-key", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runtime: HARuntime | None = None
    try:
        runtime = _runtime()
        if args.command == "doctor":
            return _doctor(runtime)
        if args.command == "serve":
            return _serve(runtime)
        if args.command == "scheduler-once":
            fires, delivered = runtime.scheduler.run_once()
            _json({"fires": fires, "delivered": delivered})
            return 0
        if args.command == "outbox-once":
            if runtime.outbox_dispatcher is None:
                raise RuntimeError(
                    "configure an HTTPS COGDOC_WEBHOOK_URL for outbox delivery"
                )
            _json({"delivered": runtime.outbox_dispatcher.run_once()})
            return 0
        if args.command == "migrate":
            compatible = runtime.versions.contract_floor()
            if compatible is None:
                compatible = args.minimum_compatible_version
            elif args.minimum_compatible_version is not None:
                compatible = min(compatible, args.minimum_compatible_version)
            rows = MigrationRunner(
                runtime.backend,
                REGISTERED_MIGRATIONS,
                owner_id=runtime.config.worker_id,
            ).run(
                batch_size=args.batch_size,
                max_batches=args.max_batches,
                allow_contract=args.allow_contract,
                minimum_compatible_version=compatible,
            )
            _json({"migrations": rows})
            return 0
        if args.command == "publish-index":
            return _publish(runtime, args)
        if args.command == "enqueue-index":
            return _enqueue_index(runtime, args)
        if args.command == "gc-index":
            return _gc(runtime, args)
        if args.command == "scrub-index":
            return _scrub(runtime, args)
        if args.command == "replay-job":
            row = runtime.jobs.replay_dead_letter(
                args.job_id, replay_key=args.replay_key
            )
            _json(
                {
                    "job_id": row["job_id"],
                    "replay_of": row["replay_of"],
                    "status": row["status"],
                }
            )
            return 0
        raise AssertionError("unhandled command")
    except Exception as exc:
        print(f"cogdoc-ha: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if runtime is not None:
            try:
                runtime.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
