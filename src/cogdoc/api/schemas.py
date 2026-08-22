from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from cogdoc.config.settings import get_settings
from cogdoc.service.claim_verification_policy import (
    claim_verification_policy_projection,
)
from cogdoc.service.claim_verification_rollout import ROLLOUT_DECISIONS
from cogdoc.tools.citation_ledger import is_valid_evidence_id
from cogdoc.tools.public_citation_ledger import validate_public_citation_ledger
from cogdoc.tools.retriever.metadata import safe_retrieval_metadata


API_SCHEMA_VERSION: Literal["v1"] = "v1"


def _research_contract_key(value: str) -> str:
    return unicodedata.normalize("NFKC", " ".join(value.split())).casefold()


# 所有接口模型的基类，统一严格契约与枚举字符串化。
class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


# 对话请求模式，支持自动路由或强制指定任务。
class ChatMode(str, Enum):
    AUTO = "auto"
    QA = "qa"
    SUMMARY = "summary"
    COMPARE = "compare"


# 实际执行的任务类型，响应里回显。
class ChatTask(str, Enum):
    QA = "qa"
    SUMMARY = "summary"
    COMPARE = "compare"
    UNKNOWN = "unknown"


# 稳定错误码，前端按码处理失败、不依赖文案。
class ErrorCode(str, Enum):
    CITATION_REJECTED = "CITATION_REJECTED"
    NO_EVIDENCE = "NO_EVIDENCE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    STREAM_INTERRUPTED = "STREAM_INTERRUPTED"
    UNKNOWN_INTENT = "UNKNOWN_INTENT"
    BAD_REQUEST = "BAD_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    AUTH_CONFLICT = "AUTH_CONFLICT"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    MEMBER_NOT_FOUND = "MEMBER_NOT_FOUND"
    SERVICE_ACCOUNT_NOT_FOUND = "SERVICE_ACCOUNT_NOT_FOUND"
    INVITE_INVALID = "INVITE_INVALID"
    INVITE_NOT_FOUND = "INVITE_NOT_FOUND"
    TENANT_QUOTA_EXCEEDED = "TENANT_QUOTA_EXCEEDED"
    REQUEST_THROTTLED = "REQUEST_THROTTLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    KB_NOT_FOUND = "KB_NOT_FOUND"
    KB_EXISTS = "KB_EXISTS"
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    INVALID_PDF = "INVALID_PDF"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    INGEST_FAILED = "INGEST_FAILED"
    KB_CLEANUP_FAILED = "KB_CLEANUP_FAILED"
    TRACE_NOT_FOUND = "TRACE_NOT_FOUND"
    KNOWLEDGE_NOT_FOUND = "KNOWLEDGE_NOT_FOUND"
    RESEARCH_JOB_NOT_FOUND = "RESEARCH_JOB_NOT_FOUND"
    RESEARCH_JOB_REVISION_CONFLICT = "RESEARCH_JOB_REVISION_CONFLICT"
    RESEARCH_JOB_STATE_CONFLICT = "RESEARCH_JOB_STATE_CONFLICT"
    RESEARCH_EVIDENCE_STALE = "RESEARCH_EVIDENCE_STALE"
    RESEARCH_CAPACITY_EXHAUSTED = "RESEARCH_CAPACITY_EXHAUSTED"
    CHAT_SESSION_CONFLICT = "CHAT_SESSION_CONFLICT"
    CLAIM_REVIEW_REVISION_CONFLICT = "CLAIM_REVIEW_REVISION_CONFLICT"
    CLAIM_REVIEW_NOT_FOUND = "CLAIM_REVIEW_NOT_FOUND"
    CREDENTIAL_NOT_FOUND = "CREDENTIAL_NOT_FOUND"
    CREDENTIAL_UNAVAILABLE = "CREDENTIAL_UNAVAILABLE"
    CREDENTIAL_REVISION_CONFLICT = "CREDENTIAL_REVISION_CONFLICT"
    OAUTH_SESSION_INVALID = "OAUTH_SESSION_INVALID"
    OAUTH_PROVIDER_UNAVAILABLE = "OAUTH_PROVIDER_UNAVAILABLE"
    OIDC_FLOW_INVALID = "OIDC_FLOW_INVALID"
    OIDC_PROVIDER_UNAVAILABLE = "OIDC_PROVIDER_UNAVAILABLE"
    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    SOURCE_VERSION_NOT_FOUND = "SOURCE_VERSION_NOT_FOUND"
    SYNC_REPLAY_CONFLICT = "SYNC_REPLAY_CONFLICT"


