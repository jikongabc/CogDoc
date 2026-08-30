import json

import pytest

from cogdoc.agents import evidence_unit_verifier, evidence_verifier
from cogdoc.agents.evidence_verifier import (
    EvidenceVerification,
    EvidenceVerifierAgent,
    RequirementEvidenceAssessment,
    requires_evidence_verification,
    select_verification_docs,
    should_verify_evidence,
)
from cogdoc.config.settings import Settings
from cogdoc.graph.subgraphs import qa
from cogdoc.tools.citation_ledger import assign_evidence_ids


def _settings(**overrides):
    return Settings(_env_file=None, **overrides)


def _doc(
    chunk_id: str,
    source: str,
    text: str = "evidence",
    matched_requirement_ids: list[str] | None = None,
) -> dict:
    doc = {
        "text": text,
        "meta": {
            "chunk_id": chunk_id,
            "source": source,
            "page": 1,
            "page_start": 1,
            "page_end": 1,
        },
    }
    if matched_requirement_ids is not None:
        doc["retrieval"] = {
            "matched_requirement_ids": matched_requirement_ids,
        }
    return doc


def _requirement(requirement_id: str, question: str) -> dict[str, str]:
    return {
        "requirement_id": requirement_id,
        "question": question,
        "retrieval_query": question,
        "recovery_query": question,
    }


def test_evidence_payload_uses_generator_renderer_before_truncation():
    doc = _doc("child:1", "paper.pdf", "训练分为预训练和微调。")
    doc["meta"].update(
        {
            "section_path": "Methods > Training",
            "context": "前文：模型结构。",
        }
    )
    rendered = evidence_verifier.Generator._build_context_string([doc])
    max_chars = len(rendered) - 7

    row = json.loads(evidence_verifier._evidence_payload([doc], max_chars))[0]

    assert row["text"] == rendered[:max_chars]
    assert "章节路径：Methods &gt; Training" in row["text"]
    assert "定位上下文：" in row["text"]
    assert evidence_verifier.Generator._build_context_string([doc]) == rendered


# 精确事实问题进入校验，普通概念解释不增加额外模型调用。
def test_fact_query_detection_is_selective():
    assert requires_evidence_verification("比赛时长分别是多少") is True
    assert requires_evidence_verification("报名费报销比例是多少") is True
    assert requires_evidence_verification("What is the deadline?") is True
    assert requires_evidence_verification("项目负责人是谁") is True
    assert requires_evidence_verification("ICPC全称是什么") is True
    assert requires_evidence_verification("系统是否支持 SSO") is True
    assert requires_evidence_verification("Who founded the company?") is True
    assert requires_evidence_verification("Does CogDoc support SSO?") is True
    assert requires_evidence_verification("Can viewers export audit logs?") is True
    assert requires_evidence_verification("当前版本会自动备份吗？") is True
    assert requires_evidence_verification("介绍一下这个比赛") is False


# 校验候选优先覆盖不同来源，再按原始排名补足。
def test_select_verification_docs_diversifies_sources():
    docs = [
        _doc("a1", "a.pdf"),
        _doc("a2", "a.pdf"),
        _doc("b1", "b.pdf"),
        _doc("c1", "c.pdf"),
    ]

    selected = select_verification_docs(docs, 3)

    assert [doc["meta"]["chunk_id"] for doc in selected] == ["a1", "b1", "c1"]


# 需求归因覆盖优先于通用来源多样化。
def test_select_verification_docs_prioritizes_each_requirement():
    docs = [
        _doc("general", "a.pdf"),
        _doc("r2-doc", "a.pdf", matched_requirement_ids=["r2"]),
        _doc("r1-doc", "b.pdf", matched_requirement_ids=["r1"]),
    ]

    selected = select_verification_docs(docs, 2, ["r1", "r2"])

    assert [doc["meta"]["chunk_id"] for doc in selected] == ["r1-doc", "r2-doc"]


def test_select_verification_docs_preserves_pinned_adaptive_evidence():
    pinned = _doc("pinned", "a.pdf", matched_requirement_ids=["r1"])
    docs = [
        _doc("r1-new", "b.pdf", matched_requirement_ids=["r1"]),
        _doc("r2-new", "c.pdf", matched_requirement_ids=["r2"]),
        pinned,
    ]

    selected = select_verification_docs(
        docs,
        2,
        ["r1", "r2"],
        pinned_chunk_ids={"pinned"},
    )

    assert [doc["meta"]["chunk_id"] for doc in selected] == [
        "pinned",
        "r2-new",
    ]


