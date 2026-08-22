from __future__ import annotations


IDENTITY_ACCESS_TABLES = (
    "auth_schema_meta",
    "auth_users",
    "auth_workspaces",
    "auth_memberships",
    "auth_sessions",
    "auth_invites",
    "auth_password_capabilities",
    "auth_oidc_identities",
    "auth_workspace_oidc_policies",
    "auth_oidc_managed_memberships",
    "auth_scim_users",
    "auth_scim_groups",
    "auth_scim_group_members",
    "auth_service_accounts",
    "auth_service_tokens",
    "auth_service_account_policies",
    "auth_workspace_session_policies",
    "auth_oidc_flow_meta",
    "auth_oidc_flows",
    "ha_oidc_flow_keys",
    "ha_identity_config",
    "resource_access_kb_policies",
    "resource_access_document_policies",
    "resource_access_subject_grants",
    "resource_access_acl_epochs",
    "resource_access_membership_tombstones",
    "resource_access_subject_locks",
    "resource_access_retiring_documents",
    "external_acl_sync_state",
)


_IDENTITY_ACCESS_DDL_RAW = (
    "CREATE TABLE IF NOT EXISTS auth_schema_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS auth_users (
    user_id TEXT PRIMARY KEY,email TEXT NOT NULL UNIQUE,display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,personal_workspace_id TEXT NOT NULL,
    failed_login_count INTEGER NOT NULL DEFAULT 0 CHECK(failed_login_count>=0),
    locked_until REAL,created_at REAL NOT NULL,updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS auth_workspaces (
    workspace_id TEXT PRIMARY KEY,name TEXT NOT NULL,personal_owner_user_id TEXT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),created_at REAL NOT NULL,
    updated_at REAL NOT NULL,FOREIGN KEY(personal_owner_user_id)
    REFERENCES auth_users(user_id))""",
    """CREATE TABLE IF NOT EXISTS auth_memberships (
    member_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner','admin','editor','reviewer','viewer')),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),joined_at REAL NOT NULL,
    updated_at REAL NOT NULL,UNIQUE(workspace_id,user_id),FOREIGN KEY(workspace_id)
    REFERENCES auth_workspaces(workspace_id) ON DELETE CASCADE,FOREIGN KEY(user_id)
    REFERENCES auth_users(user_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id TEXT PRIMARY KEY,token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash)=64),
    user_id TEXT NOT NULL,active_workspace_id TEXT,created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,expires_at REAL NOT NULL,revoked_at REAL,
    FOREIGN KEY(user_id) REFERENCES auth_users(user_id) ON DELETE CASCADE,
    FOREIGN KEY(active_workspace_id) REFERENCES auth_workspaces(workspace_id)
    ON DELETE SET NULL)""",
    """CREATE TABLE IF NOT EXISTS auth_invites (
    invite_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,email TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','editor','reviewer','viewer')),
    token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash)=64),created_by TEXT NOT NULL,
    created_at REAL NOT NULL,expires_at REAL NOT NULL,accepted_at REAL,
    accepted_by TEXT,revoked_at REAL,FOREIGN KEY(workspace_id)
    REFERENCES auth_workspaces(workspace_id) ON DELETE CASCADE,
    FOREIGN KEY(created_by) REFERENCES auth_users(user_id),
    FOREIGN KEY(accepted_by) REFERENCES auth_users(user_id))""",
    """CREATE TABLE IF NOT EXISTS auth_password_capabilities (
    user_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    updated_at REAL NOT NULL,FOREIGN KEY(user_id) REFERENCES auth_users(user_id)
    ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS auth_oidc_identities (
    identity_id TEXT PRIMARY KEY,issuer TEXT NOT NULL,subject TEXT NOT NULL,
    user_id TEXT NOT NULL,email_at_link TEXT NOT NULL,created_at REAL NOT NULL,
    last_login_at REAL NOT NULL,UNIQUE(issuer,subject),UNIQUE(user_id,issuer),
    FOREIGN KEY(user_id) REFERENCES auth_users(user_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS auth_workspace_oidc_policies (
    workspace_id TEXT PRIMARY KEY,issuer TEXT NOT NULL,allowed_domains_json TEXT NOT NULL,
    default_role TEXT NOT NULL CHECK(default_role IN ('admin','editor','reviewer','viewer')),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),group_claim TEXT NOT NULL DEFAULT 'groups',
    group_role_map_json TEXT NOT NULL DEFAULT '{}',require_mapped_group INTEGER NOT NULL
    DEFAULT 0 CHECK(require_mapped_group IN (0,1)),revision INTEGER NOT NULL DEFAULT 0
    CHECK(revision>=0),created_at REAL NOT NULL,updated_at REAL NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS auth_oidc_managed_memberships (
    member_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,user_id TEXT NOT NULL,
    issuer TEXT NOT NULL,subject TEXT NOT NULL,policy_revision INTEGER NOT NULL
    CHECK(policy_revision>=0),updated_at REAL NOT NULL,UNIQUE(workspace_id,user_id),
    FOREIGN KEY(member_id) REFERENCES auth_memberships(member_id) ON DELETE CASCADE,
    FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES auth_users(user_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS auth_scim_users (
    scim_user_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,external_id TEXT,
    user_name TEXT NOT NULL,display_name TEXT NOT NULL,user_id TEXT NOT NULL,member_id TEXT,
    issuer TEXT NOT NULL,active INTEGER NOT NULL CHECK(active IN (0,1)),base_role TEXT NOT NULL
    CHECK(base_role IN ('admin','editor','reviewer','viewer')),revision INTEGER NOT NULL DEFAULT 1
    CHECK(revision>=1),created_at REAL NOT NULL,updated_at REAL NOT NULL,deleted_at REAL,
    FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES auth_users(user_id) ON DELETE CASCADE,
    FOREIGN KEY(member_id) REFERENCES auth_memberships(member_id) ON DELETE SET NULL)""",
    """CREATE TABLE IF NOT EXISTS auth_scim_groups (
    scim_group_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,external_id TEXT,
    display_name TEXT NOT NULL,mapped_role TEXT CHECK(mapped_role IN
    ('admin','editor','reviewer','viewer')),revision INTEGER NOT NULL DEFAULT 1
    CHECK(revision>=1),created_at REAL NOT NULL,updated_at REAL NOT NULL,deleted_at REAL,
    FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS auth_scim_group_members (
    scim_group_id TEXT NOT NULL,scim_user_id TEXT NOT NULL,created_at REAL NOT NULL,
    PRIMARY KEY(scim_group_id,scim_user_id),FOREIGN KEY(scim_group_id)
    REFERENCES auth_scim_groups(scim_group_id) ON DELETE CASCADE,
    FOREIGN KEY(scim_user_id) REFERENCES auth_scim_users(scim_user_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS auth_service_accounts (
    service_account_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',role TEXT NOT NULL CHECK(role IN
    ('admin','editor','reviewer','viewer')),active INTEGER NOT NULL CHECK(active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),created_by TEXT NOT NULL,
    created_at REAL NOT NULL,updated_at REAL NOT NULL,deleted_at REAL,
    FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id) ON DELETE CASCADE,
    FOREIGN KEY(created_by) REFERENCES auth_users(user_id))""",
    """CREATE TABLE IF NOT EXISTS auth_service_tokens (
    token_id TEXT PRIMARY KEY,service_account_id TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE
    CHECK(length(token_hash)=64),label TEXT NOT NULL,secret_hint TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),created_at REAL NOT NULL,
    expires_at REAL,last_used_at REAL,revoked_at REAL,permissions_json TEXT,
    FOREIGN KEY(service_account_id) REFERENCES auth_service_accounts(service_account_id)
    ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS auth_service_account_policies (
    workspace_id TEXT PRIMARY KEY,max_accounts INTEGER NOT NULL CHECK(max_accounts BETWEEN 1 AND 500),
    max_tokens_per_account INTEGER NOT NULL CHECK(max_tokens_per_account BETWEEN 1 AND 50),
    max_token_ttl_days INTEGER NOT NULL CHECK(max_token_ttl_days BETWEEN 1 AND 365),
    allow_non_expiring INTEGER NOT NULL CHECK(allow_non_expiring IN (0,1)),
    allowed_permissions_json TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
    created_at REAL NOT NULL,updated_at REAL NOT NULL,FOREIGN KEY(workspace_id)
    REFERENCES auth_workspaces(workspace_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS auth_workspace_session_policies (
    workspace_id TEXT PRIMARY KEY,idle_timeout_minutes INTEGER CHECK(idle_timeout_minutes IS NULL
    OR idle_timeout_minutes BETWEEN 5 AND 43200),absolute_timeout_hours INTEGER
    CHECK(absolute_timeout_hours IS NULL OR absolute_timeout_hours BETWEEN 1 AND 8760),
    max_active_sessions INTEGER CHECK(max_active_sessions IS NULL OR max_active_sessions BETWEEN 1 AND 50),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),created_at REAL NOT NULL,
    updated_at REAL NOT NULL,FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id)
    ON DELETE CASCADE)""",
    "CREATE TABLE IF NOT EXISTS auth_oidc_flow_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS auth_oidc_flows (
    flow_id TEXT PRIMARY KEY,state_hash TEXT NOT NULL UNIQUE CHECK(length(state_hash)=64),
    intent TEXT NOT NULL CHECK(intent IN ('login','link')),encrypted_context BYTEA NOT NULL,
    return_url TEXT NOT NULL,workspace_id TEXT,user_id TEXT,session_id TEXT,
    created_at REAL NOT NULL,expires_at REAL NOT NULL,consumed_at REAL,
    result_code_hash TEXT UNIQUE,encrypted_result BYTEA,result_expires_at REAL,
    result_consumed_at REAL,CHECK((intent='login' AND user_id IS NULL AND session_id IS NULL)
    OR (intent='link' AND user_id IS NOT NULL AND session_id IS NOT NULL)))""",
    """CREATE TABLE IF NOT EXISTS ha_oidc_flow_keys (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),key_fingerprint TEXT NOT NULL,
    registered_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ha_identity_config (
    config_name TEXT PRIMARY KEY,config_version BIGINT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    registered_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS resource_access_kb_policies (
    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,owner_id TEXT NOT NULL,
    owner_membership_id TEXT,policy TEXT NOT NULL CHECK(policy IN ('workspace','private')),
    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(tenant_id,kb_id))""",
    """CREATE TABLE IF NOT EXISTS resource_access_document_policies (
    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,document_id TEXT NOT NULL,source TEXT NOT NULL,
    owner_id TEXT NOT NULL,owner_membership_id TEXT,policy TEXT NOT NULL
    CHECK(policy IN ('workspace','private','inherit')),created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,PRIMARY KEY(tenant_id,kb_id,document_id),
    UNIQUE(tenant_id,kb_id,source))""",
    """CREATE TABLE IF NOT EXISTS resource_access_subject_grants (
    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,document_key TEXT NOT NULL,
    subject_id TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN
    ('owner','admin','editor','reviewer','viewer')),managed_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
    PRIMARY KEY(tenant_id,kb_id,document_key,subject_id))""",
    """CREATE TABLE IF NOT EXISTS resource_access_acl_epochs (
    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,epoch INTEGER NOT NULL CHECK(epoch>=0),
    updated_at TEXT NOT NULL,PRIMARY KEY(tenant_id,kb_id))""",
    """CREATE TABLE IF NOT EXISTS resource_access_membership_tombstones (
    tenant_id TEXT NOT NULL,subject_id TEXT NOT NULL,membership_id TEXT NOT NULL,
    revoked_at TEXT NOT NULL,PRIMARY KEY(tenant_id,subject_id,membership_id))""",
    """CREATE TABLE IF NOT EXISTS resource_access_subject_locks (
    tenant_id TEXT NOT NULL,subject_id TEXT NOT NULL,updated_at TEXT NOT NULL,
    PRIMARY KEY(tenant_id,subject_id))""",
    """CREATE TABLE IF NOT EXISTS resource_access_retiring_documents (
    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,document_id TEXT NOT NULL,
    managed_by TEXT NOT NULL,started_at TEXT NOT NULL,PRIMARY KEY(tenant_id,kb_id,document_id))""",
    """CREATE TABLE IF NOT EXISTS external_acl_sync_state (
    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,document_id TEXT NOT NULL,
    managed_by TEXT NOT NULL,status TEXT NOT NULL,acl_fingerprint TEXT NOT NULL,
    provider_version TEXT,resolved_count INTEGER NOT NULL,unresolved_count INTEGER NOT NULL,
    updated_at REAL NOT NULL,PRIMARY KEY(tenant_id,kb_id,document_id,managed_by))""",
    "CREATE INDEX IF NOT EXISTS idx_auth_memberships_user ON auth_memberships(user_id,workspace_id)",
    "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id,created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_auth_sessions_workspace_activity ON auth_sessions(active_workspace_id,created_at DESC,session_id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_auth_invites_workspace ON auth_invites(workspace_id,created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_auth_invites_email ON auth_invites(email,created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_auth_oidc_identities_user ON auth_oidc_identities(user_id,created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_scim_users_external_active ON auth_scim_users(workspace_id,external_id) WHERE external_id IS NOT NULL AND deleted_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_scim_users_name_active ON auth_scim_users(workspace_id,user_name) WHERE deleted_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_auth_scim_users_user ON auth_scim_users(user_id,workspace_id,updated_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_scim_groups_external_active ON auth_scim_groups(workspace_id,external_id) WHERE external_id IS NOT NULL AND deleted_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_scim_groups_name_active ON auth_scim_groups(workspace_id,display_name) WHERE deleted_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_service_accounts_name_active ON auth_service_accounts(workspace_id,name) WHERE deleted_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_auth_service_accounts_workspace ON auth_service_accounts(workspace_id,created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_auth_service_tokens_account ON auth_service_tokens(service_account_id,created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_auth_oidc_flows_expiry ON auth_oidc_flows(expires_at,result_expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_resource_access_documents_tenant_kb ON resource_access_document_policies(tenant_id,kb_id,document_id)",
    "CREATE INDEX IF NOT EXISTS idx_resource_access_grants_subject ON resource_access_subject_grants(tenant_id,kb_id,subject_id,document_key)",
    "CREATE INDEX IF NOT EXISTS idx_resource_access_grants_tenant_subject ON resource_access_subject_grants(tenant_id,subject_id,kb_id)",
)

# SQLite's REAL is 64-bit, while PostgreSQL REAL is only 32-bit and cannot
# represent current epoch seconds with security-policy precision. DOUBLE
# PRECISION is accepted by both engines and preserves session/OIDC deadlines.
IDENTITY_ACCESS_DDL = tuple(
    statement.replace(" REAL", " DOUBLE PRECISION")
    for statement in _IDENTITY_ACCESS_DDL_RAW
)


__all__ = ["IDENTITY_ACCESS_DDL", "IDENTITY_ACCESS_TABLES"]
