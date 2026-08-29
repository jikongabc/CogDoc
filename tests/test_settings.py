import pytest
from cogdoc.config.settings import Settings, get_settings


# 清理 settings cache。
@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# 验证 settings defaults match current runtime contract 场景。
def test_settings_defaults_match_current_runtime_contract():
    settings = Settings()

    assert settings.cogdoc_doc_dir == "your_documents"
    assert settings.cogdoc_default_doc_id == "arch_blueprint_2026"
    assert settings.llm_model_name == "deepseek-chat"
    assert settings.ollama_model_name == "qwen2.5:7b"
    assert settings.qa_retrieval_top_k == 9
    assert settings.qa_rag_vector_route_weight == 1.0
    assert settings.qa_rag_bm25_route_weight == 1.0
    assert settings.qa_derived_knowledge_vector_route_weight == 0.9
    assert settings.qa_derived_knowledge_lexical_route_weight == 0.8
    assert settings.qa_rerank_top_n == 3
    assert settings.qa_rerank_docs_per_requirement == 2
    assert settings.qa_rerank_docs_per_route == 1
    assert settings.qa_query_rewrite_fast_path_enabled is True
    assert settings.qa_parent_context_enabled is True
    assert settings.qa_parent_context_max_chunks == 5
    assert settings.qa_parent_context_max_chars == 3600
    assert settings.qa_evidence_span_enabled is True
    assert settings.qa_evidence_span_max_chars_per_doc == 420
    assert settings.qa_evidence_span_context_sentences == 1
    assert settings.qa_evidence_pack_max_docs == 8
    assert settings.qa_evidence_pack_max_chars == 7200
    assert settings.qa_abstain_enabled is True
    assert settings.qa_abstain_max_vector_distance == 0.7117711305618286
    assert settings.qa_abstain_min_bm25_score == 12.328925491936891
    assert settings.qa_abstain_min_knowledge_vector_score == 0.5
    assert settings.qa_abstain_min_knowledge_lexical_score == 0.5
    assert settings.qa_abstain_allow_missing_signals is False
    assert settings.qa_evidence_verify_enabled is True
    assert settings.qa_evidence_verify_max_docs == 3
    assert settings.qa_evidence_verify_max_chars_per_doc == 1600
    assert settings.qa_evidence_verify_borderline_min_score == 0.75
    assert settings.evidence_unit_verify_enabled is True
    assert settings.evidence_unit_verify_max_chars_per_doc == 1200
    assert settings.evidence_unit_verify_max_units_per_batch == 8
    assert settings.qa_retrieval_max_queries == 7
    assert settings.qa_adaptive_retrieval_enabled is True
    assert settings.qa_adaptive_retrieval_max_retries == 1
    assert settings.qa_adaptive_retrieval_top_k_multiplier == 2.0
    assert settings.qa_adaptive_retrieval_max_top_k == 36
    assert settings.claim_verification_enabled is False
    assert settings.claim_verification_mode is None
    assert settings.effective_claim_verification_mode == "off"
    assert settings.claim_verification_rollout_percent == 100.0
    assert settings.claim_verification_rollout_seed == "cogdoc-v1"
    assert settings.claim_verification_observation_retention_days == 30
    assert settings.claim_verification_observation_max_per_tenant == 100000
    assert settings.claim_verification_operational_min_samples == 200
    assert settings.claim_verification_operational_max_error_rate == 0.02
    assert settings.claim_verification_review_sample_percent == 0.0
    assert settings.claim_verification_review_sample_seed == "cogdoc-review-v1"
    assert settings.claim_verification_review_retention_days == 30
    assert settings.claim_verification_review_max_per_tenant == 10000
    assert settings.claim_verification_review_max_claims_per_response == 5
    assert settings.claim_verification_review_max_evidence_per_claim == 6
    assert settings.claim_verification_review_max_chars_per_evidence == 1600
    assert settings.claim_verification_max_claims == 40
    assert settings.claim_verification_max_claims_per_batch == 8
    assert settings.claim_verification_max_docs_per_batch == 12
    assert settings.claim_verification_max_chars_per_doc == 1600
    assert settings.claim_verification_max_repair_attempts == 1
    assert settings.hybrid_rrf_k == 60
    assert settings.memory_retrieval_enabled is True
    assert settings.memory_semantic_enabled is True
    assert settings.memory_retrieval_short_limit == 8
    assert settings.memory_retrieval_mid_limit == 4
    assert settings.memory_retrieval_recent_pin == 4
    assert settings.memory_semantic_include_short is False
    assert settings.memory_rrf_k == 60.0
    assert settings.memory_recency_weight == 1.0
    assert settings.memory_lexical_weight == 1.4
    assert settings.memory_semantic_weight == 1.6
    assert settings.memory_importance_weight == 0.8
    assert settings.memory_mid_priority_weight == 0.8
    assert settings.cogdoc_log_level == "INFO"
    assert settings.cogdoc_log_file == "logs/cogdoc.jsonl"
    assert settings.cogdoc_log_to_console is False
    assert settings.cogdoc_trace_enabled is True
    assert settings.cogdoc_trace_dir == "logs/traces"
    assert settings.cogdoc_chat_stream_idle_timeout_seconds == 300.0
    assert settings.cogdoc_connector_vault_keys == ""
    assert settings.cogdoc_connector_vault_active_key_id == "v1"
    assert settings.cogdoc_connector_oauth_session_ttl_seconds == 600
    assert settings.cogdoc_connector_index_timeout_seconds == 30.0
    assert settings.cogdoc_confluence_allowed_hosts == ""
    assert settings.cogdoc_s3_endpoint_allowed_hosts == ""
    assert settings.cogdoc_local_connector_allowed_roots == ""
    assert settings.cogdoc_git_connector_allowed_roots == ""
    assert settings.cogdoc_url_connector_allowed_hosts == ""
    assert settings.cogdoc_source_artifact_max_file_mb == 100
    assert settings.cogdoc_source_artifact_max_tenant_mb == 512
    assert settings.cogdoc_source_artifact_max_versions == 10
    assert settings.eval_review_api_key_set == set()
    assert settings.cogdoc_ocr_enabled is False
    assert settings.cogdoc_ocr_provider == "tesseract"
    assert settings.cogdoc_ocr_binary == "tesseract"
    assert settings.cogdoc_ocr_languages == "eng+chi_sim"
    assert settings.cogdoc_ocr_dpi == 300
    assert settings.cogdoc_ocr_min_native_chars == 40
    assert settings.cogdoc_ocr_max_pages == 100
    assert settings.cogdoc_ocr_page_timeout_seconds == 30.0
    assert settings.cogdoc_ocr_required is False


