from __future__ import annotations

import hashlib

from cogdoc.ha.migrations import Migration
from cogdoc.ha.identity_schema import IDENTITY_ACCESS_DDL, IDENTITY_ACCESS_TABLES
from cogdoc.ha.storage import DatabaseBackend


CURRENT_SCHEMA_VERSION = 9
MINIMUM_SCHEMA_VERSION = 1

_BASELINE_TABLES = (
    "ha_index_generations",
    "ha_index_heads",
    "ha_job_keys",
    "ha_jobs",
    "ha_outbox",
    "ha_schedule_fires",
    "ha_schedules",
)


def _tables_are_present(backend: DatabaseBackend, tables: tuple[str, ...]) -> bool:
    with backend.transaction() as connection:
        for table in tables:
            try:
                connection.execute(f"SELECT 1 FROM {table} WHERE 1=0")
            except Exception:
                return False
    return True


def _baseline_is_present(backend: DatabaseBackend) -> bool:
    return _tables_are_present(backend, _BASELINE_TABLES)


def _chat_scope_identity_is_present(backend: DatabaseBackend) -> bool:
    """Validate the v7 columns and completed backfill, not just table names."""

    try:
        with backend.transaction() as connection:
            scope_nulls = connection.execute(
                "SELECT COUNT(*) FROM ha_chat_memory_scopes WHERE storage_id IS NULL"
            ).fetchone()[0]
            lease_nulls = connection.execute(
                "SELECT COUNT(*) FROM ha_chat_session_leases WHERE storage_id IS NULL"
            ).fetchone()[0]
    except Exception:
        return False
    return int(scope_nulls) == 0 and int(lease_nulls) == 0


def _backfill_derived_index_refreshes(
    backend: DatabaseBackend, _cursor: str | None, _limit: int
) -> str | None:
    """Create one durable per-KB watermark for every v8 knowledge ledger."""

    with backend.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO ha_derived_knowledge_refreshes("
            "kb_id,requested_sequence,status,updated_at) "
            "SELECT kb_id,MAX(event_sequence),'pending',0 "
            "FROM ha_derived_knowledge_events GROUP BY kb_id "
            "ON CONFLICT(kb_id) DO NOTHING"
        )
    return None


def _derived_index_refresh_is_present(backend: DatabaseBackend) -> bool:
    if not _tables_are_present(backend, _DERIVED_INDEX_REFRESH_TABLES):
        return False
    try:
        with backend.transaction() as connection:
            missing = connection.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT kb_id,MAX(event_sequence) AS requested_sequence "
                "FROM ha_derived_knowledge_events GROUP BY kb_id"
                ") AS events LEFT JOIN ha_derived_knowledge_refreshes AS refresh "
                "ON refresh.kb_id=events.kb_id "
                "WHERE refresh.kb_id IS NULL "
                "OR refresh.requested_sequence<events.requested_sequence"
            ).fetchone()[0]
    except Exception:
        return False
    return int(missing) == 0


def migrations_are_current(backend: DatabaseBackend) -> bool:
    """Verify the authoritative migration ledger without creating any tables."""

    marker = backend.sql(sqlite="?", postgres="%s")
    try:
        with backend.transaction() as connection:
            rows = connection.execute(
                "SELECT version,name,checksum,phase FROM ha_schema_migrations "
                f"WHERE version<={marker} ORDER BY version",
                (CURRENT_SCHEMA_VERSION,),
            ).fetchall()
    except Exception:
        return False
    expected = {migration.version: migration for migration in REGISTERED_MIGRATIONS}
    if len(rows) != len(expected):
        return False
    for row in rows:
        version = int(row["version"] if isinstance(row, dict) else row[0])
        migration = expected.get(version)
        if migration is None:
            return False
        name = str(row["name"] if isinstance(row, dict) else row[1])
        checksum = str(row["checksum"] if isinstance(row, dict) else row[2])
        phase = str(row["phase"] if isinstance(row, dict) else row[3])
        if (
            name != migration.name
            or checksum != migration.checksum
            or phase not in {"validated", "contracted"}
        ):
            return False
    return True


