import { expect, test, type Page, type Route } from "@playwright/test";

const workspace = {
  workspace_id: "ws-acme",
  name: "Acme Research",
  role: "owner",
  revision: 1,
  created_at: "2026-08-01T00:00:00Z",
};
const user = {
  user_id: "usr-alice",
  email: "alice@acme.example",
  display_name: "Alice Chen",
};
const session = {
  schema_version: "v1",
  access_token: "test-session-token",
  token_type: "bearer",
  expires_at: "2026-08-24T00:00:00Z",
  user,
  workspace,
  permissions: ["read", "query", "write"],
};

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

interface ApiFixtureOptions {
  historyDelayMs?: number;
  historyMessages?: Record<string, unknown>[];
  traceDelayMs?: number;
  streamInterrupted?: boolean;
  streamUnauthorized?: boolean;
  invalidFinal?: boolean;
  malformedFinal?: boolean;
  invalidHistoryLedger?: boolean;
  historyEvidenceId?: string;
  repairedHistory?: boolean;
  malformedHistoryInternal?: boolean;
  knowledgeBasesFail?: boolean;
  sessionsFail?: boolean;
  streamLocation?: Record<string, unknown>;
  jobAlwaysRunning?: boolean;
  uploadDelayMs?: number;
  metadataDelayMs?: number;
  initialKnowledgeBase?: boolean;
  initialEmbeddingProfile?: "local" | "cloud";
  includeCustomRole?: boolean;
  permissions?: string[];
  authMeFails?: boolean;
}

