"use client";

import { apiFetch } from "@/lib/api/client";
import type { AuthSession, Workspace } from "@/lib/api/types";

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonRecord | JsonValue[];
export interface JsonRecord {
  [key: string]: JsonValue | undefined;
}

function pathPart(value: string) {
  return encodeURIComponent(value);
}

function query(params: Record<string, string | number | boolean | null | undefined>) {
  const values = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") values.set(key, String(value));
  });
  const encoded = values.toString();
  return encoded ? `?${encoded}` : "";
}

function json(method: "POST" | "PUT" | "PATCH", body?: JsonRecord): RequestInit {
  return { method, ...(body ? { body: JSON.stringify(body) } : {}) };
}

export function records(value: unknown, keys: string[] = ["items"]): JsonRecord[] {
  if (Array.isArray(value)) return value.filter(isRecord);
  if (!isRecord(value)) return [];
  for (const key of keys) {
    const nested = value[key];
    if (Array.isArray(nested)) return nested.filter(isRecord);
  }
  return [];
}

export function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function textValue(value: JsonValue | undefined, fallback = "—") {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

export function numberValue(value: JsonValue | undefined, fallback = 0) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export const controlApi = {
  workspaces: () => apiFetch<Workspace[]>("/workspaces"),
  createWorkspace: (name: string) => apiFetch<Workspace>("/workspaces", json("POST", { name })),
  updateWorkspace: (workspaceId: string, name: string, expectedRevision?: number) =>
    apiFetch<Workspace>(
      `/workspaces/${pathPart(workspaceId)}`,
      json("PATCH", { name, expected_revision: expectedRevision }),
    ),
  deleteWorkspace: (workspaceId: string) =>
    apiFetch<void>(`/workspaces/${pathPart(workspaceId)}`, { method: "DELETE" }),
  members: (workspaceId: string) =>
    apiFetch<unknown>(`/workspaces/${pathPart(workspaceId)}/members`),
  updateMember: (workspaceId: string, memberId: string, role: string, expectedRevision?: number) =>
    apiFetch<unknown>(
      `/workspaces/${pathPart(workspaceId)}/members/${pathPart(memberId)}`,
      json("PATCH", { role, expected_revision: expectedRevision }),
    ),
  removeMember: (workspaceId: string, memberId: string) =>
    apiFetch<void>(
      `/workspaces/${pathPart(workspaceId)}/members/${pathPart(memberId)}`,
      { method: "DELETE" },
    ),
  invites: (workspaceId: string) =>
    apiFetch<unknown>(`/workspaces/${pathPart(workspaceId)}/invites`),
  createInvite: (workspaceId: string, email: string, role: string) =>
    apiFetch<unknown>(
      `/workspaces/${pathPart(workspaceId)}/invites`,
      json("POST", { email, role }),
    ),
  revokeInvite: (workspaceId: string, inviteId: string) =>
    apiFetch<void>(
      `/workspaces/${pathPart(workspaceId)}/invites/${pathPart(inviteId)}`,
      { method: "DELETE" },
    ),
  acceptAuthenticatedInvite: (token: string) =>
    apiFetch<AuthSession>("/auth/invitations/accept", json("POST", { token })),

  authSessions: () => apiFetch<unknown>("/auth/sessions"),
  revokeAuthSession: (sessionId: string) =>
    apiFetch<void>(`/auth/sessions/${pathPart(sessionId)}`, { method: "DELETE" }),
  changePassword: (currentPassword: string, newPassword: string) =>
    apiFetch<void>(
      "/auth/change-password",
      json("POST", { current_password: currentPassword, new_password: newPassword }),
    ),
  logoutAll: () => apiFetch<void>("/auth/logout-all", { method: "POST" }),
  securitySessions: (workspaceId: string, includeInactive = false) =>
    apiFetch<unknown>(
      `/workspaces/${pathPart(workspaceId)}/security-sessions${query({ limit: 100, include_inactive: includeInactive })}`,
    ),
  revokeSecuritySession: (workspaceId: string, sessionId: string) =>
    apiFetch<void>(
      `/workspaces/${pathPart(workspaceId)}/security-sessions/${pathPart(sessionId)}`,
      { method: "DELETE" },
    ),
  sessionPolicy: (workspaceId: string) =>
    apiFetch<unknown>(`/workspaces/${pathPart(workspaceId)}/session-policy`),
  updateSessionPolicy: (workspaceId: string, payload: JsonRecord) =>
    apiFetch<unknown>(
      `/workspaces/${pathPart(workspaceId)}/session-policy`,
      json("PUT", payload),
    ),

  oidcIdentities: () => apiFetch<unknown>("/auth/oidc/identities"),
  startOidcLink: (returnUrl: string) =>
    apiFetch<unknown>("/auth/oidc/link/authorize", json("POST", { return_url: returnUrl })),
  unlinkOidcIdentity: (identityId: string) =>
    apiFetch<void>(`/auth/oidc/identities/${pathPart(identityId)}`, { method: "DELETE" }),
  oidcPolicy: (workspaceId: string) =>
    apiFetch<unknown>(`/workspaces/${pathPart(workspaceId)}/oidc-policy`),
  updateOidcPolicy: (workspaceId: string, payload: JsonRecord) =>
    apiFetch<unknown>(
      `/workspaces/${pathPart(workspaceId)}/oidc-policy`,
      json("PUT", payload),
    ),
  scimStatus: (workspaceId: string) =>
    apiFetch<unknown>(`/workspaces/${pathPart(workspaceId)}/scim-status`),

  serviceAccounts: (workspaceId: string) =>
    apiFetch<unknown>(`/workspaces/${pathPart(workspaceId)}/service-accounts`),
  createServiceAccount: (workspaceId: string, payload: JsonRecord) =>
    apiFetch<unknown>(
      `/workspaces/${pathPart(workspaceId)}/service-accounts`,
      json("POST", payload),
    ),
  updateServiceAccount: (workspaceId: string, accountId: string, payload: JsonRecord) =>
    apiFetch<unknown>(
      `/workspaces/${pathPart(workspaceId)}/service-accounts/${pathPart(accountId)}`,
      json("PATCH", payload),
    ),
  deleteServiceAccount: (workspaceId: string, accountId: string, expectedRevision: number) =>
    apiFetch<void>(
      `/workspaces/${pathPart(workspaceId)}/service-accounts/${pathPart(accountId)}${query({ expected_revision: expectedRevision })}`,
      { method: "DELETE" },
    ),
  serviceAccountPolicy: (workspaceId: string) =>
    apiFetch<unknown>(`/workspaces/${pathPart(workspaceId)}/service-account-policy`),
  serviceTokens: (workspaceId: string, accountId: string) =>
    apiFetch<unknown>(
      `/workspaces/${pathPart(workspaceId)}/service-accounts/${pathPart(accountId)}/tokens`,
    ),
  createServiceToken: (workspaceId: string, accountId: string, payload: JsonRecord) =>
    apiFetch<unknown>(
      `/workspaces/${pathPart(workspaceId)}/service-accounts/${pathPart(accountId)}/tokens`,
      json("POST", payload),
    ),
  revokeServiceToken: (
    workspaceId: string,
    accountId: string,
    tokenId: string,
    expectedRevision: number,
  ) =>
    apiFetch<void>(
      `/workspaces/${pathPart(workspaceId)}/service-accounts/${pathPart(accountId)}/tokens/${pathPart(tokenId)}${query({ expected_revision: expectedRevision })}`,
      { method: "DELETE" },
    ),

  auditEvents: (limit = 100) => apiFetch<unknown>(`/audit-events${query({ limit })}`),
  auditExports: (limit = 100) =>
    apiFetch<unknown>(`/audit-events/exports${query({ limit })}`),
  createAuditExport: (payload: JsonRecord) =>
    apiFetch<unknown>("/audit-events/exports", json("POST", payload)),
  deleteAuditExport: (jobId: string, expectedRevision: number) =>
    apiFetch<void>(
      `/audit-events/exports/${pathPart(jobId)}${query({ expected_revision: expectedRevision })}`,
      { method: "DELETE" },
    ),

  researchJobs: (kbId: string, status?: string) =>
    apiFetch<unknown>(`/research-jobs${query({ kb_id: kbId, status, limit: 100 })}`),
  researchSummaries: (kbId: string, status?: string) =>
    apiFetch<unknown>(
      `/research-jobs/summaries${query({ kb_id: kbId, status, limit: 100 })}`,
    ),
  createResearchJob: (payload: JsonRecord) =>
    apiFetch<unknown>("/research-jobs", json("POST", payload)),
  researchJob: (jobId: string) => apiFetch<unknown>(`/research-jobs/${pathPart(jobId)}`),
  researchAction: (jobId: string, action: string) =>
    apiFetch<unknown>(`/research-jobs/${pathPart(jobId)}/${pathPart(action)}`, {
      method: "POST",
    }),
  generateResearchPlan: (jobId: string, expectedRevision: number, isLocal: boolean) =>
    apiFetch<unknown>(
      `/research-jobs/${pathPart(jobId)}/plan/auto`,
      json("POST", { expected_revision: expectedRevision, is_local: isLocal }),
    ),
  updateResearchPlan: (jobId: string, expectedRevision: number, sections: JsonValue[]) =>
    apiFetch<unknown>(
      `/research-jobs/${pathPart(jobId)}/plan`,
      json("PUT", { expected_revision: expectedRevision, sections }),
    ),
  researchReport: (jobId: string) =>
    apiFetch<unknown>(`/research-jobs/${pathPart(jobId)}/report`),
  researchProvenance: (jobId: string) =>
    apiFetch<unknown>(`/research-jobs/${pathPart(jobId)}/provenance`),
  reviewResearch: (jobId: string, expectedRevision: number, decisions: JsonValue[]) =>
    apiFetch<unknown>(
      `/research-jobs/${pathPart(jobId)}/review`,
      json("PUT", { expected_revision: expectedRevision, decisions }),
    ),
  publishResearch: (jobId: string, expectedRevision: number) =>
    apiFetch<unknown>(
      `/research-jobs/${pathPart(jobId)}/publish`,
      json("POST", { expected_revision: expectedRevision }),
    ),

  knowledge: (kbId: string, status?: string) =>
    apiFetch<unknown>(`/knowledge${query({ kb_id: kbId, status })}`),
  createKnowledge: (payload: JsonRecord) =>
    apiFetch<unknown>("/knowledge", json("POST", payload)),
  reviewKnowledge: (knowledgeId: string, action: string, note?: string) =>
    apiFetch<unknown>(
      `/knowledge/${pathPart(knowledgeId)}/${pathPart(action)}`,
      json("POST", { note }),
    ),
  reviseKnowledge: (knowledgeId: string, payload: JsonRecord) =>
    apiFetch<unknown>(
      `/knowledge/${pathPart(knowledgeId)}/revise`,
      json("POST", payload),
    ),
  deleteKnowledge: (knowledgeId: string) =>
    apiFetch<void>(`/knowledge/${pathPart(knowledgeId)}`, { method: "DELETE" }),
  pendingKnowledge: (kbId: string) =>
    apiFetch<unknown>(`/knowledge/pending-count${query({ kb_id: kbId })}`),
  knowledgeIndexStatus: (kbId: string) =>
    apiFetch<unknown>(`/knowledge/index-status${query({ kb_id: kbId })}`),
  scanStaleKnowledge: (kbId: string) =>
    apiFetch<unknown>(`/knowledge/stale-scan${query({ kb_id: kbId })}`, {
      method: "POST",
    }),
  feedback: (kbId: string) =>
    apiFetch<unknown>(`/feedback${query({ kb_id: kbId, limit: 100 })}`),
  feedbackAnalysis: (kbId: string) =>
    apiFetch<unknown>(`/feedback-analysis${query({ kb_id: kbId, limit: 100 })}`),
  retrievalFeedback: (kbId: string, enabled?: boolean) =>
    apiFetch<unknown>(
      `/retrieval-feedback${query({ kb_id: kbId, enabled, limit: 100 })}`,
    ),
  setRetrievalFeedback: (feedbackId: string, enabled: boolean, reason?: string) =>
    apiFetch<unknown>(
      `/retrieval-feedback/${pathPart(feedbackId)}/${enabled ? "enable" : "disable"}`,
      enabled ? { method: "POST" } : json("POST", { reason }),
    ),
  reviewQueue: (kbId: string) =>
    apiFetch<unknown>(`/review-queue${query({ kb_id: kbId })}`),
  feedbackLoopMetrics: (kbId: string) =>
    apiFetch<unknown>(`/feedback-loop-metrics${query({ kb_id: kbId })}`),

  retrievalEvalDrafts: (kbId?: string, status?: string) =>
    apiFetch<unknown>(
      `/retrieval-eval-drafts${query({ kb_id: kbId, status, limit: 100 })}`,
    ),
  retrievalEvalDraft: (draftId: string) =>
    apiFetch<unknown>(`/retrieval-eval-drafts/${pathPart(draftId)}`),
  retrievalEvalCandidates: (draftId: string) =>
    apiFetch<unknown>(`/retrieval-eval-drafts/${pathPart(draftId)}/candidates?top_k=12`),
  reviewRetrievalEval: (draftId: string, payload: JsonRecord) =>
    apiFetch<unknown>(
      `/retrieval-eval-drafts/${pathPart(draftId)}/review`,
      json("POST", payload),
    ),
  claimReviewSummary: () => apiFetch<unknown>("/claim-verification/reviews/summary"),
  claimReviews: (status?: string) =>
    apiFetch<unknown>(
      `/claim-verification/reviews${query({ status, limit: 100 })}`,
    ),
  claimReview: (reviewId: string) =>
    apiFetch<unknown>(`/claim-verification/reviews/${pathPart(reviewId)}`),
  labelClaimReview: (reviewId: string, payload: JsonRecord) =>
    apiFetch<unknown>(
      `/claim-verification/reviews/${pathPart(reviewId)}/label`,
      json("POST", payload),
    ),

  connections: (kbId: string) =>
    apiFetch<unknown>(`/knowledge-bases/${pathPart(kbId)}/connections`),
  createConnection: (kbId: string, payload: JsonRecord) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/connections`,
      json("POST", payload),
    ),
  updateConnection: (kbId: string, connectionId: string, payload: JsonRecord) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/connections/${pathPart(connectionId)}`,
      json("PATCH", payload),
    ),
  deleteConnection: (kbId: string, connectionId: string) =>
    apiFetch<void>(
      `/knowledge-bases/${pathPart(kbId)}/connections/${pathPart(connectionId)}`,
      { method: "DELETE" },
    ),
  syncConnection: (kbId: string, connectionId: string) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/connections/${pathPart(connectionId)}/sync`,
      { method: "POST" },
    ),
  syncJobs: (kbId: string) =>
    apiFetch<unknown>(`/knowledge-bases/${pathPart(kbId)}/sync-jobs`),
  replaySyncJob: (kbId: string, jobId: string) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/sync-jobs/${pathPart(jobId)}/replay`,
      { method: "POST" },
    ),
  connectionHealth: (kbId: string) =>
    apiFetch<unknown>(`/knowledge-bases/${pathPart(kbId)}/connection-health`),
  connectorCredentials: (kbId: string) =>
    apiFetch<unknown>(`/knowledge-bases/${pathPart(kbId)}/connector-credentials`),
  createConnectorCredential: (kbId: string, payload: JsonRecord) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/connector-credentials`,
      json("POST", payload),
    ),
  rotateConnectorCredential: (kbId: string, credentialId: string, payload: JsonRecord) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/connector-credentials/${pathPart(credentialId)}`,
      json("PATCH", payload),
    ),
  refreshConnectorCredential: (kbId: string, credentialId: string, expectedRevision?: number) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/connector-credentials/${pathPart(credentialId)}/refresh${query({ expected_revision: expectedRevision })}`,
      { method: "POST" },
    ),
  deleteConnectorCredential: (kbId: string, credentialId: string, expectedRevision?: number) =>
    apiFetch<void>(
      `/knowledge-bases/${pathPart(kbId)}/connector-credentials/${pathPart(credentialId)}${query({ expected_revision: expectedRevision })}`,
      { method: "DELETE" },
    ),
  connectorCredentialEvents: (kbId: string, credentialId?: string) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/connector-credentials/audit/events${query({ credential_id: credentialId, limit: 100 })}`,
    ),
  authorizeConnectorOauth: (kbId: string, provider: string, connectionId?: string) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/connector-oauth/authorize`,
      json("POST", { provider, connection_id: connectionId }),
    ),
  sourceCatalog: (kbId: string, includeDeleted = false) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/source-catalog${query({ include_deleted: includeDeleted })}`,
    ),
  sourceVersions: (kbId: string, sourceId: string) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/source-catalog/${pathPart(sourceId)}/versions`,
    ),
  sourceVersionDiff: (kbId: string, sourceId: string, fromVersionId: string, toVersionId: string) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/source-catalog/${pathPart(sourceId)}/diff${query({ from_version_id: fromVersionId, to_version_id: toVersionId })}`,
    ),
  deleteSourceArtifact: (kbId: string, sourceId: string, versionId: string) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/source-catalog/${pathPart(sourceId)}/versions/${pathPart(versionId)}/artifact`,
      { method: "DELETE" },
    ),
  restoreSourceArtifact: (kbId: string, recoveryToken: string) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/source-artifacts/${pathPart(recoveryToken)}/restore`,
      { method: "POST" },
    ),
  sourceArtifactUsage: (kbId: string) =>
    apiFetch<unknown>(`/knowledge-bases/${pathPart(kbId)}/source-artifacts/usage`),

  kbAccess: (kbId: string) =>
    apiFetch<unknown>(`/knowledge-bases/${pathPart(kbId)}/access`),
  updateKbAccess: (kbId: string, policy: string) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/access`,
      json("PATCH", { schema_version: "v1", policy }),
    ),
  kbGrants: (kbId: string) =>
    apiFetch<unknown>(`/knowledge-bases/${pathPart(kbId)}/access/grants`),
  grantKbAccess: (kbId: string, subjectId: string, role: string) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/access/grants`,
      json("POST", { schema_version: "v1", subject_id: subjectId, role }),
    ),
  revokeKbAccess: (kbId: string, subjectId: string) =>
    apiFetch<void>(
      `/knowledge-bases/${pathPart(kbId)}/access/grants/${pathPart(subjectId)}`,
      { method: "DELETE" },
    ),
  documentAccess: (kbId: string, documentId: string) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/documents/${pathPart(documentId)}/access`,
    ),
  updateDocumentAccess: (kbId: string, documentId: string, policy: string, source?: string) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/documents/${pathPart(documentId)}/access`,
      json("PATCH", { schema_version: "v1", policy, source }),
    ),
  documentGrants: (kbId: string, documentId: string) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/documents/${pathPart(documentId)}/access/grants`,
    ),
  grantDocumentAccess: (kbId: string, documentId: string, subjectId: string, role: string) =>
    apiFetch<unknown>(
      `/knowledge-bases/${pathPart(kbId)}/documents/${pathPart(documentId)}/access/grants`,
      json("POST", { schema_version: "v1", subject_id: subjectId, role }),
    ),
  revokeDocumentAccess: (kbId: string, documentId: string, subjectId: string) =>
    apiFetch<void>(
      `/knowledge-bases/${pathPart(kbId)}/documents/${pathPart(documentId)}/access/grants/${pathPart(subjectId)}`,
      { method: "DELETE" },
    ),

  traces: (kbId?: string) =>
    apiFetch<unknown>(`/traces${query({ doc_id: kbId, limit: 100 })}`),
  diagnoseRetrieval: (kbId: string, searchQuery: string, topK = 12, rerank = true) =>
    apiFetch<unknown>(
      "/retrieval-diagnostics",
      json("POST", {
        doc_id: kbId,
        query: searchQuery,
        top_k: topK,
        rerank,
        requirements: [],
      }),
    ),
  scanIndexMigrations: () => apiFetch<unknown>("/index-migrations/scan"),
  startIndexMigration: (kbIds: string[]) =>
    apiFetch<unknown>("/index-migrations", json("POST", { kb_ids: kbIds })),
  indexMigration: (runId: string) =>
    apiFetch<unknown>(`/index-migrations/${pathPart(runId)}`),
  rollbackIndexMigration: (runId: string) =>
    apiFetch<unknown>(`/index-migrations/${pathPart(runId)}/rollback`, json("POST", { kb_ids: [] })),
  finalizeIndexMigration: (runId: string) =>
    apiFetch<unknown>(`/index-migrations/${pathPart(runId)}/finalize`, { method: "POST" }),
  haJobs: () => apiFetch<unknown>("/ha/jobs?limit=200"),
  cancelHaJob: (jobId: string) =>
    apiFetch<unknown>(`/ha/jobs/${pathPart(jobId)}/cancel`, { method: "POST" }),
  replayHaJob: (jobId: string, replayKey: string) =>
    apiFetch<unknown>(`/ha/jobs/${pathPart(jobId)}/replay`, json("POST", { replay_key: replayKey })),
};