# 验证 settings reads environment overrides 场景。
def test_settings_reads_environment_overrides(monkeypatch):
    monkeypatch.setenv("COGDOC_DOC_DIR", "papers")
    monkeypatch.setenv("LLM_MODEL_NAME", "custom-model")
    monkeypatch.setenv("QA_RETRIEVAL_TOP_K", "11")
    monkeypatch.setenv("QA_RAG_VECTOR_ROUTE_WEIGHT", "1.2")
    monkeypatch.setenv("QA_DERIVED_KNOWLEDGE_LEXICAL_ROUTE_WEIGHT", "0.6")
    monkeypatch.setenv("QA_RERANK_DOCS_PER_ROUTE", "2")
    monkeypatch.setenv("QA_PARENT_CONTEXT_ENABLED", "false")
    monkeypatch.setenv("QA_PARENT_CONTEXT_MAX_CHUNKS", "7")
    monkeypatch.setenv("QA_PARENT_CONTEXT_MAX_CHARS", "4200")
    monkeypatch.setenv("QA_EVIDENCE_SPAN_ENABLED", "false")
    monkeypatch.setenv("QA_EVIDENCE_SPAN_MAX_CHARS_PER_DOC", "480")
    monkeypatch.setenv("QA_EVIDENCE_SPAN_CONTEXT_SENTENCES", "2")
    monkeypatch.setenv("QA_EVIDENCE_PACK_MAX_DOCS", "10")
    monkeypatch.setenv("QA_EVIDENCE_PACK_MAX_CHARS", "8400")
    monkeypatch.setenv("EVIDENCE_UNIT_VERIFY_ENABLED", "false")
    monkeypatch.setenv("EVIDENCE_UNIT_VERIFY_MAX_CHARS_PER_DOC", "900")
    monkeypatch.setenv("EVIDENCE_UNIT_VERIFY_MAX_UNITS_PER_BATCH", "5")
    monkeypatch.setenv("QA_ABSTAIN_MAX_VECTOR_DISTANCE", "0.75")
    monkeypatch.setenv("COGDOC_MEMORY_SEMANTIC_ENABLED", "false")
    monkeypatch.setenv("COGDOC_MEMORY_RETRIEVAL_SHORT_LIMIT", "6")
    monkeypatch.setenv("COGDOC_MEMORY_RETRIEVAL_RECENT_PIN", "2")
    monkeypatch.setenv("COGDOC_MEMORY_SEMANTIC_WEIGHT", "2.5")
    monkeypatch.setenv("COGDOC_OCR_ENABLED", "true")
    monkeypatch.setenv("COGDOC_OCR_DPI", "240")
    monkeypatch.setenv("COGDOC_OCR_REQUIRED", "true")
    monkeypatch.setenv("CLAIM_VERIFICATION_ENABLED", "true")
    monkeypatch.setenv("CLAIM_VERIFICATION_MAX_CLAIMS", "24")
    monkeypatch.setenv("QA_ADAPTIVE_RETRIEVAL_MAX_RETRIES", "2")
    monkeypatch.setenv("QA_ADAPTIVE_RETRIEVAL_MAX_TOP_K", "24")
    monkeypatch.setenv("COGDOC_EVAL_REVIEW_API_KEYS", "review-a, review-b")
    monkeypatch.setenv("COGDOC_CHAT_STREAM_IDLE_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("COGDOC_CHAT_STREAM_WORKERS", "12")
    monkeypatch.setenv("COGDOC_CONNECTOR_INDEX_TIMEOUT_SECONDS", "75")

    settings = get_settings()

    assert settings.cogdoc_doc_dir == "papers"
    assert settings.llm_model_name == "custom-model"
    assert settings.qa_retrieval_top_k == 11
    assert settings.qa_rag_vector_route_weight == 1.2
    assert settings.qa_derived_knowledge_lexical_route_weight == 0.6
    assert settings.qa_rerank_docs_per_route == 2
    assert settings.qa_parent_context_enabled is False
    assert settings.qa_parent_context_max_chunks == 7
    assert settings.qa_parent_context_max_chars == 4200
    assert settings.qa_evidence_span_enabled is False
    assert settings.qa_evidence_span_max_chars_per_doc == 480
    assert settings.qa_evidence_span_context_sentences == 2
    assert settings.qa_evidence_pack_max_docs == 10
    assert settings.qa_evidence_pack_max_chars == 8400
    assert settings.qa_abstain_max_vector_distance == 0.75
    assert settings.evidence_unit_verify_enabled is False
    assert settings.evidence_unit_verify_max_chars_per_doc == 900
    assert settings.evidence_unit_verify_max_units_per_batch == 5
    assert settings.memory_semantic_enabled is False
    assert settings.memory_retrieval_short_limit == 6
    assert settings.memory_retrieval_recent_pin == 2
    assert settings.memory_semantic_weight == 2.5
    assert settings.cogdoc_ocr_enabled is True
    assert settings.cogdoc_ocr_dpi == 240
    assert settings.cogdoc_ocr_required is True
    assert settings.claim_verification_enabled is True
    assert settings.effective_claim_verification_mode == "enforce"
    assert settings.claim_verification_max_claims == 24
    assert settings.qa_adaptive_retrieval_max_retries == 2
    assert settings.qa_adaptive_retrieval_max_top_k == 24
    assert settings.eval_review_api_key_set == {"review-a", "review-b"}
    assert settings.cogdoc_chat_stream_idle_timeout_seconds == 45.0
    assert settings.cogdoc_chat_stream_workers == 12
    assert settings.cogdoc_connector_index_timeout_seconds == 75.0


def test_claim_verification_mode_takes_precedence_over_legacy_boolean(monkeypatch):
    monkeypatch.setenv("CLAIM_VERIFICATION_ENABLED", "true")
    monkeypatch.setenv("CLAIM_VERIFICATION_MODE", "shadow")

    settings = Settings(_env_file=None)

    assert settings.claim_verification_enabled is True
    assert settings.claim_verification_mode == "shadow"
    assert settings.effective_claim_verification_mode == "shadow"


def test_claim_verification_rollout_settings_are_bounded():
    assert (
        Settings(
            _env_file=None, claim_verification_rollout_percent=0.0
        ).claim_verification_rollout_percent
        == 0.0
    )
    assert (
        Settings(
            _env_file=None, claim_verification_rollout_percent=100.0
        ).claim_verification_rollout_percent
        == 100.0
    )
    with pytest.raises(ValueError):
        Settings(_env_file=None, claim_verification_rollout_percent=-0.1)
    with pytest.raises(ValueError):
        Settings(_env_file=None, claim_verification_rollout_percent=100.1)
    with pytest.raises(ValueError):
        Settings(_env_file=None, claim_verification_rollout_seed="")


def test_claim_verification_observation_settings_are_bounded():
    with pytest.raises(ValueError):
        Settings(_env_file=None, claim_verification_observation_retention_days=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, claim_verification_observation_max_per_tenant=99)
    with pytest.raises(ValueError):
        Settings(_env_file=None, claim_verification_operational_min_samples=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, claim_verification_operational_max_error_rate=1.1)
    with pytest.raises(ValueError):
        Settings(_env_file=None, claim_verification_review_sample_percent=100.1)
    with pytest.raises(ValueError):
        Settings(_env_file=None, claim_verification_review_sample_seed="")
    with pytest.raises(ValueError):
        Settings(_env_file=None, claim_verification_review_retention_days=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, claim_verification_review_max_per_tenant=99)
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            claim_verification_review_max_chars_per_evidence=199,
        )