async function installApi(page: Page, options: ApiFixtureOptions = {}) {
  const fixtureSession = { ...session, permissions: options.permissions ?? session.permissions };
  let knowledgeBases: Record<string, unknown>[] = options.initialKnowledgeBase ? [{
    kb_id: "policies",
    created_at: "2026-08-23T00:00:00Z",
    document_count: 0,
    tenant_id: workspace.workspace_id,
    owner_id: user.user_id,
    embedding_profile_id: options.initialEmbeddingProfile ?? "local",
    embedding_model: options.initialEmbeddingProfile === "cloud" ? "enterprise-embed" : "BAAI/bge-m3",
  }] : [];
  let documentReady = false;
  let jobReads = 0;
  let feedbackPayload: Record<string, unknown> | null = null;
  let savedKnowledgePayload: Record<string, unknown> | null = null;
  let uploadedBody = "";

  await page.route("**/api/cogdoc/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace("/api/cogdoc/v1", "");

    if (path === "/auth/config") return json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: true });
    if (path === "/auth/login" && request.method() === "POST") return json(route, fixtureSession);
    if (path === "/auth/me") return options.authMeFails
      ? json(route, { schema_version: "v1", error_code: "INTERNAL_ERROR", message: "identity service unavailable" }, 503)
      : json(route, { schema_version: "v1", user, workspace, permissions: fixtureSession.permissions, workspaces: [workspace] });
    if (path === `/workspaces/${workspace.workspace_id}/roles`) {
      if (options.metadataDelayMs) await new Promise((resolve) => setTimeout(resolve, options.metadataDelayMs));
      const roleIds = ["owner", "admin", "editor", "reviewer", "viewer", ...(options.includeCustomRole ? ["legal"] : [])];
      return json(route, { schema_version: "v1", workspace_id: workspace.workspace_id, roles: roleIds.map((roleId) => ({ role_id: roleId, name: roleId, base_role: roleId === "legal" ? "viewer" : roleId, system: roleId !== "legal", member_count: roleId === "owner" ? 1 : 0, revision: 1 })) });
    }
    if (path === "/embedding-profiles") return json(route, [
      { profile_id: "local", kind: "local", label: "本地 · BGE-M3", model: "BAAI/bge-m3", dimensions: 1024, available: true, description: "本地模型" },
      { profile_id: "cloud", kind: "cloud", label: "云端 · enterprise-embed", model: "enterprise-embed", dimensions: 1024, available: true, description: "云端模型" },
    ]);
    if (path === "/knowledge-bases" && request.method() === "GET") {
      if (options.metadataDelayMs) await new Promise((resolve) => setTimeout(resolve, options.metadataDelayMs));
      return options.knowledgeBasesFail
        ? json(route, { schema_version: "v1", error_code: "INTERNAL_ERROR", message: "知识服务暂不可用" }, 503)
        : json(route, knowledgeBases);
    }
    if (path === "/knowledge-bases" && request.method() === "POST") {
      const payload = request.postDataJSON() as { kb_id: string };
      const created = { kb_id: payload.kb_id, created_at: "2026-08-23T00:00:00Z", document_count: 0, tenant_id: workspace.workspace_id, owner_id: user.user_id, embedding_profile_id: "local", embedding_model: "BAAI/bge-m3" };
      knowledgeBases = [created];
      return json(route, created, 201);
    }
    if (path === "/knowledge-bases/policies/documents" && request.method() === "GET") {
      return json(route, documentReady ? [{ name: "security.pdf", sha256: "abc123", document_id: "doc-security", source_id: "src-security", version_id: "ver-1", connector_type: "legacy-upload", media_type: "application/pdf", kind: "file", origin_uri: null }] : []);
    }
    if ((path === "/knowledge-bases/policies/documents" || path === "/knowledge-bases/policies/documents/batch") && request.method() === "POST") {
      uploadedBody = request.postData() || "";
      if (options.uploadDelayMs) await new Promise((resolve) => setTimeout(resolve, options.uploadDelayMs));
      return json(route, { job_id: "job-1" }, 202);
    }
    if (path === "/index-jobs/job-1") {
      jobReads += 1;
      if (options.jobAlwaysRunning || jobReads < 2) return json(route, { job_id: "job-1", kb_id: "policies", status: "running", created_at: "2026-08-23T00:00:00Z" });
      documentReady = true;
      knowledgeBases = knowledgeBases.map((item) => ({ ...item, document_count: 1 }));
      return json(route, { job_id: "job-1", kb_id: "policies", status: "succeeded", created_at: "2026-08-23T00:00:00Z", finished_at: "2026-08-23T00:01:00Z", document_count: 1, chunk_count: 18 });
    }
    if (path === "/sessions" && request.method() === "GET") return options.sessionsFail
      ? json(route, { schema_version: "v1", error_code: "INTERNAL_ERROR", message: "会话服务暂不可用" }, 503)
      : json(route, { schema_version: "v1", doc_id: "policies", sessions: [] });
    if (path.includes("/history")) {
      if (options.historyDelayMs) await new Promise((resolve) => setTimeout(resolve, options.historyDelayMs));
      return json(route, { schema_version: "v1", doc_id: "policies", session_id: path.split("/")[2], messages: options.historyMessages ?? [] });
    }
    if (path === "/traces/trace-history") {
      if (options.traceDelayMs) await new Promise((resolve) => setTimeout(resolve, options.traceDelayMs));
      const answer = options.repairedHistory
        ? "补充检索后的结论。[repair.pdf:P2]"
        : `历史策略要求会话在三十分钟后锁定。[security.pdf:P3]${options.malformedHistoryInternal ? "[E-002]" : ""}`;
      const citation = options.repairedHistory ? "[repair.pdf:P2]" : "[security.pdf:P3]";
      const start = Array.from(answer.slice(0, answer.indexOf(citation))).length;
      const evidenceId = options.repairedHistory ? "E002" : options.historyEvidenceId ?? "E009";
      const chunkId = options.repairedHistory ? "chunk-repair" : "chunk-history";
      const source = options.repairedHistory ? "repair.pdf" : "security.pdf";
      const pageNumber = options.repairedHistory ? 2 : 3;
      return json(route, {
        schema_version: "v1", trace_id: "trace-history", request_id: "req-history", task_type: "qa", status: "ok", execution_status: "SUCCESS",
        output: {
          answer, critique: "",
          sources: [{ chunk_id: chunkId, source_type: "document", source, page: pageNumber }],
          citation_ledger: [{ evidence_id: evidenceId, chunk_id: chunkId, source_type: "document", source, page: pageNumber, span_start: options.repairedHistory ? 4 : 0, span_end: options.repairedHistory ? 18 : 26, occurrences: [{ index: 0, answer_start: start, answer_end: start + Array.from(citation).length + (options.invalidHistoryLedger ? 1 : 0) }] }],
          evidence: options.repairedHistory
            ? [{ evidence_id: "E001", chunk_id: "chunk-original", parent_chunk_id: "", section_title: "初始候选", section_path: "", source_type: "document", source: "original.pdf", page: 1, text_preview: "初始检索内容", retrieval: { evidence_id: "E001", evidence_text_start: 0, evidence_text_end: 8 } }]
            : [{ evidence_id: evidenceId, chunk_id: chunkId, parent_chunk_id: "", section_title: "历史会话策略", section_path: "安全 / 历史", source_type: "document", source, page: pageNumber, text_preview: "空闲三十分钟后锁定会话。", retrieval: { evidence_id: evidenceId, evidence_text_start: 0, evidence_text_end: 26 } }],
          evidence_ledger: options.repairedHistory ? [
            { evidence_id: "E001", chunk_id: "chunk-original", source_type: "document", source: "original.pdf", page: 1, span_start: 0, span_end: 8 },
            { evidence_id: "E002", chunk_id: "chunk-repair", source_type: "document", source: "repair.pdf", page: 2, span_start: 4, span_end: 18 },
          ] : undefined,
        },
      });
    }
    if (path === "/chat/stream") {
      if (options.streamUnauthorized) return json(route, { schema_version: "v1", error_code: "UNAUTHORIZED", message: "登录状态已过期" }, 401);
      if (options.streamInterrupted) {
        return route.fulfill({ status: 200, contentType: "text/event-stream", body: [
          "event: start\ndata: {\"trace_id\":\"trace-interrupted\"}\n\n",
          "event: token\ndata: {\"content\":\"这是尚未完成的回答\"}\n\n",
        ].join("") });
      }
      const sourceName = options.streamLocation ? "security.xlsx" : "security.pdf";
      const citation = options.streamLocation ? "[security.xlsx@sheet-FY26!B2:C8]" : "[security.pdf:P3]";
      const answer = `会话空闲超时由工作区安全策略统一控制。${citation}`;
      const start = Array.from(answer).slice(0, answer.indexOf(citation)).length;
      const sourceFields = options.streamLocation
        ? { source: sourceName, source_version_id: "ver-1", location: options.streamLocation }
        : { source: sourceName, page: 3 };
      const final = {
        schema_version: "v1",
        request_id: "req-1",
        trace_id: "trace-1",
        doc_id: "policies",
        session_id: "session-1",
        task_type: "qa",
        answer,
        citations: [{ chunk_id: "chunk-1", source_type: "document", knowledge_id: "", ...sourceFields }],
        citation_ledger: [{ evidence_id: "E001", chunk_id: "chunk-1", source_type: "document", knowledge_id: "", ...sourceFields, span_start: 0, span_end: 24, occurrences: [{ index: 0, answer_start: start, answer_end: start + Array.from(citation).length }] }],
        evidence: [{ chunk_id: "chunk-1", parent_chunk_id: "", section_title: "会话安全策略", section_path: "安全 / 会话", source_type: "document", knowledge_id: "", ...sourceFields, text_preview: "工作区可以设置会话空闲超时、绝对时长和并发会话上限。", retrieval: {} }],
        critique: options.invalidFinal ? "回答未通过引用或声明证据校验。" : "",
        is_valid: !options.invalidFinal,
      };
      if (options.malformedFinal) {
        return route.fulfill({ status: 200, contentType: "text/event-stream", body: "event: final\ndata: {}\n\n" });
      }
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: [
        "event: start\ndata: {\"trace_id\":\"trace-1\"}\n\n",
        "event: node\ndata: {\"stage\":\"retrieve\"}\n\n",
        "event: token\ndata: {\"content\":\"会话空闲超时由\"}\n\n",
        `event: final\ndata: ${JSON.stringify(final)}\n\n`,
      ].join("") });
    }
    if (path === "/feedback" && request.method() === "POST") {
      feedbackPayload = request.postDataJSON() as Record<string, unknown>;
      return json(route, { schema_version: "v1", feedback_id: "fb-1", status: "recorded", is_bad_case: false }, 201);
    }
    if (path === "/knowledge" && request.method() === "POST") {
      savedKnowledgePayload = request.postDataJSON() as Record<string, unknown>;
      return json(route, { knowledge_id: "knowledge-saved", status: "pending" }, 201);
    }
    return json(route, { schema_version: "v1", error_code: "NOT_FOUND", message: `Unhandled ${request.method()} ${path}` }, 404);
  });

  return { feedback: () => feedbackPayload, savedKnowledge: () => savedKnowledgePayload, uploadedBody: () => uploadedBody };
}