def test_pinned_docs_do_not_starve_newly_recovered_requirement():
    pinned = [
        _doc(f"pinned-{index}", "a.pdf", matched_requirement_ids=["r1"])
        for index in range(3)
    ]
    recovered = _doc("r2-new", "b.pdf", matched_requirement_ids=["r2"])

    selected = select_verification_docs(
        [*pinned, recovered],
        3,
        ["r1", "r2"],
        pinned_chunk_ids={doc["meta"]["chunk_id"] for doc in pinned},
    )

    selected_ids = [doc["meta"]["chunk_id"] for doc in selected]
    assert selected_ids[:2] == ["pinned-0", "r2-new"]
    assert len(set(selected_ids) & {"pinned-0", "pinned-1", "pinned-2"}) == 2


# 第一阶段放行和阈值附近的事实问题都进入二阶段，明显低分仍直接拒答。
def test_should_verify_supported_and_borderline_fact_queries():
    settings = _settings(qa_evidence_verify_borderline_min_score=0.75)

    assert should_verify_evidence(
        {
            "query": "比赛时长是多少",
            "retrieval_first_stage_supported": True,
        },
        settings,
    )
    assert should_verify_evidence(
        {
            "query": "比赛时长是多少",
            "retrieval_first_stage_supported": False,
            "retrieval_abstained": True,
            "retrieval_abstain_reason": "below_threshold",
            "retrieval_confidence": 0.9,
        },
        settings,
    )
    assert not should_verify_evidence(
        {
            "query": "比赛时长是多少",
            "retrieval_first_stage_supported": False,
            "retrieval_abstained": True,
            "retrieval_abstain_reason": "below_threshold",
            "retrieval_confidence": 0.5,
        },
        settings,
    )


# 身份问题无论一阶段是强召回还是阈值附近，都进入二阶段校验，避免“提到即支持”。
def test_identity_query_always_triggers_evidence_verification():
    settings = _settings(qa_evidence_verify_borderline_min_score=0.75)

    assert should_verify_evidence(
        {
            "query": "项目负责人是谁",
            "retrieval_first_stage_supported": False,
            "retrieval_abstained": True,
            "retrieval_abstain_reason": "below_threshold",
            "retrieval_confidence": 0.9,
        },
        settings,
    )

    assert should_verify_evidence(
        {
            "query": "项目负责人是谁",
            "retrieval_first_stage_supported": True,
        },
        settings,
    )


# 多个原子需求即使不命中数值/日期标记，也必须逐项校验。
def test_multiple_requirements_trigger_verification_without_fact_marker():
    assert should_verify_evidence(
        {
            "query": "介绍两个方面",
            "evidence_requirements": [
                _requirement("r1", "第一个方面是什么？"),
                _requirement("r2", "第二个方面是什么？"),
            ],
            "retrieval_first_stage_supported": True,
        },
        _settings(),
    )


# 结构化结论只有引用闭集中的真实 chunk_id 才能放行。
def test_verifier_accepts_supported_evidence_with_valid_chunk_id(monkeypatch):
    monkeypatch.setattr(evidence_verifier, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        evidence_verifier.Generator, "_get_client", lambda **kwargs: object()
    )
    monkeypatch.setattr(
        evidence_verifier,
        "invoke_structured",
        lambda *_args, **_kwargs: EvidenceVerification(
            supported=True,
            evidence_chunk_ids=["a1"],
            reason="证据明确给出时长",
        ),
    )

    output = EvidenceVerifierAgent.verify(
        {
            "query": "比赛时长是多少",
            "retrieval_first_stage_supported": False,
            "retrieval_abstained": True,
            "verification_docs": [_doc("a1", "a.pdf", "比赛持续 5 小时")],
        }
    )

    assert output["evidence_supported"] is True
    assert output["retrieval_abstained"] is False
    assert output["evidence_verified_chunk_ids"] == ["a1"]


