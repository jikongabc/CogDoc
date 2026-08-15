from functools import lru_cache
import json
from pathlib import Path
from typing import Any
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# 仓库根目录，作为环境文件与默认数据目录的锚点。
PROJECT_ROOT = Path(__file__).resolve().parents[3]


# 项目路径。
class Settings(BaseSettings):
    cogdoc_doc_dir: str = Field(
        default="your_documents", validation_alias="COGDOC_DOC_DIR"
    )
    cogdoc_data_dir: str = Field(default="./data", validation_alias="COGDOC_DATA_DIR")
    cogdoc_default_doc_id: str = Field(
        default="arch_blueprint_2026", validation_alias="COGDOC_DEFAULT_DOC_ID"
    )
    cogdoc_log_level: str = Field(default="INFO", validation_alias="COGDOC_LOG_LEVEL")
    cogdoc_log_file: str = Field(
        default="logs/cogdoc.jsonl", validation_alias="COGDOC_LOG_FILE"
    )
    cogdoc_log_to_console: bool = Field(
        default=False, validation_alias="COGDOC_LOG_TO_CONSOLE"
    )
    cogdoc_webhook_url: str = Field(default="", validation_alias="COGDOC_WEBHOOK_URL")
    cogdoc_webhook_secret: str = Field(
        default="", validation_alias="COGDOC_WEBHOOK_SECRET"
    )
    cogdoc_webhook_timeout_seconds: float = Field(
        default=3.0, validation_alias="COGDOC_WEBHOOK_TIMEOUT_SECONDS"
    )
    cogdoc_feedback_store: str = Field(
        default="jsonl", validation_alias="COGDOC_FEEDBACK_STORE"
    )
    cogdoc_state_backend: str = Field(
        default="jsonl",
        pattern="^(jsonl|sqlite)$",
        validation_alias="COGDOC_STATE_BACKEND",
    )
    cogdoc_derived_knowledge_index_auto_refresh: bool = Field(
        default=False,
        validation_alias="COGDOC_DERIVED_KNOWLEDGE_INDEX_AUTO_REFRESH",
    )
    cogdoc_trace_enabled: bool = Field(
        default=True, validation_alias="COGDOC_TRACE_ENABLED"
    )
    cogdoc_trace_dir: str = Field(
        default="logs/traces", validation_alias="COGDOC_TRACE_DIR"
    )

    # 访问控制：旧密钥逗号分隔；只有全部三类凭据均为空才关闭鉴权。
    cogdoc_api_keys: str = Field(default="", validation_alias="COGDOC_API_KEYS")
    # 团队工作区身份映射。JSON 对象的 key 是 API key，value 包含
    # tenant_id / subject_id / role。与旧 COGDOC_API_KEYS 可并存；旧 key
    # 为了无损升级仍映射到 default 租户的 admin。
    cogdoc_api_principals: str = Field(
        default="", validation_alias="COGDOC_API_PRINCIPALS"
    )
    # 评测与 Research 发布审核是高权限操作；未配置时相关接口保持关闭。
    cogdoc_eval_review_api_keys: str = Field(
        default="", validation_alias="COGDOC_EVAL_REVIEW_API_KEYS"
    )
    # Persistent human accounts are opt-in for compatibility with existing
    # local/API-key deployments. Once enabled, protected endpoints require a
    # real session (or an explicitly configured service API key).
    cogdoc_account_auth_enabled: bool = Field(
        default=False, validation_alias="COGDOC_ACCOUNT_AUTH_ENABLED"
    )
    cogdoc_self_registration_enabled: bool = Field(
        default=True, validation_alias="COGDOC_SELF_REGISTRATION_ENABLED"
    )
    cogdoc_auth_session_ttl_seconds: float = Field(
        default=30 * 24 * 60 * 60,
        ge=300,
        le=10 * 365 * 24 * 60 * 60,
        validation_alias="COGDOC_AUTH_SESSION_TTL_SECONDS",
    )
    cogdoc_auth_invite_ttl_seconds: float = Field(
        default=7 * 24 * 60 * 60,
        ge=300,
        le=365 * 24 * 60 * 60,
        validation_alias="COGDOC_AUTH_INVITE_TTL_SECONDS",
    )
    cogdoc_auth_max_failed_logins: int = Field(
        default=5,
        ge=1,
        le=100,
        validation_alias="COGDOC_AUTH_MAX_FAILED_LOGINS",
    )
    cogdoc_auth_lockout_seconds: float = Field(
        default=15 * 60,
        ge=1,
        le=24 * 60 * 60,
        validation_alias="COGDOC_AUTH_LOCKOUT_SECONDS",
    )
    # 限流令牌桶：每分钟补充速率 + 突发容量；容量<=0 关闭限流。
    rate_limit_per_minute: int = Field(
        default=120, validation_alias="RATE_LIMIT_PER_MINUTE"
    )
    rate_limit_burst: int = Field(default=120, validation_alias="RATE_LIMIT_BURST")
    # 0 表示不设限。配额只以服务端解析出的 tenant_id 为信任边界。
    cogdoc_tenant_max_knowledge_bases: int = Field(
        default=0,
        ge=0,
        validation_alias="COGDOC_TENANT_MAX_KNOWLEDGE_BASES",
    )
    cogdoc_tenant_max_documents: int = Field(
        default=0,
        ge=0,
        validation_alias="COGDOC_TENANT_MAX_DOCUMENTS",
    )
    cogdoc_tenant_max_storage_mb: int = Field(
        default=0,
        ge=0,
        validation_alias="COGDOC_TENANT_MAX_STORAGE_MB",
    )
    cogdoc_offload_workers: int = Field(
        default=2, validation_alias="COGDOC_OFFLOAD_WORKERS"
    )
    # SSE worker events cross a thread/event-loop boundary.  Bound the time
    # without any event so a wedged provider cannot leave an HTTP request open
    # forever; the queue bridge itself polls more frequently as a lost-wakeup
    # watchdog.
    cogdoc_chat_stream_idle_timeout_seconds: float = Field(
        default=300.0,
        ge=1.0,
        le=3600.0,
        validation_alias="COGDOC_CHAT_STREAM_IDLE_TIMEOUT_SECONDS",
    )
    cogdoc_research_workers: int = Field(
        default=2,
        ge=1,
        le=16,
        validation_alias="COGDOC_RESEARCH_WORKERS",
    )
    cogdoc_research_retrieval_top_k: int = Field(
        default=8,
        ge=1,
        le=50,
        validation_alias="COGDOC_RESEARCH_RETRIEVAL_TOP_K",
    )
    # Deep Research uses a bounded submission queue plus durable, per-phase
    # execution budgets.  Provider request timeouts remain a lower-level guard;
    # these limits bound the complete background attempt.
    cogdoc_research_max_pending: int = Field(
        default=32,
        ge=1,
        le=1000,
        validation_alias="COGDOC_RESEARCH_MAX_PENDING",
    )
    cogdoc_research_provider_workers: int = Field(
        default=4,
        ge=1,
        le=64,
        validation_alias="COGDOC_RESEARCH_PROVIDER_WORKERS",
    )
    cogdoc_research_provider_max_pending: int = Field(
        default=16,
        ge=1,
        le=1000,
        validation_alias="COGDOC_RESEARCH_PROVIDER_MAX_PENDING",
    )
    cogdoc_research_provider_call_timeout_seconds: float = Field(
        default=180.0,
        ge=0.01,
        le=3600.0,
        validation_alias="COGDOC_RESEARCH_PROVIDER_CALL_TIMEOUT_SECONDS",
    )
    cogdoc_research_llm_process_isolation_enabled: bool = Field(
        default=True,
        validation_alias="COGDOC_RESEARCH_LLM_PROCESS_ISOLATION_ENABLED",
    )
    cogdoc_research_provider_kill_grace_seconds: float = Field(
        default=0.5,
        ge=0.01,
        le=10.0,
        validation_alias="COGDOC_RESEARCH_PROVIDER_KILL_GRACE_SECONDS",
    )
    cogdoc_research_provider_ipc_max_bytes: int = Field(
        default=2_000_000,
        ge=1024,
        le=100_000_000,
        validation_alias="COGDOC_RESEARCH_PROVIDER_IPC_MAX_BYTES",
    )
    cogdoc_research_evidence_deadline_seconds: float = Field(
        default=900.0,
        ge=1.0,
        le=86400.0,
        validation_alias="COGDOC_RESEARCH_EVIDENCE_DEADLINE_SECONDS",
    )
    cogdoc_research_report_deadline_seconds: float = Field(
        default=1800.0,
        ge=1.0,
        le=86400.0,
        validation_alias="COGDOC_RESEARCH_REPORT_DEADLINE_SECONDS",
    )
    cogdoc_research_planning_deadline_seconds: float = Field(
        default=300.0,
        ge=1.0,
        le=3600.0,
        validation_alias="COGDOC_RESEARCH_PLANNING_DEADLINE_SECONDS",
    )
    cogdoc_research_planning_workers: int = Field(
        default=1,
        ge=1,
        le=8,
        validation_alias="COGDOC_RESEARCH_PLANNING_WORKERS",
    )
    cogdoc_research_planning_max_pending: int = Field(
        default=8,
        ge=1,
        le=100,
        validation_alias="COGDOC_RESEARCH_PLANNING_MAX_PENDING",
    )
    cogdoc_research_max_retrieval_queries: int = Field(
        default=128,
        ge=1,
        le=10000,
        validation_alias="COGDOC_RESEARCH_MAX_RETRIEVAL_QUERIES",
    )
    cogdoc_research_max_candidate_docs: int = Field(
        default=2048,
        ge=1,
        le=100000,
        validation_alias="COGDOC_RESEARCH_MAX_CANDIDATE_DOCS",
    )
    cogdoc_research_max_llm_calls: int = Field(
        default=256,
        ge=1,
        le=10000,
        validation_alias="COGDOC_RESEARCH_MAX_LLM_CALLS",
    )
    cogdoc_research_max_model_input_chars: int = Field(
        default=5_000_000,
        ge=1000,
        le=100_000_000,
        validation_alias="COGDOC_RESEARCH_MAX_MODEL_INPUT_CHARS",
    )

    # 分层记忆预算：展示历史不受这些限制，只有送入模型的工作上下文会被裁剪。
    memory_short_message_limit: int = Field(
        default=12, ge=2, le=100, validation_alias="COGDOC_MEMORY_SHORT_MESSAGE_LIMIT"
    )
    memory_short_char_limit: int = Field(
        default=6000,
        ge=500,
        le=100000,
        validation_alias="COGDOC_MEMORY_SHORT_CHAR_LIMIT",
    )
    memory_mid_char_limit: int = Field(
        default=4000, ge=500, le=50000, validation_alias="COGDOC_MEMORY_MID_CHAR_LIMIT"
    )
    memory_long_fact_limit: int = Field(
        default=64, ge=1, le=1000, validation_alias="COGDOC_MEMORY_LONG_FACT_LIMIT"
    )
    memory_context_long_limit: int = Field(
        default=8, ge=0, le=100, validation_alias="COGDOC_MEMORY_CONTEXT_LONG_LIMIT"
    )
    memory_retrieval_enabled: bool = Field(
        default=True, validation_alias="COGDOC_MEMORY_RETRIEVAL_ENABLED"
    )
    memory_semantic_enabled: bool = Field(
        default=True, validation_alias="COGDOC_MEMORY_SEMANTIC_ENABLED"
    )
    memory_retrieval_short_limit: int = Field(
        default=8,
        ge=0,
        le=100,
        validation_alias="COGDOC_MEMORY_RETRIEVAL_SHORT_LIMIT",
    )
    memory_retrieval_mid_limit: int = Field(
        default=4,
        ge=0,
        le=100,
        validation_alias="COGDOC_MEMORY_RETRIEVAL_MID_LIMIT",
    )
    memory_retrieval_recent_pin: int = Field(
        default=4,
        ge=0,
        le=100,
        validation_alias="COGDOC_MEMORY_RETRIEVAL_RECENT_PIN",
    )
    memory_semantic_include_short: bool = Field(
        default=False,
        validation_alias="COGDOC_MEMORY_SEMANTIC_INCLUDE_SHORT",
    )
    memory_rrf_k: float = Field(
        default=60.0, gt=0.0, validation_alias="COGDOC_MEMORY_RRF_K"
    )
    memory_recency_weight: float = Field(
        default=1.0, ge=0.0, validation_alias="COGDOC_MEMORY_RECENCY_WEIGHT"
    )
    memory_lexical_weight: float = Field(
        default=1.4, ge=0.0, validation_alias="COGDOC_MEMORY_LEXICAL_WEIGHT"
    )
    memory_semantic_weight: float = Field(
        default=1.6, ge=0.0, validation_alias="COGDOC_MEMORY_SEMANTIC_WEIGHT"
    )
    memory_importance_weight: float = Field(
        default=0.8, ge=0.0, validation_alias="COGDOC_MEMORY_IMPORTANCE_WEIGHT"
    )
    memory_mid_priority_weight: float = Field(
        default=0.8,
        ge=0.0,
        validation_alias="COGDOC_MEMORY_MID_PRIORITY_WEIGHT",
    )

    # 云端模型兼容后端。
    llm_model_name: str = Field(
        default="deepseek-chat", validation_alias="LLM_MODEL_NAME"
    )
    llm_base_url: str = Field(
        default="https://api.deepseek.com/v1", validation_alias="LLM_BASE_URL"
    )
    llm_api_key: str = Field(default="", validation_alias="LLM_API_KEY")
    llm_structured_output_method: str = Field(
        default="auto", validation_alias="LLM_STRUCTURED_OUTPUT_METHOD"
    )
    # 云端韧性：单次调用硬超时与传输层重试次数。
    llm_timeout_seconds: float = Field(
        default=90.0, validation_alias="LLM_TIMEOUT_SECONDS"
    )
    llm_max_retries: int = Field(default=2, validation_alias="LLM_MAX_RETRIES")
    # 独立阅卷模型配置；留空时回退到云端主模型，但角色仍保持 Judge。
    llm_judge_model_name: str = Field(
        default="", validation_alias="LLM_JUDGE_MODEL_NAME"
    )
    llm_judge_enabled: bool = Field(default=True, validation_alias="LLM_JUDGE_ENABLED")
    llm_judge_temperature: float = Field(
        default=0.0, ge=0.0, le=1.0, validation_alias="LLM_JUDGE_TEMPERATURE"
    )

    # 云端节点级模型覆盖；留空时回退到 LLM_MODEL_NAME。
    llm_router_model_name: str = Field(
        default="", validation_alias="LLM_ROUTER_MODEL_NAME"
    )
    llm_query_rewriter_model_name: str = Field(
        default="", validation_alias="LLM_QUERY_REWRITER_MODEL_NAME"
    )
    llm_research_planner_model_name: str = Field(
        default="", validation_alias="LLM_RESEARCH_PLANNER_MODEL_NAME"
    )
    llm_source_resolver_model_name: str = Field(
        default="", validation_alias="LLM_SOURCE_RESOLVER_MODEL_NAME"
    )
    llm_evidence_verifier_model_name: str = Field(
        default="", validation_alias="LLM_EVIDENCE_VERIFIER_MODEL_NAME"
    )
    llm_claim_verifier_model_name: str = Field(
        default="", validation_alias="LLM_CLAIM_VERIFIER_MODEL_NAME"
    )
    llm_claim_repairer_model_name: str = Field(
        default="", validation_alias="LLM_CLAIM_REPAIRER_MODEL_NAME"
    )
    llm_qa_generator_model_name: str = Field(
        default="", validation_alias="LLM_QA_GENERATOR_MODEL_NAME"
    )
    llm_summary_generator_model_name: str = Field(
        default="", validation_alias="LLM_SUMMARY_GENERATOR_MODEL_NAME"
    )
    llm_compare_profile_model_name: str = Field(
        default="", validation_alias="LLM_COMPARE_PROFILE_MODEL_NAME"
    )
    llm_compare_conclusion_model_name: str = Field(
        default="", validation_alias="LLM_COMPARE_CONCLUSION_MODEL_NAME"
    )
    llm_router_backend: str = Field(
        default="default", validation_alias="LLM_ROUTER_BACKEND"
    )
    llm_query_rewriter_backend: str = Field(
        default="default", validation_alias="LLM_QUERY_REWRITER_BACKEND"
    )
    llm_research_planner_backend: str = Field(
        default="default", validation_alias="LLM_RESEARCH_PLANNER_BACKEND"
    )
    llm_source_resolver_backend: str = Field(
        default="default", validation_alias="LLM_SOURCE_RESOLVER_BACKEND"
    )
    llm_evidence_verifier_backend: str = Field(
        default="default", validation_alias="LLM_EVIDENCE_VERIFIER_BACKEND"
    )
    llm_claim_verifier_backend: str = Field(
        default="default", validation_alias="LLM_CLAIM_VERIFIER_BACKEND"
    )
    llm_claim_repairer_backend: str = Field(
        default="default", validation_alias="LLM_CLAIM_REPAIRER_BACKEND"
    )
    llm_qa_generator_backend: str = Field(
        default="default", validation_alias="LLM_QA_GENERATOR_BACKEND"
    )
    llm_summary_generator_backend: str = Field(
        default="default", validation_alias="LLM_SUMMARY_GENERATOR_BACKEND"
    )
    llm_compare_profile_backend: str = Field(
        default="default", validation_alias="LLM_COMPARE_PROFILE_BACKEND"
    )
    llm_compare_conclusion_backend: str = Field(
        default="default", validation_alias="LLM_COMPARE_CONCLUSION_BACKEND"
    )

    # 本地模型兼容后端。
    ollama_model_name: str = Field(
        default="qwen2.5:7b", validation_alias="OLLAMA_MODEL_NAME"
    )
    ollama_base_url: str = Field(
        default="http://localhost:11434/v1", validation_alias="OLLAMA_BASE_URL"
    )
    ollama_api_key: str = Field(default="ollama", validation_alias="OLLAMA_API_KEY")
    # 本地模型通常比云端慢，超时更长且重试更少。
    ollama_timeout_seconds: float = Field(
        default=180.0, validation_alias="OLLAMA_TIMEOUT_SECONDS"
    )
    ollama_max_retries: int = Field(default=1, validation_alias="OLLAMA_MAX_RETRIES")

    # 本地节点级模型覆盖；留空时回退到 OLLAMA_MODEL_NAME。
    ollama_router_model_name: str = Field(
        default="", validation_alias="OLLAMA_ROUTER_MODEL_NAME"
    )
    ollama_query_rewriter_model_name: str = Field(
        default="", validation_alias="OLLAMA_QUERY_REWRITER_MODEL_NAME"
    )
    ollama_research_planner_model_name: str = Field(
        default="", validation_alias="OLLAMA_RESEARCH_PLANNER_MODEL_NAME"
    )
    ollama_source_resolver_model_name: str = Field(
        default="", validation_alias="OLLAMA_SOURCE_RESOLVER_MODEL_NAME"
    )
    ollama_evidence_verifier_model_name: str = Field(
        default="", validation_alias="OLLAMA_EVIDENCE_VERIFIER_MODEL_NAME"
    )
    ollama_claim_verifier_model_name: str = Field(
        default="", validation_alias="OLLAMA_CLAIM_VERIFIER_MODEL_NAME"
    )
    ollama_claim_repairer_model_name: str = Field(
        default="", validation_alias="OLLAMA_CLAIM_REPAIRER_MODEL_NAME"
    )
    ollama_qa_generator_model_name: str = Field(
        default="", validation_alias="OLLAMA_QA_GENERATOR_MODEL_NAME"
    )
    ollama_summary_generator_model_name: str = Field(
        default="", validation_alias="OLLAMA_SUMMARY_GENERATOR_MODEL_NAME"
    )
    ollama_compare_profile_model_name: str = Field(
        default="", validation_alias="OLLAMA_COMPARE_PROFILE_MODEL_NAME"
    )
    ollama_compare_conclusion_model_name: str = Field(
        default="", validation_alias="OLLAMA_COMPARE_CONCLUSION_MODEL_NAME"
    )

    # 检索与生成控制。
    qa_retrieval_top_k: int = Field(default=9, validation_alias="QA_RETRIEVAL_TOP_K")
    qa_rerank_top_n: int = Field(default=3, validation_alias="QA_RERANK_TOP_N")
    qa_rerank_max_candidates: int = Field(
        default=12, validation_alias="QA_RERANK_MAX_CANDIDATES"
    )
    qa_rerank_on_cpu: bool = Field(default=False, validation_alias="QA_RERANK_ON_CPU")
    # 多需求问题为每个原子需求保留独立精排锚点；单需求仍沿用固定 top-n。
    qa_rerank_docs_per_requirement: int = Field(
        default=2,
        ge=1,
        le=5,
        validation_alias="QA_RERANK_DOCS_PER_REQUIREMENT",
    )
    # 简短、无指代、无并列需求的问题直接使用原问题检索，省去一次 LLM 改写。
    qa_query_rewrite_fast_path_enabled: bool = Field(
        default=True,
        validation_alias="QA_QUERY_REWRITE_FAST_PATH_ENABLED",
    )
    # 重排命中保持 child citation identity，同时从同一结构父块补充有界上下文。
    qa_parent_context_enabled: bool = Field(
        default=True, validation_alias="QA_PARENT_CONTEXT_ENABLED"
    )
    qa_parent_context_max_chunks: int = Field(
        default=5,
        ge=1,
        le=20,
        validation_alias="QA_PARENT_CONTEXT_MAX_CHUNKS",
    )
    qa_parent_context_max_chars: int = Field(
        default=3600,
        ge=200,
        le=30000,
        validation_alias="QA_PARENT_CONTEXT_MAX_CHARS",
    )
    # 在全局 pack 之前从长 chunk 中选择可回溯的连续原文区间。
    qa_evidence_span_enabled: bool = Field(
        default=True, validation_alias="QA_EVIDENCE_SPAN_ENABLED"
    )
    qa_evidence_span_max_chars_per_doc: int = Field(
        default=420,
        ge=120,
        le=5000,
        validation_alias="QA_EVIDENCE_SPAN_MAX_CHARS_PER_DOC",
    )
    qa_evidence_span_context_sentences: int = Field(
        default=1,
        ge=0,
        le=5,
        validation_alias="QA_EVIDENCE_SPAN_CONTEXT_SENTENCES",
    )
    # Parent hydration 后再施加一次请求级证据预算；anchor/pinned 是不可丢硬约束。
    qa_evidence_pack_max_docs: int = Field(
        default=8,
        ge=1,
        le=50,
        validation_alias="QA_EVIDENCE_PACK_MAX_DOCS",
    )
    qa_evidence_pack_max_chars: int = Field(
        default=7200,
        ge=500,
        le=100000,
        validation_alias="QA_EVIDENCE_PACK_MAX_CHARS",
    )
    qa_abstain_enabled: bool = Field(
        default=True, validation_alias="QA_ABSTAIN_ENABLED"
    )
    qa_abstain_max_vector_distance: float = Field(
        default=0.7117711305618286,
        ge=0.0,
        validation_alias="QA_ABSTAIN_MAX_VECTOR_DISTANCE",
    )
    qa_abstain_min_bm25_score: float = Field(
        default=12.328925491936891,
        ge=0.0,
        validation_alias="QA_ABSTAIN_MIN_BM25_SCORE",
    )
    # 向量派生知识与词法回退的分数尺度不同，分别校准。旧变量仅作为
    # 两个新阈值的兼容回退；任一新变量显式存在时始终优先。
    qa_abstain_min_knowledge_vector_score: float = Field(
        default=0.5,
        ge=0.0,
        validation_alias=AliasChoices(
            "QA_ABSTAIN_MIN_KNOWLEDGE_VECTOR_SCORE",
            "QA_ABSTAIN_MIN_KNOWLEDGE_SCORE",
        ),
    )
    qa_abstain_min_knowledge_lexical_score: float = Field(
        default=0.5,
        ge=0.0,
        validation_alias=AliasChoices(
            "QA_ABSTAIN_MIN_KNOWLEDGE_LEXICAL_SCORE",
            "QA_ABSTAIN_MIN_KNOWLEDGE_SCORE",
        ),
    )
    qa_abstain_allow_missing_signals: bool = Field(
        default=False,
        validation_alias="QA_ABSTAIN_ALLOW_MISSING_SIGNALS",
    )
    qa_evidence_verify_enabled: bool = Field(
        default=True, validation_alias="QA_EVIDENCE_VERIFY_ENABLED"
    )
    qa_evidence_verify_max_docs: int = Field(
        default=3, ge=1, le=10, validation_alias="QA_EVIDENCE_VERIFY_MAX_DOCS"
    )
    qa_evidence_verify_max_chars_per_doc: int = Field(
        default=1600,
        ge=200,
        le=10000,
        validation_alias="QA_EVIDENCE_VERIFY_MAX_CHARS_PER_DOC",
    )
    qa_evidence_verify_borderline_min_score: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        validation_alias="QA_EVIDENCE_VERIFY_BORDERLINE_MIN_SCORE",
    )
    # Summary/Compare and future task agents share this closed-set unit gate.
    evidence_unit_verify_enabled: bool = Field(
        default=True, validation_alias="EVIDENCE_UNIT_VERIFY_ENABLED"
    )
    evidence_unit_verify_max_chars_per_doc: int = Field(
        default=1200,
        ge=200,
        le=10000,
        validation_alias="EVIDENCE_UNIT_VERIFY_MAX_CHARS_PER_DOC",
    )
    evidence_unit_verify_max_units_per_batch: int = Field(
        default=8,
        ge=1,
        le=64,
        validation_alias="EVIDENCE_UNIT_VERIFY_MAX_UNITS_PER_BATCH",
    )
    # 多部分问题按证据需求组织候选；首次覆盖不足时只允许一次有界深召回。
    qa_retrieval_max_queries: int = Field(
        default=7,
        ge=1,
        le=16,
        validation_alias="QA_RETRIEVAL_MAX_QUERIES",
    )
    qa_adaptive_retrieval_enabled: bool = Field(
        default=True,
        validation_alias="QA_ADAPTIVE_RETRIEVAL_ENABLED",
    )
    qa_adaptive_retrieval_max_retries: int = Field(
        default=1,
        ge=0,
        le=2,
        validation_alias="QA_ADAPTIVE_RETRIEVAL_MAX_RETRIES",
    )
    qa_adaptive_retrieval_top_k_multiplier: float = Field(
        default=2.0,
        ge=1.0,
        le=4.0,
        validation_alias="QA_ADAPTIVE_RETRIEVAL_TOP_K_MULTIPLIER",
    )
    qa_adaptive_retrieval_max_top_k: int = Field(
        default=36,
        ge=1,
        le=100,
        validation_alias="QA_ADAPTIVE_RETRIEVAL_MAX_TOP_K",
    )
    # 生成后逐声明证据校验；初次发布默认关闭，便于先建立人工基线。
    claim_verification_enabled: bool = Field(
        default=False, validation_alias="CLAIM_VERIFICATION_ENABLED"
    )
    claim_verification_max_claims: int = Field(
        default=40,
        ge=1,
        le=200,
        validation_alias="CLAIM_VERIFICATION_MAX_CLAIMS",
    )
    claim_verification_max_claims_per_batch: int = Field(
        default=8,
        ge=1,
        le=40,
        validation_alias="CLAIM_VERIFICATION_MAX_CLAIMS_PER_BATCH",
    )
    claim_verification_max_docs_per_batch: int = Field(
        default=12,
        ge=1,
        le=40,
        validation_alias="CLAIM_VERIFICATION_MAX_DOCS_PER_BATCH",
    )
    claim_verification_max_chars_per_doc: int = Field(
        default=1600,
        ge=200,
        le=10000,
        validation_alias="CLAIM_VERIFICATION_MAX_CHARS_PER_DOC",
    )
    claim_verification_max_repair_attempts: int = Field(
        default=1,
        ge=0,
        le=3,
        validation_alias="CLAIM_VERIFICATION_MAX_REPAIR_ATTEMPTS",
    )
    hybrid_rrf_k: int = Field(default=60, validation_alias="HYBRID_RRF_K")
    cloud_section_max_workers: int = Field(
        default=6, validation_alias="CLOUD_SECTION_MAX_WORKERS"
    )

    # 模型设备阈值，单位兆字节。
    embedder_min_cuda_free_mb: int = Field(
        default=800, validation_alias="EMBEDDER_MIN_CUDA_FREE_MB"
    )
    reranker_min_cuda_free_mb: int = Field(
        default=2800, validation_alias="RERANKER_MIN_CUDA_FREE_MB"
    )
    torch_num_threads: int = Field(
        default=2, validation_alias="COGDOC_TORCH_NUM_THREADS"
    )
    cogdoc_embedder_max_concurrency: int = Field(
        default=1, validation_alias="COGDOC_EMBEDDER_MAX_CONCURRENCY"
    )
    cogdoc_reranker_max_concurrency: int = Field(
        default=1, validation_alias="COGDOC_RERANKER_MAX_CONCURRENCY"
    )

    # 入库上传单文件大小上限，最小毒丸防护。
    max_upload_mb: int = Field(default=50, validation_alias="COGDOC_MAX_UPLOAD_MB")

    # 扫描页 OCR 默认关闭；开启后仅处理原生文本不足的候选页。
    cogdoc_ocr_enabled: bool = Field(
        default=False, validation_alias="COGDOC_OCR_ENABLED"
    )
    cogdoc_ocr_provider: str = Field(
        default="tesseract",
        pattern="^tesseract$",
        validation_alias="COGDOC_OCR_PROVIDER",
    )
    cogdoc_ocr_binary: str = Field(
        default="tesseract", min_length=1, validation_alias="COGDOC_OCR_BINARY"
    )
    cogdoc_ocr_languages: str = Field(
        default="eng+chi_sim",
        pattern=r"^[A-Za-z0-9_+-]+$",
        validation_alias="COGDOC_OCR_LANGUAGES",
    )
    cogdoc_ocr_dpi: int = Field(
        default=300, ge=72, le=600, validation_alias="COGDOC_OCR_DPI"
    )
    cogdoc_ocr_min_native_chars: int = Field(
        default=40,
        ge=0,
        le=5000,
        validation_alias="COGDOC_OCR_MIN_NATIVE_CHARS",
    )
    cogdoc_ocr_max_pages: int = Field(
        default=100, ge=1, le=10000, validation_alias="COGDOC_OCR_MAX_PAGES"
    )
    cogdoc_ocr_page_timeout_seconds: float = Field(
        default=30.0,
        ge=0.1,
        le=300.0,
        validation_alias="COGDOC_OCR_PAGE_TIMEOUT_SECONDS",
    )
    cogdoc_ocr_required: bool = Field(
        default=False, validation_alias="COGDOC_OCR_REQUIRED"
    )

    # 评测默认路径。
    eval_set_path: str = Field(
        default="eval/retrieval_eval.jsonl", validation_alias="COGDOC_EVAL_SET"
    )
    eval_example_set_path: str = Field(
        default="eval/retrieval_eval.example.jsonl",
        validation_alias="COGDOC_EVAL_EXAMPLE_SET",
    )
    quality_eval_set_path: str = Field(
        default="eval/quality_eval.jsonl", validation_alias="COGDOC_QUALITY_EVAL_SET"
    )
    quality_eval_example_set_path: str = Field(
        default="eval/quality_eval.example.jsonl",
        validation_alias="COGDOC_QUALITY_EVAL_EXAMPLE_SET",
    )

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # 处理数据目录。
    @property
    def data_dir(self) -> Path:
        return Path(self.cogdoc_data_dir)

    # 构造分层记忆策略。
    @property
    def memory_policy(self):
        from cogdoc.memory.manager import MemoryPolicy

        return MemoryPolicy(
            short_term_message_limit=self.memory_short_message_limit,
            short_term_char_limit=self.memory_short_char_limit,
            mid_term_char_limit=self.memory_mid_char_limit,
            long_term_fact_limit=self.memory_long_fact_limit,
            context_long_term_limit=self.memory_context_long_limit,
            memory_retrieval_enabled=self.memory_retrieval_enabled,
            memory_semantic_enabled=self.memory_semantic_enabled,
            memory_retrieval_short_limit=self.memory_retrieval_short_limit,
            memory_retrieval_mid_limit=self.memory_retrieval_mid_limit,
            memory_retrieval_recent_pin=self.memory_retrieval_recent_pin,
            memory_semantic_include_short=self.memory_semantic_include_short,
            memory_rrf_k=self.memory_rrf_k,
            memory_recency_weight=self.memory_recency_weight,
            memory_lexical_weight=self.memory_lexical_weight,
            memory_semantic_weight=self.memory_semantic_weight,
            memory_importance_weight=self.memory_importance_weight,
            memory_mid_priority_weight=self.memory_mid_priority_weight,
        )

    # 处理向量持久化目录。
    @property
    def chroma_persist_dir(self) -> str:
        return str(self.data_dir / "chroma_db")

    # 处理关键词索引持久化目录。
    @property
    def bm25_persist_dir(self) -> str:
        return str(self.data_dir / "bm25_db")

    # 构造目录。
    @property
    def manifest_dir(self) -> str:
        return str(self.data_dir / "manifests")

    # 完成 知识库根目录 处理。
    @property
    def kb_root(self) -> str:
        return str(self.data_dir / "kb")

    # 完成 知识库注册表路径 处理。
    @property
    def kb_registry_path(self) -> str:
        return str(self.data_dir / "kb" / "registry.json")

    # 完成 知识库来源目录 处理。
    def kb_source_dir(self, kb_id: str) -> str:
        # 每个知识库一个源文档目录，构建时硬链接快照到索引代工作区。
        return str(self.data_dir / "kb" / kb_id / "sources")

    # 完成 知识库状态路径 处理。
    def kb_state_path(self, kb_id: str) -> str:
        # 事务化索引的提交指针，与每文档清单分离。
        return str(self.data_dir / "kb" / kb_id / "state.json")

    # 完成 知识库索引代目录 处理。
    def kb_generation_dir(self, kb_id: str, generation_id: str) -> str:
        # 单个索引代的工作区，保存源文件快照和内部产物。
        return str(self.data_dir / "kb" / kb_id / "generations" / generation_id)

    # 处理知识库集合标识。
    def kb_collection_id(self, kb_id: str, gen_id: str) -> str:
        # 集合标识由知识库短哈希和索引代标识组成。
        import hashlib

        return f"{hashlib.sha256(kb_id.encode()).hexdigest()[:8]}-{gen_id}"

    # 处理接口密钥集合。
    @property
    def api_key_set(self) -> set[str]:
        # 解析逗号分隔的密钥列表，空集合表示鉴权关闭。
        return {k.strip() for k in self.cogdoc_api_keys.split(",") if k.strip()}

    @property
    def api_principal_map(self) -> dict[str, dict[str, Any]]:
        """Parse the optional API-key-to-principal map fail closed.

        Parsing is deliberately deferred until application construction so a
        malformed production credential map cannot silently degrade to the
        legacy shared-default identity.
        """

        raw = self.cogdoc_api_principals.strip()
        if not raw:
            return {}

        def reject_duplicate_keys(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError(
                        f"COGDOC_API_PRINCIPALS contains duplicate key: {key!r}"
                    )
                value[key] = item
            return value

        try:
            payload = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise ValueError("COGDOC_API_PRINCIPALS must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("COGDOC_API_PRINCIPALS must be a JSON object")
        result: dict[str, dict[str, Any]] = {}
        for raw_key, raw_principal in payload.items():
            key = str(raw_key).strip()
            if not key or not isinstance(raw_principal, dict):
                raise ValueError(
                    "COGDOC_API_PRINCIPALS entries require a non-blank key "
                    "and object value"
                )
            if key in result:
                raise ValueError(
                    "COGDOC_API_PRINCIPALS contains keys that collide after trimming"
                )
            if set(raw_principal) != {"tenant_id", "subject_id", "role"}:
                raise ValueError(
                    "COGDOC_API_PRINCIPALS principal fields must be exactly "
                    "tenant_id, subject_id, and role"
                )
            result[key] = dict(raw_principal)
        return result

    @property
    def eval_review_api_key_set(self) -> set[str]:
        return {
            key.strip()
            for key in self.cogdoc_eval_review_api_keys.split(",")
            if key.strip()
        }

    # 处理状态库路径。
    @property
    def state_db_path(self) -> str:
        # 会话与入库任务落盘，进程重启不丢状态。
        return str(self.data_dir / "state.db")

    @property
    def audit_log_path(self) -> str:
        return str(self.data_dir / "audit" / "events.jsonl")

    # 处理反馈数据库路径。
    @property
    def feedback_db_path(self) -> str:
        return str(self.data_dir / "feedback" / "feedback.db")

    # 处理反馈日志路径。
    @property
    def feedback_log_path(self) -> str:
        return str(self.data_dir / "feedback" / "feedback.jsonl")

    # 完成 坏样本用例列表路径 处理。
    @property
    def bad_cases_path(self) -> str:
        # 点踩和纠错自动归集到此，供离线质量评测使用。
        return str(self.data_dir / "feedback" / "bad_cases.jsonl")

    # 完成 反馈分析路径 处理。
    @property
    def feedback_analysis_path(self) -> str:
        return str(self.data_dir / "feedback" / "feedback_analysis.jsonl")

    # 完成 派生知识路径 处理。
    @property
    def derived_knowledge_path(self) -> str:
        return str(self.data_dir / "knowledge" / "derived_knowledge.jsonl")

    # 完成 检索反馈路径 处理。
    @property
    def retrieval_feedback_path(self) -> str:
        return str(self.data_dir / "feedback" / "retrieval_feedback.jsonl")

    @property
    def retrieval_eval_drafts_path(self) -> str:
        return str(self.data_dir / "feedback" / "retrieval_eval_drafts.jsonl")

    @property
    def research_jobs_path(self) -> str:
        return str(self.data_dir / "research" / "research_jobs.json")

    # 返回根目录。
    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    # 处理显存阈值。
    def cuda_min_free_bytes(self, setting_name: str) -> int:
        mb_by_name = {
            "EMBEDDER_MIN_CUDA_FREE_MB": self.embedder_min_cuda_free_mb,
            "RERANKER_MIN_CUDA_FREE_MB": self.reranker_min_cuda_free_mb,
        }
        if setting_name not in mb_by_name:
            raise ValueError(f"未知 CUDA 显存阈值配置: {setting_name}")
        return int(mb_by_name[setting_name]) * 1024 * 1024

    # 返回节点配置的模型名，节点未覆盖时使用对应后端的全局模型。
    def model_name_for_node(self, node_name: str | None, *, is_local: bool) -> str:
        default = self.ollama_model_name if is_local else self.llm_model_name
        if not node_name:
            return default
        prefix = "ollama" if is_local else "llm"
        value = getattr(self, f"{prefix}_{node_name}_model_name", "")
        return str(value or default).strip()

    # 节点可显式选择云端或本地后端；default 跟随本次请求模式。
    def is_local_for_node(self, node_name: str, *, request_is_local: bool) -> bool:
        backend = str(getattr(self, f"llm_{node_name}_backend", "default")).lower()
        if backend == "default":
            return request_is_local
        if backend in {"local", "ollama"}:
            return True
        if backend == "cloud":
            return False
        raise ValueError(
            f"无效节点后端 LLM_{node_name.upper()}_BACKEND={backend!r}; "
            "可选值为 default、cloud、local"
        )


# 返回设置。
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