def test_legacy_knowledge_threshold_falls_back_to_both_split_channels(monkeypatch):
    monkeypatch.setenv("QA_ABSTAIN_MIN_KNOWLEDGE_SCORE", "0.37")

    settings = Settings(_env_file=None)

    assert settings.qa_abstain_min_knowledge_vector_score == 0.37
    assert settings.qa_abstain_min_knowledge_lexical_score == 0.37


def test_split_knowledge_thresholds_override_legacy_fallback(monkeypatch):
    monkeypatch.setenv("QA_ABSTAIN_MIN_KNOWLEDGE_SCORE", "0.37")
    monkeypatch.setenv("QA_ABSTAIN_MIN_KNOWLEDGE_VECTOR_SCORE", "0.61")
    monkeypatch.setenv("QA_ABSTAIN_MIN_KNOWLEDGE_LEXICAL_SCORE", "4.2")

    settings = Settings(_env_file=None)

    assert settings.qa_abstain_min_knowledge_vector_score == 0.61
    assert settings.qa_abstain_min_knowledge_lexical_score == 4.2


def test_chat_stream_idle_timeout_is_bounded():
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_chat_stream_idle_timeout_seconds=0.5)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_chat_stream_idle_timeout_seconds=3601)


def test_connector_index_timeout_is_bounded():
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_connector_index_timeout_seconds=0.5)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_connector_index_timeout_seconds=3601)


