from __future__ import annotations

from cogdoc.service.evidence_unit_pipeline import (
    EvidenceUnitExecutionStatus,
    EvidenceUnitPipelinePolicy,
    _document_cost,
    retrieve_evidence_units,
)
from cogdoc.service.evidence_units import (
    EvidenceUnitBudget,
    build_compare_evidence_units,
    build_qa_evidence_units,
    build_summary_evidence_units,
)
from cogdoc.tools.reranker import BGEReranker


def _doc(source: str, chunk: str, text: str) -> dict:
    return {
        "text": text,
        "meta": {
            "chunk_id": chunk,
            "source_sha256": f"sha:{source}",
            "local_chunk_index": 0,
            "chunk_index": 0,
            "source": source,
            "page": 1,
            "page_start": 1,
            "page_end": 1,
            "origin": "file",
        },
    }


class _Engine:
    def __init__(self, docs, *, fail=False, recovery_only=False):
        self.docs = list(docs)
        self.fail = fail
        self.recovery_only = recovery_only
        self.calls = []

    def search(self, query, top_k=3, *, scope=None):
        self.calls.append((query, top_k, scope))
        if self.fail:
            raise RuntimeError("index unavailable")
        if self.recovery_only and "替代" not in query:
            return []
        docs = self.docs
        if scope is not None and scope.allowed_sources:
            docs = [
                doc for doc in docs if doc["meta"]["source"] in scope.allowed_sources
            ]
        return docs[:top_k]

    def load_source_chunks(self, source):
        return [doc for doc in self.docs if doc["meta"]["source"] == source]


class _NoDerived:
    def search(self, kb_id, query, top_k=3, *, scope=None):
        return []


class _NoFeedback:
    def boosts_for_query(self, kb_id, query):
        return {}


def _policy(**overrides):
    values = {
        "retrieval_top_k": 4,
        "recovery_top_k": 4,
        "rerank_max_candidates": 4,
        "rerank_top_n": 1,
        "parent_context_enabled": False,
        "evidence_span_enabled": False,
    }
    values.update(overrides)
    return EvidenceUnitPipelinePolicy(**values)


def _budget(unit_count: int, *, docs_per_unit=1, chars_per_unit=1000):
    return EvidenceUnitBudget(
        max_total_docs=unit_count * docs_per_unit,
        max_total_chars=unit_count * chars_per_unit,
        max_docs_per_unit=docs_per_unit,
        max_chars_per_unit=chars_per_unit,
    )


def test_compare_units_enforce_source_scope_and_freeze_one_global_ledger(monkeypatch):
    monkeypatch.setattr(BGEReranker, "default_device", lambda: "cpu")
    docs = [
        _doc("a.pdf", "a-method", "A 文档采用图检索方法。"),
        _doc("b.pdf", "b-method", "B 文档采用关键词方法。"),
        _doc("distractor.pdf", "wrong", "无关文档具有极高相关性。"),
    ]
    units = build_compare_evidence_units(
        "对比 a.pdf 和 b.pdf",
        ["a.pdf", "b.pdf"],
        [{"dimension_id": "method", "title": "方法", "instruction": "概括方法"}],
    )

    batch = retrieve_evidence_units(
        units,
        kb_id="kb",
        original_query="对比 a.pdf 和 b.pdf",
        engine=_Engine(docs),
        derived_knowledge_retriever=_NoDerived(),
        retrieval_feedback_store=_NoFeedback(),
        budget=_budget(2),
        policy=_policy(),
        rrf_k=60.0,
    )

    assert [result.status for result in batch.results] == [
        EvidenceUnitExecutionStatus.READY,
        EvidenceUnitExecutionStatus.READY,
    ]
    assert [result.selected_docs[0]["meta"]["source"] for result in batch.results] == [
        "a.pdf",
        "b.pdf",
    ]
    assert [
        result.selected_docs[0]["retrieval"]["evidence_id"] for result in batch.results
    ] == ["E001", "E002"]
    assert len(batch.evidence_ledger) == 2