test("login to evidence feedback vertical slice", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const apiState = await installApi(page);
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/home$/);
  await expect(page.locator("#main-content").getByText("Acme Research")).toBeVisible();
  await page.goto("/knowledge");

  await page.getByRole("button", { name: "创建知识库" }).first().click();
  await page.getByLabel("知识库 ID").fill("policies");
  await page.getByRole("button", { name: "创建知识库", exact: true }).last().click();
  await expect(page).toHaveURL(/\/knowledge\/policies$/);

  await page.goto("/knowledge");
  const knowledgeLink = page.getByRole("link", { name: /policies/ }).first();
  await knowledgeLink.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/knowledge\/policies$/);

  await page.getByRole("tab", { name: "上传" }).click();
  await page.locator('input[type="file"]').setInputFiles({ name: "security.pdf", mimeType: "application/pdf", buffer: Buffer.from("mock pdf") });
  await page.getByRole("button", { name: "上传 1 个文件" }).click();
  await expect(page.getByText(/入库完成/)).toBeVisible({ timeout: 10_000 });

  await page.getByRole("button", { name: "开始对话" }).click();
  await expect(page).toHaveURL(/\/knowledge\/policies\/chat\//);
  await page.getByPlaceholder("询问这个知识库…").fill("会话超时由谁控制？");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("会话空闲超时由工作区安全策略统一控制。")).toBeVisible();
  const citationButton = page.getByRole("button", { name: /打开证据 1/ });
  await citationButton.click();
  const evidencePanel = page.getByRole("complementary", { name: "证据详情" });
  await expect(evidencePanel).toContainText("工作区可以设置会话空闲超时");
  await expect(evidencePanel.locator("mark")).toBeVisible();
  await expect(evidencePanel).toHaveAttribute("data-motion", "reduced");
  await expect(evidencePanel).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(evidencePanel).toBeHidden();
  await expect(citationButton).toBeFocused();

  await page.getByRole("button", { name: "回答有帮助" }).click();
  await expect(page.getByLabel("对话").getByText("反馈已记录")).toBeVisible();
  expect(apiState.feedback()).toMatchObject({ trace_id: "trace-1", feedback: "thumbs_up", kb_id: "policies" });
  await page.getByRole("button", { name: "保存为派生知识" }).click();
  await expect(page.getByRole("button", { name: "已保存" })).toBeVisible();
  expect(apiState.savedKnowledge()).toMatchObject({
    kb_id: "policies",
    created_from_trace_id: "trace-1",
    related_source: "security.pdf",
    related_chunk_ids: ["chunk-1"],
    related_page_start: 3,
    related_page_end: 3,
  });
});