_BASELINE_CONTRACT = "cogdoc-ha-control-plane-v1:" + ",".join(_BASELINE_TABLES)
_SHARED_API_TABLES = (
    "ha_api_index_jobs",
    "ha_api_kb_deletions",
    "ha_api_knowledge_bases",
    "ha_api_mutation_leases",
    "ha_invalidation_offsets",
    "ha_source_artifact_locks",
    "ha_source_artifact_reservation_items",
    "ha_source_artifact_reservations",
    "ha_source_artifact_scopes",
    "ha_source_artifact_uploads",
    "ha_source_artifacts",
    "ha_source_catalog_documents",
    "ha_source_catalog_locks",
    "ha_source_catalog_versions",
    "ha_source_generations",
    "ha_source_heads",
    "ha_tenant_quota_locks",
    "ha_tenant_quota_reservations",
)
_SHARED_API_DDL = (
    """CREATE TABLE IF NOT EXISTS ha_api_knowledge_bases (
    storage_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,created_at TEXT NOT NULL,lifecycle TEXT NOT NULL,
    epoch BIGINT NOT NULL,revision BIGINT NOT NULL,updated_at DOUBLE PRECISION NOT NULL,
    UNIQUE(tenant_id,kb_id))""",
    """CREATE TABLE IF NOT EXISTS ha_api_mutation_leases (
    storage_id TEXT PRIMARY KEY,lease_owner TEXT NOT NULL,lease_token TEXT NOT NULL,
    lease_expires_at DOUBLE PRECISION NOT NULL,fencing_token BIGINT NOT NULL,
    kb_epoch BIGINT NOT NULL,updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ha_api_index_jobs (
    job_id TEXT PRIMARY KEY,kb_id TEXT NOT NULL,status TEXT NOT NULL,
    record_json TEXT NOT NULL,lease_owner TEXT,lease_token TEXT,
    lease_expires_at DOUBLE PRECISION,created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ha_api_kb_deletions (
    storage_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_epoch BIGINT NOT NULL,
    phase TEXT NOT NULL,index_generation_id TEXT,source_generation_id TEXT,
    artifact_versions BIGINT NOT NULL DEFAULT 0,catalog_documents BIGINT NOT NULL DEFAULT 0,
    started_at DOUBLE PRECISION NOT NULL,updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ha_invalidation_offsets (
    consumer_id TEXT PRIMARY KEY,last_created_at DOUBLE PRECISION NOT NULL,
    last_event_id TEXT NOT NULL,updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ha_source_generations (
    generation_id TEXT PRIMARY KEY,storage_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
    kb_epoch BIGINT NOT NULL,base_generation_id TEXT,build_id TEXT,status TEXT NOT NULL,
    manifest_key TEXT NOT NULL,manifest_sha256 TEXT NOT NULL,file_count INTEGER NOT NULL,
    total_bytes BIGINT NOT NULL,document_count INTEGER NOT NULL,document_bytes BIGINT NOT NULL,
    fencing_token BIGINT NOT NULL,created_at DOUBLE PRECISION NOT NULL,
    published_at DOUBLE PRECISION)""",
    """CREATE TABLE IF NOT EXISTS ha_source_heads (
    storage_id TEXT PRIMARY KEY,generation_id TEXT NOT NULL,revision BIGINT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ha_tenant_quota_locks (
    tenant_id TEXT PRIMARY KEY,revision BIGINT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ha_tenant_quota_reservations (
    token TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kind TEXT NOT NULL,
    storage_id TEXT NOT NULL,filename TEXT NOT NULL,document_delta BIGINT NOT NULL,
    byte_delta BIGINT NOT NULL,lease_owner TEXT NOT NULL,
    lease_expires_at DOUBLE PRECISION NOT NULL,created_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ha_source_catalog_documents (
    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,source_id TEXT NOT NULL,
    connection_id TEXT,connector_type TEXT NOT NULL,external_id TEXT NOT NULL,
    display_name TEXT NOT NULL,media_type TEXT NOT NULL,kind TEXT NOT NULL,
    origin_uri TEXT,current_version_id TEXT NOT NULL,metadata_json TEXT NOT NULL,
    health_status TEXT NOT NULL DEFAULT 'unknown',last_sync_at DOUBLE PRECISION,
    last_sync_error TEXT,health_job_sequence BIGINT NOT NULL DEFAULT 0,
    health_job_attempt BIGINT NOT NULL DEFAULT 0,health_event_rank BIGINT NOT NULL DEFAULT 0,
    deleted_at DOUBLE PRECISION,updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(tenant_id,kb_id,source_id),
    UNIQUE(tenant_id,kb_id,connector_type,external_id))""",
    """CREATE TABLE IF NOT EXISTS ha_source_catalog_versions (
    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,source_id TEXT NOT NULL,
    version_id TEXT NOT NULL,content_sha256 TEXT NOT NULL,byte_size BIGINT,
    etag TEXT,modified_at TEXT,fetched_at DOUBLE PRECISION NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(tenant_id,kb_id,source_id,version_id))""",
    """CREATE TABLE IF NOT EXISTS ha_source_catalog_locks (
    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(tenant_id,kb_id,scope_kind,scope_id))""",
    """CREATE TABLE IF NOT EXISTS ha_source_artifact_locks (
    lock_id TEXT PRIMARY KEY,revision BIGINT NOT NULL,updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ha_source_artifacts (
    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,source_id TEXT NOT NULL,
    version_id TEXT NOT NULL,object_key TEXT NOT NULL UNIQUE,content_sha256 TEXT NOT NULL,
    byte_size BIGINT NOT NULL,media_type TEXT NOT NULL,display_name TEXT,
    created_at DOUBLE PRECISION NOT NULL,recovery_token TEXT UNIQUE,
    deleted_at DOUBLE PRECISION,object_version_id TEXT,object_etag TEXT,
    PRIMARY KEY(tenant_id,kb_id,source_id,version_id))""",
    """CREATE TABLE IF NOT EXISTS ha_source_artifact_reservations (
    token TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
    reservation_key TEXT NOT NULL,fingerprint TEXT NOT NULL,lease_owner TEXT NOT NULL,
    lease_expires_at DOUBLE PRECISION NOT NULL,created_at DOUBLE PRECISION NOT NULL,
    UNIQUE(tenant_id,kb_id,reservation_key))""",
    """CREATE TABLE IF NOT EXISTS ha_source_artifact_reservation_items (
    token TEXT NOT NULL,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,source_id TEXT NOT NULL,
    version_id TEXT NOT NULL,metadata_json TEXT NOT NULL,reserved_bytes BIGINT NOT NULL,
    reserved_version INTEGER NOT NULL,consumed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(token,source_id,version_id),
    UNIQUE(tenant_id,kb_id,source_id,version_id))""",
    """CREATE TABLE IF NOT EXISTS ha_source_artifact_uploads (
    object_key TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
    source_id TEXT NOT NULL,version_id TEXT NOT NULL,metadata_json TEXT NOT NULL,
    reservation_token TEXT,reserved_bytes BIGINT NOT NULL,lease_owner TEXT NOT NULL,
    lease_expires_at DOUBLE PRECISION NOT NULL,created_at DOUBLE PRECISION NOT NULL,
    UNIQUE(tenant_id,kb_id,source_id,version_id))""",
    """CREATE TABLE IF NOT EXISTS ha_source_artifact_scopes (
    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,state TEXT NOT NULL,
    kb_epoch BIGINT NOT NULL DEFAULT 0,
    updated_at DOUBLE PRECISION NOT NULL,PRIMARY KEY(tenant_id,kb_id))""",
)
_CONNECTOR_CONTROL_TABLES = (
    "ha_connector_commits",
    "ha_connector_reference_locks",
    "ha_connector_keyring_versions",
    "connector_connections",
    "connector_credential_event_sequence",
    "connector_credential_events",
    "connector_credential_pending_bindings",
    "connector_credentials",
    "connector_oauth_sessions",
    "connector_sync_checkpoints",
    "connector_sync_health",
    "connector_sync_job_sequence",
    "connector_sync_jobs",
)
_CONNECTOR_CONTROL_DDL = (
    """CREATE TABLE IF NOT EXISTS ha_connector_commits (
    job_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,connector_type TEXT NOT NULL,
    kb_epoch BIGINT NOT NULL,fencing_token BIGINT NOT NULL,phase TEXT NOT NULL,
    index_job_id TEXT,manifest_key TEXT NOT NULL,manifest_sha256 TEXT NOT NULL,
    file_count INTEGER NOT NULL,total_bytes BIGINT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ha_connector_reference_locks (
    lock_id TEXT PRIMARY KEY,lease_owner TEXT NOT NULL,lease_token TEXT NOT NULL,
    lease_expires_at DOUBLE PRECISION NOT NULL,updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ha_connector_keyring_versions (
    key_version TEXT PRIMARY KEY,key_fingerprint TEXT NOT NULL,
    registered_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS connector_connections (
    connection_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
    connector_type TEXT NOT NULL,name TEXT NOT NULL,config_json TEXT NOT NULL,
    secret_env_json TEXT NOT NULL,owner_id TEXT NOT NULL,
    workspace_visible INTEGER NOT NULL,enabled INTEGER NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,updated_at DOUBLE PRECISION NOT NULL,
    revision INTEGER NOT NULL,credential_id TEXT,credential_fields_json TEXT NOT NULL DEFAULT '[]',
    deleting INTEGER NOT NULL DEFAULT 0,delete_index_job_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS connector_sync_jobs (
    job_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,connector_type TEXT NOT NULL,status TEXT NOT NULL,
    start_cursor TEXT,cursor TEXT,lease_token TEXT,lease_expires_at DOUBLE PRECISION,
    cancel_requested INTEGER NOT NULL DEFAULT 0,attempt INTEGER NOT NULL DEFAULT 0,
    pages_processed INTEGER NOT NULL DEFAULT 0,documents_seen INTEGER NOT NULL DEFAULT 0,
    documents_fetched INTEGER NOT NULL DEFAULT 0,deleted_seen INTEGER NOT NULL DEFAULT 0,
    bytes_fetched BIGINT NOT NULL DEFAULT 0,error_code TEXT,error_message TEXT,
    retry_at DOUBLE PRECISION,created_at DOUBLE PRECISION NOT NULL,
    started_at DOUBLE PRECISION,updated_at DOUBLE PRECISION NOT NULL,
    finished_at DOUBLE PRECISION,revision INTEGER NOT NULL DEFAULT 0,replay_of TEXT,
    job_sequence BIGINT NOT NULL,connection_revision INTEGER NOT NULL DEFAULT 0,
    health_duration_seconds DOUBLE PRECISION,
    health_failure_recorded INTEGER NOT NULL DEFAULT 0,credential_id TEXT,
    credential_revision INTEGER NOT NULL DEFAULT 0,
    cleanup_pending INTEGER NOT NULL DEFAULT 0,attempt_started_at DOUBLE PRECISION)""",
    """CREATE TABLE IF NOT EXISTS connector_sync_job_sequence (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),last_value BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS connector_sync_checkpoints (
    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,connection_id TEXT NOT NULL,cursor TEXT,
    last_job_id TEXT NOT NULL,last_success_at DOUBLE PRECISION NOT NULL,
    counters_json TEXT NOT NULL,PRIMARY KEY(tenant_id,kb_id,connection_id))""",
    """CREATE TABLE IF NOT EXISTS connector_sync_health (
    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,connection_id TEXT NOT NULL,
    schedule_seconds INTEGER,next_run_at DOUBLE PRECISION,
    health_status TEXT NOT NULL DEFAULT 'unknown',last_job_id TEXT,last_job_status TEXT,
    last_started_at DOUBLE PRECISION,last_success_at DOUBLE PRECISION,
    last_failure_at DOUBLE PRECISION,last_error_code TEXT,last_duration_seconds DOUBLE PRECISION,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,last_job_sequence BIGINT NOT NULL DEFAULT 0,
    last_success_sequence BIGINT NOT NULL DEFAULT 0,updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(tenant_id,kb_id,connection_id))""",
    """CREATE TABLE IF NOT EXISTS connector_credentials (
    credential_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
    connection_id TEXT,provider TEXT NOT NULL,credential_kind TEXT NOT NULL,label TEXT NOT NULL,
    subject TEXT,scopes_json TEXT NOT NULL,secret_fields_json TEXT NOT NULL,
    wrapped_key_nonce BYTEA NOT NULL,wrapped_key_ciphertext BYTEA NOT NULL,
    payload_nonce BYTEA NOT NULL,payload_ciphertext BYTEA NOT NULL,key_version TEXT NOT NULL,
    expires_at DOUBLE PRECISION,last_used_at DOUBLE PRECISION,created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,revision INTEGER NOT NULL,
    lifecycle TEXT NOT NULL DEFAULT 'active')""",
    """CREATE TABLE IF NOT EXISTS connector_credential_events (
    event_id TEXT PRIMARY KEY,event_sequence BIGINT NOT NULL,credential_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,connection_id TEXT,action TEXT NOT NULL,
    actor_id TEXT NOT NULL,revision INTEGER NOT NULL,key_version TEXT NOT NULL,
    occurred_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS connector_credential_event_sequence (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),last_value BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS connector_credential_pending_bindings (
    credential_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,credential_revision INTEGER NOT NULL,
    expected_connection_revision INTEGER NOT NULL,bound_connection_revision INTEGER NOT NULL,
    previous_credential_id TEXT,previous_credential_fields_json TEXT NOT NULL,
    previous_secret_env_json TEXT NOT NULL,created_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS connector_oauth_sessions (
    session_id TEXT PRIMARY KEY,state_hash BYTEA NOT NULL UNIQUE,
    verifier_credential_id TEXT NOT NULL,provider TEXT NOT NULL,tenant_id TEXT NOT NULL,
    kb_id TEXT NOT NULL,connection_id TEXT,user_id TEXT NOT NULL,redirect_uri TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,expires_at DOUBLE PRECISION NOT NULL,
    consumed_at DOUBLE PRECISION,cancelled_at DOUBLE PRECISION,
    kb_epoch BIGINT NOT NULL DEFAULT 0,membership_id TEXT,principal_fingerprint TEXT,
    connection_revision INTEGER)""",
    "CREATE INDEX IF NOT EXISTS idx_connector_connections_scope ON connector_connections(tenant_id,kb_id,created_at,connection_id)",
    "CREATE INDEX IF NOT EXISTS idx_connector_sync_jobs_scope ON connector_sync_jobs(tenant_id,kb_id,connection_id,created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_connector_sync_jobs_runnable ON connector_sync_jobs(status,retry_at,lease_expires_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_connector_sync_jobs_sequence ON connector_sync_jobs(job_sequence)",
    "CREATE INDEX IF NOT EXISTS idx_connector_sync_jobs_scope_sequence ON connector_sync_jobs(tenant_id,kb_id,connection_id,job_sequence DESC)",
    "CREATE INDEX IF NOT EXISTS idx_connector_sync_jobs_replay_of ON connector_sync_jobs(replay_of)",
    "CREATE INDEX IF NOT EXISTS idx_connector_sync_jobs_terminal_cleanup ON connector_sync_jobs(status,cleanup_pending,finished_at,job_sequence)",
    "CREATE INDEX IF NOT EXISTS idx_connector_sync_health_due ON connector_sync_health(next_run_at,tenant_id,kb_id,connection_id)",
    "CREATE INDEX IF NOT EXISTS idx_connector_sync_health_last_job ON connector_sync_health(last_job_id)",
    "CREATE INDEX IF NOT EXISTS idx_connector_sync_checkpoints_last_job ON connector_sync_checkpoints(last_job_id)",
    "CREATE INDEX IF NOT EXISTS idx_connector_credentials_scope ON connector_credentials(tenant_id,kb_id,connection_id,created_at,credential_id)",
    "CREATE INDEX IF NOT EXISTS idx_connector_credentials_lifecycle ON connector_credentials(lifecycle,updated_at,credential_id)",
    "CREATE INDEX IF NOT EXISTS idx_connector_credential_events_scope ON connector_credential_events(tenant_id,kb_id,event_sequence)",
    "CREATE INDEX IF NOT EXISTS idx_connector_credential_use_retention ON connector_credential_events(occurred_at,event_id) WHERE action='use'",
    "CREATE INDEX IF NOT EXISTS idx_connector_pending_bindings_scope ON connector_credential_pending_bindings(tenant_id,kb_id,created_at,credential_id)",
    "CREATE INDEX IF NOT EXISTS idx_connector_oauth_sessions_expiry ON connector_oauth_sessions(expires_at,consumed_at,cancelled_at)",
    "CREATE INDEX IF NOT EXISTS idx_ha_connector_commits_scope ON ha_connector_commits(tenant_id,kb_id,created_at,job_id)",
)