def test_missing_primary_query_retries_only_with_the_unit_recovery_query(monkeypatch):
    monkeypatch.setattr(BGEReranker, "default_device", lambda: "cpu")
    units = build_summary_evidence_units(
        "总结 a.pdf",
        "a.pdf",
        [
            {
                "section_id": "method",
                "title": "方法",
                "instruction": "概括方法",
                "retrieval_query": "主要方法",
                "recovery_query": "替代 实施流程",
            }
        ],
    )
    engine = _Engine(
        [_doc("a.pdf", "a-method", "文档说明了实施流程。")],
        recovery_only=True,
    )

    batch = retrieve_evidence_units(
        units,
        kb_id="kb",
        original_query="总结 a.pdf",
        engine=engine,
        derived_knowledge_retriever=_NoDerived(),
        retrieval_feedback_store=_NoFeedback(),
        budget=_budget(1),
        policy=_policy(),
        rrf_k=60.0,
    )

    result = batch.results[0]
    assert result.status is EvidenceUnitExecutionStatus.READY
    assert result.retrieval_round == 1
    assert result.executed_queries == (
        units[0].retrieval_query,
        units[0].recovery_query,
    )


def test_explicit_recovery_phase_keeps_plan_and_uses_only_recovery_query(monkeypatch):
    monkeypatch.setattr(BGEReranker, "default_device", lambda: "cpu")
    units = build_summary_evidence_units(
        "总结 a.pdf",
        "a.pdf",
        [
            {
                "section_id": "method",
                "title": "方法",
                "instruction": "概括方法",
                "retrieval_query": "主要方法",
                "recovery_query": "替代 实施流程",
            }
        ],
    )
    engine = _Engine(
        [_doc("a.pdf", "a-method", "文档说明了实施流程。")],
        recovery_only=True,
    )

    batch = retrieve_evidence_units(
        units,
        kb_id="kb",
        original_query="总结 a.pdf",
        engine=engine,
        derived_knowledge_retriever=_NoDerived(),
        retrieval_feedback_store=_NoFeedback(),
        budget=_budget(1),
        policy=_policy(),
        rrf_k=60.0,
        query_phase="recovery",
        retrieval_round=2,
    )

    result = batch.results[0]
    assert result.unit is units[0]
    assert result.status is EvidenceUnitExecutionStatus.READY
    assert result.retrieval_round == 2
    assert result.executed_queries == (units[0].recovery_query,)
    assert {call[0] for call in engine.calls} == {units[0].recovery_query}


def test_unrestricted_unit_preserves_legacy_retriever_call_shape(monkeypatch):
    monkeypatch.setattr(BGEReranker, "default_device", lambda: "cpu")
    doc = _doc("a.pdf", "answer", "答案是四十二。")

    class LegacyEngine:
        def search(self, query, top_k=3):
            return [doc]

        def load_source_chunks(self, source):
            return [doc]

    class LegacyDerived:
        def search(self, kb_id, query, top_k=3):
            return []

    units = build_qa_evidence_units(
        "答案是多少",
        [
            {
                "requirement_id": "r1",
                "question": "答案是多少",
                "retrieval_query": "答案",
                "recovery_query": "精确答案",
            }
        ],
    )

    batch = retrieve_evidence_units(
        units,
        kb_id="kb",
        original_query="答案是多少",
        engine=LegacyEngine(),
        derived_knowledge_retriever=LegacyDerived(),
        retrieval_feedback_store=_NoFeedback(),
        budget=_budget(1),
        policy=_policy(),
        rrf_k=60.0,
    )

    assert batch.results[0].status is EvidenceUnitExecutionStatus.READY
    assert batch.results[0].selected_docs[0]["meta"]["chunk_id"] == "answer"


def test_loaded_source_fallback_is_explicit_and_stays_inside_scope(monkeypatch):
    monkeypatch.setattr(BGEReranker, "default_device", lambda: "cpu")
    source_doc = _doc("a.pdf", "a-method", "文档说明了方法和流程。")
    units = build_summary_evidence_units(
        "总结 a.pdf",
        "a.pdf",
        [{"section_id": "method", "title": "方法", "instruction": "概括流程"}],
    )

    batch = retrieve_evidence_units(
        units,
        kb_id="kb",
        original_query="总结 a.pdf",
        engine=_Engine([source_doc], fail=True),
        derived_knowledge_retriever=_NoDerived(),
        retrieval_feedback_store=_NoFeedback(),
        budget=_budget(1),
        policy=_policy(),
        rrf_k=60.0,
        fallback_docs_by_source={"a.pdf": [source_doc]},
    )

    result = batch.results[0]
    assert result.status is EvidenceUnitExecutionStatus.READY
    assert result.fallback_used is True
    assert result.selected_docs[0]["meta"]["source"] == "a.pdf"