def test_api_principal_map_parses_json_without_exposing_legacy_keys(monkeypatch):
    monkeypatch.setenv(
        "COGDOC_API_PRINCIPALS",
        '{"team-key":{"tenant_id":"team-a","subject_id":"alice","role":"editor"}}',
    )
    settings = Settings(_env_file=None)

    assert settings.api_principal_map == {
        "team-key": {
            "tenant_id": "team-a",
            "subject_id": "alice",
            "role": "editor",
        }
    }
    assert settings.api_key_set == set()


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '"not-an-object"',
        '{"key":"not-an-object"}',
        ('{"key":{"tenant_id":"t","subject_id":"s","role":"viewer","rol":"owner"}}'),
        ('{"key":{"tenant_id":"t","subject_id":"s","role":"viewer","role":"owner"}}'),
        (
            '{" key ":{"tenant_id":"t","subject_id":"s","role":"viewer"},'
            '"key":{"tenant_id":"t","subject_id":"s","role":"owner"}}'
        ),
        "{broken",
    ],
)
def test_api_principal_map_rejects_malformed_configuration(monkeypatch, raw):
    monkeypatch.setenv("COGDOC_API_PRINCIPALS", raw)
    settings = Settings(_env_file=None)

    with pytest.raises(ValueError, match="COGDOC_API_PRINCIPALS"):
        _ = settings.api_principal_map


