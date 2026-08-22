from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from cogdoc.api.research_job_store import (
    ResearchJobRevisionConflictError,
    SqliteResearchJobStore,
    research_run_control,
)
from cogdoc.ha.research import (
    DISPATCH_SUCCEEDED,
    ResearchDispatchStore,
    StaleResearchDispatch,
)
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.service.research_artifact_composer import (
    canonical_research_gap_content,
    compose_research_markdown,
)
from cogdoc.service.research_execution import ResearchExecutionManager
from cogdoc.service.research_provenance import (
    RESEARCH_CONTRACT_VERSION,
    RESEARCH_PROVENANCE_VERSION,
)


def _wait_for(store: SqliteResearchJobStore, job_id: str, status: str) -> dict:
    deadline = time.monotonic() + 5
    row = None
    while time.monotonic() < deadline:
        row = store.get(job_id)
        if row is not None and row["status"] == status:
            return row
        time.sleep(0.01)
    raise AssertionError(f"research job did not reach {status}: {row!r}")


def _gap_report(current: dict) -> dict:
    section = current["sections"][0]
    result = {
        "section_id": str(section["section_id"]),
        "status": "insufficient_evidence",
        "verification_status": "insufficient",
        "verification_reason_code": "insufficient_evidence",
        "evidence_requirement_results": [],
        "content": canonical_research_gap_content(
            "insufficient_evidence", "insufficient"
        ),
        "citation_ledger": [],
        "claim_audit": {},
        "coverage_audit": {},
        "evidence": [],
        "error": "",
    }
    markdown, ledger = compose_research_markdown(
        current, [{"title": str(section["title"]), **result}]
    )
    return {
        "status": "ready_with_gaps",
        "markdown": markdown,
        "citation_ledger": list(ledger),
        "verification_metrics": {},
        "sections": [result],
    }


def _provenance() -> dict:
    return {
        "schema_version": RESEARCH_PROVENANCE_VERSION,
        "kb_id": "kb",
        "index_generation": "generation-1",
        "index_build_version": "index-build-v1",
        "chunk_identity_version": "chunk-identity-v1",
        "source_versions": [],
        "derived_knowledge_revision": "derived-1",
        "retrieval_tuning_revision": "tuning-1",
        "research_contract_version": RESEARCH_CONTRACT_VERSION,
        "research_contract_revision": "contract-1",
        "captured_at": "2026-08-22T00:00:00+00:00",
    }


def test_shared_research_store_serializes_revision_updates(tmp_path):
    backend = SQLiteBackend(tmp_path / "research.db")
    first = SqliteResearchJobStore(None, backend=backend)
    second = SqliteResearchJobStore(None, backend=backend)
    row = first.create(kb_id="kb", objective="objective")
    barrier = threading.Barrier(2)

    def update(store: SqliteResearchJobStore, title: str) -> str:
        barrier.wait()
        sections = [
            {**section, "title": f"{title}-{index}"}
            for index, section in enumerate(row["sections"])
        ]
        try:
            store.update_plan(
                row["job_id"], sections=sections, expected_revision=1
            )
        except ResearchJobRevisionConflictError:
            return "conflict"
        return "updated"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(update, first, "first"),
            pool.submit(update, second, "second"),
        )
        outcomes = {future.result() for future in futures}
    assert outcomes == {"updated", "conflict"}
    assert first.get(row["job_id"])["revision"] == 2


def test_research_dispatch_expiry_fences_old_worker(tmp_path):
    now = [100.0]
    backend = SQLiteBackend(tmp_path / "dispatch.db")
    store = ResearchDispatchStore(backend, clock=lambda: now[0])
    queued = store.enqueue("rj-one", "evidence", "attempt-one")
    first = store.claim("node-a", lease_seconds=10)
    assert first is not None and first["dispatch_id"] == queued["dispatch_id"]
    now[0] = 111.0
    second = store.claim("node-b", lease_seconds=10)
    assert second is not None and second["dispatch_id"] == queued["dispatch_id"]
    with pytest.raises(StaleResearchDispatch):
        store.heartbeat(
            queued["dispatch_id"],
            "node-a",
            str(first["lease_token"]),
            lease_seconds=10,
        )


def test_research_takeover_rejects_old_worker_failure(tmp_path):
    backend = SQLiteBackend(tmp_path / "takeover.db")
    store = SqliteResearchJobStore(None, backend=backend)
    created = store.create(
        kb_id="kb", objective="objective", section_titles=["section"]
    )
    started = store.start(created["job_id"])
    attempt_id = str(started["execution_id"])
    first = store.activate_distributed_attempt(
        created["job_id"], phase="evidence", attempt_id=attempt_id
    )
    first_lease = str(research_run_control(first, "evidence")["lease_id"])
    _claimed, section = store.claim_next_section(
        created["job_id"], attempt_id, lease_id=first_lease
    )
    assert section is not None

    second = store.activate_distributed_attempt(
        created["job_id"], phase="evidence", attempt_id=attempt_id
    )
    second_lease = str(research_run_control(second, "evidence")["lease_id"])
    assert second_lease != first_lease
    assert second["sections"][0]["status"] == "pending"

    stale = store.fail_section(
        created["job_id"],
        str(section["section_id"]),
        execution_id=attempt_id,
        lease_id=first_lease,
        error_class="OldWorkerError",
    )
    assert stale["status"] == "running"
    assert stale["sections"][0]["status"] == "pending"
    assert research_run_control(stale, "evidence")["lease_id"] == second_lease