# 模型声称支持但编造证据标识时必须拒答。
def test_verifier_rejects_fabricated_chunk_id(monkeypatch):
    monkeypatch.setattr(evidence_verifier, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        evidence_verifier.Generator, "_get_client", lambda **kwargs: object()
    )
    monkeypatch.setattr(
        evidence_verifier,
        "invoke_structured",
        lambda *_args, **_kwargs: EvidenceVerification(
            supported=True,
            evidence_chunk_ids=["fabricated"],
            reason="声称有证据",
        ),
    )

    output = EvidenceVerifierAgent.verify(
        {
            "query": "比赛时长是多少",
            "retrieval_first_stage_supported": True,
            "verification_docs": [_doc("a1", "a.pdf")],
        }
    )

    assert output["evidence_supported"] is False
    assert output["retrieval_abstain_reason"] == "evidence_not_supported"
    assert output["evidence_verified_chunk_ids"] == []


# 逐需求结论完整且每项都引用闭集证据时才能放行。
def test_verifier_accepts_complete_requirement_assessments(monkeypatch):
    captured = {}
    monkeypatch.setattr(evidence_verifier, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        evidence_verifier.Generator, "_get_client", lambda **kwargs: object()
    )

    def invoke(_llm, _schema, messages):
        captured["messages"] = messages
        return EvidenceVerification(
            supported=True,
            evidence_chunk_ids=["a1", "b1"],
            reason="两项需求均有直接证据",
            assessments=[
                RequirementEvidenceAssessment(
                    requirement_id="r1",
                    verdict="supported",
                    evidence_chunk_ids=["a1"],
                    reason="A 证据完整",
                ),
                RequirementEvidenceAssessment(
                    requirement_id="r2",
                    verdict="supported",
                    evidence_chunk_ids=["b1"],
                    reason="B 证据完整",
                ),
            ],
        )

    monkeypatch.setattr(evidence_verifier, "invoke_structured", invoke)
    output = EvidenceVerifierAgent.verify(
        {
            "query": "A 和 B 分别是什么？",
            "evidence_requirements": [
                _requirement("r1", "A 是什么？"),
                _requirement("r2", "B 是什么？"),
            ],
            "retrieval_first_stage_supported": True,
            "verification_docs": [
                _doc("a1", "a.pdf", matched_requirement_ids=["r1"]),
                _doc("b1", "b.pdf", matched_requirement_ids=["r2"]),
            ],
        }
    )

    assert output["evidence_supported"] is True
    assert output["missing_evidence_requirement_ids"] == []
    assert output["evidence_verified_chunk_ids"] == ["a1", "b1"]
    assert [
        assessment["requirement_id"]
        for assessment in output["evidence_requirement_assessments"]
    ] == ["r1", "r2"]
    assert '"requirement_id": "r1"' in captured["messages"][1]["content"]
    assert '"matched_requirement_ids": ["r1"]' in captured["messages"][1]["content"]


# 部分需求缺失时整体拒答，但保留已验证 chunk 与缺失 ID 供补检索。
def test_verifier_reports_partial_missing_requirement(monkeypatch):
    monkeypatch.setattr(evidence_verifier, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        evidence_verifier.Generator, "_get_client", lambda **kwargs: object()
    )
    monkeypatch.setattr(
        evidence_verifier,
        "invoke_structured",
        lambda *_args, **_kwargs: EvidenceVerification(
            supported=False,
            evidence_chunk_ids=["a1"],
            reason="B 证据缺失",
            assessments=[
                RequirementEvidenceAssessment(
                    requirement_id="r1",
                    verdict="supported",
                    evidence_chunk_ids=["a1"],
                    reason="A 已支持",
                ),
                RequirementEvidenceAssessment(
                    requirement_id="r2",
                    verdict="missing",
                    evidence_chunk_ids=[],
                    reason="未找到 B",
                ),
            ],
        ),
    )

    output = EvidenceVerifierAgent.verify(
        {
            "query": "A 和 B",
            "evidence_requirements": [
                _requirement("r1", "A 是什么？"),
                _requirement("r2", "B 是什么？"),
            ],
            "retrieval_first_stage_supported": True,
            "verification_docs": [_doc("a1", "a.pdf")],
        }
    )

    assert output["evidence_supported"] is False
    assert output["missing_evidence_requirement_ids"] == ["r2"]
    assert output["evidence_verified_chunk_ids"] == ["a1"]