def test_tenant_quota_settings_and_audit_path(monkeypatch, tmp_path):
    monkeypatch.setenv("COGDOC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COGDOC_TENANT_MAX_KNOWLEDGE_BASES", "4")
    monkeypatch.setenv("COGDOC_TENANT_MAX_DOCUMENTS", "20")
    monkeypatch.setenv("COGDOC_TENANT_MAX_STORAGE_MB", "512")
    settings = Settings(_env_file=None)

    assert settings.cogdoc_tenant_max_knowledge_bases == 4
    assert settings.cogdoc_tenant_max_documents == 20
    assert settings.cogdoc_tenant_max_storage_mb == 512
    assert settings.audit_log_path == str(tmp_path / "audit" / "events.jsonl")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("qa_evidence_span_max_chars_per_doc", 119),
        ("qa_evidence_span_max_chars_per_doc", 5001),
        ("qa_evidence_span_context_sentences", -1),
        ("qa_evidence_span_context_sentences", 6),
    ],
)
def test_settings_rejects_evidence_span_values_outside_safe_bounds(field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


def test_settings_exposes_bounded_research_execution_controls(monkeypatch):
    monkeypatch.setenv("COGDOC_RESEARCH_WORKERS", "3")
    monkeypatch.setenv("COGDOC_RESEARCH_RETRIEVAL_TOP_K", "12")
    monkeypatch.setenv("COGDOC_RESEARCH_MAX_PENDING", "9")
    monkeypatch.setenv("COGDOC_RESEARCH_PROVIDER_WORKERS", "2")
    monkeypatch.setenv("COGDOC_RESEARCH_PROVIDER_MAX_PENDING", "7")
    monkeypatch.setenv("COGDOC_RESEARCH_PROVIDER_CALL_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("COGDOC_RESEARCH_LLM_PROCESS_ISOLATION_ENABLED", "false")
    monkeypatch.setenv("COGDOC_RESEARCH_PROVIDER_KILL_GRACE_SECONDS", "0.2")
    monkeypatch.setenv("COGDOC_RESEARCH_PROVIDER_IPC_MAX_BYTES", "4096")
    monkeypatch.setenv("COGDOC_RESEARCH_EVIDENCE_DEADLINE_SECONDS", "120")
    monkeypatch.setenv("COGDOC_RESEARCH_REPORT_DEADLINE_SECONDS", "240")
    monkeypatch.setenv("COGDOC_RESEARCH_PLANNING_DEADLINE_SECONDS", "60")
    monkeypatch.setenv("COGDOC_RESEARCH_PLANNING_WORKERS", "2")
    monkeypatch.setenv("COGDOC_RESEARCH_PLANNING_MAX_PENDING", "5")
    monkeypatch.setenv("COGDOC_RESEARCH_MAX_RETRIEVAL_QUERIES", "18")
    monkeypatch.setenv("COGDOC_RESEARCH_MAX_CANDIDATE_DOCS", "72")
    monkeypatch.setenv("COGDOC_RESEARCH_MAX_LLM_CALLS", "20")
    monkeypatch.setenv("COGDOC_RESEARCH_MAX_MODEL_INPUT_CHARS", "42000")

    settings = get_settings()

    assert settings.cogdoc_research_workers == 3
    assert settings.cogdoc_research_retrieval_top_k == 12
    assert settings.cogdoc_research_max_pending == 9
    assert settings.cogdoc_research_provider_workers == 2
    assert settings.cogdoc_research_provider_max_pending == 7
    assert settings.cogdoc_research_provider_call_timeout_seconds == 45
    assert settings.cogdoc_research_llm_process_isolation_enabled is False
    assert settings.cogdoc_research_provider_kill_grace_seconds == 0.2
    assert settings.cogdoc_research_provider_ipc_max_bytes == 4096
    assert settings.cogdoc_research_evidence_deadline_seconds == 120
    assert settings.cogdoc_research_report_deadline_seconds == 240
    assert settings.cogdoc_research_planning_deadline_seconds == 60
    assert settings.cogdoc_research_planning_workers == 2
    assert settings.cogdoc_research_planning_max_pending == 5
    assert settings.cogdoc_research_max_retrieval_queries == 18
    assert settings.cogdoc_research_max_candidate_docs == 72
    assert settings.cogdoc_research_max_llm_calls == 20
    assert settings.cogdoc_research_max_model_input_chars == 42000
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_workers=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_retrieval_top_k=51)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_max_pending=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_provider_workers=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_provider_max_pending=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_provider_call_timeout_seconds=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_provider_kill_grace_seconds=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_provider_ipc_max_bytes=100)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_report_deadline_seconds=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_planning_deadline_seconds=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_planning_workers=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_planning_max_pending=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_max_retrieval_queries=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_max_candidate_docs=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_max_llm_calls=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_research_max_model_input_chars=999)


