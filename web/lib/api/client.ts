"use client";

import { ZodError } from "zod";
import { useSessionStore } from "@/stores/session-store";
import type {
  ApiErrorBody,
  AuthConfig,
  AuthMe,
  AuthSession,
  ChatMode,
  Document,
  FeedbackResponse,
  IndexJob,
  KnowledgeBase,
  EmbeddingProfile,
  OidcExchangeResponse,
  OidcStartResponse,
  SessionHistoryResponse,
  SessionListResponse,
  StreamEvent,
  TraceResponse,
  WorkspaceRoleListResponse,
} from "@/lib/api/types";
import { streamEventDataSchemas } from "@/lib/api/schemas";

const API_PREFIX = "/api/cogdoc/v1";

export class ApiError extends Error {
  status: number;
  errorCode: string;
  requestId?: string | null;
  traceId?: string | null;
  details?: Record<string, unknown> | null;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message || `Request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.errorCode = body.error_code || "INTERNAL_ERROR";
    this.requestId = body.request_id;
    this.traceId = body.trace_id;
    this.details = body.details;
  }
}

interface RequestOptions {
  auth?: boolean;
  workspace?: boolean;
}

function requestHeaders(init?: HeadersInit, options?: RequestOptions) {
  const headers = new Headers(init);
  headers.set("Accept", "application/json");
  const state = useSessionStore.getState();
  if (options?.auth !== false && state.accessToken) {
    headers.set("Authorization", `Bearer ${state.accessToken}`);
  }
  if (options?.workspace !== false && state.selectedWorkspaceId && !headers.has("X-CogDoc-Workspace")) {
    headers.set("X-CogDoc-Workspace", state.selectedWorkspaceId);
  }
  return headers;
}

async function errorBody(response: Response): Promise<ApiErrorBody> {
  try {
    const value = await response.json() as Record<string, unknown>;
    const detail = typeof value.detail === "object" && value.detail !== null && !Array.isArray(value.detail)
      ? value.detail as Record<string, unknown>
      : null;
    return {
      ...value,
      message: typeof value.message === "string"
        ? value.message
        : typeof value.detail === "string"
          ? value.detail
          : typeof detail?.message === "string"
            ? detail.message
          : response.statusText || "请求失败",
      details: typeof value.details === "object" && value.details !== null && !Array.isArray(value.details)
        ? value.details as Record<string, unknown>
        : detail,
    } as ApiErrorBody;
  } catch {
    return { message: response.statusText || "请求失败" };
  }
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
  options: RequestOptions = {},
): Promise<T> {
  const headers = requestHeaders(init.headers, options);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${API_PREFIX}${path}`, {
    ...init,
    headers,
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await errorBody(response);
    if (response.status === 401 && options.auth !== false) {
      useSessionStore.getState().clearSession();
    }
    throw new ApiError(response.status, body);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function apiDownload(path: string): Promise<Blob> {
  const response = await fetch(`${API_PREFIX}${path}`, {
    headers: requestHeaders(),
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await errorBody(response);
    if (response.status === 401) useSessionStore.getState().clearSession();
    throw new ApiError(response.status, body);
  }
  return response.blob();
}

export const api = {
  authConfig: () => apiFetch<AuthConfig>("/auth/config", {}, { auth: false, workspace: false }),
  login: (email: string, password: string) =>
    apiFetch<AuthSession>(
      "/auth/login",
      { method: "POST", body: JSON.stringify({ email, password }) },
      { auth: false, workspace: false },
    ),
  register: (payload: { email: string; password: string; display_name: string; workspace_name?: string }) =>
    apiFetch<AuthSession>(
      "/auth/register",
      { method: "POST", body: JSON.stringify(payload) },
      { auth: false, workspace: false },
    ),
  acceptInvite: (payload: { token: string; email: string; password: string; display_name?: string }) =>
    apiFetch<AuthSession>(
      "/auth/invitations/accept",
      { method: "POST", body: JSON.stringify(payload) },
      { auth: false, workspace: false },
    ),
  startOidc: (returnUrl: string) =>
    apiFetch<OidcStartResponse>(
      "/auth/oidc/authorize",
      { method: "POST", body: JSON.stringify({ return_url: returnUrl }) },
      { auth: false, workspace: false },
    ),
  exchangeOidc: (code: string) =>
    apiFetch<OidcExchangeResponse>(
      "/auth/oidc/exchange",
      { method: "POST", body: JSON.stringify({ code }) },
      { auth: false, workspace: false },
    ),
  me: () => apiFetch<AuthMe>("/auth/me"),
  logout: () => apiFetch<void>("/auth/logout", { method: "POST" }),
  switchWorkspace: (workspaceId: string) =>
    apiFetch<AuthSession>(`/workspaces/${encodeURIComponent(workspaceId)}/switch`, {
      method: "POST",
      headers: { "X-CogDoc-Workspace": workspaceId },
    }),
  workspaceRoles: (workspaceId: string) =>
    apiFetch<WorkspaceRoleListResponse>(`/workspaces/${encodeURIComponent(workspaceId)}/roles`),
  knowledgeBases: () => apiFetch<KnowledgeBase[]>("/knowledge-bases"),
  embeddingProfiles: () => apiFetch<EmbeddingProfile[]>("/embedding-profiles"),
  createKnowledgeBase: (kbId: string, accessPolicy: "workspace" | "private", roleIds: string[]) =>
    apiFetch<KnowledgeBase>("/knowledge-bases", {
      method: "POST",
      body: JSON.stringify({ kb_id: kbId, access_policy: accessPolicy, role_ids: roleIds }),
    }),
  deleteKnowledgeBase: (kbId: string) =>
    apiFetch<void>(`/knowledge-bases/${encodeURIComponent(kbId)}`, { method: "DELETE" }),
  documents: (kbId: string) =>
    apiFetch<Document[]>(`/knowledge-bases/${encodeURIComponent(kbId)}/documents`),
  deleteDocument: (kbId: string, name: string) =>
    apiFetch<void>(`/knowledge-bases/${encodeURIComponent(kbId)}/documents/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
  uploadDocuments: (
    kbId: string,
    files: File[],
    allowedRoleIds: string[],
    embeddingProfileId: "local" | "cloud",
  ) => {
    const body = new FormData();
    files.forEach((file) => body.append("files", file, file.name));
    allowedRoleIds.forEach((roleId) => body.append("allowed_role_ids", roleId));
    body.append("embedding_profile_id", embeddingProfileId);
    return apiFetch<{ job_id: string }>(
      `/knowledge-bases/${encodeURIComponent(kbId)}/documents/batch`,
      { method: "POST", body },
    );
  },
  indexJob: (jobId: string) => apiFetch<IndexJob>(`/index-jobs/${encodeURIComponent(jobId)}`),
  sessions: (docId: string) =>
    apiFetch<SessionListResponse>(`/sessions?doc_id=${encodeURIComponent(docId)}`),
  sessionHistory: (docId: string, sessionId: string) =>
    apiFetch<SessionHistoryResponse>(
      `/sessions/${encodeURIComponent(sessionId)}/history?doc_id=${encodeURIComponent(docId)}`,
    ),
  deleteSession: (docId: string, sessionId: string) =>
    apiFetch<void>(
      `/sessions/${encodeURIComponent(sessionId)}?doc_id=${encodeURIComponent(docId)}`,
      { method: "DELETE" },
    ),
  trace: (traceId: string, signal?: AbortSignal) =>
    apiFetch<TraceResponse>(`/traces/${encodeURIComponent(traceId)}`, { signal }),
  feedback: (payload: Record<string, unknown>) =>
    apiFetch<FeedbackResponse>("/feedback", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};

function parseSseFrame(frame: string): StreamEvent | null {
  let eventName = "message";
  const dataLines: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!(eventName in streamEventDataSchemas)) return null;
  if (!dataLines.length) {
    throw new ApiError(502, { error_code: "INVALID_STREAM_EVENT", message: "响应协议不完整，请重试。", details: { event: eventName } });
  }
  try {
    const rawData: unknown = JSON.parse(dataLines.join("\n"));
    if (eventName === "start") return { type: "start", data: streamEventDataSchemas.start.parse(rawData) };
    if (eventName === "node") return { type: "node", data: streamEventDataSchemas.node.parse(rawData) };
    if (eventName === "token") return { type: "token", data: streamEventDataSchemas.token.parse(rawData) };
    if (eventName === "final") return { type: "final", data: streamEventDataSchemas.final.parse(rawData) };
    return { type: "error", data: streamEventDataSchemas.error.parse(rawData) };
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof ZodError) {
      throw new ApiError(502, { error_code: "INVALID_STREAM_EVENT", message: "响应协议不完整，请重试。", details: { event: eventName } });
    }
    throw new ApiError(502, { error_code: "INVALID_STREAM_EVENT", message: "响应协议无法解析，请重试。", details: { event: eventName } });
  }
}

export async function* streamChat(
  payload: {
    query: string;
    doc_id: string;
    session_id: string;
    mode: ChatMode;
    is_local: boolean;
  },
  signal: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const response = await fetch(`${API_PREFIX}/chat/stream`, {
    method: "POST",
    headers: requestHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ schema_version: "v1", ...payload }),
    signal,
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await errorBody(response);
    if (response.status === 401) useSessionStore.getState().clearSession();
    throw new ApiError(response.status, body);
  }
  if (!response.body) {
    throw new ApiError(502, { error_code: "STREAM_INTERRUPTED", message: "响应流不可用" });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const event = parseSseFrame(frame);
      if (event) yield event;
    }
    if (done) break;
  }
  if (buffer.trim()) {
    const event = parseSseFrame(buffer);
    if (event) yield event;
  }
}