@pytest.mark.parametrize("malformed_kind", ["omitted", "unknown"])
def test_verifier_rejects_omitted_or_unknown_requirement_id(
    monkeypatch, malformed_kind
):
    assessments = [
        RequirementEvidenceAssessment(
            requirement_id="r1",
            verdict="supported",
            evidence_chunk_ids=["a1"],
            reason="A 已支持",
        )
    ]
    if malformed_kind == "unknown":
        assessments.append(
            RequirementEvidenceAssessment(
                requirement_id="r999",
                verdict="supported",
                evidence_chunk_ids=["a1"],
                reason="未知需求",
            )
        )
    monkeypatch.setattr(evidence_verifier, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        evidence_verifier.Generator, "_get_client", lambda **kwargs: object()
    )
    monkeypatch.setattr(
        evidence_verifier,
        "invoke_structured",
        lambda *_args, **_kwargs: EvidenceVerification(
            supported=True,
            evidence_chunk_ids=["a1"],
            reason="声称完整",
            assessments=assessments,
        ),
    )

    output = EvidenceVerifierAgent.verify(
        {
            "query": "A 和 B",
            "evidence_requirements": [
                _requirement("r1", "A 是什么？"),
                _requirement("r2", "B 是什么？"),
            ],
            "retrieval_first_stage_supported": True,
            "verification_docs": [_doc("a1", "a.pdf")],
        }
    )

    assert output["evidence_supported"] is False
    assert output["missing_evidence_requirement_ids"] == ["r1", "r2"]


# 逐需求评估引用伪造 chunk 时必须降级为 missing，不得只过滤后放行。
def test_verifier_rejects_fabricated_requirement_chunk_id(monkeypatch):
    monkeypatch.setattr(evidence_verifier, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        evidence_verifier.Generator, "_get_client", lambda **kwargs: object()
    )
    monkeypatch.setattr(
        evidence_verifier,
        "invoke_structured",
        lambda *_args, **_kwargs: EvidenceVerification(
            supported=True,
            evidence_chunk_ids=[],
            reason="声称有证据",
            assessments=[
                RequirementEvidenceAssessment(
                    requirement_id="r1",
                    verdict="supported",
                    evidence_chunk_ids=["fabricated"],
                    reason="伪造证据",
                )
            ],
        ),
    )

    output = EvidenceVerifierAgent.verify(
        {
            "query": "A 是什么？",
            "evidence_requirements": [_requirement("r1", "A 是什么？")],
            "retrieval_first_stage_supported": True,
            "verification_docs": [_doc("a1", "a.pdf")],
        }
    )

    assert output["evidence_supported"] is False
    assert output["missing_evidence_requirement_ids"] == ["r1"]
    assert output["evidence_verified_chunk_ids"] == []
    assert output["evidence_requirement_assessments"][0]["verdict"] == "missing"


# contradictory 也是证据判断；没有闭集 chunk 支撑时不能成立。
def test_contradictory_requirement_without_chunk_is_downgraded(monkeypatch):
    monkeypatch.setattr(evidence_verifier, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        evidence_verifier.Generator, "_get_client", lambda **kwargs: object()
    )
    monkeypatch.setattr(
        evidence_verifier,
        "invoke_structured",
        lambda *_args, **_kwargs: EvidenceVerification(
            supported=False,
            reason="声称冲突",
            assessments=[
                RequirementEvidenceAssessment(
                    requirement_id="r1",
                    verdict="contradictory",
                    evidence_chunk_ids=[],
                    reason="无证据的冲突",
                )
            ],
        ),
    )

    output = EvidenceVerifierAgent.verify(
        {
            "query": "A 是什么？",
            "evidence_requirements": [_requirement("r1", "A 是什么？")],
            "retrieval_first_stage_supported": True,
            "verification_docs": [_doc("a1", "a.pdf")],
        }
    )

    assert output["evidence_supported"] is False
    assert output["missing_evidence_requirement_ids"] == ["r1"]
    assert output["evidence_requirement_assessments"][0]["verdict"] == "missing"