# 验证节点可以独立选择后端和模型。
def test_settings_resolves_node_backend_and_model(monkeypatch):
    monkeypatch.setenv("LLM_EVIDENCE_VERIFIER_BACKEND", "local")
    monkeypatch.setenv("OLLAMA_EVIDENCE_VERIFIER_MODEL_NAME", "qwen-review:7b")

    settings = get_settings()

    is_local = settings.is_local_for_node("evidence_verifier", request_is_local=False)
    assert is_local is True
    assert (
        settings.model_name_for_node("evidence_verifier", is_local=is_local)
        == "qwen-review:7b"
    )
    assert settings.is_local_for_node("qa_generator", request_is_local=False) is False
    assert (
        settings.model_name_for_node("qa_generator", is_local=False)
        == settings.llm_model_name
    )


# 验证声明校验与修复可以各自选择独立后端和模型。
def test_settings_resolves_claim_gate_node_backends(monkeypatch):
    monkeypatch.setenv("LLM_CLAIM_VERIFIER_BACKEND", "local")
    monkeypatch.setenv("OLLAMA_CLAIM_VERIFIER_MODEL_NAME", "claim-review:7b")
    monkeypatch.setenv("LLM_CLAIM_REPAIRER_BACKEND", "cloud")
    monkeypatch.setenv("LLM_CLAIM_REPAIRER_MODEL_NAME", "claim-repair")

    settings = get_settings()

    verifier_local = settings.is_local_for_node(
        "claim_verifier", request_is_local=False
    )
    repairer_local = settings.is_local_for_node("claim_repairer", request_is_local=True)
    assert verifier_local is True
    assert (
        settings.model_name_for_node("claim_verifier", is_local=verifier_local)
        == "claim-review:7b"
    )
    assert repairer_local is False
    assert (
        settings.model_name_for_node("claim_repairer", is_local=repairer_local)
        == "claim-repair"
    )