def test_research_dispatch_prunes_only_old_terminal_rows(tmp_path):
    now = [100.0]
    backend = SQLiteBackend(tmp_path / "prune.db")
    store = ResearchDispatchStore(backend, clock=lambda: now[0])
    old = store.enqueue("rj-old", "evidence", "attempt-old")
    claimed = store.claim("node-a", lease_seconds=10)
    assert claimed is not None
    store.finish(
        str(old["dispatch_id"]),
        "node-a",
        str(claimed["lease_token"]),
        status=DISPATCH_SUCCEEDED,
    )
    now[0] = 200.0
    fresh = store.enqueue("rj-fresh", "evidence", "attempt-fresh")
    assert store.prune_terminal(before=150.0, limit=10) == 1
    assert store.get(str(old["dispatch_id"])) is None
    assert store.get(str(fresh["dispatch_id"])) is not None


def test_shared_research_kb_delete_atomically_removes_dispatches(tmp_path):
    backend = SQLiteBackend(tmp_path / "delete.db")
    jobs = SqliteResearchJobStore(None, backend=backend)
    dispatches = ResearchDispatchStore(backend)
    created = jobs.create(kb_id="kb", objective="objective")
    started = jobs.start(created["job_id"])
    dispatch = dispatches.enqueue(
        created["job_id"], "evidence", str(started["execution_id"])
    )
    jobs.clear_kb("kb")
    assert jobs.get(created["job_id"]) is None
    assert dispatches.get(str(dispatch["dispatch_id"])) is None


def test_research_attempt_can_execute_on_another_node(tmp_path):
    backend = SQLiteBackend(tmp_path / "shared.db")
    writer = SqliteResearchJobStore(None, backend=backend)
    worker_store = SqliteResearchJobStore(None, backend=backend)
    dispatches = ResearchDispatchStore(backend)
    manager_a = ResearchExecutionManager(
        writer,
        retrieve=lambda _kb, _query: [],
        kb_exists=lambda _kb: True,
        dispatch_store=dispatches,
        worker_id="node-a",
        dispatch_lease_seconds=30,
    )
    manager_b = ResearchExecutionManager(
        worker_store,
        retrieve=lambda _kb, _query: [],
        kb_exists=lambda _kb: True,
        dispatch_store=dispatches,
        worker_id="node-b",
        dispatch_lease_seconds=30,
    )
    row = writer.create(
        kb_id="kb", objective="objective", section_titles=["section"]
    )
    accepted = manager_a.start(row["job_id"])
    assert accepted["status"] == "running"
    assert manager_b.dispatch_once() is True
    completed = _wait_for(writer, row["job_id"], "evidence_ready")
    assert completed["sections"][0]["status"] == "completed"
    manager_a.shutdown(wait=True)
    manager_b.shutdown(wait=True)


def test_research_report_can_execute_on_another_node(tmp_path):
    backend = SQLiteBackend(tmp_path / "shared-report.db")
    writer = SqliteResearchJobStore(None, backend=backend)
    worker_store = SqliteResearchJobStore(None, backend=backend)
    dispatches = ResearchDispatchStore(backend)
    manager_a = ResearchExecutionManager(
        writer,
        retrieve=lambda _kb, _query: [],
        kb_exists=lambda _kb: True,
        report_builder=_gap_report,
        provenance_reader=lambda _kb: _provenance(),
        dispatch_store=dispatches,
        worker_id="node-a",
        dispatch_lease_seconds=30,
    )
    manager_b = ResearchExecutionManager(
        worker_store,
        retrieve=lambda _kb, _query: [],
        kb_exists=lambda _kb: True,
        report_builder=_gap_report,
        provenance_reader=lambda _kb: _provenance(),
        dispatch_store=dispatches,
        worker_id="node-b",
        dispatch_lease_seconds=30,
    )
    row = writer.create(
        kb_id="kb", objective="objective", section_titles=["section"]
    )
    manager_a.start(row["job_id"])
    assert manager_b.dispatch_once() is True
    _wait_for(writer, row["job_id"], "evidence_ready")

    accepted = manager_a.compile(row["job_id"])
    assert accepted["status"] == "generating"
    assert manager_b.dispatch_once() is True
    completed = _wait_for(writer, row["job_id"], "completed")
    assert completed["report_status"] == "ready_with_gaps"
    assert completed["report"]["content"].startswith("# ")
    manager_a.shutdown(wait=True)
    manager_b.shutdown(wait=True)


def test_distributed_research_readiness_tracks_dispatcher_lifecycle(tmp_path):
    backend = SQLiteBackend(tmp_path / "readiness.db")
    store = SqliteResearchJobStore(None, backend=backend)
    dispatches = ResearchDispatchStore(backend)
    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb, _query: [],
        kb_exists=lambda _kb: True,
        dispatch_store=dispatches,
        worker_id="node-a",
    )
    assert manager.check() is False
    manager.start_dispatcher()
    assert manager.check() is True
    manager.shutdown(wait=True)
    assert manager.check() is False


def test_distributed_reconcile_redispatches_every_active_page(tmp_path):
    backend = SQLiteBackend(tmp_path / "recover.db")
    store = SqliteResearchJobStore(None, backend=backend)
    dispatches = ResearchDispatchStore(backend)
    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb, _query: [],
        kb_exists=lambda _kb: True,
        dispatch_store=dispatches,
        worker_id="node-a",
    )
    rows = [
        store.create(kb_id="kb", objective=f"objective {index}")
        for index in range(1001)
    ]
    for row in rows:
        store.start(row["job_id"])
    assert manager.reconcile_orphans() == 1001
    manager.shutdown(wait=True)