# Evidence Pack 已是生成闭集；校验节点不得再把闭集外文档注入生成上下文。
def test_evidence_verify_node_does_not_expand_authoritative_pack(monkeypatch):
    top_doc = _doc("a1", "a.pdf")
    cross_source_doc = _doc("b1", "b.pdf")
    logged = []
    monkeypatch.setattr(
        qa.EvidenceVerifierAgent,
        "verify",
        lambda _state: {
            "evidence_verification_required": True,
            "evidence_supported": True,
            "evidence_verification_reason": "两篇证据均完整",
            "evidence_verified_chunk_ids": ["a1", "b1"],
            "retrieval_abstained": False,
            "retrieval_abstain_reason": "evidence_supported",
        },
    )
    monkeypatch.setattr(
        qa, "log_event", lambda *args, **kwargs: logged.append((args, kwargs))
    )

    output = qa.evidence_verify_node(
        {
            "reranked_docs": [top_doc],
            "verification_docs": [top_doc, cross_source_doc],
        }
    )

    assert "reranked_docs" not in output
    assert logged[0][1]["generation_evidence_count"] == 1


# 校验器异常时不改变第一阶段结论：原本放行则放行，原本拒答则继续拒答。
@pytest.mark.parametrize("first_stage_supported", [True, False])
def test_verifier_error_preserves_first_stage_decision(
    monkeypatch, first_stage_supported
):
    monkeypatch.setattr(evidence_verifier, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        evidence_verifier.Generator,
        "_get_client",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    output = EvidenceVerifierAgent.verify(
        {
            "query": "比赛时长是多少",
            "retrieval_first_stage_supported": first_stage_supported,
            "retrieval_abstained": not first_stage_supported,
            "retrieval_abstain_reason": "below_threshold",
            "verification_docs": [_doc("a1", "a.pdf")],
        }
    )

    assert output["evidence_supported"] is first_stage_supported
    assert output["retrieval_abstained"] is (not first_stage_supported)
    assert output["evidence_verifier_error"] == "RuntimeError"


# 已进入需求化闭集校验后，校验器异常必须 fail-closed。
def test_requirement_verifier_error_fails_closed(monkeypatch):
    monkeypatch.setattr(evidence_verifier, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        evidence_verifier.Generator,
        "_get_client",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    output = EvidenceVerifierAgent.verify(
        {
            "query": "A 是什么？",
            "evidence_requirements": [_requirement("r1", "A 是什么？")],
            "retrieval_first_stage_supported": True,
            "verification_docs": [_doc("a1", "a.pdf")],
        }
    )

    assert output["evidence_supported"] is False
    assert output["retrieval_abstained"] is True
    assert output["missing_evidence_requirement_ids"] == ["r1"]
    assert output["evidence_verifier_error"] == "RuntimeError"


def test_runtime_qa_plan_uses_generic_closed_set_verifier_and_gate(monkeypatch):
    monkeypatch.setattr(evidence_verifier, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        evidence_unit_verifier.Generator,
        "get_client_for_node",
        lambda *_args, **_kwargs: object(),
    )

    def invoke_structured(_llm, schema, messages):
        payload = json.loads(messages[1]["content"])["untrusted_data"][
            "evidence_units"
        ]
        return schema(
            assessments=[
                {
                    "unit_id": row["unit_id"],
                    "status": "supported",
                    "evidence_ids": [row["candidate_evidence_ids"][0]],
                    "reason": "闭集证据直接支持",
                }
                for row in payload
            ]
        )

    monkeypatch.setattr(
        evidence_unit_verifier, "invoke_structured", invoke_structured
    )
    docs, ledger = assign_evidence_ids([_doc("a1", "a.pdf", "A 是答案。")])

    output = EvidenceVerifierAgent.verify(
        {
            "query": "A 是什么？",
            "evidence_requirements": [_requirement("r1", "A 是什么？")],
            "evidence_units": [{"unit_id": "runtime-plan-present"}],
            "retrieval_round": 0,
            "verification_docs": docs,
            "evidence_ledger": ledger,
        }
    )

    assert output["evidence_unit_adapter_outcome"] == "verified"
    assert output["evidence_supported"] is True
    assert output["evidence_verified_chunk_ids"] == ["a1"]
    assert output["evidence_unit_gate_decisions"][0]["action"] == "generate"
    assert output["evidence_unit_batch_can_generate"] is True