test("delayed history is gated and restores trace-backed evidence", async ({ page }) => {
  const apiState = await installApi(page, {
    historyDelayMs: 1200,
    traceDelayMs: 150,
    historyMessages: [
      { role: "user", content: "历史会话怎么锁定？" },
      { role: "assistant", content: "历史策略要求会话在三十分钟后锁定。[security.pdf:P3]", trace_id: "trace-history", query: "历史会话怎么锁定？", task_type: "qa" },
    ],
  });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/chat/session-history");

  const composer = page.getByPlaceholder("正在恢复会话…");
  await expect(composer).toBeDisabled();
  await expect(page.getByText("历史会话怎么锁定？")).toBeVisible();
  const readyComposer = page.getByPlaceholder("询问这个知识库…");
  await expect(readyComposer).toBeEnabled();
  await readyComposer.fill("继续说明");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(page.getByText("继续说明", { exact: true })).toBeVisible();
  await expect(page.getByText("会话空闲超时由工作区安全策略统一控制。")).toBeVisible();
  await expect(page.getByRole("button", { name: /打开证据 1: security.pdf/ }).first()).toBeVisible();
  await expect(page.getByText("证据暂不可用")).toHaveCount(0);

  await page.getByRole("button", { name: "回答需要改进" }).last().click();
  await page.getByLabel("问题类型").click();
  await page.getByRole("option", { name: "我有更正" }).click();
  await page.getByRole("button", { name: "记录反馈" }).click();
  await expect(page.getByText("请填写建议的正确内容")).toBeVisible();
  await page.getByLabel("正确内容").fill("会话策略由安全管理员统一配置。");
  await page.getByRole("button", { name: "记录反馈" }).click();
  await expect(page.getByText("反馈已记录").last()).toBeVisible();
  expect(apiState.feedback()).toMatchObject({ feedback: "correction", feedback_type: "correction", correction_text: "会话策略由安全管理员统一配置。" });
});

test("OIDC callback code is scrubbed before exchange completes", async ({ page }) => {
  let exchangedCode = "";
  await page.route("**/api/cogdoc/v1/auth/config", (route) => json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: true, oidc_display_name: "Acme SSO", scim_enabled: false }));
  await page.route("**/api/cogdoc/v1/auth/oidc/exchange", async (route) => {
    exchangedCode = (route.request().postDataJSON() as { code: string }).code;
    // Keep the pending state observable even when the full suite runs with
    // multiple workers and the dev server is already warm.
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    return json(route, { schema_version: "v1", error_code: "UNAUTHORIZED", message: "企业登录失败" }, 401);
  });
  await page.goto("/login?oidc_code=one-time-secret");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByText("正在完成企业登录")).toBeVisible();
  await expect.poll(() => exchangedCode).toBe("one-time-secret");
});

test("self registration follows auth configuration", async ({ page }) => {
  await page.route("**/api/cogdoc/v1/auth/config", (route) => json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: true, oidc_enabled: false, oidc_display_name: "", scim_enabled: false }));
  await page.route("**/api/cogdoc/v1/auth/register", (route) => json(route, session, 201));
  await page.route("**/api/cogdoc/v1/auth/me", (route) => json(route, { schema_version: "v1", user, workspace, permissions: session.permissions, workspaces: [workspace] }));
  await page.route("**/api/cogdoc/v1/knowledge-bases", (route) => json(route, []));
  await page.goto("/login");
  await page.getByRole("tab", { name: "注册" }).click();
  const registration = page.getByRole("tabpanel", { name: "注册" });
  await registration.getByLabel("显示名称").fill("Alice Chen");
  await registration.getByLabel("邮箱").fill("alice@acme.example");
  await registration.getByLabel("个人工作区名称（可选）").fill("Acme Research");
  await registration.getByLabel("密码").fill("correct horse battery");
  await registration.getByRole("button", { name: "创建账号", exact: true }).click();
  await expect(page).toHaveURL(/\/home$/);
});

