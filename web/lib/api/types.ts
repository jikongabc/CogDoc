export type WorkspaceRole = "owner" | "admin" | "editor" | "reviewer" | "viewer";
export type Permission = "read" | "query" | "write" | "delete" | "review" | "publish" | "manage_access" | "manage_tenant";

export interface AuthConfig {
  schema_version: "v1";
  account_auth_enabled: boolean;
  self_registration_enabled: boolean;
  oidc_enabled: boolean;
  oidc_display_name: string;
  scim_enabled: boolean;
}

export interface AuthUser {
  user_id: string;
  email: string;
  display_name: string;
  created_at?: string;
  updated_at?: string;
}

export interface Workspace {
  workspace_id: string;
  name: string;
  role: WorkspaceRole;
  revision: number;
  created_at?: string;
  updated_at?: string;
}

export interface AuthSession {
  schema_version: "v1";
  access_token: string;
  token_type: "bearer";
  expires_at: string;
  user: AuthUser;
  workspace: Workspace;
  permissions: string[];
}

export interface AuthMe {
  schema_version: "v1";
  user: AuthUser;
  workspace: Workspace;
  permissions: string[];
  workspaces: Workspace[];
}

export interface OidcStartResponse {
  schema_version: "v1";
  flow_id: string;
  authorization_url: string;
  expires_at: number;
}

export interface OidcExchangeResponse {
  schema_version: "v1";
  kind: "login" | "link";
  session?: AuthSession;
}

export interface KnowledgeBase {
  kb_id: string;
  created_at: string;
  document_count: number;
  tenant_id: string;
  owner_id: string;
}

export interface Document {
  name: string;
  sha256: string;
  document_id: string;
  source_id: string;
  version_id: string;
  connector_type: string;
  media_type: string;
  kind: string;
  origin_uri?: string | null;
}

export type IndexJobStatus = "pending" | "running" | "succeeded" | "failed";

export interface OcrSummary {
  candidate_pages: number;
  attempted_pages: number;
  succeeded_pages: number;
  degraded_pages: number;
  failed_pages: number;
  status_counts: Record<string, number>;
}

export interface IndexJob {
  job_id: string;
  kb_id: string;
  status: IndexJobStatus;
  created_at: string;
  finished_at?: string | null;
  document_count?: number | null;
  chunk_count?: number | null;
  ocr_summary?: OcrSummary | null;
  error_code?: string | null;
  message?: string | null;
}

export interface CitationOccurrence {
  index: number;
  answer_start: number;
  answer_end: number;
}

export interface SourceLocation {
  [key: string]: unknown;
}

export interface Citation {
  chunk_id: string;
  source_type: string;
  knowledge_id: string;
  source: string;
  source_id?: string;
  source_version_id?: string;
  media_type?: string;
  location?: SourceLocation;
  page?: number | null;
  page_start?: number | null;
  page_end?: number | null;
}

export interface CitationLedgerEntry extends Citation {
  evidence_id: string;
  span_start: number;
  span_end: number;
  occurrences: CitationOccurrence[];
}

export interface Evidence extends Citation {
  parent_chunk_id: string;
  section_title: string;
  section_path: string;
  section_level?: number | null;
  child_index_in_parent?: number | null;
  chunk_index?: number | null;
  rerank_score?: number | null;
  rewrite_query?: string | null;
  text_preview: string;
  retrieval: Record<string, unknown>;
}

export interface ClaimVerificationSummary {
  policy_id?: string;
  decision?: string;
  effective_mode?: string;
  released?: boolean;
  [key: string]: unknown;
}

export interface ChatResponse {
  schema_version: "v1";
  request_id: string;
  trace_id: string;
  doc_id: string;
  session_id?: string | null;
  task_type: "qa" | "summary" | "compare" | "unknown";
  answer: string;
  citations: Citation[];
  citation_ledger: CitationLedgerEntry[];
  evidence: Evidence[];
  critique: string;
  is_valid: boolean;
  claim_audit?: Record<string, unknown> | null;
  claim_verification?: ClaimVerificationSummary | null;
}

export interface SessionSummary {
  session_id: string;
  title: string;
  message_count: number;
}

export interface SessionListResponse {
  schema_version: "v1";
  doc_id: string;
  sessions: SessionSummary[];
}

export interface HistoryMessage {
  role: "user" | "assistant" | string;
  content: string;
  trace_id?: string;
  query?: string;
  task_type?: string;
  citations?: Citation[];
  citation_ledger?: CitationLedgerEntry[];
  evidence?: Evidence[];
  is_valid?: boolean;
  [key: string]: unknown;
}

export interface SessionHistoryResponse {
  schema_version: "v1";
  session_id: string;
  doc_id: string;
  messages: HistoryMessage[];
}

export interface TraceResponse {
  schema_version: "v1";
  trace_id: string;
  request_id: string;
  task_type: string;
  status: string;
  execution_status: string;
  duration_ms?: number | null;
  evidence_completeness?: number | null;
  steps?: Record<string, unknown>[];
  output: Record<string, unknown>;
}

export interface FeedbackResponse {
  schema_version: "v1";
  feedback_id: string;
  status: string;
  is_bad_case: boolean;
}

export interface ApiErrorBody {
  schema_version?: "v1";
  error_code?: string;
  message?: string;
  request_id?: string | null;
  trace_id?: string | null;
  details?: Record<string, unknown> | null;
}

export type ChatMode = "auto" | "qa" | "summary" | "compare";

export type StreamEvent =
  | { type: "start"; data: Record<string, unknown> }
  | { type: "node"; data: { stage?: string; [key: string]: unknown } }
  | { type: "token"; data: { content?: string } }
  | { type: "final"; data: ChatResponse }
  | { type: "error"; data: ApiErrorBody };