# 验证研究规划器可以独立于查询改写器选择模型。
def test_settings_resolves_research_planner_backend(monkeypatch):
    monkeypatch.setenv("LLM_RESEARCH_PLANNER_BACKEND", "local")
    monkeypatch.setenv("OLLAMA_RESEARCH_PLANNER_MODEL_NAME", "plan-review:7b")

    settings = get_settings()
    is_local = settings.is_local_for_node("research_planner", request_is_local=False)

    assert is_local is True
    assert (
        settings.model_name_for_node("research_planner", is_local=is_local)
        == "plan-review:7b"
    )


# 验证非法节点后端不会被静默解释为云端或本地。
def test_settings_rejects_invalid_node_backend(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_BACKEND", "somewhere")
    settings = get_settings()

    with pytest.raises(ValueError, match="无效节点后端"):
        settings.is_local_for_node("router", request_is_local=False)


# 验证 cuda thresholds are exposed as bytes 场景。
def test_cuda_thresholds_are_exposed_as_bytes(monkeypatch):
    monkeypatch.setenv("EMBEDDER_MIN_CUDA_FREE_MB", "123")

    settings = get_settings()

    assert settings.cuda_min_free_bytes("EMBEDDER_MIN_CUDA_FREE_MB") == (
        123 * 1024 * 1024
    )


# 验证 cuda thresholds reject unknown keys 场景。
def test_cuda_thresholds_reject_unknown_keys():
    settings = get_settings()

    with pytest.raises(ValueError, match="未知 CUDA 显存阈值配置"):
        settings.cuda_min_free_bytes("UNKNOWN_MIN_CUDA_FREE_MB")


def test_enterprise_oidc_settings_are_opt_in_and_bounded(monkeypatch):
    monkeypatch.setenv("COGDOC_OIDC_ENABLED", "true")
    monkeypatch.setenv("COGDOC_OIDC_ISSUER", "https://id.example.com")
    monkeypatch.setenv("COGDOC_OIDC_CLIENT_ID", "client")
    monkeypatch.setenv("COGDOC_OIDC_FLOW_TTL_SECONDS", "300")
    monkeypatch.setenv("COGDOC_OIDC_HANDOFF_TTL_SECONDS", "45")

    settings = get_settings()

    assert settings.cogdoc_oidc_enabled is True
    assert settings.cogdoc_oidc_issuer == "https://id.example.com"
    assert settings.cogdoc_oidc_client_id == "client"
    assert settings.cogdoc_oidc_flow_ttl_seconds == 300
    assert settings.cogdoc_oidc_handoff_ttl_seconds == 45
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_oidc_flow_ttl_seconds=29)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_oidc_handoff_ttl_seconds=301)


def test_scim_settings_are_explicit_and_role_bounded():
    settings = Settings(
        _env_file=None,
        cogdoc_scim_enabled=True,
        cogdoc_scim_bearer_tokens='[{"token":"secret","workspace_id":"wsp"}]',
        cogdoc_scim_default_role="reviewer",
        cogdoc_scim_group_role_map='{"Admins":"admin"}',
    )
    assert settings.cogdoc_scim_enabled is True
    assert settings.cogdoc_scim_default_role == "reviewer"
    with pytest.raises(ValueError):
        Settings(_env_file=None, cogdoc_scim_default_role="owner")


def test_rate_limit_settings_prefer_prefixed_names_and_accept_legacy(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "41")
    monkeypatch.setenv("RATE_LIMIT_BURST", "42")
    assert Settings(_env_file=None).rate_limit_per_minute == 41
    assert Settings(_env_file=None).rate_limit_burst == 42

    monkeypatch.setenv("COGDOC_RATE_LIMIT_PER_MINUTE", "51")
    monkeypatch.setenv("COGDOC_RATE_LIMIT_BURST", "52")
    settings = Settings(_env_file=None)
    assert settings.rate_limit_per_minute == 51
    assert settings.rate_limit_burst == 52