test("self registration explains an existing account conflict", async ({ page }) => {
  await page.route("**/api/cogdoc/v1/auth/config", (route) => json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: true, oidc_enabled: false, oidc_display_name: "", scim_enabled: false }));
  await page.route("**/api/cogdoc/v1/auth/register", (route) => json(route, { schema_version: "v1", error_code: "AUTH_CONFLICT", message: "account already exists" }, 409));
  await page.goto("/login");
  await page.getByRole("tab", { name: "注册" }).click();
  const registration = page.getByRole("tabpanel", { name: "注册" });
  await registration.getByLabel("显示名称").fill("Existing User");
  await registration.getByLabel("邮箱").fill("existing@acme.example");
  await registration.getByLabel("密码").fill("correct horse battery");
  await registration.getByRole("button", { name: "创建账号", exact: true }).click();
  await expect(registration.getByRole("alert")).toHaveText("该邮箱已注册，请切换到“登录”；如果忘记密码，请联系工作区管理员。");
});

test("closed registration remains explained and invitation acceptance is functional", async ({ page }) => {
  let invitationPayload: Record<string, unknown> | null = null;
  await page.route("**/api/cogdoc/v1/auth/config", (route) => json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: false }));
  await page.route("**/api/cogdoc/v1/auth/invitations/accept", (route) => {
    invitationPayload = route.request().postDataJSON() as Record<string, unknown>;
    return json(route, session, 201);
  });
  await page.route("**/api/cogdoc/v1/auth/me", (route) => json(route, { schema_version: "v1", user, workspace, permissions: session.permissions, workspaces: [workspace] }));
  await page.route("**/api/cogdoc/v1/knowledge-bases", (route) => json(route, []));

  await page.goto("/login");
  await page.getByRole("tab", { name: "注册" }).click();
  await expect(page.getByText("当前部署未开放自主注册")).toBeVisible();

  await page.goto("/login?invite=invite-token-1234567890&email=alice%40acme.example");
  await expect(page).toHaveURL(/\/login$/);
  const invitation = page.getByRole("tabpanel", { name: "接受邀请" });
  await expect(invitation.getByLabel("邀请令牌")).toHaveValue("invite-token-1234567890");
  await expect(invitation.getByLabel("受邀邮箱")).toHaveValue("alice@acme.example");
  await invitation.getByLabel("设置密码").fill("correct horse battery");
  await invitation.getByRole("button", { name: "接受邀请", exact: true }).click();
  await expect(page).toHaveURL(/\/home$/);
  expect(invitationPayload).toEqual({ token: "invite-token-1234567890", email: "alice@acme.example", password: "correct horse battery" });
});

test("evidence inspector stays usable on a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  await installApi(page, {
    historyMessages: [
      { role: "user", content: "历史会话怎么锁定？" },
      { role: "assistant", content: "历史策略要求会话在三十分钟后锁定。[security.pdf:P3]", trace_id: "trace-history", query: "历史会话怎么锁定？", task_type: "qa" },
    ],
  });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/chat/session-mobile");
  await expect(page.getByRole("button", { name: "打开导航" })).toBeVisible();
  const citation = page.getByRole("button", { name: /打开证据 1/ });
  await citation.click();
  const panel = page.getByRole("dialog", { name: "证据详情" });
  const bounds = await panel.boundingBox();
  expect(bounds?.width).toBeLessThanOrEqual(360);
  await expect(panel).toBeVisible();
  for (let index = 0; index < 4; index += 1) {
    await page.keyboard.press("Tab");
    expect(await page.evaluate(() => Boolean(document.activeElement?.closest('[role="dialog"]')))).toBe(true);
  }
  await page.keyboard.press("Escape");
  await expect(citation).toBeFocused();
});

test("citation selection remains bound to the response trace", async ({ page }) => {
  await installApi(page, {
    historyEvidenceId: "E001",
    historyMessages: [
      { role: "user", content: "历史会话怎么锁定？" },
      { role: "assistant", content: "历史策略要求会话在三十分钟后锁定。[security.pdf:P3]", trace_id: "trace-history", query: "历史会话怎么锁定？", task_type: "qa" },
    ],
  });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/chat/session-collision");
  await expect(page.getByRole("button", { name: /打开证据 1/ }).first()).toBeVisible();
  await page.getByLabel("消息").fill("再说明一次");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByRole("button", { name: /打开证据 1/ })).toHaveCount(2);
  await page.getByRole("button", { name: /打开证据 1/ }).first().click();
  await expect(page.getByRole("complementary", { name: "证据详情" })).toContainText("空闲三十分钟后锁定会话");
  await expect(page.getByRole("complementary", { name: "证据详情" })).not.toContainText("绝对时长和并发会话上限");
});

