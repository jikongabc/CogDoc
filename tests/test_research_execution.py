import time
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from cogdoc.api.metrics import Metrics
from cogdoc.api.research_job_store import (
    ResearchJobStateConflictError,
    ResearchJobStore,
)
from cogdoc.service.research_artifact_composer import (
    canonical_research_gap_content,
    compose_research_markdown,
)
from cogdoc.service.research_execution import (
    ResearchEvidenceStaleError,
    ResearchExecutionCapacityError,
    ResearchExecutionManager,
    public_research_evidence,
)
from cogdoc.service.research_observability import ResearchObserver
from cogdoc.research_control import research_checkpoint
from cogdoc.service.kb_readers import has_readers
from cogdoc.service.research_provenance import (
    RESEARCH_CONTRACT_VERSION,
    RESEARCH_PROVENANCE_VERSION,
)


def _wait_for(store, job_id: str, expected: str, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = store.get(job_id)
        if row["status"] == expected:
            return row
        time.sleep(0.01)
    raise AssertionError(f"job did not reach {expected}: {store.get(job_id)}")


def _doc(chunk_id: str, text: str = "完整证据正文"):
    return {
        "text": text,
        "meta": {
            "chunk_id": chunk_id,
            "source": "rules.pdf",
            "page": 2,
            "section_title": "报名规则",
        },
        "retrieval": {"rerank_score": 0.9, "search_channel": "hybrid"},
    }


def _provenance(generation: str = "generation-1"):
    return {
        "schema_version": RESEARCH_PROVENANCE_VERSION,
        "kb_id": "kb",
        "index_generation": generation,
        "index_build_version": "index-build-v1",
        "chunk_identity_version": "chunk-identity-v1",
        "source_versions": [
            {"source": "rules.pdf", "sha256": "source-sha-1"}
        ],
        "derived_knowledge_revision": "derived-1",
        "retrieval_tuning_revision": "tuning-1",
        "research_contract_version": RESEARCH_CONTRACT_VERSION,
        "research_contract_revision": "contract-1",
        "captured_at": "2026-08-09T00:00:00+00:00",
    }


def _report_result(current, sections, *, status, verification_metrics):
    title_by_id = {
        section["section_id"]: section["title"] for section in current["sections"]
    }
    normalized = [
        {"title": title_by_id[section["section_id"]], **section}
        for section in sections
    ]
    markdown, ledger = compose_research_markdown(current, normalized)
    return {
        "status": status,
        "markdown": markdown,
        "citation_ledger": list(ledger),
        "verification_metrics": verification_metrics,
        "sections": normalized,
    }


def _grounded_section(current, content: str = "结论。"):
    section = current["sections"][0]
    evidence = dict(section["evidence"][0])
    citation = "[rules.pdf:P2]"
    answer = f"{content}{citation}"
    start = len(content)
    requirement_ids = list(section["evidence_requirement_ids"])
    return {
        "section_id": "s1",
        "status": "generated",
        "verification_status": "supported",
        "verification_reason_code": "supported",
        "evidence_requirement_results": [
            {
                "requirement_id": requirement_id,
                "status": "supported",
                "reason_code": "supported",
                "evidence_count": 1,
            }
            for requirement_id in requirement_ids
        ],
        "content": answer,
        "citation_ledger": [
            {
                "evidence_id": "E001",
                "chunk_id": evidence["chunk_id"],
                "source_type": "document",
                "source": "rules.pdf",
                "page": 2,
                "page_start": 2,
                "page_end": 2,
                "span_start": evidence["span_start"],
                "span_end": evidence["span_end"],
                "occurrences": [
                    {
                        "index": 0,
                        "answer_start": start,
                        "answer_end": start + len(citation),
                    }
                ],
            }
        ],
        "claim_audit": {
            "status": "passed",
            "counts": {
                "claim_count": 1,
                "supported": 1,
                "unsupported": 0,
                "insufficient": 0,
                "cited": 1,
            },
        },
        "coverage_audit": {
            "status": "passed",
            "requirement_count": len(requirement_ids),
            "covered_count": len(requirement_ids),
            "missing_requirement_ids": [],
        },
        "evidence": [evidence],
        "error": "",
    }


def test_public_research_evidence_is_bounded_and_deduplicated():
    invalid_score = _doc("c2")
    invalid_score["retrieval"]["rerank_score"] = float("nan")
    evidence = public_research_evidence(
        [_doc("c1", "a" * 800), _doc("c1", "a" * 800), invalid_score],
        limit=2,
        preview_chars=20,
    )

    assert [item["chunk_id"] for item in evidence] == ["c1", "c2"]
    assert len(evidence[0]["text_preview"]) == 20
    assert "text" not in evidence[0]
    assert evidence[1]["rerank_score"] is None


def test_public_research_evidence_retains_distinct_spans_of_the_same_chunk():
    first = _doc("c1", "first view")
    first["retrieval"]["evidence_text_start"] = 10
    second = _doc("c1", "second view")
    second["retrieval"]["evidence_text_start"] = 40

    evidence = public_research_evidence([first, second], limit=2)

    assert [
        (item["chunk_id"], item["span_start"], item["span_end"])
        for item in evidence
    ] == [("c1", 10, 20), ("c1", 40, 51)]


def test_research_review_and_publish_commit_inside_kb_read_lease():
    observed = []

    class Store:
        @staticmethod
        def get(job_id):
            return {"job_id": job_id, "kb_id": "kb"}

        @staticmethod
        def review_report(job_id, **kwargs):
            observed.append(("review", job_id, has_readers("kb"), kwargs))
            return {"job_id": job_id}

        @staticmethod
        def publish_report(job_id, **kwargs):
            observed.append(("publish", job_id, has_readers("kb"), kwargs))
            return {"job_id": job_id}

    manager = ResearchExecutionManager(
        Store(), retrieve=lambda _kb, _query: [], kb_exists=lambda _kb: True
    )
    try:
        manager.review_report(
            "rj",
            decisions=[{"section_id": "s1", "decision": "approved"}],
            expected_revision=4,
            reviewer_actor="reviewer:fingerprint",
        )
        manager.publish_report(
            "rj",
            expected_revision=5,
            publisher_actor="reviewer:fingerprint",
        )
    finally:
        manager.shutdown()

    assert [row[:3] for row in observed] == [
        ("review", "rj", True),
        ("publish", "rj", True),
    ]
    assert observed[0][3]["reviewer_actor"] == "reviewer:fingerprint"
    assert observed[1][3]["publisher_actor"] == "reviewer:fingerprint"


def test_research_execution_manager_collects_section_evidence(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(
        kb_id="kb", objective="研究", section_titles=["有证据", "无证据"]
    )

    def retrieve(kb_id, query):
        assert kb_id == "kb"
        return [_doc("c1")] if "有证据" in query else []

    manager = ResearchExecutionManager(
        store, retrieve=retrieve, kb_exists=lambda kb_id: kb_id == "kb"
    )
    try:
        started = manager.start(job["job_id"])
        assert started["status"] == "running"
        completed = _wait_for(store, job["job_id"], "evidence_ready")
    finally:
        manager.shutdown()

    assert [section["evidence_status"] for section in completed["sections"]] == [
        "partial",
        "missing",
    ]
    assert completed["sections"][0]["evidence"][0]["chunk_id"] == "c1"
    assert completed["sections"][0]["execution_metrics"]["candidate_count"] == 1


def test_research_execution_observer_records_durable_background_outcome(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(kb_id="kb", objective="观测", section_titles=["章节"])
    metrics = Metrics()
    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb_id, _query: [_doc("observed")],
        kb_exists=lambda _kb_id: True,
        observer=ResearchObserver(metrics),
    )
    try:
        manager.start(job["job_id"])
        _wait_for(store, job["job_id"], "evidence_ready")
    finally:
        manager.shutdown()

    scraped = metrics.render().decode()
    assert (
        'cogdoc_research_background_total{outcome="succeeded",stage="evidence"} '
        "1.0"
    ) in scraped
    assert (
        'cogdoc_research_background_in_progress{stage="evidence"} 0.0'
        in scraped
    )
    assert "cogdoc_research_section_candidate_count_count 1.0" in scraped
    assert "cogdoc_research_section_evidence_count_count 1.0" in scraped


def test_runtime_report_builder_honors_persisted_local_mode(tmp_path, monkeypatch):
    from cogdoc.service import research_report

    modes = []

    def fake_from_runtime(cls, *, state_runtime, is_local=False, **_kwargs):
        assert state_runtime.marker == "runtime"
        modes.append(is_local)
        return lambda job: {"job_id": job["job_id"], "is_local": is_local}

    monkeypatch.setattr(
        research_report.ResearchReportBuilder,
        "from_runtime",
        classmethod(fake_from_runtime),
    )
    store = ResearchJobStore(str(tmp_path / "research.json"))
    manager = ResearchExecutionManager.from_runtime(
        store,
        state_runtime=SimpleNamespace(marker="runtime"),
        kb_exists=lambda _kb_id: True,
    )
    try:
        result = manager._report_builder(
            {"job_id": "rj_local", "is_local": True}
        )
    finally:
        manager.shutdown()

    assert modes == [True]
    assert result == {"job_id": "rj_local", "is_local": True}


def test_research_execution_manager_pauses_between_sections(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(
        kb_id="kb", objective="研究", section_titles=["第一章", "第二章"]
    )
    entered = Event()
    release = Event()

    def retrieve(_kb_id, query):
        if "第一章" in query:
            entered.set()
            assert release.wait(2)
        return [_doc(query)]

    manager = ResearchExecutionManager(
        store, retrieve=retrieve, kb_exists=lambda _kb_id: True
    )
    try:
        manager.start(job["job_id"])
        assert entered.wait(2)
        paused = manager.pause(job["job_id"])
        assert paused["status"] == "paused"
        release.set()
        paused_after_section = _wait_for(store, job["job_id"], "paused")
        deadline = time.monotonic() + 2
        while (
            paused_after_section["sections"][0]["status"] != "completed"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            paused_after_section = store.get(job["job_id"])
        assert paused_after_section["sections"][0]["status"] == "completed"
        assert paused_after_section["sections"][1]["status"] == "pending"

        manager.resume(job["job_id"])
        completed = _wait_for(store, job["job_id"], "evidence_ready")
    finally:
        release.set()
        manager.shutdown()

    assert all(section["status"] == "completed" for section in completed["sections"])


def test_research_execution_manager_records_failure_and_allows_retry(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(kb_id="kb", objective="研究", section_titles=["章节"])
    attempts = 0

    def retrieve(_kb_id, _query):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("temporary")
        return [_doc("c1")]

    manager = ResearchExecutionManager(
        store, retrieve=retrieve, kb_exists=lambda _kb_id: True
    )
    try:
        manager.start(job["job_id"])
        failed = _wait_for(store, job["job_id"], "failed")
        assert failed["error"] == "TimeoutError"
        manager.start(job["job_id"])
        completed = _wait_for(store, job["job_id"], "evidence_ready")
    finally:
        manager.shutdown()

    assert attempts == 2
    assert completed["sections"][0]["evidence_status"] == "partial"


def test_research_execution_manager_builds_and_persists_report(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(kb_id="kb", objective="研究", section_titles=["章节"])
    metrics = Metrics()

    def report_builder(current):
        assert current["status"] == "generating"
        return _report_result(
            current,
            [_grounded_section(current)],
            status="ready",
            verification_metrics={"supported_count": 1},
        )

    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb_id, _query: [_doc("c1")],
        kb_exists=lambda _kb_id: True,
        report_builder=report_builder,
        provenance_reader=lambda _kb_id: _provenance(),
        observer=ResearchObserver(metrics),
    )
    try:
        manager.start(job["job_id"])
        _wait_for(store, job["job_id"], "evidence_ready")
        generating = manager.compile(job["job_id"])
        assert generating["status"] == "generating"
        completed = _wait_for(store, job["job_id"], "completed")
    finally:
        manager.shutdown()

    assert completed["report_status"] == "ready"
    assert completed["report"]["content"].startswith("# 研究报告")
    assert completed["sections"][0]["generation_status"] == "generated"
    scraped = metrics.render().decode()
    assert (
        'cogdoc_research_background_total{outcome="succeeded",stage="report"} '
        "1.0"
    ) in scraped
    assert (
        'cogdoc_claim_audit_runs_total{status="passed",task_type="research"} 1.0'
        in scraped
    )
    assert 'cogdoc_research_coverage_audits_total{status="passed"} 1.0' in scraped


def test_research_evidence_revocation_before_commit_does_not_persist_output(
    tmp_path, monkeypatch
):
    from cogdoc.service import research_execution

    store = ResearchJobStore(str(tmp_path / "research.json"))
    authorization = {
        "version": "research-auth-v1",
        "tenant_id": "tenant-a",
        "created_by": "user-a",
        "creator_role": "editor",
        "auth_kind": "user_session",
        "mode": "all",
        "acl_epoch": 1,
        "allowed_sources": [],
    }
    job = store.create(
        kb_id="kb",
        objective="revoked evidence",
        section_titles=["section"],
        authorization=authorization,
    )
    authorized = True
    shape_evidence = research_execution.public_research_evidence

    def shape_then_revoke(docs, *args, **kwargs):
        nonlocal authorized
        evidence = shape_evidence(docs, *args, **kwargs)
        authorized = False
        return evidence

    monkeypatch.setattr(
        research_execution,
        "public_research_evidence",
        shape_then_revoke,
    )

    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb_id, _query: [_doc("revoked-evidence")],
        kb_exists=lambda _kb_id: True,
        authorization_checker=lambda _job: authorized,
    )
    try:
        manager.start(job["job_id"])
        failed = _wait_for(store, job["job_id"], "failed")
    finally:
        manager.shutdown()

    section = failed["sections"][0]
    assert section["status"] == "failed"
    assert section["evidence"] == []
    assert section["evidence_status"] == "unsearched"
    assert set(section["execution_metrics"]) == {"_research_control"}
    assert not {
        "candidate_count",
        "evidence_count",
        "query_count",
        "requirements",
    } & set(section["execution_metrics"])
    assert "revoked-evidence" not in str(failed)


def test_research_report_revocation_before_commit_does_not_persist_output(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    authorization = {
        "version": "research-auth-v1",
        "tenant_id": "tenant-a",
        "created_by": "user-a",
        "creator_role": "editor",
        "auth_kind": "user_session",
        "mode": "all",
        "acl_epoch": 1,
        "allowed_sources": [],
    }
    job = store.create(
        kb_id="kb",
        objective="revoked report",
        section_titles=["section"],
        authorization=authorization,
    )
    authorized = True
    report_built = False

    def provenance_after_possible_revocation(_kb_id):
        nonlocal authorized
        if report_built:
            authorized = False
        return _provenance()

    def build_then_mark(current):
        nonlocal report_built
        result = _report_result(
            current,
            [
                {
                    "section_id": "s1",
                    "status": "no_evidence",
                    "verification_status": "no_evidence",
                    "verification_reason_code": "no_direct_support",
                    "content": "revoked report body",
                    "claim_audit": {},
                    "coverage_audit": {},
                    "evidence": [],
                    "error": "",
                }
            ],
            status="ready_with_gaps",
            verification_metrics={"verification_error_count": 1},
        )
        report_built = True
        return result

    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb_id, _query: [],
        kb_exists=lambda _kb_id: True,
        report_builder=build_then_mark,
        provenance_reader=provenance_after_possible_revocation,
        authorization_checker=lambda _job: authorized,
    )
    try:
        manager.start(job["job_id"])
        _wait_for(store, job["job_id"], "evidence_ready")
        manager.compile(job["job_id"])
        failed = _wait_for(store, job["job_id"], "failed")
    finally:
        manager.shutdown()

    assert failed["report_status"] == "failed"
    assert failed["report"] is None
    assert failed["sections"][0]["citation_ledger"] == []
    assert "revoked report body" not in str(failed)


def test_research_execution_manager_fails_report_closed_and_allows_retry(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(kb_id="kb", objective="研究", section_titles=["章节"])
    attempts = 0

    def report_builder(current):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("temporary")
        return _report_result(
            current,
            [
                {
                    "section_id": "s1",
                    "status": "no_evidence",
                    "verification_status": "no_evidence",
                    "verification_reason_code": "no_direct_support",
                    "content": canonical_research_gap_content(
                        "no_evidence", "no_evidence"
                    ),
                    "claim_audit": {},
                    "coverage_audit": {},
                    "evidence": [],
                    "error": "",
                }
            ],
            status="ready_with_gaps",
            verification_metrics={"verification_error_count": 1},
        )

    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb_id, _query: [],
        kb_exists=lambda _kb_id: True,
        report_builder=report_builder,
        provenance_reader=lambda _kb_id: _provenance(),
    )
    try:
        manager.start(job["job_id"])
        _wait_for(store, job["job_id"], "evidence_ready")
        manager.compile(job["job_id"])
        failed = _wait_for(store, job["job_id"], "failed")
        assert failed["report_status"] == "failed"
        assert failed["error"] == "TimeoutError"
        manager.compile(job["job_id"])
        completed = _wait_for(store, job["job_id"], "completed")
    finally:
        manager.shutdown()

    assert attempts == 2
    assert completed["report_status"] == "ready_with_gaps"


def test_research_execution_manager_captures_provenance_on_start(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(kb_id="kb", objective="冻结输入", section_titles=["章节"])
    snapshot = _provenance()
    provenance_reads = []

    def read_provenance(kb_id):
        provenance_reads.append(kb_id)
        return snapshot

    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb_id, _query: [_doc("c1")],
        kb_exists=lambda _kb_id: True,
        provenance_reader=read_provenance,
    )
    try:
        started = manager.start(job["job_id"])
        completed = _wait_for(store, job["job_id"], "evidence_ready")
        status = manager.provenance(completed)
    finally:
        manager.shutdown()

    assert started["evidence_provenance"] == snapshot
    assert completed["evidence_provenance"] == snapshot
    assert status["status"] == "current"
    assert provenance_reads
    assert set(provenance_reads) == {"kb"}


def test_research_execution_manager_blocks_compile_after_index_drift(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(kb_id="kb", objective="编译门禁", section_titles=["章节"])
    current = _provenance()
    report_calls = 0

    def build_report(_job):
        nonlocal report_calls
        report_calls += 1
        raise AssertionError("stale evidence must not reach report generation")

    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb_id, _query: [_doc("c1")],
        kb_exists=lambda _kb_id: True,
        report_builder=build_report,
        provenance_reader=lambda _kb_id: current,
    )
    try:
        manager.start(job["job_id"])
        _wait_for(store, job["job_id"], "evidence_ready")
        current["index_generation"] = "generation-2"

        with pytest.raises(ResearchEvidenceStaleError) as caught:
            manager.compile(job["job_id"])
    finally:
        manager.shutdown()

    assert caught.value.reasons == ("index_generation_changed",)
    assert report_calls == 0
    assert store.get(job["job_id"])["report_status"] == "not_started"


def test_research_execution_manager_blocks_resume_after_index_drift(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(kb_id="kb", objective="恢复门禁", section_titles=["章节"])
    current = _provenance()
    entered = Event()
    release = Event()

    def retrieve(_kb_id, _query):
        entered.set()
        assert release.wait(2)
        return [_doc("c1")]

    manager = ResearchExecutionManager(
        store,
        retrieve=retrieve,
        kb_exists=lambda _kb_id: True,
        provenance_reader=lambda _kb_id: current,
    )
    try:
        manager.start(job["job_id"])
        assert entered.wait(2)
        manager.pause(job["job_id"])
        release.set()
        deadline = time.monotonic() + 2
        paused = store.get(job["job_id"])
        while (
            paused["sections"][0]["status"] != "completed"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            paused = store.get(job["job_id"])
        assert paused["status"] == "paused"
        assert paused["sections"][0]["status"] == "completed"
        current["index_generation"] = "generation-2"

        with pytest.raises(ResearchEvidenceStaleError) as caught:
            manager.resume(job["job_id"])
    finally:
        release.set()
        manager.shutdown()

    assert caught.value.reasons == ("index_generation_changed",)
    assert store.get(job["job_id"])["status"] == "paused"


def test_research_execution_manager_refresh_reruns_every_section(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(
        kb_id="kb",
        objective="全量刷新",
        section_titles=["第一章", "第二章"],
    )
    initial = store.start(
        job["job_id"], evidence_provenance=_provenance("generation-1")
    )
    for section_id in ("s1", "s2"):
        _, claimed = store.claim_next_section(
            job["job_id"], initial["execution_id"]
        )
        assert claimed["section_id"] == section_id
        store.complete_section(
            job["job_id"],
            section_id,
            execution_id=initial["execution_id"],
            evidence_status="partial",
            evidence=[{"chunk_id": f"old-{section_id}"}],
            execution_metrics={"candidate_count": 1},
        )
    old_execution_id = initial["execution_id"]
    refreshed_snapshot = _provenance("generation-2")
    queries = []

    def retrieve(_kb_id, query):
        queries.append(query)
        return [_doc(f"new-{len(queries)}")]

    manager = ResearchExecutionManager(
        store,
        retrieve=retrieve,
        kb_exists=lambda _kb_id: True,
        provenance_reader=lambda _kb_id: refreshed_snapshot,
    )
    try:
        refreshed = manager.refresh(job["job_id"])
        completed = _wait_for(store, job["job_id"], "evidence_ready")
    finally:
        manager.shutdown()

    assert refreshed["execution_id"] != old_execution_id
    assert refreshed["evidence_provenance"] == refreshed_snapshot
    assert len(queries) == 2
    assert [section["evidence"][0]["chunk_id"] for section in completed["sections"]] == [
        "new-1",
        "new-2",
    ]


def test_research_refresh_rejects_untrackable_snapshot_without_mutation(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(kb_id="kb", objective="保留旧报告", section_titles=["章节"])
    running = store.start(
        job["job_id"], evidence_provenance=_provenance("generation-1")
    )
    _, section = store.claim_next_section(job["job_id"], running["execution_id"])
    store.complete_section(
        job["job_id"],
        section["section_id"],
        execution_id=running["execution_id"],
        evidence_status="missing",
        evidence=[],
        execution_metrics={},
    )
    generating = store.begin_report(job["job_id"])
    store.complete_report(
        job["job_id"],
        report_execution_id=generating["report_execution_id"],
        result=_report_result(
            generating,
            [
                {
                    "section_id": "s1",
                    "status": "no_evidence",
                    "verification_status": "no_evidence",
                    "content": canonical_research_gap_content(
                        "no_evidence", "no_evidence"
                    ),
                }
            ],
            status="ready_with_gaps",
            verification_metrics={},
        ),
    )
    before = store.get(job["job_id"])
    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb_id, _query: [],
        kb_exists=lambda _kb_id: True,
        provenance_reader=lambda _kb_id: {
            "schema_version": RESEARCH_PROVENANCE_VERSION,
            "kb_id": "kb",
        },
    )
    try:
        with pytest.raises(
            ResearchJobStateConflictError,
            match="provenance is unavailable",
        ):
            manager.refresh(job["job_id"])
    finally:
        manager.shutdown()

    assert store.get(job["job_id"]) == before


def test_research_provenance_reader_error_is_bounded_and_non_mutating(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(kb_id="kb", objective="读取错误", section_titles=["章节"])

    def broken_reader(_kb_id):
        raise TimeoutError("provenance backend unavailable")

    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb_id, _query: [],
        kb_exists=lambda _kb_id: True,
        provenance_reader=broken_reader,
    )
    try:
        status = manager.provenance(job)
        listed_status = manager.provenance_many([job])[0]
        with pytest.raises(
            ResearchJobStateConflictError,
            match="provenance is unavailable",
        ):
            manager.start(job["job_id"])
    finally:
        manager.shutdown()

    assert status == listed_status == {
        "status": "untracked",
        "stale_reasons": ["provenance_reader_error:TimeoutError"],
        "captured": {},
        "current": {},
    }
    assert store.get(job["job_id"])["status"] == "planned"


def test_research_refresh_starts_new_execution_while_paused_worker_unwinds(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(kb_id="kb", objective="刷新竞态", section_titles=["章节"])
    old_entered = Event()
    refreshed_entered = Event()
    release_old = Event()
    calls = []

    def retrieve(_kb_id, _query):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            old_entered.set()
            assert release_old.wait(2)
            return [_doc("obsolete")]
        refreshed_entered.set()
        return [_doc("fresh")]

    manager = ResearchExecutionManager(
        store,
        retrieve=retrieve,
        kb_exists=lambda _kb_id: True,
        provenance_reader=lambda _kb_id: _provenance(),
        max_workers=2,
    )
    try:
        manager.start(job["job_id"])
        assert old_entered.wait(2)
        manager.pause(job["job_id"])
        refreshed = manager.refresh(job["job_id"])
        assert refreshed_entered.wait(2)
        completed = _wait_for(store, job["job_id"], "evidence_ready")
    finally:
        release_old.set()
        manager.shutdown()

    assert completed["execution_id"] == refreshed["execution_id"]
    assert completed["sections"][0]["evidence"][0]["chunk_id"] == "fresh"


def test_research_report_commit_fails_if_provenance_drifts_during_build(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(kb_id="kb", objective="报告竞态", section_titles=["章节"])
    current = _provenance()

    def build_report(_job):
        current["index_generation"] = "generation-2"
        return {
            "status": "ready",
            "markdown": "# 不应提交\n",
            "citation_ledger": [],
            "verification_metrics": {},
            "sections": [],
        }

    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb_id, _query: [_doc("evidence")],
        kb_exists=lambda _kb_id: True,
        report_builder=build_report,
        provenance_reader=lambda _kb_id: current,
    )
    try:
        manager.start(job["job_id"])
        _wait_for(store, job["job_id"], "evidence_ready")
        started = manager.compile(job["job_id"])
        failed = _wait_for(store, job["job_id"], "failed")
    finally:
        manager.shutdown()

    assert started["status"] == "generating"
    assert failed["report_status"] == "failed"
    assert failed["report"] is None
    assert failed["error"] == "ResearchEvidenceStaleError"


def test_research_resume_uses_fresh_lease_while_old_worker_unwinds(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    job = store.create(kb_id="kb", objective="恢复竞态", section_titles=["章节"])
    old_entered = Event()
    new_entered = Event()
    release_old = Event()
    calls = []

    def retrieve(_kb_id, _query):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            old_entered.set()
            assert release_old.wait(2)
            return [_doc("obsolete")]
        new_entered.set()
        return [_doc("fresh")]

    manager = ResearchExecutionManager(
        store,
        retrieve=retrieve,
        kb_exists=lambda _kb_id: True,
        provenance_reader=lambda _kb_id: _provenance(),
        max_workers=2,
    )
    try:
        started = manager.start(job["job_id"])
        assert old_entered.wait(2)
        manager.pause(job["job_id"])
        resumed = manager.resume(job["job_id"])
        assert resumed["execution_id"] == started["execution_id"]
        assert new_entered.wait(2)
        completed = _wait_for(store, job["job_id"], "evidence_ready")
    finally:
        release_old.set()
        manager.shutdown()

    assert completed["sections"][0]["evidence"][0]["chunk_id"] == "fresh"


def test_research_cancel_mid_section_prevents_next_requirement_query(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    created = store.create(kb_id="kb", objective="取消查询", section_titles=["章节"])
    planned = store.update_plan(
        created["job_id"],
        expected_revision=created["revision"],
        sections=[
            {
                "title": "章节",
                "research_question": "查明两项要求",
                "evidence_requirements": [
                    {
                        "question": "第一项",
                        "retrieval_query": "first",
                        "recovery_query": "first recovery",
                    },
                    {
                        "question": "第二项",
                        "retrieval_query": "second",
                        "recovery_query": "second recovery",
                    },
                ],
            }
        ],
    )
    entered = Event()
    release = Event()
    queries = []

    def retrieve(_kb_id, query):
        queries.append(query)
        entered.set()
        assert release.wait(2)
        return [_doc(query)]

    manager = ResearchExecutionManager(
        store,
        retrieve=retrieve,
        kb_exists=lambda _kb_id: True,
    )
    try:
        manager.start(planned["job_id"])
        assert entered.wait(2)
        manager.cancel(planned["job_id"])
        release.set()
    finally:
        release.set()
        manager.shutdown()

    assert queries == ["first"]
    assert store.get(planned["job_id"])["status"] == "cancelled"


def test_research_cancel_during_report_stops_at_cooperative_checkpoint(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    created = store.create(kb_id="kb", objective="取消生成", section_titles=["章节"])
    running = store.start(
        created["job_id"], evidence_provenance=_provenance()
    )
    _, section = store.claim_next_section(created["job_id"], running["execution_id"])
    store.complete_section(
        created["job_id"],
        section["section_id"],
        execution_id=running["execution_id"],
        evidence_status="missing",
        evidence=[],
        execution_metrics={},
    )
    entered = Event()
    release = Event()
    continued = Event()

    def report_builder(_job):
        entered.set()
        assert release.wait(2)
        research_checkpoint()
        continued.set()
        raise AssertionError("cancelled report continued after checkpoint")

    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb_id, _query: [],
        kb_exists=lambda _kb_id: True,
        report_builder=report_builder,
        provenance_reader=lambda _kb_id: _provenance(),
    )
    try:
        manager.compile(created["job_id"])
        assert entered.wait(2)
        cancelled = manager.cancel(created["job_id"])
        release.set()
    finally:
        release.set()
        manager.shutdown()

    assert cancelled["status"] == "cancelled"
    assert not continued.is_set()


def test_research_pending_admission_rejects_before_state_change(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    first = store.create(kb_id="kb", objective="占用队列", section_titles=["章节"])
    second = store.create(kb_id="kb", objective="被拒绝", section_titles=["章节"])
    entered = Event()
    release = Event()

    def retrieve(_kb_id, _query):
        entered.set()
        assert release.wait(2)
        return []

    manager = ResearchExecutionManager(
        store,
        retrieve=retrieve,
        kb_exists=lambda _kb_id: True,
        max_workers=1,
        max_pending=1,
    )
    try:
        manager.start(first["job_id"])
        assert entered.wait(2)
        with pytest.raises(ResearchJobStateConflictError, match="queue is full"):
            manager.start(second["job_id"])
    finally:
        release.set()
        manager.shutdown()

    assert store.get(second["job_id"])["status"] == "planned"


def test_research_admission_preserves_missing_and_store_lookup_errors(
    tmp_path,
    monkeypatch,
):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb_id, _query: [],
        kb_exists=lambda _kb_id: True,
        max_workers=1,
        max_pending=1,
    )
    try:
        monkeypatch.setattr(store, "get", lambda _job_id: None)
        with pytest.raises(KeyError):
            manager.start("missing")
        assert manager._submission_reservations == 0

        def broken_lookup(_job_id):
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(store, "get", broken_lookup)
        with pytest.raises(RuntimeError, match="store unavailable"):
            manager.refresh("missing")
        assert manager._submission_reservations == 0
    finally:
        manager.shutdown(wait=False)


def test_research_shutdown_is_bounded_and_invalidates_running_lease(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    created = store.create(kb_id="kb", objective="关闭", section_titles=["章节"])
    entered = Event()
    release = Event()

    def retrieve(_kb_id, _query):
        entered.set()
        release.wait()
        return [_doc("late")]

    manager = ResearchExecutionManager(
        store,
        retrieve=retrieve,
        kb_exists=lambda _kb_id: True,
        max_workers=1,
    )
    manager.start(created["job_id"])
    assert entered.wait(2)

    started = time.monotonic()
    drained = manager.shutdown(wait=False)
    elapsed = time.monotonic() - started
    stopped = store.get(created["job_id"])
    control = stopped["sections"][0]["execution_metrics"]["_research_control"][
        "evidence"
    ]

    assert elapsed < 1
    assert drained is False
    assert stopped["status"] == "paused"
    assert stopped["sections"][0]["status"] == "pending"
    assert control["control_state"] == "paused"
    assert control["lease_id"] == ""
    assert control["draining_lease_id"] == ""

    release.set()
    deadline = time.monotonic() + 2
    while not manager.shutdown(wait=False) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert store.get(created["job_id"])["status"] == "paused"


def test_research_shutdown_cannot_miss_start_registration_window(tmp_path):
    entered_store = Event()
    release_store = Event()

    class BlockingStartStore(ResearchJobStore):
        def start(self, *args, **kwargs):
            row = super().start(*args, **kwargs)
            entered_store.set()
            assert release_store.wait(2)
            return row

    store = BlockingStartStore(str(tmp_path / "research.json"))
    created = store.create(kb_id="kb", objective="竞态", section_titles=["章节"])
    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb_id, _query: [],
        kb_exists=lambda _kb_id: True,
        max_workers=1,
    )
    start_errors = []
    shutdown_finished = Event()

    def start_job():
        try:
            manager.start(created["job_id"])
        except BaseException as exc:
            start_errors.append(exc)

    starter = Thread(target=start_job)
    stopper = Thread(
        target=lambda: (manager.shutdown(wait=False), shutdown_finished.set())
    )
    starter.start()
    assert entered_store.wait(2)
    stopper.start()
    assert not shutdown_finished.wait(0.1)
    release_store.set()
    starter.join(timeout=2)
    stopper.join(timeout=2)

    assert not starter.is_alive()
    assert not stopper.is_alive()
    assert not start_errors
    stopped = store.get(created["job_id"])
    control = stopped["sections"][0]["execution_metrics"]["_research_control"][
        "evidence"
    ]
    assert stopped["status"] in {"paused", "evidence_ready"}
    assert control["control_state"] != "running"
    assert control["lease_id"] == ""


def test_research_shutdown_reports_undrained_pretransition_reservation(tmp_path):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    created = store.create(kb_id="kb", objective="准入竞态", section_titles=["章节"])
    entered = Event()
    release = Event()

    def blocked_provenance(_kb_id):
        entered.set()
        assert release.wait(timeout=3)
        return _provenance()

    manager = ResearchExecutionManager(
        store,
        retrieve=lambda _kb_id, _query: [],
        kb_exists=lambda _kb_id: True,
        provenance_reader=blocked_provenance,
        max_workers=1,
        max_pending=1,
    )
    errors = []

    def start_job():
        try:
            manager.start(created["job_id"])
        except BaseException as exc:
            errors.append(exc)

    starter = Thread(target=start_job)
    starter.start()
    assert entered.wait(timeout=2)
    assert manager.shutdown(wait=False) is False
    assert manager._submission_reservations == 1

    release.set()
    starter.join(timeout=3)
    assert not starter.is_alive()
    assert errors
    assert store.get(created["job_id"])["status"] == "planned"
    assert manager.shutdown(wait=False) is True


def test_research_executor_cancelled_queue_items_keep_physical_capacity_bounded(
    tmp_path,
):
    store = ResearchJobStore(str(tmp_path / "research.json"))
    jobs = [
        store.create(kb_id="kb", objective=f"研究 {position}", section_titles=["章节"])
        for position in range(8)
    ]
    entered = Event()
    release = Event()

    def blocked_retrieve(_kb_id, _query):
        entered.set()
        assert release.wait(timeout=3)
        return []

    manager = ResearchExecutionManager(
        store,
        retrieve=blocked_retrieve,
        kb_exists=lambda _kb_id: True,
        max_workers=1,
        max_pending=2,
    )
    try:
        manager.start(jobs[0]["job_id"])
        assert entered.wait(timeout=2)
        manager.start(jobs[1]["job_id"])
        manager.cancel(jobs[1]["job_id"])

        for job in jobs[2:]:
            with pytest.raises(ResearchExecutionCapacityError):
                manager.start(job["job_id"])
            assert store.get(job["job_id"])["status"] == "planned"

        assert manager._executor._queue.qsize() == 1
        assert manager._executor._pending == 2
    finally:
        release.set()
        manager.shutdown(wait=True)