def test_compare_admission_group_fails_atomically_when_batch_chars_are_tight(
    monkeypatch,
):
    monkeypatch.setattr(BGEReranker, "default_device", lambda: "cpu")
    docs = [
        _doc("a.pdf", "a", "A" * 180),
        _doc("b.pdf", "b", "B" * 180),
    ]
    units = build_compare_evidence_units(
        "对比 a.pdf 和 b.pdf",
        ["a.pdf", "b.pdf"],
        [{"dimension_id": "method", "title": "方法", "instruction": "概括方法"}],
    )
    # Each unit fits its own 400-char closure, but both first documents cannot
    # fit the dimension-level 500-char batch reservation together.
    budget = EvidenceUnitBudget(
        max_total_docs=2,
        max_total_chars=500,
        max_docs_per_unit=1,
        max_chars_per_unit=400,
    )

    batch = retrieve_evidence_units(
        units,
        kb_id="kb",
        original_query="对比 a.pdf 和 b.pdf",
        engine=_Engine(docs),
        derived_knowledge_retriever=_NoDerived(),
        retrieval_feedback_store=_NoFeedback(),
        budget=budget,
        policy=_policy(),
        rrf_k=60.0,
    )

    assert [result.status for result in batch.results] == [
        EvidenceUnitExecutionStatus.BUDGET_EXHAUSTED,
        EvidenceUnitExecutionStatus.BUDGET_EXHAUSTED,
    ]
    assert batch.evidence_ledger == ()


def test_required_unit_enforces_minimum_character_reservation(monkeypatch):
    monkeypatch.setattr(BGEReranker, "default_device", lambda: "cpu")
    doc = _doc("a.pdf", "short", "短")
    units = build_summary_evidence_units(
        "总结 a.pdf",
        "a.pdf",
        [{"section_id": "summary", "title": "摘要", "instruction": "概括"}],
    )
    minimum = _document_cost(doc) + 1
    budget = EvidenceUnitBudget(
        max_total_docs=1,
        max_total_chars=minimum,
        max_docs_per_unit=1,
        max_chars_per_unit=minimum,
        min_chars_per_required_unit=minimum,
    )
    batch = retrieve_evidence_units(
        units,
        kb_id="kb",
        original_query="总结 a.pdf",
        engine=_Engine([doc]),
        derived_knowledge_retriever=_NoDerived(),
        retrieval_feedback_store=_NoFeedback(),
        budget=budget,
        policy=_policy(),
        rrf_k=60.0,
    )
    assert batch.results[0].status is EvidenceUnitExecutionStatus.BUDGET_EXHAUSTED


def test_same_chunk_different_unit_spans_receive_different_eids(monkeypatch):
    monkeypatch.setattr(BGEReranker, "default_device", lambda: "cpu")
    text = (
        "阿尔法方案采用分层索引并明确描述方法。"
        + "中间背景材料。" * 30
        + "欧米伽限制要求离线运行并明确描述边界。"
    )
    doc = _doc("a.pdf", "shared", text)
    units = build_summary_evidence_units(
        "总结 a.pdf",
        "a.pdf",
        [
            {
                "section_id": "alpha",
                "title": "阿尔法方案",
                "instruction": "提取分层索引方法",
            },
            {
                "section_id": "omega",
                "title": "欧米伽限制",
                "instruction": "提取离线运行边界",
            },
        ],
    )

    batch = retrieve_evidence_units(
        units,
        kb_id="kb",
        original_query="总结 a.pdf",
        engine=_Engine([doc]),
        derived_knowledge_retriever=_NoDerived(),
        retrieval_feedback_store=_NoFeedback(),
        budget=_budget(2, chars_per_unit=800),
        policy=_policy(
            evidence_span_enabled=True,
            evidence_span_max_chars_per_doc=120,
            evidence_span_context_sentences=0,
        ),
        rrf_k=60.0,
    )

    first, second = batch.results
    assert first.selected_docs[0]["meta"]["chunk_id"] == "shared"
    assert second.selected_docs[0]["meta"]["chunk_id"] == "shared"
    assert (
        first.selected_docs[0]["retrieval"]["evidence_id"]
        != (second.selected_docs[0]["retrieval"]["evidence_id"])
    )
    assert len(batch.evidence_ledger) == 2