test("repaired historical citations validate against the global evidence registry", async ({ page }) => {
  await installApi(page, {
    repairedHistory: true,
    historyMessages: [
      { role: "user", content: "补充检索后有什么结论？" },
      { role: "assistant", content: "补充检索后的结论。[repair.pdf:P2]", trace_id: "trace-history", query: "补充检索后有什么结论？", task_type: "qa" },
    ],
  });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/chat/session-repaired");
  await expect(page.getByText("已校验", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /打开证据 1: repair.pdf/ })).toBeVisible();
});

test("malformed internal evidence identifiers in history fail closed", async ({ page }) => {
  await installApi(page, {
    malformedHistoryInternal: true,
    historyMessages: [
      { role: "user", content: "历史会话怎么锁定？" },
      { role: "assistant", content: "历史策略要求会话在三十分钟后锁定。[security.pdf:P3]", trace_id: "trace-history", query: "历史会话怎么锁定？", task_type: "qa" },
    ],
  });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/chat/session-malformed-history");
  await expect(page.getByText("需要审核", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /打开证据/ })).toHaveCount(0);
});

test("universal source locations retain spreadsheet precision", async ({ page }) => {
  await installApi(page, { streamLocation: { sheet: "FY26", cell_range: "B2:C8" } });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/chat/session-spreadsheet");
  await page.getByLabel("消息").fill("说明表格策略");
  await page.getByRole("button", { name: "发送" }).click();
  await page.getByRole("button", { name: /打开证据 1/ }).click();
  await expect(page.getByRole("complementary", { name: "证据详情" })).toContainText("工作表 FY26 · B2:C8");
});

test("workspace switch refreshes tenant-scoped data", async ({ page }) => {
  const betaWorkspace = { ...workspace, workspace_id: "ws-beta", name: "Beta Legal", role: "editor" };
  let activeWorkspace = workspace;
  let knowledgeHeader = "";
  await page.route("**/api/cogdoc/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/cogdoc/v1", "");
    if (path === "/auth/config") return json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: false });
    if (path === "/auth/login") return json(route, session);
    if (path === "/auth/me") return json(route, { schema_version: "v1", user, workspace: activeWorkspace, permissions: session.permissions, workspaces: [workspace, betaWorkspace] });
    if (path === "/workspaces/ws-beta/switch") {
      activeWorkspace = betaWorkspace;
      return json(route, { ...session, workspace: betaWorkspace });
    }
    if (path === "/knowledge-bases") {
      knowledgeHeader = request.headers()["x-cogdoc-workspace"] || "";
      return json(route, activeWorkspace.workspace_id === "ws-beta" ? [{ kb_id: "legal", created_at: "2026-08-23T00:00:00Z", document_count: 4, tenant_id: "ws-beta", owner_id: user.user_id }] : []);
    }
    return json(route, { schema_version: "v1", error_code: "NOT_FOUND", message: path }, 404);
  });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("切换工作区").click();
  await page.getByRole("menuitem", { name: /Beta Legal/ }).click();
  await expect(page.getByLabel("切换工作区")).toContainText("Beta Legal");
  await expect(page.getByRole("link", { name: /legal/ }).first()).toBeVisible();
  expect(knowledgeHeader).toBe("ws-beta");
});

test("interrupted stream keeps partial text and remains actionable", async ({ page }) => {
  await installApi(page, { streamInterrupted: true });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/chat/session-interrupted");
  const composer = page.getByPlaceholder("询问这个知识库…");
  await composer.fill("生成一份说明");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("这是尚未完成的回答")).toBeVisible();
  await expect(page.getByText("未完成", { exact: true })).toBeVisible();
  await expect(page.getByText("响应在完成前中断，请重试。", { exact: true })).toBeVisible();
  await expect(page.getByText(/不会参与后续问题的上下文/)).toBeVisible();
  await expect(composer).toBeEnabled();
});

test("malformed final stream event becomes a recoverable protocol error", async ({ page }) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await installApi(page, { malformedFinal: true });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/chat/session-invalid-protocol");
  await page.getByLabel("消息").fill("测试协议校验");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("响应协议不完整，请重试。", { exact: true })).toBeVisible();
  await expect(page.getByLabel("消息")).toBeEnabled();
  expect(pageErrors).toEqual([]);
});