_RESEARCH_CONTROL_TABLES = (
    "ha_research_dispatches",
    "research_jobs",
)
_RESEARCH_CONTROL_DDL = (
    """CREATE TABLE IF NOT EXISTS research_jobs (
    job_id TEXT PRIMARY KEY,kb_id TEXT NOT NULL,status TEXT NOT NULL,
    updated_at TEXT NOT NULL,data TEXT NOT NULL,summary TEXT NOT NULL DEFAULT '{}')""",
    "CREATE INDEX IF NOT EXISTS idx_research_jobs_queue ON research_jobs(kb_id,status,updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_research_jobs_summary_page ON research_jobs(kb_id,updated_at DESC,job_id DESC)",
    """CREATE TABLE IF NOT EXISTS ha_research_dispatches (
    dispatch_id TEXT PRIMARY KEY,research_job_id TEXT NOT NULL,phase TEXT NOT NULL,
    attempt_id TEXT NOT NULL,status TEXT NOT NULL,lease_owner TEXT,lease_token TEXT,
    lease_expires_at DOUBLE PRECISION,created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,finished_at DOUBLE PRECISION,
    revision BIGINT NOT NULL DEFAULT 1,
    UNIQUE(research_job_id,phase,attempt_id))""",
    "CREATE INDEX IF NOT EXISTS idx_ha_research_dispatch_claim ON ha_research_dispatches(status,lease_expires_at,created_at,dispatch_id)",
    "CREATE INDEX IF NOT EXISTS idx_ha_research_dispatch_job ON ha_research_dispatches(research_job_id,phase,created_at DESC)",
)

