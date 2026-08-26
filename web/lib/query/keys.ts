export const queryKeys = {
  authConfig: ["auth", "config"] as const,
  me: ["auth", "me"] as const,
  workspaceRoles: (workspaceId?: string | null) => ["workspace-roles", workspaceId] as const,
  knowledgeBases: (workspaceId?: string | null) => ["knowledge-bases", workspaceId] as const,
  embeddingProfiles: ["embedding-profiles"] as const,
  documents: (workspaceId: string | null | undefined, kbId: string) =>
    ["documents", workspaceId, kbId] as const,
  indexJob: (workspaceId: string | null | undefined, jobId: string) => ["index-job", workspaceId, jobId] as const,
  sessions: (workspaceId: string | null | undefined, docId: string) =>
    ["sessions", workspaceId, docId] as const,
  history: (workspaceId: string | null | undefined, docId: string, sessionId: string) =>
    ["session-history", workspaceId, docId, sessionId] as const,
};