# 带查询和知识库标识的请求基类。
class QueryDocRequest(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    query: str = Field(min_length=1)
    doc_id: str = Field(default_factory=lambda: get_settings().cogdoc_default_doc_id)

    # 清理必填文本。
    @field_validator("query", "doc_id")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


# 对话接口请求体。
class ChatRequest(QueryDocRequest):
    session_id: str | None = None
    mode: ChatMode = ChatMode.AUTO
    is_local: bool = False

    # 解析强制任务模式。
    @property
    def forced_task(self) -> str | None:
        # 枚举值已转成字符串，自动模式不强制任务。
        return None if self.mode == ChatMode.AUTO else str(self.mode)


# 引用来源取自文档元数据，不含正文。
class Citation(ApiModel):
    chunk_id: str = ""
    source_type: str = "document"
    knowledge_id: str = ""
    source: str = ""
    source_id: str = Field(default="", exclude_if=lambda value: not value)
    source_version_id: str = Field(default="", exclude_if=lambda value: not value)
    media_type: str = Field(default="", exclude_if=lambda value: not value)
    location: dict[str, Any] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    page: int | None = None
    page_start: int | None = None
    page_end: int | None = None


class ConnectionCreate(ApiModel):
    connector_type: Literal[
        "local-directory",
        "git",
        "url",
        "zotero",
        "notion",
        "confluence",
        "sharepoint",
        "s3",
    ]
    name: str = Field(min_length=1, max_length=160)
    config: dict[str, Any] = Field(default_factory=dict)
    secret_env: dict[str, str] = Field(default_factory=dict)
    credential_id: str | None = Field(default=None, min_length=1, max_length=160)
    workspace_visible: bool = False


class ConnectionEnabledUpdate(ApiModel):
    enabled: bool


class Connection(ApiModel):
    connection_id: str
    kb_id: str
    connector_type: str
    name: str
    config: dict[str, Any]
    secret_fields: list[str]
    credential_id: str | None = None
    credential_source: Literal["vault", "environment", "none"] = "none"
    workspace_visible: bool
    enabled: bool
    created_at: float
    updated_at: float
    revision: int


class ConnectionList(ApiModel):
    connections: list[Connection]


class ConnectorSyncJob(ApiModel):
    job_id: str
    kb_id: str
    connection_id: str
    connector_type: str
    status: str
    attempt: int
    pages_processed: int
    documents_seen: int
    documents_fetched: int
    deleted_seen: int
    bytes_fetched: int
    error_code: str | None = None
    error_message: str | None = None
    retry_at: float | None = None
    created_at: float
    started_at: float | None = None
    updated_at: float
    finished_at: float | None = None
    revision: int
    replay_of: str | None = None


class ConnectorSyncJobList(ApiModel):
    jobs: list[ConnectorSyncJob]


class ConnectorCredentialCreate(ApiModel):
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    credential_kind: str = Field(
        default="static", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$"
    )
    label: str = Field(min_length=1, max_length=160)
    secret_values: dict[str, str] = Field(min_length=1, max_length=32)
    connection_id: str | None = Field(default=None, min_length=1, max_length=160)
    subject: str | None = Field(default=None, min_length=1, max_length=512)
    scopes: list[str] = Field(default_factory=list, max_length=128)
    expires_at: float | None = Field(default=None, gt=0)


class ConnectorCredentialRotate(ApiModel):
    secret_values: dict[str, str] | None = Field(
        default=None, min_length=1, max_length=32
    )
    expires_at: float | None = Field(default=None, gt=0)
    expected_revision: int | None = Field(default=None, ge=1, strict=True)


class ConnectorCredential(ApiModel):
    credential_id: str
    kb_id: str
    connection_id: str | None = None
    provider: str
    credential_kind: str
    label: str
    subject: str | None = None
    scopes: list[str]
    secret_fields: list[str]
    key_version: str
    expires_at: float | None = None
    last_used_at: float | None = None
    created_by: str
    updated_by: str
    created_at: float
    updated_at: float
    revision: int


class ConnectorCredentialList(ApiModel):
    credentials: list[ConnectorCredential]


class ConnectorCredentialEvent(ApiModel):
    event_id: str
    credential_id: str
    kb_id: str
    connection_id: str | None = None
    action: str
    actor_id: str
    revision: int
    key_version: str
    occurred_at: float


class ConnectorCredentialEventList(ApiModel):
    events: list[ConnectorCredentialEvent]


class ConnectorOAuthStart(ApiModel):
    provider: Literal["notion", "atlassian", "microsoft"]
    connection_id: str | None = Field(default=None, min_length=1, max_length=160)


class ConnectorOAuthAuthorization(ApiModel):
    session_id: str
    provider: str
    authorization_url: str
    redirect_uri: str
    expires_at: float


class ConnectorOAuthCallback(ApiModel):
    credential_id: str
    provider: str
    connection_id: str | None = None
    kb_id: str
    status: Literal["connected"] = "connected"


class ConnectorSyncHealth(ApiModel):
    kb_id: str
    connection_id: str
    schedule_seconds: int | None = None
    next_run_at: float | None = None
    health_status: str
    last_job_id: str | None = None
    last_job_status: str | None = None
    last_started_at: float | None = None
    last_success_at: float | None = None
    last_failure_at: float | None = None
    last_error_code: str | None = None
    last_duration_seconds: float | None = None
    consecutive_failures: int = 0
    updated_at: float | None = None
    backlog: int = 0


class ConnectorSyncHealthList(ApiModel):
    connections: list[ConnectorSyncHealth]


class SourceCatalogEntry(ApiModel):
    source_id: str
    connection_id: str | None = None
    connector_type: str
    external_id: str
    display_name: str
    media_type: str
    kind: str
    origin_uri: str | None = None
    version_id: str
    metadata: dict[str, Any]
    health_status: str
    last_sync_at: float | None = None
    last_sync_error: str | None = None
    deleted_at: float | None = None
    updated_at: float
    content_sha256: str
    byte_size: int | None = None
    etag: str | None = None
    modified_at: str | None = None
    fetched_at: float
    document_id: str | None = None
    access_policy: str | None = None
    access_configured: bool = False
    acl_epoch: int | None = None


class SourceCatalogList(ApiModel):
    sources: list[SourceCatalogEntry]


class SourceVersion(ApiModel):
    source_id: str
    version_id: str
    content_sha256: str
    byte_size: int | None = None
    etag: str | None = None
    modified_at: str | None = None
    fetched_at: float
    created_at: float
    is_current: bool
    artifact_available: bool = False


class SourceVersionList(ApiModel):
    versions: list[SourceVersion]


class SourceArtifactVersionSummary(ApiModel):
    source_id: str
    version_id: str
    content_sha256: str
    byte_size: int
    media_type: str
    display_name: str | None = None
    created_at: float


class SourceVersionDiff(ApiModel):
    source_id: str
    from_version_id: str
    to_version_id: str
    kind: Literal["text", "binary"]
    truncated: bool
    added_lines: int
    removed_lines: int
    diff: str | None = None
    from_version: SourceArtifactVersionSummary
    to_version: SourceArtifactVersionSummary


class SourceArtifactDelete(ApiModel):
    source_id: str
    version_id: str
    recovery_token: str
    deleted: bool


class SourceArtifactRestore(ApiModel):
    source_id: str
    version_id: str
    restored: bool = True


class SourceArtifactUsage(ApiModel):
    active_bytes: int
    active_versions: int
    trash_bytes: int
    trash_versions: int


class SourceArtifactPurge(ApiModel):
    purged: int


# 单次引用在最终答案中的位置；偏移是 Unicode code point 的 0-based half-open 区间。
class CitationOccurrence(ApiModel):
    """One citation position in Python/Unicode code-point coordinates."""

    index: int = Field(ge=0, strict=True, description="全答案引用出现序号")
    answer_start: int = Field(
        ge=0,
        strict=True,
        description="0-based Unicode code-point 起点（包含）",
    )
    answer_end: int = Field(
        ge=0,
        strict=True,
        description="0-based Unicode code-point 终点（不包含）",
    )

    @model_validator(mode="after")
    def _validate_range(self):
        if self.answer_end <= self.answer_start:
            raise ValueError("answer_end must be greater than answer_start")
        return self


# 精确引用账本只公开定位信息，不含证据正文、内部展示模板或私有元数据。
class CitationLedgerEntry(ApiModel):
    evidence_id: str = Field(min_length=4, max_length=160)
    chunk_id: str = Field(min_length=1)
    source_type: str = "document"
    knowledge_id: str = ""
    source: str = ""
    source_id: str = Field(default="", exclude_if=lambda value: not value)
    source_version_id: str = Field(default="", exclude_if=lambda value: not value)
    media_type: str = Field(default="", exclude_if=lambda value: not value)
    location: dict[str, Any] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    page: int | None = Field(default=None, ge=0, strict=True)
    page_start: int | None = Field(default=None, ge=0, strict=True)
    page_end: int | None = Field(default=None, ge=0, strict=True)
    span_start: int = Field(ge=0, strict=True)
    span_end: int = Field(ge=0, strict=True)
    occurrences: list[CitationOccurrence] = Field(min_length=1)

    @field_validator("evidence_id")
    @classmethod
    def _validate_evidence_id(cls, value: str) -> str:
        if not is_valid_evidence_id(value):
            raise ValueError("evidence_id must use canonical E001/E1000 spelling")
        return value

    @model_validator(mode="after")
    def _validate_entry(self):
        if self.span_end <= self.span_start:
            raise ValueError("span_end must be greater than span_start")
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("page_end must be greater than or equal to page_start")
        if self.source_type == "derived_knowledge":
            if not self.knowledge_id.strip():
                raise ValueError("derived knowledge citation requires knowledge_id")
        elif self.source_type == "document":
            if not self.source.strip() or (self.page is None and not self.location):
                raise ValueError("document citation requires source and location")
            if self.location and not self.source_version_id.strip():
                raise ValueError("universal citation requires source_version_id")
        else:
            raise ValueError("unsupported citation source_type")
        return self


# 证据片段带截断预览，供前端证据面板展示。
class Evidence(ApiModel):
    chunk_id: str = ""
    parent_chunk_id: str = ""
    section_title: str = ""
    section_path: str = ""
    section_level: int | None = None
    child_index_in_parent: int | None = None
    source_type: str = "document"
    knowledge_id: str = ""
    chunk_index: int | None = None
    source: str = ""
    source_id: str = Field(default="", exclude_if=lambda value: not value)
    source_version_id: str = Field(default="", exclude_if=lambda value: not value)
    media_type: str = Field(default="", exclude_if=lambda value: not value)
    location: dict[str, Any] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    page: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    rerank_score: float | None = None
    rewrite_query: str | None = None
    text_preview: str = ""
    retrieval: dict[str, Any] = Field(default_factory=dict)


# 公开审计摘要不含声明全文和证据正文，完整明细仅留在 trace。
class ClaimAuditSummary(ApiModel):
    status: str = "not_run"
    reason_code: str = ""
    counts: dict[str, int] = Field(default_factory=dict)
    metrics: dict[str, float | None] = Field(default_factory=dict)
    repair: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float | None = None


ClaimVerificationDecision = Literal[
    "skipped",
    "allow",
    "allow_exempt",
    "repair",
    "block",
    "would_allow",
    "would_allow_exempt",
    "would_repair",
    "would_block",
]


class ClaimVerificationRolloutSummary(ApiModel):
    version: Literal["v1"] = "v1"
    mode: Literal["off", "shadow", "enforce"] = "off"
    configured_mode: Literal["off", "shadow", "enforce"] = "off"
    rollout_percent: float = Field(default=100.0, ge=0.0, le=100.0)
    cohort_bucket: int = Field(default=0, ge=0, le=9999)
    cohort_selected: bool = True
    fallback_mode: Literal["off", "shadow"] = "off"
    policy_id: str = Field(default="", pattern=r"^(?:[0-9a-f]{16})?$")
    decision: ClaimVerificationDecision = "skipped"
    executed: bool = False
    enforced: bool = False
    released: bool = True
    would_intervene: bool = False
    would_repair: bool = False
    would_block: bool = False
    audit_status: str = Field(default="not_run", max_length=32)
    reason_code: str = Field(default="", max_length=128)
    repair_count: int = Field(default=0, ge=0)


class ClaimVerificationOperationalReadiness(ApiModel):
    ready: bool = False
    sample_count: int = Field(default=0, ge=0)
    minimum_samples: int = Field(default=0, ge=1)
    verifier_error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    maximum_verifier_error_rate: float = Field(default=0.02, ge=0.0, le=1.0)
    blockers: list[Literal["minimum_samples", "verifier_error_rate"]] = Field(
        default_factory=list
    )
    semantic_release_gate_required: Literal[True] = True


class ClaimVerificationObservationSummaryResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    tenant_id: str
    window_hours: int = Field(ge=1, le=720)
    window_start: str
    generated_at: str
    effective_mode_filter: Literal["off", "shadow", "enforce"] | None = None
    policy_id_filter: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    total_count: int = Field(default=0, ge=0)
    counts: dict[str, int] = Field(default_factory=dict)
    rates: dict[str, float | None] = Field(default_factory=dict)
    by_configured_mode: dict[str, int] = Field(default_factory=dict)
    by_effective_mode: dict[str, int] = Field(default_factory=dict)
    by_decision: dict[str, int] = Field(default_factory=dict)
    by_task_type: dict[str, int] = Field(default_factory=dict)
    operational_readiness: ClaimVerificationOperationalReadiness


ClaimVerdict = Literal["supported", "unsupported", "insufficient", "not_factual"]


class ClaimVerificationReviewEvidence(ApiModel):
    chunk_id: str = Field(min_length=1, max_length=256)
    source: str = Field(default="", max_length=512)
    page: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    text: str = Field(default="", max_length=8000)
    text_truncated: bool = False


class ClaimVerificationReviewSummary(ApiModel):
    review_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    tenant_id: str
    observed_at: str
    task_type: Literal["qa", "summary", "compare"]
    policy_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    effective_mode: Literal["shadow", "enforce"]
    decision: str = Field(default="", max_length=32)
    claim_id: str = Field(min_length=1, max_length=64)
    claim: str = Field(min_length=1, max_length=8000)
    actual_verdict: ClaimVerdict
    reason: str = Field(default="", max_length=1000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    duration_ms: float | None = Field(default=None, ge=0.0)
    evidence_complete: bool
    evidence_count: int = Field(default=0, ge=0, le=12)
    status: Literal["pending", "reviewed"]
    expected_verdict: ClaimVerdict | None = None
    reviewer: str = Field(default="", max_length=160)
    review_note: str = Field(default="", max_length=2000)
    reviewed_at: str | None = None
    revision: int = Field(ge=1)


class ClaimVerificationReviewDetail(ClaimVerificationReviewSummary):
    cited_chunk_ids: list[str] = Field(default_factory=list, max_length=12)
    supporting_chunk_ids: list[str] = Field(default_factory=list, max_length=12)
    evidence: list[ClaimVerificationReviewEvidence] = Field(
        default_factory=list, max_length=12
    )


class ClaimVerificationReviewListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    tenant_id: str
    items: list[ClaimVerificationReviewSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class ClaimVerificationReviewVerdictCounts(ApiModel):
    supported: int = Field(default=0, ge=0)
    unsupported: int = Field(default=0, ge=0)
    insufficient: int = Field(default=0, ge=0)
    not_factual: int = Field(default=0, ge=0)


class ClaimVerificationReviewSummaryResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    tenant_id: str
    total_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    reviewed_count: int = Field(ge=0)
    shadow_count: int = Field(ge=0)
    enforce_count: int = Field(ge=0)
    evidence_incomplete_count: int = Field(ge=0)
    agreement_count: int = Field(ge=0)
    disagreement_count: int = Field(ge=0)
    agreement_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    oldest_pending_at: str | None = None
    actual_verdict_counts: ClaimVerificationReviewVerdictCounts
    expected_verdict_counts: ClaimVerificationReviewVerdictCounts


class ClaimVerificationReviewLabelRequest(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    expected_verdict: ClaimVerdict
    expected_revision: int = Field(ge=1)
    review_note: str = Field(default="", max_length=2000)

    @field_validator("review_note")
    @classmethod
    def _strip_review_note(cls, value: str) -> str:
        return value.strip()


class ClaimVerificationReviewExportItem(ApiModel):
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    layer: Literal["qa", "summary", "compare"]
    claim_id: str
    claim: str
    expected_verdict: ClaimVerdict
    actual_verdict: ClaimVerdict
    duration_ms: float | None = Field(default=None, ge=0.0)
    reviewer: str
    notes: str
    policy_id: str = Field(pattern=r"^[0-9a-f]{16}$")


class ClaimVerificationReviewExportResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    tenant_id: str
    count: int = Field(ge=0)
    items: list[ClaimVerificationReviewExportItem] = Field(default_factory=list)
    next_cursor: str | None = None


# 对话接口结构化响应。
class ChatResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    request_id: str
    trace_id: str
    doc_id: str
    session_id: str | None = None
    task_type: ChatTask
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    citation_ledger: list[CitationLedgerEntry] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    critique: str = ""
    is_valid: bool
    claim_audit: ClaimAuditSummary | None = None
    claim_verification: ClaimVerificationRolloutSummary | None = None


# 独立任务接口请求体，摘要和对比任务由路由层固定。
class TaskRequest(QueryDocRequest):
    session_id: str | None = None
    is_local: bool = False


# 检索接口请求体，不调用模型。
class RetrieveRequest(QueryDocRequest):
    top_k: int = Field(default=8, ge=1, le=50)
    rerank: bool = False
    rerank_top_n: int | None = Field(default=None, ge=1, le=50)


# 检索命中项，供前端证据面板和调试面板直接消费。
class RetrieveHit(Evidence):
    rank: int


# 检索接口结构化响应。
class RetrieveResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    doc_id: str
    query: str
    top_k: int
    rerank: bool
    hits: list[RetrieveHit] = Field(default_factory=list)


# 统一错误响应体，所有失败路径共用。
class ErrorResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    error_code: ErrorCode
    message: str
    request_id: str | None = None
    trace_id: str | None = None
    details: dict[str, Any] | None = None


# 入库任务状态机。
class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# 建知识库请求体，限制长度避免集合名截断后碰撞。
class KnowledgeBaseCreate(ApiModel):
    kb_id: str = Field(min_length=1, max_length=56)
    access_policy: Literal["workspace", "private"] = "workspace"

    # 校验结果。
    @field_validator("kb_id")
    @classmethod
    def _slug(cls, value: str) -> str:
        # 标识符会进入路径，禁止分隔符与空白，避免目录穿越。
        stripped = value.strip()
        if (
            not stripped
            or any(c in stripped for c in "/\\ \t")
            or stripped in {".", ".."}
        ):
            raise ValueError("kb_id 只能是不含路径分隔符与空白的标识符")
        return stripped


# 知识库元数据，预留多租户字段。
class KnowledgeBase(ApiModel):
    kb_id: str
    created_at: str
    document_count: int = 0
    tenant_id: str = "default"
    owner_id: str = "default"


class ResearchJobCreate(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    kb_id: str = Field(min_length=1, max_length=56)
    objective: str = Field(min_length=1, max_length=4000)
    title: str = Field(default="", max_length=160)
    section_titles: list[str] = Field(default_factory=list, max_length=12)
    is_local: bool = False

    @field_validator("kb_id", "objective", "title")
    @classmethod
    def _strip_research_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("kb_id")
    @classmethod
    def _require_research_kb_id(cls, value: str) -> str:
        if not value:
            raise ValueError("kb_id must not be blank")
        return value

    @field_validator("objective")
    @classmethod
    def _require_objective(cls, value: str) -> str:
        if not value:
            raise ValueError("objective must not be blank")
        return value

    @field_validator("section_titles")
    @classmethod
    def _normalize_section_titles(cls, values: list[str]) -> list[str]:
        normalized = [" ".join(value.split()) for value in values]
        if any(not value for value in normalized):
            raise ValueError("section titles must not be blank")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("section titles must be unique")
        return normalized


class ResearchPlanSectionInput(ApiModel):
    title: str = Field(min_length=1, max_length=160)
    research_question: str = Field(min_length=1, max_length=2000)
    evidence_requirements: list["ResearchEvidenceRequirementInput"] = Field(
        min_length=1,
        max_length=3,
    )
    success_criteria: str = Field(default="", max_length=1000)

    @field_validator("title", "research_question")
    @classmethod
    def _strip_required_plan_text(cls, value: str) -> str:
        stripped = " ".join(value.split())
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("success_criteria")
    @classmethod
    def _strip_optional_plan_text(cls, value: str) -> str:
        return " ".join(value.split())

    @model_validator(mode="after")
    def _unique_requirement_questions(self):
        keys = [
            _research_contract_key(requirement.question)
            for requirement in self.evidence_requirements
        ]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "research evidence requirement questions must be unique per section"
            )
        return self


class ResearchEvidenceRequirementInput(ApiModel):
    question: str = Field(min_length=1, max_length=1000)
    retrieval_query: str = Field(min_length=1, max_length=1000)
    recovery_query: str = Field(min_length=1, max_length=1000)

    @field_validator("question", "retrieval_query", "recovery_query")
    @classmethod
    def _strip_requirement_text(cls, value: str) -> str:
        return " ".join(value.split())

    @model_validator(mode="after")
    def _require_distinct_queries(self):
        if _research_contract_key(self.retrieval_query) == _research_contract_key(
            self.recovery_query
        ):
            raise ValueError("retrieval_query and recovery_query must be distinct")
        return self


class ResearchPlanUpdate(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    expected_revision: int = Field(ge=1, strict=True)
    sections: list[ResearchPlanSectionInput] = Field(min_length=1, max_length=12)


class ResearchPlanGenerateRequest(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    expected_revision: int = Field(ge=1, strict=True)
    is_local: bool | None = None


class ResearchReviewDecision(ApiModel):
    section_id: str = Field(min_length=1, max_length=64)
    decision: Literal["approved", "accepted_gap", "changes_requested"]
    note: str = Field(default="", max_length=2000)

    @field_validator("section_id", "note")
    @classmethod
    def _strip_review_text(cls, value: str) -> str:
        return " ".join(value.split())

    @model_validator(mode="after")
    def _require_decision_note(self):
        if self.decision in {"changes_requested", "accepted_gap"} and not self.note:
            raise ValueError(f"{self.decision} review requires a non-blank note")
        return self


class ResearchReportReviewUpdate(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    expected_revision: int = Field(ge=1, strict=True)
    decisions: list[ResearchReviewDecision] = Field(min_length=1, max_length=12)


class ResearchReportPublishRequest(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    expected_revision: int = Field(ge=1, strict=True)


class _ResearchAuditPublicModel(BaseModel):
    """Ignore unknown verifier detail so claim text can never enter API output."""

    model_config = ConfigDict(extra="ignore")


class ResearchClaimAuditCounts(_ResearchAuditPublicModel):
    claim_count: int = Field(default=0, ge=0)
    supported: int = Field(default=0, ge=0)
    unsupported: int = Field(default=0, ge=0)
    insufficient: int = Field(default=0, ge=0)
    cited: int = Field(default=0, ge=0)
    skipped_statements: int = Field(default=0, ge=0)


class ResearchClaimAuditMetrics(_ResearchAuditPublicModel):
    claim_support_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    citation_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    unsupported_claim_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class ResearchClaimAuditRepair(_ResearchAuditPublicModel):
    attempted: bool = False
    attempt_count: int = Field(default=0, ge=0, le=1)
    succeeded: bool = False
    error: str = Field(default="", max_length=64)


class ResearchClaimAuditVerifier(_ResearchAuditPublicModel):
    duration_ms: float = Field(default=0.0, ge=0.0, le=3_600_000.0)
    call_count: int = Field(default=0, ge=0)
    version: str = Field(default="v1", max_length=16)


class ResearchClaimAuditSummary(_ResearchAuditPublicModel):
    status: str = Field(default="not_run", max_length=32)
    reason_code: str = Field(default="", max_length=128)
    counts: ResearchClaimAuditCounts = Field(default_factory=ResearchClaimAuditCounts)
    metrics: ResearchClaimAuditMetrics = Field(
        default_factory=ResearchClaimAuditMetrics
    )
    repair: ResearchClaimAuditRepair = Field(default_factory=ResearchClaimAuditRepair)
    verifier: ResearchClaimAuditVerifier = Field(
        default_factory=ResearchClaimAuditVerifier
    )


class ResearchCoverageAuditAuditor(_ResearchAuditPublicModel):
    call_count: int = Field(default=0, ge=0, le=2)
    version: str = Field(default="v1", max_length=16)


class ResearchCoverageAuditSummary(_ResearchAuditPublicModel):
    """Bounded public obligation coverage result; never exposes claim prose."""

    status: str = Field(default="not_run", max_length=32)
    reason_code: str = Field(default="", max_length=128)
    requirement_count: int = Field(default=0, ge=0, le=16)
    covered_count: int = Field(default=0, ge=0, le=16)
    missing_requirement_ids: list[str] = Field(default_factory=list, max_length=16)
    repair: ResearchClaimAuditRepair = Field(default_factory=ResearchClaimAuditRepair)
    auditor: ResearchCoverageAuditAuditor = Field(
        default_factory=ResearchCoverageAuditAuditor
    )


class ResearchPlanSection(ApiModel):
    section_id: str
    position: int = Field(ge=1, strict=True)
    title: str
    research_question: str
    evidence_requirements: list["ResearchEvidenceRequirement"] = Field(
        default_factory=list
    )
    success_criteria: str = ""
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    evidence_status: Literal[
        "unsearched", "missing", "partial", "supported", "contradictory"
    ] = "unsearched"
    evidence_requirement_ids: list[str] = Field(default_factory=list)
    evidence_requirement_results: list["ResearchVerificationRequirementResult"] = Field(
        default_factory=list
    )
    evidence: list["ResearchEvidenceItem"] = Field(default_factory=list)
    execution_metrics: dict[str, Any] = Field(default_factory=dict)
    citation_ledger: list[CitationLedgerEntry] = Field(default_factory=list)
    claim_audit: ResearchClaimAuditSummary | None = None
    coverage_audit: ResearchCoverageAuditSummary | None = None
    revision_instruction: str = ""
    verification_status: str = ""
    verification_reason_code: str = ""
    generation_status: str = ""
    content: str = ""
    review_status: Literal[
        "not_started",
        "pending",
        "approved",
        "accepted_gap",
        "changes_requested",
    ] = "not_started"
    review_note: str = ""
    reviewed_at: str | None = None
    error: str = ""

    @field_validator("claim_audit", mode="before")
    @classmethod
    def _empty_claim_audit_is_not_run(cls, value):
        return value or None

    @field_validator("coverage_audit", mode="before")
    @classmethod
    def _empty_coverage_audit_is_not_run(cls, value):
        return value or None


class ResearchEvidenceRequirement(ApiModel):
    requirement_id: str
    question: str
    retrieval_query: str
    recovery_query: str


class ResearchEvidenceItem(ApiModel):
    chunk_id: str = ""
    source_type: str = "document"
    knowledge_id: str = ""
    source: str = ""
    source_id: str = Field(default="", exclude_if=lambda value: not value)
    source_version_id: str = Field(default="", exclude_if=lambda value: not value)
    media_type: str = Field(default="", exclude_if=lambda value: not value)
    location: dict[str, Any] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    source_sha256: str = ""
    text_hash: str = ""
    page: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    span_start: int | None = Field(default=None, ge=0, strict=True)
    span_end: int | None = Field(default=None, ge=0, strict=True)
    section_title: str = ""
    text_preview: str = ""
    search_channel: str = ""
    rerank_score: float | int | None = None
    rrf_score: float | int | None = None


class ResearchSourceVersion(ApiModel):
    source: str
    sha256: str


class ResearchProvenanceSnapshot(ApiModel):
    schema_version: Literal["research-provenance-v1"] = "research-provenance-v1"
    kb_id: str = ""
    kb_epoch: int | None = Field(
        default=None, ge=0, strict=True, exclude_if=lambda value: value is None
    )
    acl_epoch: int | None = Field(
        default=None, ge=0, strict=True, exclude_if=lambda value: value is None
    )
    index_generation: str = ""
    index_build_version: str = ""
    chunk_identity_version: str = ""
    source_versions: list[ResearchSourceVersion] = Field(default_factory=list)
    derived_knowledge_revision: str = ""
    retrieval_tuning_revision: str = ""
    research_contract_version: str = ""
    research_contract_revision: str = ""
    captured_at: str = ""


class ResearchVerificationRequirementResult(ApiModel):
    requirement_id: str
    status: str = ""
    reason_code: str = ""
    evidence_count: int = Field(default=0, ge=0, strict=True)


class ResearchVerificationRequirementPlan(ApiModel):
    requirement_id: str
    question: str
    retrieval_query: str
    recovery_query: str


class ResearchVerificationNode(ApiModel):
    node: str
    backend: Literal["local", "cloud"]
    model: str
    protocol_version: str


class ResearchVerificationExecution(ApiModel):
    job_id: str
    kb_id: str
    execution_id: str
    report_execution_id: str
    title: str
    objective: str
    is_local: bool
    nodes: list[ResearchVerificationNode] = Field(default_factory=list)


class ResearchEvidenceCommitment(ApiModel):
    chunk_id: str = ""
    source_type: str = "document"
    knowledge_id: str = ""
    source: str = ""
    source_sha256: str = ""
    text_hash: str = ""
    page: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    span_start: int | None = Field(default=None, ge=0, strict=True)
    span_end: int | None = Field(default=None, ge=0, strict=True)
    section_title: str = ""
    search_channel: str = ""
    rerank_score: float | int | None = None
    rrf_score: float | int | None = None


class ResearchVerificationSection(ApiModel):
    section_id: str
    position: int = Field(ge=1, strict=True)
    title: str
    research_question: str
    success_criteria: str = ""
    revision_instruction: str = ""
    requirements: list[ResearchVerificationRequirementPlan] = Field(
        default_factory=list
    )
    generation_status: str = ""
    verification_status: str = ""
    verification_reason_code: str = ""
    requirement_results: list[ResearchVerificationRequirementResult] = Field(
        default_factory=list
    )
    claim_audit: ResearchClaimAuditSummary
    coverage_audit: ResearchCoverageAuditSummary
    evidence_commitments: list[ResearchEvidenceCommitment] = Field(default_factory=list)


class ResearchVerificationSnapshot(ApiModel):
    schema_version: Literal["research-verification-v2"] = "research-verification-v2"
    execution: ResearchVerificationExecution
    aggregate: dict[str, Any] = Field(default_factory=dict)
    sections: list[ResearchVerificationSection] = Field(default_factory=list)


class ResearchReportArtifact(ApiModel):
    artifact_schema_version: str = ""
    format: Literal["markdown"] = "markdown"
    content: str
    citation_ledger: list[CitationLedgerEntry] = Field(default_factory=list)
    verification_metrics: dict[str, Any] = Field(default_factory=dict)
    verification: ResearchVerificationSnapshot | None = None
    provenance: ResearchProvenanceSnapshot | None = None
    sha256: str = ""
    version: int = Field(default=1, ge=1, strict=True)
    generated_at: str
    published_at: str | None = None
    published_by: str = ""
    publication_sha256: str = ""

    @field_validator("provenance", mode="before")
    @classmethod
    def _empty_provenance_is_untracked(cls, value):
        return value or None

    @field_validator("verification", mode="before")
    @classmethod
    def _empty_verification_is_untracked(cls, value):
        return value or None


class ResearchReportVersion(ApiModel):
    version: int = Field(ge=1, strict=True)
    report_status: str
    review_status: str
    archived_at: str
    report: ResearchReportArtifact


class ResearchJob(ApiModel):
    job_id: str
    kb_id: str
    title: str
    objective: str
    is_local: bool = False
    artifact_schema_floor: str = ""
    status: Literal[
        "planned",
        "running",
        "paused",
        "evidence_ready",
        "generating",
        "completed",
        "failed",
        "cancelled",
    ]
    revision: int = Field(ge=1, strict=True)
    created_at: str
    updated_at: str
    sections: list[ResearchPlanSection]
    execution_id: str = ""
    started_at: str | None = None
    evidence_completed_at: str | None = None
    report_status: Literal[
        "not_started",
        "generating",
        "ready",
        "ready_with_gaps",
        "published",
        "failed",
    ] = "not_started"
    report_execution_id: str = ""
    report_execution_nodes: list[ResearchVerificationNode] = Field(default_factory=list)
    report_completed_at: str | None = None
    report: ResearchReportArtifact | None = None
    report_version: int = Field(default=0, ge=0, strict=True)
    report_history: list[ResearchReportVersion] = Field(default_factory=list)
    review_status: Literal[
        "not_started", "pending", "approved", "changes_requested", "published"
    ] = "not_started"
    review_history: list[dict[str, Any]] = Field(default_factory=list)
    published_report: ResearchReportArtifact | None = None
    published_at: str | None = None
    published_by: str = ""
    publication_sha256: str = ""
    regeneration_section_ids: list[str] = Field(default_factory=list)
    last_regenerated_section_ids: list[str] = Field(default_factory=list)
    evidence_provenance: ResearchProvenanceSnapshot | None = None
    execution_control: "ResearchExecutionControlSummary" = Field(
        default_factory=lambda: ResearchExecutionControlSummary()
    )
    provenance_status: Literal["untracked", "current", "stale"] = "untracked"
    provenance_stale_reasons: list[str] = Field(default_factory=list)
    error: str = ""

    @field_validator("evidence_provenance", mode="before")
    @classmethod
    def _empty_evidence_provenance_is_untracked(cls, value):
        return value or None


class ResearchJobSectionCounts(ApiModel):
    total: int = Field(ge=0, strict=True)
    pending: int = Field(ge=0, strict=True)
    running: int = Field(ge=0, strict=True)
    completed: int = Field(ge=0, strict=True)
    failed: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _counts_cover_every_section(self):
        if self.pending + self.running + self.completed + self.failed != self.total:
            raise ValueError("research section counts must add up to total")
        return self


class ResearchJobSummary(ApiModel):
    """Bounded collection projection; it never embeds evidence or report bodies."""

    job_id: str = Field(min_length=1, max_length=128, strict=True)
    kb_id: str = Field(min_length=1, max_length=128, strict=True)
    title: str = Field(min_length=1, max_length=160, strict=True)
    objective_preview: str = Field(min_length=1, max_length=240, strict=True)
    is_local: bool = Field(default=False, strict=True)
    status: Literal[
        "planned",
        "running",
        "paused",
        "evidence_ready",
        "generating",
        "completed",
        "failed",
        "cancelled",
    ]
    revision: int = Field(ge=1, strict=True)
    created_at: str = Field(min_length=1, max_length=128, strict=True)
    updated_at: str = Field(min_length=1, max_length=128, strict=True)
    section_counts: ResearchJobSectionCounts
    report_status: Literal[
        "not_started",
        "generating",
        "ready",
        "ready_with_gaps",
        "published",
        "failed",
    ] = "not_started"
    report_version: int = Field(default=0, ge=0, strict=True)
    review_status: Literal[
        "not_started", "pending", "approved", "changes_requested", "published"
    ] = "not_started"
    provenance_status: Literal["untracked", "current", "stale"] = "untracked"
    provenance_stale_reasons: list[str] = Field(default_factory=list, max_length=16)
    report_history_count: int = Field(default=0, ge=0, strict=True)
    has_report: bool = Field(default=False, strict=True)
    has_published_report: bool = Field(default=False, strict=True)
    report_size_bytes: int = Field(default=0, ge=0, strict=True)
    published_at: str | None = Field(default=None, max_length=128, strict=True)
    error: str = Field(default="", max_length=256, strict=True)

    @field_validator("provenance_stale_reasons")
    @classmethod
    def _bounded_provenance_reasons(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 256 for value in values):
            raise ValueError(
                "research provenance stale reasons must be non-blank bounded strings"
            )
        return values

    @model_validator(mode="after")
    def _artifact_hints_are_consistent(self):
        if not self.has_report and self.report_size_bytes:
            raise ValueError("research report size requires an available report")
        if self.has_published_report and self.published_at is None:
            raise ValueError("published research report requires published_at")
        return self


class ResearchJobSummaryPage(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    jobs: list[ResearchJobSummary] = Field(default_factory=list, max_length=100)
    next_cursor: str | None = Field(default=None, max_length=1024, strict=True)
    has_more: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def _cursor_matches_page_state(self):
        if self.has_more != bool(self.next_cursor):
            raise ValueError("research summary next_cursor must match has_more")
        return self


class ResearchResourceBudget(ApiModel):
    retrieval_queries: int = Field(default=0, ge=0, strict=True)
    candidate_docs: int = Field(default=0, ge=0, strict=True)
    llm_calls: int = Field(default=0, ge=0, strict=True)
    model_input_chars: int = Field(default=0, ge=0, strict=True)


class ResearchRunControlSummary(ApiModel):
    phase: Literal["evidence", "report"]
    attempt_id: str = Field(default="", max_length=128, strict=True)
    control_state: Literal[
        "running",
        "paused",
        "cancelled",
        "expired",
        "budget_exhausted",
        "failed",
        "completed",
    ]
    deadline_at: str = Field(default="", max_length=128, strict=True)
    limits: ResearchResourceBudget = Field(default_factory=ResearchResourceBudget)
    used: ResearchResourceBudget = Field(default_factory=ResearchResourceBudget)
    started_at: str = Field(default="", max_length=128, strict=True)
    heartbeat_at: str = Field(default="", max_length=128, strict=True)
    finished_at: str | None = Field(default=None, max_length=128, strict=True)
    terminal_reason: str = Field(default="", max_length=128, strict=True)


class ResearchExecutionControlSummary(ApiModel):
    evidence: ResearchRunControlSummary | None = None
    report: ResearchRunControlSummary | None = None


class ResearchJobResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    job: ResearchJob


class ResearchJobListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    jobs: list[ResearchJob] = Field(default_factory=list)


class ResearchProvenanceResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    job_id: str
    status: Literal["untracked", "current", "stale"]
    stale_reasons: list[str] = Field(default_factory=list)
    captured: ResearchProvenanceSnapshot | None = None
    current: ResearchProvenanceSnapshot | None = None


# 知识库内的一篇文档，来自清单。
class Document(ApiModel):
    name: str
    sha256: str = ""
    document_id: str = ""
    source_id: str = ""
    version_id: str = ""
    connector_type: str = "legacy-upload"
    media_type: str = "application/pdf"
    kind: str = "file"
    origin_uri: str | None = None


# 知识库来源文件列表响应。
class SourceListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    kb_id: str
    sources: list[str] = Field(default_factory=list)


# 文档分块预览，不返回完整正文。
class ChunkPreview(ApiModel):
    chunk_id: str = ""
    document_id: str = ""
    chunk_index: int | None = None
    source: str = ""
    source_sha256: str = ""
    page: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    text_hash: str = ""
    anchor_hit: bool = False
    text_preview: str = ""
    context_preview: str = ""


# 单个来源文件的分块预览响应。
class SourceChunksResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    kb_id: str
    source: str
    total_count: int
    offset: int
    limit: int
    chunks: list[ChunkPreview] = Field(default_factory=list)


# OCR 入库汇总，不包含页面正文或错误详情。
class OcrSummary(ApiModel):
    candidate_pages: int = 0
    attempted_pages: int = 0
    succeeded_pages: int = 0
    degraded_pages: int = 0
    failed_pages: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)


# 后台入库任务记录，供轮询状态。
class IndexJob(ApiModel):
    job_id: str
    kb_id: str
    status: JobStatus
    created_at: str
    finished_at: str | None = None
    document_count: int | None = None
    chunk_count: int | None = None
    ocr_summary: OcrSummary | None = None
    error_code: ErrorCode | None = None
    message: str | None = None


# 反馈类型：赞 / 踩 / 纠错。
class FeedbackType(str, Enum):
    THUMBS_UP = "thumbs_up"
    THUMBS_DOWN = "thumbs_down"
    CORRECTION = "correction"


# 回答反馈原因，用于后续结构化分析和调权。
class FeedbackIssueType(str, Enum):
    NO_EVIDENCE = "no_evidence"
    WRONG_ANSWER = "wrong_answer"
    BAD_RETRIEVAL = "bad_retrieval"
    CORRECTION = "correction"
    OTHER = "other"


# 被反馈来源类型。
class FeedbackSourceType(str, Enum):
    DOCUMENT = "document"
    DERIVED_KNOWLEDGE = "derived_knowledge"
    MIXED = "mixed"
    NONE = "none"


# 反馈请求体，跟踪标识关联被反馈的那次回答。
class FeedbackRequest(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    trace_id: str = Field(min_length=1)
    feedback: FeedbackType
    kb_id: str | None = None
    session_id: str | None = None
    query: str | None = None
    answer: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    source_type: FeedbackSourceType | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    feedback_type: FeedbackIssueType | None = None
    feedback_text: str | None = None
    correction_text: str | None = None
    comment: str | None = None
    correction: str | None = None
    save_as_knowledge: bool = False
    skip_retrieval_feedback: bool = False
    related_document_id: str | None = None
    related_source: str | None = None
    related_source_sha256: str | None = None
    related_chunk_ids: list[str] = Field(default_factory=list)
    related_page_start: int | None = Field(default=None, ge=0)
    related_page_end: int | None = Field(default=None, ge=0)
    related_chunk_text_hash: str | None = None
    related_anchor_text: str | None = None
    source_note: str | None = None
    certainty: Literal["high", "medium", "low"] = "medium"
    created_by: str | None = None


# 反馈落盘结果，标记是否进入坏样本集。
class FeedbackResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    feedback_id: str
    status: str = "recorded"
    is_bad_case: bool
    feedback_analysis_id: str | None = None
    feedback_analysis_action: str | None = None
    feedback_analysis_confidence: float | None = None
    knowledge_id: str | None = None
    knowledge_status: str | None = None
    knowledge_deduplicated: bool = False
    retrieval_eval_draft_id: str | None = None
    retrieval_eval_draft_status: str | None = None


# 证据评测草稿的人工审核请求。reviewer 身份只能来自服务端审核密钥，
# 不接受客户端自报 actor。
class RetrievalEvalDraftReviewRequest(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    decision: Literal["approved", "rejected"]
    expected_revision: int = Field(ge=1, strict=True)
    annotations: dict[str, Any] | None = None
    reason: str = ""


class RetrievalDiagnosticRequirement(ApiModel):
    requirement_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=2000)
    retrieval_query: str = Field(default="", max_length=2000)
    recovery_query: str = Field(default="", max_length=2000)


class RetrievalDiagnosticRequest(QueryDocRequest):
    top_k: int = Field(default=12, ge=1, le=50)
    rerank: bool = True
    rerank_top_n: int | None = Field(default=None, ge=1, le=50)
    route_weights: dict[str, float] | None = None
    route_min_candidates: int = Field(default=1, ge=0, le=10)
    requirements: list[RetrievalDiagnosticRequirement] = Field(
        default_factory=list, max_length=10
    )

    @field_validator("route_weights")
    @classmethod
    def _validate_route_weights(
        cls, value: dict[str, float] | None
    ) -> dict[str, float] | None:
        allowed = {
            "rag_vector",
            "rag_bm25",
            "derived_knowledge_vector",
            "derived_knowledge_lexical",
        }
        if value is None:
            return None
        if set(value) - allowed:
            raise ValueError("route_weights contains an unknown route")
        if any(
            not math.isfinite(weight) or weight < 0 or weight > 5
            for weight in value.values()
        ):
            raise ValueError("route weights must be finite numbers between 0 and 5")
        return value


class RetrievalDiagnosticEvidence(ApiModel):
    chunk_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_sha256: str = Field(min_length=1)
    parent_chunk_id: str = ""


class RetrievalDiagnosticLabelRequest(QueryDocRequest):
    requirement_id: str = Field(default="r1", min_length=1, max_length=100)
    requirement_label: str = Field(default="", max_length=2000)
    no_answer: bool = False
    acceptable_evidence: list[RetrievalDiagnosticEvidence] = Field(
        default_factory=list, max_length=50
    )
    hard_negative_evidence: list[RetrievalDiagnosticEvidence] = Field(
        default_factory=list, max_length=50
    )

    @model_validator(mode="after")
    def _validate_diagnostic_label(self):
        if self.no_answer and self.acceptable_evidence:
            raise ValueError("no-answer labels cannot include acceptable evidence")
        if not self.no_answer and not self.acceptable_evidence:
            raise ValueError("supported labels require acceptable evidence")
        return self


class IndexMigrationRequest(ApiModel):
    kb_ids: list[str] = Field(default_factory=list, max_length=500)
    include_current: bool = False

    @field_validator("kb_ids")
    @classmethod
    def _normalize_migration_kb_ids(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            item = value.strip()
            if not item:
                raise ValueError("kb_ids cannot contain blank values")
            if item not in normalized:
                normalized.append(item)
        return normalized


class IndexMigrationRollbackRequest(ApiModel):
    kb_ids: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("kb_ids")
    @classmethod
    def _normalize_rollback_kb_ids(cls, values: list[str]) -> list[str]:
        return IndexMigrationRequest._normalize_migration_kb_ids(values)


# 反馈列表响应。
class FeedbackListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    feedback: list[dict[str, Any]] = Field(default_factory=list)


# 派生知识状态。
class KnowledgeStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    STALE = "stale"
    ARCHIVED = "archived"


# 派生知识来源。
class KnowledgeOrigin(str, Enum):
    MANUAL_ENTRY = "manual_entry"
    CORRECTION = "correction"
    NO_EVIDENCE = "no_evidence"
    SAVED_ANSWER = "saved_answer"
    AGENT_SUGGESTED = "agent_suggested"


# 派生知识可信度。
class KnowledgeCertainty(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# 主动新增知识请求体。
class KnowledgeCreateRequest(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    kb_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    related_document_id: str | None = None
    related_source: str | None = None
    related_source_sha256: str | None = None
    related_chunk_ids: list[str] = Field(default_factory=list)
    related_page_start: int | None = Field(default=None, ge=0)
    related_page_end: int | None = Field(default=None, ge=0)
    related_chunk_text_hash: str | None = None
    related_anchor_text: str | None = None
    source_note: str | None = None
    certainty: KnowledgeCertainty = KnowledgeCertainty.MEDIUM
    origin: KnowledgeOrigin = KnowledgeOrigin.MANUAL_ENTRY
    created_from_trace_id: str | None = None
    created_by: str | None = None

    # 清理必填文本。
    @field_validator("kb_id", "text")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


# 审核操作请求体。
class KnowledgeReviewRequest(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    actor: str | None = None
    note: str | None = None
    related_document_id: str | None = None
    related_source: str | None = None
    related_source_sha256: str | None = None
    related_chunk_ids: list[str] | None = None
    related_page_start: int | None = Field(default=None, ge=0)
    related_page_end: int | None = Field(default=None, ge=0)
    related_chunk_text_hash: str | None = None
    related_anchor_text: str | None = None


# 知识修订请求体。
class KnowledgeReviseRequest(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    text: str = Field(min_length=1)
    related_document_id: str | None = None
    related_source: str | None = None
    related_source_sha256: str | None = None
    related_chunk_ids: list[str] | None = None
    related_page_start: int | None = Field(default=None, ge=0)
    related_page_end: int | None = Field(default=None, ge=0)
    related_chunk_text_hash: str | None = None
    related_anchor_text: str | None = None
    source_note: str | None = None
    certainty: KnowledgeCertainty = KnowledgeCertainty.MEDIUM
    created_from_trace_id: str | None = None
    created_by: str | None = None

    # 清理修订正文。
    @field_validator("text")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


# 批量审核请求体。
class KnowledgeBatchReviewRequest(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    actor: str | None = None
    note: str | None = None
    knowledge_ids: list[str] = Field(min_length=1)


# 派生知识公开视图。
class DerivedKnowledge(ApiModel):
    knowledge_id: str
    kb_id: str
    text: str
    normalized_text: str
    normalized_hash: str
    version: int = 1
    previous_version_id: str | None = None
    conflict_group_id: str | None = None
    related_document_id: str | None = None
    related_source: str | None = None
    related_source_sha256: str | None = None
    related_chunk_ids: list[str] = Field(default_factory=list)
    related_page_start: int | None = None
    related_page_end: int | None = None
    related_chunk_text_hash: str | None = None
    related_anchor_text: str | None = None
    source_note: str | None = None
    certainty: KnowledgeCertainty = KnowledgeCertainty.MEDIUM
    status: KnowledgeStatus = KnowledgeStatus.PENDING
    origin: KnowledgeOrigin = KnowledgeOrigin.MANUAL_ENTRY
    created_from_trace_id: str | None = None
    created_by: str | None = None
    created_at: str
    updated_at: str
    archived_at: str | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None


# 知识冲突候选。
class KnowledgeConflictCandidate(ApiModel):
    knowledge_id: str
    text: str
    status: KnowledgeStatus
    origin: KnowledgeOrigin = KnowledgeOrigin.MANUAL_ENTRY
    related_source: str | None = None
    created_at: str


# 新增知识响应。
class KnowledgeCreateResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    knowledge: DerivedKnowledge
    deduplicated: bool = False
    requires_review: bool = False
    conflicts: list[KnowledgeConflictCandidate] = Field(default_factory=list)


# 知识列表响应。
class KnowledgeListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    knowledge: list[DerivedKnowledge] = Field(default_factory=list)


# 批量审核响应。
class KnowledgeBatchReviewResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    updated: list[DerivedKnowledge] = Field(default_factory=list)
    missing_ids: list[str] = Field(default_factory=list)


# 过期知识扫描响应。
class KnowledgeStaleScanResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    kb_id: str
    stale_marked: int = 0
    stale_knowledge: list[DerivedKnowledge] = Field(default_factory=list)


# 审核队列摘要响应。
class ReviewQueueSummaryResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    kb_id: str
    knowledge: dict[str, int] = Field(default_factory=dict)
    knowledge_origin: dict[str, int] = Field(default_factory=dict)
    knowledge_conflicts: dict[str, int] = Field(default_factory=dict)
    knowledge_auto_review: dict[str, int] = Field(default_factory=dict)
    feedback_counts: dict[str, Any] = Field(default_factory=dict)
    feedback_analysis: dict[str, int] = Field(default_factory=dict)
    feedback_analysis_type: dict[str, int] = Field(default_factory=dict)
    retrieval_feedback: dict[str, int] = Field(default_factory=dict)


# 审核队列导出响应。
class ReviewQueueExportResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    kb_id: str
    generated_at: str
    summary: ReviewQueueSummaryResponse
    pending_knowledge: list[DerivedKnowledge] = Field(default_factory=list)
    stale_knowledge: list[DerivedKnowledge] = Field(default_factory=list)
    auto_review_events: list[dict[str, Any]] = Field(default_factory=list)
    feedback_analysis_needs_review: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_feedback_enabled: list[dict[str, Any]] = Field(default_factory=list)
    feedback_bad_cases: list[dict[str, Any]] = Field(default_factory=list)


# 待审核计数响应。
class KnowledgePendingCountResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    kb_id: str
    pending: int = 0
    stale: int = 0
    feedback_analysis_needs_review: int = 0
    total: int = 0


# 反馈闭环指标响应。
class FeedbackLoopMetricsResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    kb_id: str
    counts: dict[str, int] = Field(default_factory=dict)
    rates: dict[str, float | None] = Field(default_factory=dict)


# 会话多轮历史，刷新后前端据此还原聊天记录。
class SessionHistoryResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    session_id: str
    doc_id: str
    messages: list[dict[str, Any]] = Field(default_factory=list)


# 三层记忆调试响应。
class MemorySnapshotResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    session_id: str
    doc_id: str
    short_term: list[dict[str, Any]] = Field(default_factory=list)
    mid_term: dict[str, Any] = Field(default_factory=dict)
    long_term: list[dict[str, Any]] = Field(default_factory=list)


# 会话列表里的一条，标题取首条用户消息。
class SessionSummary(ApiModel):
    session_id: str
    title: str
    message_count: int


# 某知识库下的全部会话，供前端多对话列表。
class SessionListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    doc_id: str
    sessions: list[SessionSummary] = Field(default_factory=list)


# 跟踪文件步骤摘要。
class TraceSummary(ApiModel):
    step_count: int = 0
    error_count: int = 0
    evidence_ref_count: int = 0
    node_names: list[str] = Field(default_factory=list)
    claim_audit: ClaimAuditSummary | None = None
    claim_verification: ClaimVerificationRolloutSummary | None = None


# 跟踪文件响应体。
class TraceResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    trace_id: str
    request_id: str
    task_type: str
    status: str
    duration_ms: float | None = None
    execution_status: str = "SUCCESS"
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    evidence_completeness: float | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    summary: TraceSummary = Field(default_factory=TraceSummary)
    error: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)


# 跟踪列表项，供调试控制台浏览最近请求。
class TraceListItem(ApiModel):
    trace_id: str
    request_id: str
    query_preview: str = ""
    task_type: str
    status: str
    duration_ms: float | None = None
    modified_at: str
    summary: TraceSummary = Field(default_factory=TraceSummary)


# 最近跟踪文件列表响应。
class TraceListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    traces: list[TraceListItem] = Field(default_factory=list)


# Authentication and workspace contracts deliberately use strict strings.  In
# particular, credentials and identifiers must never be produced by coercing a
# JSON number or boolean into text.
WorkspaceRole = Literal["owner", "admin", "editor", "reviewer", "viewer"]
AssignableWorkspaceRole = Literal["admin", "editor", "reviewer", "viewer"]


def _normalized_auth_text(value: str, *, field: str, maximum: int) -> str:
    normalized = unicodedata.normalize("NFKC", " ".join(value.split()))
    if not normalized:
        raise ValueError(f"{field} must not be blank")
    if len(normalized) > maximum:
        raise ValueError(f"{field} is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field} contains control characters")
    return normalized


def _normalized_email(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if (
        len(normalized) > 320
        or normalized.count("@") != 1
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError("invalid email address")
    local, domain = normalized.split("@", 1)
    if not local or not domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("invalid email address")
    return normalized


class AuthRegisterRequest(ApiModel):
    email: str = Field(strict=True, min_length=3, max_length=320)
    password: str = Field(strict=True, min_length=12, max_length=256)
    display_name: str = Field(strict=True, min_length=1, max_length=120)
    workspace_name: str | None = Field(
        default=None, strict=True, min_length=1, max_length=120
    )

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _normalized_email(value)

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str) -> str:
        return _normalized_auth_text(value, field="display_name", maximum=120)

    @field_validator("workspace_name")
    @classmethod
    def _workspace_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalized_auth_text(value, field="workspace_name", maximum=120)


class AuthLoginRequest(ApiModel):
    email: str = Field(strict=True, min_length=3, max_length=320)
    password: str = Field(strict=True, min_length=1, max_length=256)
    workspace_id: str | None = Field(
        default=None, strict=True, min_length=1, max_length=160
    )

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _normalized_email(value)

    @field_validator("workspace_id")
    @classmethod
    def _workspace_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalized_auth_text(value, field="workspace_id", maximum=160)


class AuthChangePasswordRequest(ApiModel):
    current_password: str = Field(strict=True, min_length=1, max_length=256)
    new_password: str = Field(strict=True, min_length=12, max_length=256)

    @model_validator(mode="after")
    def _password_must_change(self):
        if self.current_password == self.new_password:
            raise ValueError("new_password must differ from current_password")
        return self


class WorkspaceCreateRequest(ApiModel):
    name: str = Field(strict=True, min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _normalized_auth_text(value, field="name", maximum=120)


class WorkspaceUpdateRequest(WorkspaceCreateRequest):
    expected_revision: int | None = Field(default=None, strict=True, ge=0)


class WorkspaceMemberUpdateRequest(ApiModel):
    # Ownership transfer is intentionally not smuggled through a role edit.  A
    # dedicated transfer operation can later preserve the single-owner invariant.
    role: AssignableWorkspaceRole
    expected_revision: int | None = Field(default=None, strict=True, ge=0)


class WorkspaceInviteCreateRequest(ApiModel):
    email: str = Field(strict=True, min_length=3, max_length=320)
    role: AssignableWorkspaceRole

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _normalized_email(value)


class WorkspaceInviteAcceptRequest(ApiModel):
    token: str = Field(strict=True, min_length=16, max_length=512)
    email: str | None = Field(default=None, strict=True, min_length=3, max_length=320)
    password: str | None = Field(
        default=None, strict=True, min_length=1, max_length=256
    )
    display_name: str | None = Field(
        default=None, strict=True, min_length=1, max_length=120
    )

    @field_validator("token")
    @classmethod
    def _token(cls, value: str) -> str:
        # Tokens are opaque and case-sensitive; only surrounding whitespace is
        # rejected rather than normalized.
        if value != value.strip() or any(ord(character) < 33 for character in value):
            raise ValueError("invalid invite token")
        return value

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return None if value is None else _normalized_email(value)

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalized_auth_text(value, field="display_name", maximum=120)

    @model_validator(mode="after")
    def _anonymous_credentials_are_complete(self):
        if (self.email is None) != (self.password is None):
            raise ValueError("email and password must be supplied together")
        if self.display_name is not None and self.email is None:
            raise ValueError("display_name requires email and password")
        return self


class AuthUser(ApiModel):
    user_id: str
    email: str
    display_name: str
    created_at: str = ""
    updated_at: str = ""


class AuthWorkspace(ApiModel):
    workspace_id: str
    name: str
    role: WorkspaceRole
    created_at: str = ""
    updated_at: str = ""
    revision: int = Field(default=0, ge=0)


class AuthSessionInfo(ApiModel):
    session_id: str
    created_at: str = ""
    last_seen_at: str = ""
    expires_at: str = ""
    current: bool = False


class AuthSessionResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: str = ""
    user: AuthUser
    workspace: AuthWorkspace
    permissions: list[str] = Field(default_factory=list)


class AuthMeResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    user: AuthUser
    workspace: AuthWorkspace
    permissions: list[str] = Field(default_factory=list)
    workspaces: list[AuthWorkspace] = Field(default_factory=list)


class AuthSessionListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    sessions: list[AuthSessionInfo] = Field(default_factory=list)


class WorkspaceSessionPolicyUpdateRequest(ApiModel):
    idle_timeout_minutes: int | None = Field(default=None, strict=True, ge=5, le=43_200)
    absolute_timeout_hours: int | None = Field(
        default=None, strict=True, ge=1, le=8_760
    )
    max_active_sessions: int | None = Field(default=None, strict=True, ge=1, le=50)
    expected_revision: int = Field(strict=True, ge=0)


class WorkspaceSessionPolicy(ApiModel):
    workspace_id: str
    idle_timeout_minutes: int | None = None
    absolute_timeout_hours: int | None = None
    max_active_sessions: int | None = None
    revision: int = Field(ge=0)
    created_at: str | None = None
    updated_at: str | None = None


class WorkspaceSessionPolicyResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    policy: WorkspaceSessionPolicy


class WorkspaceSecuritySession(ApiModel):
    session_id: str
    user_id: str
    email: str
    display_name: str
    role: WorkspaceRole | None = None
    created_at: str
    last_seen_at: str
    expires_at: str
    revoked_at: str | None = None
    status: Literal["active", "expired", "revoked"]


class WorkspaceSecuritySessionListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    workspace_id: str
    total: int = Field(ge=0)
    sessions: list[WorkspaceSecuritySession] = Field(default_factory=list)
    next_before_session_id: str | None = None


class OIDCStartRequest(ApiModel):
    return_url: str = Field(strict=True, min_length=8, max_length=2048)
    workspace_id: str | None = Field(
        default=None, strict=True, min_length=1, max_length=160
    )


class OIDCStartResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    flow_id: str
    authorization_url: str
    expires_at: float = Field(ge=0)


class OIDCHandoffRequest(ApiModel):
    code: str = Field(strict=True, min_length=32, max_length=512)

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 33 for character in value):
            raise ValueError("invalid OIDC handoff code")
        return value


class OIDCIdentity(ApiModel):
    identity_id: str
    issuer: str
    subject: str
    email_at_link: str
    created_at: str = ""
    last_login_at: str = ""


class OIDCIdentityListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    identities: list[OIDCIdentity] = Field(default_factory=list)


class OIDCExchangeResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    kind: Literal["login", "link"]
    session: AuthSessionResponse | None = None
    identity: OIDCIdentity | None = None

    @model_validator(mode="after")
    def _one_result(self):
        if (self.kind == "login") != (self.session is not None):
            raise ValueError("login handoff requires exactly one session")
        if (self.kind == "link") != (self.identity is not None):
            raise ValueError("link handoff requires exactly one identity")
        return self


class WorkspaceOIDCPolicyUpdateRequest(ApiModel):
    allowed_domains: list[str] = Field(min_length=1, max_length=100)
    default_role: AssignableWorkspaceRole = "viewer"
    enabled: bool = True
    group_claim: str = Field(default="groups", min_length=1, max_length=128)
    group_role_map: dict[str, AssignableWorkspaceRole] = Field(
        default_factory=dict, max_length=100
    )
    require_mapped_group: bool = False
    expected_revision: int | None = Field(default=None, strict=True, ge=0)

    @field_validator("allowed_domains")
    @classmethod
    def _domains(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for raw in values:
            if not isinstance(raw, str):
                raise ValueError("OIDC domains must be strings")
            value = raw.strip().casefold().rstrip(".")
            if (
                not value
                or len(value) > 253
                or "@" in value
                or "/" in value
                or ":" in value
                or value.startswith(".")
                or any(character.isspace() for character in value)
            ):
                raise ValueError("invalid OIDC email domain")
            if value not in result:
                result.append(value)
        return sorted(result)

    @field_validator("group_claim")
    @classmethod
    def _group_claim(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < 33 or ord(character) == 127 for character in value
        ):
            raise ValueError("invalid OIDC group claim")
        if value in {
            "iss",
            "sub",
            "aud",
            "azp",
            "exp",
            "iat",
            "nbf",
            "nonce",
            "email",
            "email_verified",
            "name",
            "preferred_username",
        }:
            raise ValueError("OIDC group claim is reserved")
        return value

    @field_validator("group_role_map", mode="before")
    @classmethod
    def _group_role_map(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or len(value) > 100:
            raise ValueError(
                "OIDC group role map must be an object with at most 100 entries"
            )
        result: dict[str, Any] = {}
        for raw_group, role in value.items():
            if not isinstance(raw_group, str):
                raise ValueError("OIDC group names must be strings")
            group = unicodedata.normalize(
                "NFKC", " ".join(raw_group.split())
            ).casefold()
            if (
                not group
                or len(group) > 256
                or any(
                    ord(character) < 32 or ord(character) == 127 for character in group
                )
            ):
                raise ValueError("invalid OIDC group name")
            if group in result and result[group] != role:
                raise ValueError("OIDC group role map contains conflicting groups")
            result[group] = role
        return dict(sorted(result.items()))

    @model_validator(mode="after")
    def _mapped_group_requirement(self):
        if self.require_mapped_group and not self.group_role_map:
            raise ValueError("require_mapped_group requires a group role mapping")
        return self


class WorkspaceOIDCPolicy(ApiModel):
    workspace_id: str
    issuer: str
    allowed_domains: list[str]
    default_role: AssignableWorkspaceRole
    enabled: bool
    group_claim: str = "groups"
    group_role_map: dict[str, AssignableWorkspaceRole] = Field(default_factory=dict)
    require_mapped_group: bool = False
    revision: int = Field(ge=0)
    created_at: str = ""
    updated_at: str = ""


class WorkspaceOIDCPolicyResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    policy: WorkspaceOIDCPolicy | None = None


class SCIMDirectoryStatus(ApiModel):
    enabled: bool
    token_labels: list[str] = Field(default_factory=list)
    default_role: AssignableWorkspaceRole = "viewer"
    group_role_map: dict[str, AssignableWorkspaceRole] = Field(default_factory=dict)
    active_users: int = Field(ge=0)
    inactive_users: int = Field(ge=0)
    deleted_users: int = Field(ge=0)
    groups: int = Field(ge=0)
    group_memberships: int = Field(ge=0)
    last_updated_at: str | None = None


class SCIMDirectoryStatusResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    status: SCIMDirectoryStatus


class ServiceAccountCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    role: AssignableWorkspaceRole = "viewer"

    @field_validator("name", "description")
    @classmethod
    def _text(cls, value: str, info) -> str:
        if not value and info.field_name == "description":
            return ""
        return _normalized_auth_text(
            value,
            field=str(info.field_name),
            maximum=120 if info.field_name == "name" else 500,
        )


class ServiceAccountUpdateRequest(ServiceAccountCreateRequest):
    active: bool = True
    expected_revision: int = Field(strict=True, ge=1)


class ServiceAccount(ApiModel):
    service_account_id: str
    workspace_id: str
    name: str
    description: str = ""
    role: AssignableWorkspaceRole
    active: bool
    revision: int = Field(ge=1)
    created_by: str
    created_at: str
    updated_at: str


class ServiceAccountResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    service_account: ServiceAccount


class ServiceAccountListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    workspace_id: str
    service_accounts: list[ServiceAccount] = Field(default_factory=list)


class ServiceTokenCreateRequest(ApiModel):
    label: str = Field(min_length=1, max_length=120)
    expires_in_days: int | None = Field(default=90, strict=True, ge=1, le=365)
    permissions: (
        list[
            Literal[
                "read",
                "query",
                "write",
                "delete",
                "review",
                "publish",
                "manage_access",
            ]
        ]
        | None
    ) = None

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return _normalized_auth_text(value, field="label", maximum=120)


class ServiceToken(ApiModel):
    token_id: str
    service_account_id: str
    label: str
    secret_hint: str
    status: Literal["active", "expired", "revoked"]
    revision: int = Field(ge=1)
    created_at: str
    expires_at: str | None = None
    last_used_at: str | None = None
    revoked_at: str | None = None
    permissions: list[str] | None = None


class ServiceTokenCreateResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    service_token: ServiceToken
    token: str


class ServiceTokenListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    service_account_id: str
    tokens: list[ServiceToken] = Field(default_factory=list)


ServicePermission = Literal[
    "read", "query", "write", "delete", "review", "publish", "manage_access"
]


class ServiceAccountPolicyUpdate(ApiModel):
    max_accounts: int = Field(strict=True, ge=1, le=500)
    max_tokens_per_account: int = Field(strict=True, ge=1, le=50)
    max_token_ttl_days: int = Field(strict=True, ge=1, le=365)
    allow_non_expiring: bool
    allowed_permissions: list[ServicePermission] = Field(min_length=1)
    expected_revision: int = Field(strict=True, ge=0)


class ServiceAccountPolicy(ApiModel):
    workspace_id: str
    max_accounts: int
    max_tokens_per_account: int
    max_token_ttl_days: int
    allow_non_expiring: bool
    allowed_permissions: list[ServicePermission]
    revision: int = Field(ge=0)
    created_at: str | None = None
    updated_at: str | None = None


class ServiceAccountPolicyResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    policy: ServiceAccountPolicy


class WorkspaceResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    workspace: AuthWorkspace


class WorkspaceListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    workspaces: list[AuthWorkspace] = Field(default_factory=list)


class WorkspaceMember(ApiModel):
    member_id: str
    user_id: str
    email: str
    display_name: str
    role: WorkspaceRole
    joined_at: str = ""
    updated_at: str = ""


class WorkspaceMemberResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    member: WorkspaceMember


class WorkspaceMemberListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    workspace_id: str
    members: list[WorkspaceMember] = Field(default_factory=list)


class WorkspaceInvite(ApiModel):
    invite_id: str
    workspace_id: str
    email: str
    role: AssignableWorkspaceRole
    status: Literal["pending", "accepted", "revoked", "expired"] = "pending"
    created_by: str = ""
    created_at: str = ""
    expires_at: str = ""


class WorkspaceInviteCreateResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    invite: WorkspaceInvite
    # Returned exactly once so a self-hosted deployment without a mailer can
    # deliver the invitation out of band.  List responses never contain it.
    invite_token: str


class WorkspaceInviteListResponse(ApiModel):
    schema_version: Literal["v1"] = API_SCHEMA_VERSION
    workspace_id: str
    invites: list[WorkspaceInvite] = Field(default_factory=list)


# 转换为映射。
def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return vars(value)
    return {}


# 解析整数或空值。
def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


# 解析浮点数或空值。
def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_int(value: Any) -> int:
    parsed = _int_or_none(value)
    return max(parsed, 0) if parsed is not None else 0


# 规范化任务类型。
def _normalize_task_type(value: Any) -> ChatTask:
    # 上游意外或缺失时一律归一到未知任务，契约不因字段漂移崩。
    task = str(value or ChatTask.UNKNOWN.value)
    return (
        ChatTask(task)
        if task in {item.value for item in ChatTask}
        else ChatTask.UNKNOWN
    )


# 从映射构建引用。
def _citation_from_mapping(item: Any) -> Citation:
    data = _as_mapping(item)
    page = _int_or_none(data.get("page"))
    return Citation(
        chunk_id=str(data.get("chunk_id", "") or ""),
        source_type=str(data.get("source_type", "document") or "document"),
        knowledge_id=str(data.get("knowledge_id", "") or ""),
        source=str(data.get("source", "") or ""),
        source_id=str(data.get("source_id", "") or ""),
        source_version_id=str(data.get("source_version_id", "") or ""),
        media_type=str(data.get("media_type", "") or ""),
        location=dict(_as_mapping(data.get("location"))),
        page=page,
        page_start=_int_or_none(data.get("page_start", page)),
        page_end=_int_or_none(data.get("page_end", page)),
    )


def _citation_occurrence_from_mapping(item: Any) -> CitationOccurrence:
    data = _as_mapping(item)
    return CitationOccurrence(
        index=_nonnegative_int(data.get("index")),
        answer_start=_nonnegative_int(data.get("answer_start")),
        answer_end=_nonnegative_int(data.get("answer_end")),
    )


def _citation_ledger_entry_from_mapping(item: Any) -> CitationLedgerEntry:
    data = _as_mapping(item)
    return CitationLedgerEntry(
        evidence_id=str(data.get("evidence_id", "") or ""),
        chunk_id=str(data.get("chunk_id", "") or ""),
        source_type=str(data.get("source_type", "document") or "document"),
        knowledge_id=str(data.get("knowledge_id", "") or ""),
        source=str(data.get("source", "") or ""),
        source_id=str(data.get("source_id", "") or ""),
        source_version_id=str(data.get("source_version_id", "") or ""),
        media_type=str(data.get("media_type", "") or ""),
        location=dict(_as_mapping(data.get("location"))),
        page=_int_or_none(data.get("page")),
        page_start=_int_or_none(data.get("page_start")),
        page_end=_int_or_none(data.get("page_end")),
        span_start=_nonnegative_int(data.get("span_start")),
        span_end=_nonnegative_int(data.get("span_end")),
        occurrences=[
            _citation_occurrence_from_mapping(occurrence)
            for occurrence in list(data.get("occurrences", []) or [])
        ],
    )


# 从映射构建证据。
def _evidence_from_mapping(item: Any) -> Evidence:
    data = _as_mapping(item)
    page = _int_or_none(data.get("page"))
    return Evidence(
        chunk_id=str(data.get("chunk_id", "") or ""),
        parent_chunk_id=str(data.get("parent_chunk_id", "") or ""),
        section_title=str(data.get("section_title", "") or ""),
        section_path=str(data.get("section_path", "") or ""),
        section_level=_int_or_none(data.get("section_level")),
        child_index_in_parent=_int_or_none(data.get("child_index_in_parent")),
        source_type=str(data.get("source_type", "document") or "document"),
        knowledge_id=str(data.get("knowledge_id", "") or ""),
        chunk_index=_int_or_none(data.get("chunk_index")),
        source=str(data.get("source", "") or ""),
        source_id=str(data.get("source_id", "") or ""),
        source_version_id=str(data.get("source_version_id", "") or ""),
        media_type=str(data.get("media_type", "") or ""),
        location=dict(_as_mapping(data.get("location") or data.get("source_location"))),
        page=page,
        page_start=_int_or_none(data.get("page_start", page)),
        page_end=_int_or_none(data.get("page_end", page)),
        rerank_score=_float_or_none(data.get("rerank_score")),
        rewrite_query=data.get("rewrite_query"),
        text_preview=str(data.get("text_preview", "") or ""),
        retrieval=safe_retrieval_metadata(data.get("retrieval")),
    )


def _claim_audit_summary_from_mapping(item: Any) -> ClaimAuditSummary | None:
    data = _as_mapping(item)
    if not data:
        return None
    verifier = _as_mapping(data.get("verifier"))
    raw_counts = _as_mapping(data.get("counts"))
    raw_metrics = _as_mapping(data.get("metrics"))
    raw_repair = _as_mapping(data.get("repair"))
    count_keys = (
        "claim_count",
        "supported",
        "unsupported",
        "insufficient",
        "cited",
        "skipped_statements",
    )
    metric_keys = (
        "claim_support_rate",
        "citation_coverage",
        "unsupported_claim_rate",
    )
    return ClaimAuditSummary(
        status=str(data.get("status") or "not_run"),
        reason_code=str(data.get("reason_code") or ""),
        counts={
            key: _nonnegative_int(raw_counts.get(key, 0))
            for key in count_keys
            if key in raw_counts
        },
        metrics={
            key: (
                _float_or_none(raw_metrics.get(key))
                if raw_metrics.get(key) is not None
                else None
            )
            for key in metric_keys
            if key in raw_metrics
        },
        repair={
            "attempted": bool(raw_repair.get("attempted", False)),
            "attempt_count": _nonnegative_int(raw_repair.get("attempt_count", 0)),
            "succeeded": bool(raw_repair.get("succeeded", False)),
        },
        duration_ms=_float_or_none(
            data.get("duration_ms", verifier.get("duration_ms"))
        ),
    )


def _claim_verification_rollout_summary_from_mapping(
    item: Any,
) -> ClaimVerificationRolloutSummary | None:
    data = _as_mapping(item)
    if not data:
        return None
    mode = str(data.get("mode") or "off")
    if mode not in {"off", "shadow", "enforce"}:
        mode = "off"
    policy = claim_verification_policy_projection(data, effective_mode=mode)
    decision = str(data.get("decision") or "skipped")
    if decision not in ROLLOUT_DECISIONS:
        decision = "skipped"
    return ClaimVerificationRolloutSummary(
        version="v1",
        mode=mode,
        configured_mode=policy["configured_mode"],
        rollout_percent=policy["rollout_percent"],
        cohort_bucket=policy["cohort_bucket"],
        cohort_selected=policy["cohort_selected"],
        fallback_mode=policy["fallback_mode"],
        policy_id=policy["policy_id"],
        decision=decision,
        executed=bool(data.get("executed", False)),
        enforced=bool(data.get("enforced", False)),
        released=bool(data.get("released", True)),
        would_intervene=bool(data.get("would_intervene", False)),
        would_repair=bool(data.get("would_repair", False)),
        would_block=bool(data.get("would_block", False)),
        audit_status=str(data.get("audit_status") or "not_run")[:32],
        reason_code=str(data.get("reason_code") or "")[:128],
        repair_count=_nonnegative_int(data.get("repair_count")),
    )


# 把对话结果转换成响应。
def chat_result_to_response(
    result: Any,
    *,
    doc_id: str,
    session_id: str | None = None,
) -> ChatResponse:
    # 防御式取值，且不暴露原始输出、步骤和证据全文。
    raw_output = _as_mapping(getattr(result, "raw_output", None))
    task_type = _normalize_task_type(getattr(result, "task_type", None))
    answer = str(getattr(result, "answer", "") or "")
    raw_evidence = getattr(result, "evidence", []) or []
    raw_ledger = getattr(result, "citation_ledger", [])
    ledger_evidence = raw_output.get("evidence_ledger") or raw_evidence
    ledger_validation = validate_public_citation_ledger(
        answer,
        raw_ledger,
        evidence=ledger_evidence,
        require_evidence=bool(raw_ledger),
    )
    public_ledger = ledger_validation.entries if ledger_validation.is_valid else ()
    critique = str(getattr(result, "critique", "") or "")
    if task_type in {"qa", "summary", "compare"} and (
        critique or not ledger_validation.is_valid
    ):
        critique = "回答未通过引用或声明证据校验。"
    return ChatResponse(
        request_id=str(getattr(result, "request_id", "") or ""),
        trace_id=str(getattr(result, "trace_id", "") or ""),
        doc_id=doc_id,
        session_id=session_id,
        task_type=task_type,
        answer=answer,
        citations=[
            _citation_from_mapping(item)
            for item in list(getattr(result, "citations", []) or [])
        ],
        citation_ledger=[
            _citation_ledger_entry_from_mapping(item) for item in public_ledger
        ],
        evidence=[_evidence_from_mapping(item) for item in list(raw_evidence)],
        critique=critique,
        is_valid=bool(getattr(result, "is_valid", False))
        and ledger_validation.is_valid,
        claim_audit=_claim_audit_summary_from_mapping(raw_output.get("claim_audit")),
        claim_verification=_claim_verification_rollout_summary_from_mapping(
            raw_output.get("claim_verification_rollout")
        ),
    )


# 构建错误响应。
def build_error_response(
    error_code: ErrorCode,
    message: str,
    *,
    request_id: str | None = None,
    trace_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> ErrorResponse:
    return ErrorResponse(
        error_code=error_code,
        message=message,
        request_id=request_id,
        trace_id=trace_id,
        details=details,
    )