_CHAT_CONTROL_TABLES = (
    "ha_chat_long_memories",
    "ha_chat_memory_scopes",
    "ha_chat_session_leases",
    "ha_chat_sessions",
    "ha_chat_turns",
    "ha_index_reader_leases",
)
_CHAT_CONTROL_DDL = (
    """CREATE TABLE IF NOT EXISTS ha_chat_memory_scopes (
    doc_id TEXT PRIMARY KEY,revision BIGINT NOT NULL DEFAULT 1,
    updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ha_chat_sessions (
    doc_id TEXT NOT NULL,session_id TEXT NOT NULL,memory_json TEXT NOT NULL,
    display_json TEXT NOT NULL,mid_memory_json TEXT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,revision BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY(doc_id,session_id))""",
    "CREATE INDEX IF NOT EXISTS idx_ha_chat_sessions_expiry ON ha_chat_sessions(updated_at,doc_id,session_id)",
    "CREATE INDEX IF NOT EXISTS idx_ha_chat_sessions_list ON ha_chat_sessions(doc_id,updated_at DESC,session_id DESC)",
    """CREATE TABLE IF NOT EXISTS ha_chat_long_memories (
    doc_id TEXT NOT NULL,memory_id TEXT NOT NULL,type TEXT NOT NULL,
    content TEXT NOT NULL,importance DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,PRIMARY KEY(doc_id,memory_id))""",
    "CREATE INDEX IF NOT EXISTS idx_ha_chat_long_order ON ha_chat_long_memories(doc_id,importance DESC,updated_at DESC,memory_id)",
    """CREATE TABLE IF NOT EXISTS ha_chat_turns (
    doc_id TEXT NOT NULL,session_id TEXT NOT NULL,turn_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,created_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(doc_id,session_id,turn_id))""",
    "CREATE INDEX IF NOT EXISTS idx_ha_chat_turns_created ON ha_chat_turns(created_at,doc_id,session_id,turn_id)",
    """CREATE TABLE IF NOT EXISTS ha_chat_session_leases (
    doc_id TEXT NOT NULL,session_id TEXT NOT NULL,lease_owner TEXT NOT NULL,
    lease_token TEXT NOT NULL,lease_expires_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,revision BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY(doc_id,session_id))""",
    "CREATE INDEX IF NOT EXISTS idx_ha_chat_session_lease_expiry ON ha_chat_session_leases(lease_expires_at,doc_id,session_id)",
    """CREATE TABLE IF NOT EXISTS ha_index_reader_leases (
    reader_id TEXT PRIMARY KEY,generation_id TEXT NOT NULL,lease_owner TEXT NOT NULL,
    lease_token TEXT NOT NULL,lease_expires_at DOUBLE PRECISION NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,updated_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_ha_index_reader_generation ON ha_index_reader_leases(generation_id,lease_expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_ha_index_reader_expiry ON ha_index_reader_leases(lease_expires_at,reader_id)",
)

_CHAT_SCOPE_IDENTITY_DDL = (
    "ALTER TABLE ha_chat_memory_scopes ADD COLUMN storage_id TEXT",
    "UPDATE ha_chat_memory_scopes SET storage_id=doc_id WHERE storage_id IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_ha_chat_scope_storage ON ha_chat_memory_scopes(storage_id,doc_id)",
    "ALTER TABLE ha_chat_session_leases ADD COLUMN storage_id TEXT",
    "UPDATE ha_chat_session_leases SET storage_id=doc_id WHERE storage_id IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_ha_chat_lease_storage ON ha_chat_session_leases(storage_id,doc_id,session_id)",
)
_FEEDBACK_CONTROL_TABLES = (
    "ha_derived_knowledge_events",
    "ha_derived_knowledge_sequence",
    "ha_feedback_analysis",
    "ha_feedback_entries",
    "ha_retrieval_eval_drafts",
    "ha_retrieval_feedback",
)
_FEEDBACK_CONTROL_DDL = (
    """CREATE TABLE IF NOT EXISTS ha_feedback_entries (
    feedback_id TEXT PRIMARY KEY,kb_id TEXT,trace_id TEXT,session_id TEXT,
    feedback TEXT,feedback_type TEXT,is_bad_case INTEGER NOT NULL,
    quick_key TEXT UNIQUE,created_at TEXT NOT NULL,data TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_ha_feedback_kb_created ON ha_feedback_entries(kb_id,created_at DESC,feedback_id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ha_feedback_trace ON ha_feedback_entries(kb_id,trace_id)",
    """CREATE TABLE IF NOT EXISTS ha_feedback_analysis (
    feedback_analysis_id TEXT PRIMARY KEY,feedback_id TEXT,kb_id TEXT,
    trace_id TEXT,recommended_action TEXT,needs_review INTEGER,
    confidence DOUBLE PRECISION NOT NULL,created_at TEXT NOT NULL,data TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_ha_feedback_analysis_kb_created ON ha_feedback_analysis(kb_id,created_at DESC,feedback_analysis_id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ha_feedback_analysis_filters ON ha_feedback_analysis(kb_id,feedback_id,trace_id,recommended_action,needs_review)",
    """CREATE TABLE IF NOT EXISTS ha_retrieval_feedback (
    retrieval_feedback_id TEXT PRIMARY KEY,feedback_id TEXT NOT NULL UNIQUE,
    feedback_group_key TEXT NOT NULL,kb_id TEXT NOT NULL,query_hash TEXT NOT NULL,
    enabled INTEGER NOT NULL,created_at TEXT NOT NULL,data TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_ha_retrieval_feedback_kb_created ON ha_retrieval_feedback(kb_id,created_at DESC,retrieval_feedback_id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ha_retrieval_feedback_boosts ON ha_retrieval_feedback(kb_id,query_hash,enabled)",
    "CREATE INDEX IF NOT EXISTS idx_ha_retrieval_feedback_group ON ha_retrieval_feedback(feedback_group_key)",
    """CREATE TABLE IF NOT EXISTS ha_retrieval_eval_drafts (
    draft_id TEXT PRIMARY KEY,dedupe_key TEXT NOT NULL UNIQUE,
    snapshot_key TEXT NOT NULL UNIQUE,kb_id TEXT NOT NULL,status TEXT NOT NULL,
    dataset_partition TEXT NOT NULL,updated_at TEXT NOT NULL,data TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_ha_retrieval_eval_queue ON ha_retrieval_eval_drafts(kb_id,dataset_partition,status,updated_at DESC,draft_id DESC)",
    """CREATE TABLE IF NOT EXISTS ha_derived_knowledge_sequence (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),last_value BIGINT NOT NULL)""",
    "INSERT INTO ha_derived_knowledge_sequence(singleton,last_value) VALUES(1,0) ON CONFLICT(singleton) DO NOTHING",
    """CREATE TABLE IF NOT EXISTS ha_derived_knowledge_events (
    event_sequence BIGINT PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,
    knowledge_id TEXT NOT NULL,kb_id TEXT NOT NULL,status TEXT NOT NULL,
    normalized_hash TEXT,conflict_group_id TEXT,related_document_id TEXT,
    related_source TEXT,origin TEXT,created_by TEXT,created_at TEXT NOT NULL,
    reviewed_at TEXT,record_json TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_ha_knowledge_latest ON ha_derived_knowledge_events(knowledge_id,event_sequence DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ha_knowledge_kb ON ha_derived_knowledge_events(kb_id,event_sequence DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ha_knowledge_status ON ha_derived_knowledge_events(kb_id,status,event_sequence DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ha_knowledge_hash ON ha_derived_knowledge_events(kb_id,normalized_hash,event_sequence DESC)",
)
_DERIVED_INDEX_REFRESH_TABLES = ("ha_derived_knowledge_refreshes",)
_DERIVED_INDEX_REFRESH_DDL = (
    """CREATE TABLE IF NOT EXISTS ha_derived_knowledge_refreshes (
    kb_id TEXT PRIMARY KEY,requested_sequence BIGINT NOT NULL,
    status TEXT NOT NULL,lease_owner TEXT,lease_token TEXT,
    lease_expires_at DOUBLE PRECISION,attempts BIGINT NOT NULL DEFAULT 0,
    last_error TEXT,updated_at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_ha_knowledge_refresh_recovery "
    "ON ha_derived_knowledge_refreshes(status,lease_expires_at,updated_at,kb_id)",
)
REGISTERED_MIGRATIONS = (
    Migration(
        version=1,
        name="HA control-plane baseline",
        checksum=hashlib.sha256(_BASELINE_CONTRACT.encode()).hexdigest(),
        validate=_baseline_is_present,
    ),
    Migration(
        version=2,
        name="HA shared API source plane",
        checksum=hashlib.sha256(
            ("cogdoc-ha-shared-api-source-v2:" + "\n".join(_SHARED_API_DDL)).encode()
        ).hexdigest(),
        expand=_SHARED_API_DDL,
        validate=lambda backend: _tables_are_present(backend, _SHARED_API_TABLES),
    ),
    Migration(
        version=3,
        name="HA shared connector control plane",
        checksum=hashlib.sha256(
            (
                "cogdoc-ha-connector-control-v3:" + "\n".join(_CONNECTOR_CONTROL_DDL)
            ).encode()
        ).hexdigest(),
        expand=_CONNECTOR_CONTROL_DDL,
        validate=lambda backend: _tables_are_present(
            backend, _CONNECTOR_CONTROL_TABLES
        ),
    ),
    Migration(
        version=4,
        name="HA shared identity and access plane",
        checksum=hashlib.sha256(
            ("cogdoc-ha-identity-access-v4:" + "\n".join(IDENTITY_ACCESS_DDL)).encode()
        ).hexdigest(),
        expand=IDENTITY_ACCESS_DDL,
        validate=lambda backend: _tables_are_present(backend, IDENTITY_ACCESS_TABLES),
    ),
    Migration(
        version=5,
        name="HA shared research execution plane",
        checksum=hashlib.sha256(
            (
                "cogdoc-ha-research-control-v5:" + "\n".join(_RESEARCH_CONTROL_DDL)
            ).encode()
        ).hexdigest(),
        expand=_RESEARCH_CONTROL_DDL,
        validate=lambda backend: _tables_are_present(backend, _RESEARCH_CONTROL_TABLES),
    ),
    Migration(
        version=6,
        name="HA shared chat memory and index read leases",
        checksum=hashlib.sha256(
            ("cogdoc-ha-chat-control-v6:" + "\n".join(_CHAT_CONTROL_DDL)).encode()
        ).hexdigest(),
        expand=_CHAT_CONTROL_DDL,
        validate=lambda backend: _tables_are_present(backend, _CHAT_CONTROL_TABLES),
    ),
    Migration(
        version=7,
        name="HA chat scope identity fencing",
        checksum=hashlib.sha256(
            (
                "cogdoc-ha-chat-scope-identity-v7:"
                + "\n".join(_CHAT_SCOPE_IDENTITY_DDL)
            ).encode()
        ).hexdigest(),
        expand=_CHAT_SCOPE_IDENTITY_DDL,
        validate=_chat_scope_identity_is_present,
    ),
    Migration(
        version=8,
        name="HA shared feedback control plane",
        checksum=hashlib.sha256(
            (
                "cogdoc-ha-feedback-control-v8:" + "\n".join(_FEEDBACK_CONTROL_DDL)
            ).encode()
        ).hexdigest(),
        expand=_FEEDBACK_CONTROL_DDL,
        validate=lambda backend: _tables_are_present(backend, _FEEDBACK_CONTROL_TABLES),
    ),
    Migration(
        version=9,
        name="HA durable derived knowledge index refreshes",
        checksum=hashlib.sha256(
            (
                "cogdoc-ha-derived-index-refresh-v9:"
                + "\n".join(_DERIVED_INDEX_REFRESH_DDL)
                + ":backfill-v8-ledgers"
            ).encode()
        ).hexdigest(),
        expand=_DERIVED_INDEX_REFRESH_DDL,
        backfill=_backfill_derived_index_refreshes,
        validate=_derived_index_refresh_is_present,
    ),
)


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MINIMUM_SCHEMA_VERSION",
    "REGISTERED_MIGRATIONS",
    "migrations_are_current",
]