test("server failures are distinct from empty knowledge and session states", async ({ page }) => {
  await installApi(page, { knowledgeBasesFail: true, sessionsFail: true });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/chat");
  await expect(page.locator("#main-content").getByText("无法读取知识库", { exact: true })).toBeVisible();
  await expect(page.getByText("没有可用的知识库", { exact: true })).toHaveCount(0);
  await page.goto("/knowledge/policies/chat/session-service-error");
  await expect(page.getByText("无法读取对话", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("还没有历史对话", { exact: true })).toHaveCount(0);
});

test("home does not replace a failed session lookup with a new conversation", async ({ page }) => {
  await installApi(page, { initialKnowledgeBase: true, sessionsFail: true });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/home$/);
  await expect(page.getByText("无法恢复最近对话", { exact: true })).toBeVisible();
  await expect(page).not.toHaveURL(/\/chat\//);
});

test("login failure remains actionable", async ({ page }) => {
  await page.route("**/api/cogdoc/v1/auth/config", (route) => json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: false }));
  await page.route("**/api/cogdoc/v1/auth/login", (route) => json(route, { schema_version: "v1", error_code: "UNAUTHORIZED", message: "invalid credentials" }, 401));
  const response = await page.goto("/login");
  expect(response?.headers()["x-frame-options"]).toBe("DENY");
  expect(response?.headers()["referrer-policy"]).toBe("no-referrer");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("wrong password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByText("邮箱或密码不正确", { exact: true })).toBeVisible();
});

test("stale local-mode session does not loop when account authentication is enabled", async ({ page }) => {
  await page.addInitScript(() => {
    window.sessionStorage.setItem("cogdoc.session.v1", JSON.stringify({ state: { authMode: "legacy", accessToken: null, expiresAt: null, user: null, workspace: null, selectedWorkspaceId: null, permissions: [], sidebarCollapsed: false }, version: 0 }));
  });
  await page.route("**/api/cogdoc/v1/auth/config", (route) => json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: true, oidc_enabled: false, oidc_display_name: "", scim_enabled: false }));
  await page.goto("/login");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("button", { name: "登录", exact: true })).toBeVisible();
  await page.waitForTimeout(500);
  await expect(page).toHaveURL(/\/login$/);
});

test("route params accept a backend-valid percent knowledge base id", async ({ page }) => {
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await installApi(page);
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/home$/);
  await page.goto("/knowledge");
  await page.getByRole("button", { name: "创建知识库" }).first().click();
  await page.getByLabel("知识库 ID").fill("100%");
  await page.getByRole("button", { name: "创建知识库", exact: true }).last().click();
  await expect(page).toHaveURL(/\/knowledge\/100%25$/);
  await expect(page.getByRole("heading", { name: "100%", exact: true })).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test("invalid historical citation ledger fails closed", async ({ page }) => {
  await installApi(page, {
    invalidHistoryLedger: true,
    historyMessages: [
      { role: "user", content: "历史会话怎么锁定？" },
      { role: "assistant", content: "历史策略要求会话在三十分钟后锁定。[security.pdf:P3]", trace_id: "trace-history", query: "历史会话怎么锁定？", task_type: "qa" },
    ],
  });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/chat/session-invalid-history");
  await expect(page.getByText("需要审核", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /打开证据/ })).toHaveCount(0);
});

test("invalid final answer exposes review state separately from citation binding", async ({ page }) => {
  await installApi(page, { invalidFinal: true });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/chat/session-review");
  await page.getByPlaceholder("询问这个知识库…").fill("说明会话策略");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("需要审核", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /打开证据 1/ }).click();
  await expect(page.getByText("引用位置已绑定，回答整体需要审核")).toBeVisible();
});

test("viewer affordances follow backend permissions", async ({ page }) => {
  await installApi(page, { initialKnowledgeBase: true, permissions: ["read", "query"] });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/home$/);
  await page.goto("/knowledge");
  await expect(page.getByRole("button", { name: "创建知识库" }).first()).toBeDisabled();
  await expect(page.getByRole("link", { name: "管理" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "访问权限" })).toHaveCount(0);
  await page.goto("/knowledge/policies/access");
  await expect(page.getByText("当前角色不能管理访问权限")).toBeVisible();
  await page.goto("/knowledge/policies");
  await expect(page.getByRole("tab", { name: "上传" })).toBeDisabled();
  await expect(page.getByRole("navigation", { name: "知识库主视图" })).not.toContainText("证据审核");
  await page.goto("/knowledge/policies/diagnostics");
  await expect(page.getByRole("tab", { name: "RAG 评测" })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "检索诊断" })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "索引代际" })).toHaveCount(0);
  await page.goto("/reviews?kb=policies");
  await expect(page).toHaveURL(/\/knowledge\/policies\/diagnostics$/);
  await expect(page.getByRole("tab", { name: "RAG 评测" })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Trace 调试" })).toHaveAttribute("data-state", "active");
  await page.goto("/admin");
  await expect(page.getByText("当前角色不能访问管理设置")).toBeVisible();
});

test("manage-access role sees access administration but not tenant settings", async ({ page }) => {
  await installApi(page, { initialKnowledgeBase: true, permissions: ["read", "query", "manage_access"] });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge");

  await expect(page.getByRole("link", { name: "访问权限" })).toBeVisible();
  await expect(page.getByRole("link", { name: "管理" })).toHaveAttribute("href", "/admin");
  await page.goto("/admin");
  await expect(page.getByRole("link", { name: "成员与邀请" })).toBeVisible();
  await expect(page.getByRole("link", { name: "工作区设置" })).toHaveCount(0);
});

test("manage-tenant role enters tenant settings without access-management links", async ({ page }) => {
  await installApi(page, { initialKnowledgeBase: true, permissions: ["read", "query", "manage_tenant"] });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge");

  await expect(page.getByRole("link", { name: "访问权限" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "管理" })).toHaveAttribute("href", "/admin/workspace");
  await page.goto("/admin/workspace");
  await expect(page.getByRole("link", { name: "工作区设置" })).toBeVisible();
  await expect(page.getByRole("link", { name: "成员与邀请" })).toHaveCount(0);
});

test("streaming 401 clears the local session", async ({ page }) => {
  await installApi(page, { streamUnauthorized: true });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/chat/session-expired");
  await page.getByPlaceholder("询问这个知识库…").fill("测试过期登录");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("cogdoc.session.v1"))).toContain('"accessToken":null');
});

test("temporary identity outage preserves the login session", async ({ page }) => {
  await installApi(page, { authMeFails: true });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByText("暂时无法验证工作区身份")).toBeVisible();
  await expect(page).toHaveURL(/\/home$/);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("cogdoc.session.v1"))).toContain('"accessToken":"test-session-token"');
});

test("upload job is restored after navigation reload", async ({ page }) => {
  await installApi(page, { initialKnowledgeBase: true, jobAlwaysRunning: true, uploadDelayMs: 500 });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies");
  await page.getByRole("tab", { name: "上传" }).click();
  await page.locator('input[type="file"]').setInputFiles({ name: "security.pdf", mimeType: "application/pdf", buffer: Buffer.from("mock pdf") });
  await page.getByRole("button", { name: "上传 1 个文件" }).click();
  await expect(page.getByRole("button", { name: "正在上传" })).toHaveAttribute("aria-busy", "true");
  await expect(page.getByRole("progressbar", { name: "正在上传 1 个文件" })).toBeVisible();
  await expect(page.getByText("正在解析、切分并建立索引")).toBeVisible();
  await expect(page.getByRole("progressbar", { name: "正在解析、切分并建立索引" })).toBeVisible();
  await page.reload();
  await page.getByRole("tab", { name: "上传" }).click();
  await expect(page.getByText("已恢复入库任务")).toBeVisible();
  await expect(page.getByText("正在解析、切分并建立索引")).toBeVisible();
});

test("upload waits for the knowledge base model and complete workspace roles", async ({ page }) => {
  const apiState = await installApi(page, {
    initialKnowledgeBase: true,
    initialEmbeddingProfile: "cloud",
    includeCustomRole: true,
    metadataDelayMs: 1200,
  });
  await page.addInitScript((authenticatedSession) => {
    window.sessionStorage.setItem("cogdoc.session.v1", JSON.stringify({
      state: {
        authMode: "account",
        accessToken: authenticatedSession.access_token,
        expiresAt: authenticatedSession.expires_at,
        user: authenticatedSession.user,
        workspace: authenticatedSession.workspace,
        selectedWorkspaceId: authenticatedSession.workspace.workspace_id,
        permissions: authenticatedSession.permissions,
        sidebarCollapsed: false,
      },
      version: 0,
    }));
  }, session);

  await page.goto("/knowledge/policies");
  await page.getByRole("tab", { name: "上传" }).click();
  await page.locator('input[type="file"]').setInputFiles({ name: "security.pdf", mimeType: "application/pdf", buffer: Buffer.from("mock pdf") });
  const upload = page.getByRole("button", { name: "上传 1 个文件" });
  await expect(upload).toBeDisabled();
  await expect(upload).toBeEnabled({ timeout: 5000 });
  await upload.click();
  await expect.poll(() => apiState.uploadedBody()).toContain("cloud");
  await expect.poll(() => apiState.uploadedBody()).toContain("legal");
});

test("mobile navigation closes after route change", async ({ page }) => {
  await installApi(page);
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("alice@acme.example");
  await page.getByLabel("密码").fill("correct horse battery");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("button", { name: "收起侧栏" }).focus();
  await page.keyboard.press("Enter");
  await page.setViewportSize({ width: 360, height: 740 });
  await page.getByRole("button", { name: "打开导航" }).click();
  const navigation = page.getByRole("dialog", { name: "主导航" });
  await expect(navigation.getByText("Acme Research")).toBeVisible();
  await navigation.getByRole("link", { name: "知识", exact: true }).click();
  await expect(page).toHaveURL(/\/knowledge$/);
  await expect(navigation).toBeHidden();
});
