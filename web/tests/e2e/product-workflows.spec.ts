import { expect, test, type Route } from "@playwright/test";

const workspace = { workspace_id: "ws-enterprise", name: "Enterprise Knowledge", role: "owner", revision: 1, created_at: "2026-08-01T00:00:00Z" };
const user = { user_id: "usr-reviewer", email: "reviewer@example.com", display_name: "Review Owner" };
const session = { schema_version: "v1", access_token: "workflow-token", token_type: "bearer", expires_at: "2026-09-01T00:00:00Z", user, workspace, permissions: ["read", "query", "write", "delete", "review", "publish", "manage_access", "manage_tenant"] };

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

test("research lifecycle and retrieval review use authoritative backend transitions", async ({ page }) => {
  let researchAction = "";
  let researchPlan: Record<string, unknown> | undefined;
  let retrievalReview: Record<string, unknown> | undefined;
  let knowledgeReview: Record<string, unknown> | undefined;
  await page.route("**/api/cogdoc/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/cogdoc/v1", "");
    if (path === "/auth/config") return json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: false });
    if (path === "/auth/login") return json(route, session);
    if (path === "/auth/me") return json(route, { schema_version: "v1", user, workspace, permissions: session.permissions, workspaces: [workspace] });
    if (path === "/knowledge-bases") return json(route, [{ kb_id: "policies", created_at: "2026-08-01T00:00:00Z", document_count: 8, tenant_id: workspace.workspace_id, owner_id: user.user_id }]);
    if (path === "/research-jobs/summaries") return json(route, { items: [] });
    if (path === "/research-jobs" && request.method() === "GET") return json(route, { items: [{ job_id: "research-1", kb_id: "policies", title: "Policy review", objective: "Review security policy evidence", status: "planned", revision: 3 }] });
    if (path === "/research-jobs/research-1" && request.method() === "GET") return json(route, { job_id: "research-1", kb_id: "policies", title: "Policy review", objective: "Review security policy evidence", status: "planned", revision: 3, sections: [{ section_id: "s1", title: "Password policy", research_question: "What password controls are required?", evidence_requirements: [{ question: "What is the minimum password length?", retrieval_query: "minimum password length", recovery_query: "credential character requirement" }], success_criteria: "Cite the policy." }] });
    if (path === "/research-jobs/research-1/plan" && request.method() === "PUT") { researchPlan = request.postDataJSON() as Record<string, unknown>; return json(route, { status: "planned", revision: 4 }); }
    if (path === "/research-jobs/research-1/report") return route.fulfill({ status: 200, contentType: "text/markdown", body: "# Policy review" });
    if (path === "/research-jobs/research-1/provenance") return json(route, { job_id: "research-1", status: "current", stale_reasons: [], captured: { source_versions: [{ source: "security.pdf", sha256: "sha-1" }] }, current: { source_versions: [{ source: "security.pdf", sha256: "sha-1" }] } });
    if (path === "/research-jobs/research-1/start" && request.method() === "POST") { researchAction = path; return json(route, { status: "running", revision: 4 }); }
    if (path === "/knowledge" && request.method() === "GET") return json(route, { items: [{ knowledge_id: "knowledge-1", kb_id: "policies", text: "Passwords must contain at least twelve characters.", status: "pending", certainty: "high", origin: "manual_entry", related_source: "security.pdf" }] });
    if (path === "/knowledge/knowledge-1/approve" && request.method() === "POST") { knowledgeReview = request.postDataJSON() as Record<string, unknown>; return json(route, { status: "approved" }); }
    if (path === "/claim-verification/reviews") return json(route, { items: [] });
    if (path === "/claim-verification/reviews/summary") return json(route, { pending_count: 0, reviewed_count: 0 });
    if (path === "/retrieval-eval-drafts" && request.method() === "GET") return json(route, { drafts: [{ draft_id: "draft-1", status: "pending", revision: 2, query: "What is the password policy?", units: [{ unit_id: "u1", label: "Password policy", retrieval_query: "password policy", recovery_query: "credential requirements", expected_status: "supported", acceptable_evidence: [], hard_negative_chunks: [] }] }] });
    if (path === "/retrieval-eval-drafts/draft-1") return json(route, { draft_id: "draft-1", status: "pending", revision: 2, query: "What is the password policy?", units: [{ unit_id: "u1", label: "Password policy", retrieval_query: "password policy", recovery_query: "credential requirements", expected_status: "supported", acceptable_evidence: [], hard_negative_chunks: [] }] });
    if (path === "/retrieval-eval-drafts/draft-1/candidates") return json(route, { candidates: [{ rank: 1, chunk_id: "chunk-1", source: "security.pdf", source_sha256: "sha-1", text: "Passwords must contain at least twelve characters." }] });
    if (path === "/retrieval-eval-drafts/draft-1/review" && request.method() === "POST") { retrievalReview = request.postDataJSON() as Record<string, unknown>; return json(route, { status: "approved", revision: 3 }); }
    return json(route, { items: [], jobs: [], summaries: [] });
  });

  await page.goto("/login");
  await page.getByLabel("邮箱").fill("reviewer@example.com");
  await page.getByLabel("密码").fill("a-valid-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/\/home$/);

  await page.goto("/knowledge/policies/knowledge");
  await expect(page.getByRole("heading", { name: "派生知识", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "审核", exact: true }).click();
  await expect(page.getByRole("heading", { name: "审核派生知识" })).toBeVisible();
  await page.getByLabel("审核意见").fill("内容与关联文档一致");
  await page.getByRole("button", { name: "通过并发布" }).click();
  await expect.poll(() => knowledgeReview).toEqual({ note: "内容与关联文档一致" });

  await page.goto("/research");
  await expect(page.getByRole("heading", { name: "Policy review" })).toBeVisible();
  await page.getByRole("button", { name: "编辑计划" }).click();
  await page.getByLabel("标题", { exact: true }).fill("Password and session policy");
  await page.getByRole("button", { name: "保存计划修订" }).click();
  await expect.poll(() => researchPlan).toMatchObject({ expected_revision: 3, sections: [{ title: "Password and session policy", research_question: "What password controls are required?", evidence_requirements: [{ question: "What is the minimum password length?", retrieval_query: "minimum password length", recovery_query: "credential character requirement" }] }] });
  await page.getByRole("button", { name: "开始", exact: true }).click();
  await expect.poll(() => researchAction).toBe("/research-jobs/research-1/start");

  await page.goto("/knowledge/policies/diagnostics");
  await page.getByRole("tab", { name: "RAG 评测", exact: true }).click();
  await expect(page).toHaveURL(/\/knowledge\/policies\/diagnostics\?tab=rag$/);
  await expect(page.getByRole("tab", { name: /派生知识/ })).toHaveCount(0);
  await page.getByRole("tab", { name: /检索标注/ }).click();
  const reviewPanel = page.getByText("Password policy", { exact: true }).last().locator("xpath=ancestor::section[1]");
  await expect(reviewPanel).toBeVisible();
  const candidateSelect = reviewPanel.getByRole("combobox").last();
  await candidateSelect.click();
  await page.getByRole("option", { name: "正确证据" }).click();
  await page.getByRole("button", { name: "确认并通过" }).click();
  await expect.poll(() => retrievalReview).toMatchObject({
    decision: "approved",
    expected_revision: 2,
    annotations: { units: [{ unit_id: "u1", expected_status: "supported", acceptable_evidence: [{ chunk_id: "chunk-1", source: "security.pdf", source_sha256: "sha-1" }] }] },
  });
});

test("review authorization failures explain the capability instead of exposing a raw 403", async ({ page }) => {
  await page.route("**/api/cogdoc/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/cogdoc/v1", "");
    if (path === "/auth/config") return json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: false });
    if (path === "/auth/login") return json(route, session);
    if (path === "/auth/me") return json(route, { schema_version: "v1", user, workspace, permissions: session.permissions, workspaces: [workspace] });
    if (path === "/knowledge-bases") return json(route, [{ kb_id: "policies", created_at: "2026-08-01T00:00:00Z", document_count: 8, tenant_id: workspace.workspace_id, owner_id: user.user_id }]);
    if (path === "/retrieval-eval-drafts") return json(route, { detail: "独立审核接口未启用" }, 403);
    return json(route, { items: [] });
  });

  await page.goto("/login");
  await page.getByLabel("邮箱").fill("reviewer@example.com");
  await page.getByLabel("密码").fill("a-valid-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/diagnostics?tab=rag");

  await expect(page.getByRole("heading", { name: "RAG 评测", exact: true })).toBeVisible();
  await expect(page.getByText("RAG 评测服务尚未加载账号权限")).toBeVisible();
  await expect(page.getByText("Request failed with status 403")).toHaveCount(0);
});

test("stale retrieval evaluation drafts recover through a fresh diagnostic instead of showing Conflict", async ({ page }) => {
  let candidateReads = 0;
  let diagnosticPayload: Record<string, unknown> | undefined;
  await page.route("**/api/cogdoc/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/cogdoc/v1", "");
    if (path === "/auth/config") return json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: false });
    if (path === "/auth/login") return json(route, session);
    if (path === "/auth/me") return json(route, { schema_version: "v1", user, workspace, permissions: session.permissions, workspaces: [workspace] });
    if (path === "/knowledge-bases") return json(route, [{ kb_id: "policies", created_at: "2026-08-01T00:00:00Z", document_count: 1, tenant_id: workspace.workspace_id, owner_id: user.user_id }]);
    if (path === "/retrieval-eval-drafts") return json(route, { drafts: [{ draft_id: "draft-stale", status: "pending", revision: 1, query: "保留期是多少？", is_stale: true, stale_reasons: ["index_generation_changed"], units: [{ unit_id: "r1", label: "保留期", retrieval_query: "保留期是多少？", recovery_query: "", expected_status: "supported", acceptable_evidence: [], hard_negative_chunks: [] }] }] });
    if (path === "/retrieval-eval-drafts/draft-stale") return json(route, { draft: { draft_id: "draft-stale", status: "pending", revision: 1, query: "保留期是多少？", is_stale: true, stale_reasons: ["index_generation_changed"], units: [{ unit_id: "r1", label: "保留期", retrieval_query: "保留期是多少？", recovery_query: "", expected_status: "supported", acceptable_evidence: [], hard_negative_chunks: [] }] } });
    if (path === "/retrieval-eval-drafts/draft-stale/candidates") { candidateReads += 1; return json(route, { detail: { message: "索引版本已变化，不能继续标注这个草稿", reasons: ["index_generation_changed"] } }, 409); }
    if (path === "/retrieval-diagnostics" && request.method() === "POST") {
      diagnosticPayload = request.postDataJSON() as Record<string, unknown>;
      return json(route, { schema_version: "v1", kb_id: "policies", query: "保留期是多少？", routes: [], channel_counts: {}, ranking_count: 1, final: [{ rank: 1, chunk_id: "chunk-current", source: "policy.pdf", source_sha256: "sha-current", text_preview: "记录保留三十天。", retrieval: { score: 0.91 } }], decision: { supported: true, score: 0.91 }, latency_ms: { total: 24 } });
    }
    if (path === "/sessions") return json(route, { schema_version: "v1", doc_id: "policies", sessions: [] });
    return json(route, { items: [] });
  });

  await page.goto("/login");
  await page.getByLabel("邮箱").fill("reviewer@example.com");
  await page.getByLabel("密码").fill("a-valid-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/diagnostics?tab=rag");

  await expect(page.getByText("这份评测草稿需要更新")).toBeVisible();
  await expect(page.getByText("Conflict")).toHaveCount(0);
  expect(candidateReads).toBe(0);
  await page.getByRole("button", { name: "按当前索引重新诊断" }).click();
  await expect(page.getByRole("tab", { name: "检索诊断" })).toHaveAttribute("data-state", "active");
  await expect(page.getByText("记录保留三十天。", { exact: true })).toBeVisible();
  await expect.poll(() => diagnosticPayload).toMatchObject({ doc_id: "policies", query: "保留期是多少？", top_k: 12, rerank: true });
});

test("task center aggregates ingestion and opens the exact research task", async ({ page }) => {
  let researchAction = "";
  await page.route("**/api/cogdoc/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/cogdoc/v1", "");
    if (path === "/auth/config") return json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: false });
    if (path === "/auth/login") return json(route, session);
    if (path === "/auth/me") return json(route, { schema_version: "v1", user, workspace, permissions: session.permissions, workspaces: [workspace] });
    if (path === "/knowledge-bases") return json(route, [
      { kb_id: "policies", created_at: "2026-08-01T00:00:00Z", document_count: 8, tenant_id: workspace.workspace_id, owner_id: user.user_id },
      { kb_id: "finance", created_at: "2026-08-02T00:00:00Z", document_count: 2, tenant_id: workspace.workspace_id, owner_id: user.user_id },
    ]);
    if (path === "/index-jobs") return json(route, { schema_version: "v1", jobs: [{ job_id: "index-1", kb_id: "policies", status: "running", created_at: "2026-08-24T08:00:00Z", document_count: null, chunk_count: null }] });
    if (path === "/sync-jobs") return json(route, { jobs: [{ job_id: "sync-1", kb_id: "policies", connection_name: "Security wiki", connector_type: "notion", status: "running", updated_at: "2026-08-24T08:30:00Z" }] });
    if (path === "/research-jobs/summaries") return json(route, { schema_version: "v1", jobs: [
      { job_id: "research-done", kb_id: "policies", title: "Completed review", objective_preview: "Completed", status: "completed", updated_at: "2026-08-24T07:00:00Z" },
      { job_id: "research-plan", kb_id: "finance", title: "Finance controls", objective_preview: "Review finance controls", status: "planned", updated_at: "2026-08-24T09:00:00Z" },
    ] });
    if (path === "/ha/jobs") return json(route, { schema_version: "v1", jobs: [] });
    if (path === "/audit-events/exports") return json(route, { schema_version: "v1", exports: [] });
    if (path === "/research-jobs/research-plan/start" && request.method() === "POST") { researchAction = path; return json(route, { status: "running" }); }
    if (path === "/research-jobs" && request.method() === "GET") return json(route, { jobs: [{ job_id: "research-plan", kb_id: "finance", title: "Finance controls", objective: "Review finance controls", status: "planned", revision: 1 }] });
    if (path === "/research-jobs/research-plan") return json(route, { job_id: "research-plan", kb_id: "finance", title: "Finance controls", objective: "Review finance controls", status: "planned", revision: 1, sections: [] });
    if (path === "/research-jobs/research-plan/report") return route.fulfill({ status: 200, contentType: "text/markdown", body: "" });
    if (path === "/research-jobs/research-plan/provenance") return json(route, { captured: { source_versions: [] } });
    return json(route, { items: [], jobs: [] });
  });

  await page.goto("/login");
  await page.getByLabel("邮箱").fill("reviewer@example.com");
  await page.getByLabel("密码").fill("a-valid-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/tasks");

  await expect(page.getByText("文档解析与索引", { exact: true }).first()).toBeVisible();
  const syncRow = page.getByRole("row", { name: /Security wiki/ });
  await expect(syncRow.getByText("外部同步", { exact: true })).toBeVisible();
  await syncRow.getByRole("link", { name: "打开所属资源" }).click();
  await expect(page).toHaveURL(/\/integrations\?kb=policies&tab=jobs$/);
  await expect(page.getByRole("tab", { name: /同步任务/ })).toHaveAttribute("data-state", "active");
  await page.goto("/tasks");
  const completedRow = page.getByRole("row", { name: /Completed review/ });
  await expect(completedRow.getByRole("button", { name: /启动|暂停|恢复/ })).toHaveCount(0);
  const plannedRow = page.getByRole("row", { name: /Finance controls/ });
  await plannedRow.getByRole("button", { name: "启动任务" }).click();
  await expect.poll(() => researchAction).toBe("/research-jobs/research-plan/start");
  await plannedRow.getByRole("link", { name: "打开所属资源" }).click();
  await expect(page).toHaveURL(/\/research\?kb=finance&job=research-plan$/);
  await expect(page.getByRole("heading", { name: "Finance controls" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "知识库", exact: true })).toContainText("finance");
});

test("task center tolerates legacy aggregate routes and disabled HA", async ({ page }) => {
  await page.route("**/api/cogdoc/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/cogdoc/v1", "");
    if (path === "/auth/config") return json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: false });
    if (path === "/auth/login") return json(route, session);
    if (path === "/auth/me") return json(route, { schema_version: "v1", user, workspace, permissions: session.permissions, workspaces: [workspace] });
    if (path === "/knowledge-bases") return json(route, [{ kb_id: "policies", created_at: "2026-08-01T00:00:00Z", document_count: 8, tenant_id: workspace.workspace_id, owner_id: user.user_id }]);
    if (path === "/index-jobs" || path === "/sync-jobs") return json(route, { message: "Not Found" }, 404);
    if (path === "/knowledge-bases/policies/sync-jobs") return json(route, { jobs: [{ job_id: "sync-legacy", kb_id: "policies", connection_name: "Legacy connector", status: "succeeded", created_at: "2026-08-24T08:00:00Z" }] });
    if (path === "/ha/jobs") return json(route, { detail: "HA 控制面未启用" }, 503);
    if (path === "/research-jobs/summaries") return json(route, { jobs: [] });
    if (path === "/audit-events/exports") return json(route, { exports: [] });
    return json(route, { items: [], jobs: [] });
  });

  await page.goto("/login");
  await page.getByLabel("邮箱").fill("reviewer@example.com");
  await page.getByLabel("密码").fill("a-valid-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/tasks");

  await expect(page.getByRole("row", { name: /Legacy connector/ })).toBeVisible();
  await expect(page.getByText("部分任务暂时无法读取")).toHaveCount(0);
});

test("derived knowledge binds a document and role creation stays name-first", async ({ page }) => {
  let knowledgePayload: Record<string, unknown> | undefined;
  let rolePayload: Record<string, unknown> | undefined;
  let memberRolePayload: Record<string, unknown> | undefined;
  const roleRows = ["owner", "admin", "editor", "reviewer", "viewer"].map((roleId) => ({
    role_id: roleId,
    workspace_id: workspace.workspace_id,
    name: roleId,
    description: "",
    base_role: roleId,
    system: true,
    member_count: roleId === "owner" ? 1 : 0,
    revision: 0,
  }));
  roleRows.push({
    role_id: "rol-legal",
    workspace_id: workspace.workspace_id,
    name: "legal",
    description: "Legal documents",
    base_role: "viewer",
    system: false,
    member_count: 1,
    revision: 1,
  });

  await page.route("**/api/cogdoc/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/cogdoc/v1", "");
    if (path === "/auth/config") return json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: false });
    if (path === "/auth/login") return json(route, session);
    if (path === "/auth/me") return json(route, { schema_version: "v1", user, workspace, permissions: session.permissions, workspaces: [workspace] });
    if (path === "/knowledge-bases") return json(route, [{ kb_id: "policies", created_at: "2026-08-01T00:00:00Z", document_count: 1, tenant_id: workspace.workspace_id, owner_id: user.user_id, embedding_profile_id: "local", embedding_model: "BAAI/bge-m3" }]);
    if (path === "/knowledge-bases/policies/documents") return json(route, [{ name: "security.pdf", sha256: "sha-security", document_id: "doc-security", source_id: "src-security", version_id: "ver-1", connector_type: "legacy-upload", media_type: "application/pdf", kind: "file", role_ids: ["owner", "admin"] }]);
    if (path === "/knowledge" && request.method() === "POST") {
      knowledgePayload = request.postDataJSON() as Record<string, unknown>;
      return json(route, { knowledge: { knowledge_id: "kn-1", ...knowledgePayload, status: "pending" }, deduplicated: false, requires_review: false, conflicts: [] }, 201);
    }
    if (path === "/knowledge") return json(route, { knowledge: [] });
    if (path === `/workspaces/${workspace.workspace_id}/roles` && request.method() === "POST") {
      rolePayload = request.postDataJSON() as Record<string, unknown>;
      const createdRole = { role_id: "rol-finance", workspace_id: workspace.workspace_id, name: String(rolePayload.name ?? ""), description: String(rolePayload.description ?? ""), base_role: "viewer", system: false, member_count: 0, revision: 1 };
      roleRows.push(createdRole);
      return json(route, { schema_version: "v1", role: createdRole }, 201);
    }
    if (path === `/workspaces/${workspace.workspace_id}/roles`) return json(route, { schema_version: "v1", workspace_id: workspace.workspace_id, roles: roleRows });
    if (path === `/workspaces/${workspace.workspace_id}/members/member-custom` && request.method() === "PATCH") {
      memberRolePayload = request.postDataJSON() as Record<string, unknown>;
      return json(route, { schema_version: "v1", member: { member_id: "member-custom", role: "admin", role_id: "admin", revision: 8 } });
    }
    if (path === `/workspaces/${workspace.workspace_id}/members`) return json(route, { schema_version: "v1", workspace_id: workspace.workspace_id, members: [{ member_id: "member-custom", user_id: "user-custom", display_name: "Custom Member", email: "custom@example.com", role: "viewer", base_role: "viewer", role_id: "rol-legal", role_name: "legal", status: "active", revision: 7 }] });
    if (path === `/workspaces/${workspace.workspace_id}/invites`) return json(route, { schema_version: "v1", workspace_id: workspace.workspace_id, invites: [] });
    if (path === "/sessions") return json(route, { schema_version: "v1", doc_id: "policies", sessions: [] });
    return json(route, { items: [], jobs: [], summaries: [] });
  });

  await page.goto("/login");
  await page.getByLabel("邮箱").fill("reviewer@example.com");
  await page.getByLabel("密码").fill("a-valid-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();

  await page.goto("/knowledge/policies/knowledge");
  await page.getByRole("button", { name: "新增知识" }).first().click();
  await page.getByLabel("关联文档").click();
  await page.getByRole("option", { name: "security.pdf" }).click();
  await page.getByLabel("知识内容").fill("会话策略由安全团队统一维护。");
  await page.getByRole("button", { name: "保存为待审核" }).click();
  await expect.poll(() => knowledgePayload).toMatchObject({
    kb_id: "policies",
    related_document_id: "doc-security",
    related_source: "security.pdf",
    related_source_sha256: "sha-security",
  });

  await page.goto("/admin");
  await expect(page.getByRole("row", { name: /owner/ })).toBeVisible();
  await expect(page.getByRole("row", { name: /admin/ })).toBeVisible();
  await expect(page.getByRole("row", { name: /^viewer\b/ })).toBeVisible();
  const memberRole = page.getByRole("combobox", { name: "修改 Custom Member 的角色" });
  await expect(memberRole).toContainText("legal");
  await memberRole.click();
  await page.getByRole("option", { name: "admin", exact: true }).click();
  await expect.poll(() => memberRolePayload).toEqual({ role_id: "admin", expected_revision: 7 });
  await page.getByRole("button", { name: "添加角色" }).click();
  await page.getByLabel("角色名称").fill("finance");
  await page.getByLabel("说明（可选）").fill("Finance documents");
  await page.getByRole("button", { name: "添加角色" }).last().click();
  await expect.poll(() => rolePayload).toEqual({ name: "finance", description: "Finance documents" });
  await expect(page.getByRole("row", { name: /finance/ })).toBeVisible();

  await page.goto("/knowledge");
  await page.locator("#main-content").getByRole("button", { name: "创建知识库" }).first().click();
  await expect(page.getByRole("button", { name: /finance.*0 人/ })).toBeVisible();
});

test("trace debugger preserves filters, config, structured steps, evidence and refresh", async ({ page }) => {
  let traceReads = 0;
  let diagnosticPayload: Record<string, unknown> | undefined;
  let diagnosticLabelPayload: Record<string, unknown> | undefined;
  await page.route("**/api/cogdoc/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/cogdoc/v1", "");
    if (path === "/auth/config") return json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: false });
    if (path === "/auth/login") return json(route, session);
    if (path === "/auth/me") return json(route, { schema_version: "v1", user, workspace, permissions: session.permissions, workspaces: [workspace] });
    if (path === "/knowledge-bases") return json(route, [{ kb_id: "policies", created_at: "2026-08-01T00:00:00Z", document_count: 1, tenant_id: workspace.workspace_id, owner_id: user.user_id, embedding_profile_id: "local", embedding_model: "BAAI/bge-m3" }]);
    if (path === "/traces") return json(route, { schema_version: "v1", traces: [
      { trace_id: "trace-qa", request_id: "req-qa", query_preview: "会话策略由谁维护？", task_type: "qa", status: "ok", duration_ms: 128, modified_at: "2026-08-24T08:00:00Z", summary: { step_count: 3, error_count: 0, evidence_ref_count: 1, node_names: ["intent_router", "rewrite_node", "retrieve_node"] } },
      { trace_id: "trace-summary", request_id: "req-summary", query_preview: "总结安全文档", task_type: "summary", status: "degraded", duration_ms: 310, modified_at: "2026-08-24T07:00:00Z", summary: { step_count: 1, error_count: 1, evidence_ref_count: 0, node_names: ["summary_subgraph"] } },
    ] });
    if (path === "/traces/trace-qa") {
      traceReads += 1;
      return json(route, {
        schema_version: "v1", trace_id: "trace-qa", request_id: "req-qa", task_type: "qa", status: "ok", execution_status: "SUCCESS", duration_ms: 128, evidence_completeness: 1,
        input: { query: "会话策略由谁维护？" }, output: { answer: "安全团队维护。" }, config: { doc_id: "policies", session_id: "session-1", query_preview: "会话策略由谁维护？", qa_retrieval_top_k: 9 },
        summary: { step_count: 3, error_count: 0, evidence_ref_count: 1, node_names: ["intent_router", "rewrite_node", "retrieve_node"] }, error: null,
        steps: [
          { node_name: "intent_router", duration_ms: 12, task_type: "qa", model: "router-model", router_reason: "识别为文档问答", counts: {}, evidence: [] },
          { node_name: "rewrite_node", duration_ms: 24, model: "rewrite-model", rewritten_queries: ["会话安全策略维护团队"], counts: { rewritten_query_count: 1 }, evidence: [] },
          { node_name: "retrieve_node", duration_ms: 44, retrieval_top_k: 9, retrieval_top_k_used: 12, counts: { retrieved_count: 1 }, evidence: [{ chunk_id: "chunk-security", source: "security.pdf", page: 3, text_preview: "会话策略由安全团队统一维护。" }] },
        ],
      });
    }
    if (path === "/traces/trace-summary") return json(route, { schema_version: "v1", trace_id: "trace-summary", request_id: "req-summary", task_type: "summary", status: "degraded", execution_status: "TARGET_ERROR", duration_ms: 310, evidence_completeness: 0.33, input: {}, output: {}, config: { doc_id: "policies", query_preview: "总结安全文档" }, summary: { step_count: 1, error_count: 1, evidence_ref_count: 0, node_names: ["summary_subgraph"] }, error: { error_class: "ModelTimeout", message: "模型响应超时" }, steps: [{ node_name: "summary_subgraph", duration_ms: 300, error_class: "ModelTimeout", counts: {}, evidence: [] }] });
    if (path === "/retrieval-diagnostics" && request.method() === "POST") {
      diagnosticPayload = request.postDataJSON() as Record<string, unknown>;
      return json(route, { schema_version: "v1", kb_id: "policies", query: "会话策略", routes: [{ query: "会话策略", channel: "rag_vector", hits: [] }, { query: "会话策略", channel: "rag_bm25", hits: [] }], channel_counts: { rag_vector: 8, rag_bm25: 5 }, ranking_count: 13, final: [{ rank: 1, chunk_id: "chunk-diagnostic", source: "security.pdf", source_sha256: "sha-security", page_start: 3, page_end: 3, text_preview: "会话策略由安全团队统一维护。", rank_before_rerank: 3, rank_delta: 2, retrieval: { score: 0.9321 } }], decision: { supported: true, score: 0.93, reason: "supported" }, latency_ms: { retrieval: 42, rerank: 11, total: 53 } });
    }
    if (path === "/retrieval-diagnostics/labels" && request.method() === "POST") {
      diagnosticLabelPayload = request.postDataJSON() as Record<string, unknown>;
      return json(route, { schema_version: "v1", draft: { draft_id: "draft-from-diagnostic", status: "pending", revision: 1, query: "会话策略" } });
    }
    if (path === "/retrieval-eval-drafts" && request.method() === "GET") return json(route, { drafts: diagnosticLabelPayload ? [{ draft_id: "draft-from-diagnostic", status: "pending", revision: 1, query: "会话策略", units: [{ unit_id: "r1", label: "会话策略", retrieval_query: "会话策略", recovery_query: "会话策略", expected_status: "supported", acceptable_evidence: [], hard_negative_chunks: [] }] }] : [] });
    if (path === "/retrieval-eval-drafts/draft-from-diagnostic") return json(route, { draft_id: "draft-from-diagnostic", status: "pending", revision: 1, query: "会话策略", units: [{ unit_id: "r1", label: "会话策略", retrieval_query: "会话策略", recovery_query: "会话策略", expected_status: "supported", acceptable_evidence: [], hard_negative_chunks: [] }] });
    if (path === "/retrieval-eval-drafts/draft-from-diagnostic/candidates") return json(route, { candidates: [{ rank: 1, chunk_id: "chunk-diagnostic", source: "security.pdf", source_sha256: "sha-security", text: "会话策略由安全团队统一维护。" }] });
    if (path === "/sessions") return json(route, { schema_version: "v1", doc_id: "policies", sessions: [] });
    return json(route, { items: [], jobs: [], summaries: [] });
  });

  await page.goto("/login");
  await page.getByLabel("邮箱").fill("reviewer@example.com");
  await page.getByLabel("密码").fill("a-valid-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/diagnostics");

  await expect(page.getByLabel("Trace 状态")).toBeVisible();
  await expect(page.getByLabel("Trace 任务")).toBeVisible();
  await page.getByText("请求配置", { exact: true }).click();
  await expect(page.getByText(/qa_retrieval_top_k/)).toBeVisible();

  await page.getByText("意图路由", { exact: true }).click();
  await expect(page.getByText("识别为文档问答", { exact: true })).toBeVisible();
  await expect(page.getByText("router-model", { exact: true })).toBeVisible();
  await page.getByText("问题改写", { exact: true }).click();
  await expect(page.getByText("会话安全策略维护团队", { exact: true })).toBeVisible();
  await page.getByText("召回检索", { exact: true }).click();
  await expect(page.getByText("会话策略由安全团队统一维护。", { exact: true })).toBeVisible();
  await expect(page.getByText("chunk-security", { exact: true })).toBeVisible();

  await page.getByLabel("Trace 状态").click();
  await page.getByRole("option", { name: "degraded" }).click();
  await expect(page.getByText("总结安全文档", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("运行错误", { exact: true })).toBeVisible();
  await expect(page.getByText(/"error_class": "ModelTimeout"/).first()).toBeVisible();

  await page.getByLabel("Trace 状态").click();
  await page.getByRole("option", { name: "全部状态" }).click();
  await page.getByText("会话策略由谁维护？", { exact: true }).first().click();
  await page.getByRole("button", { name: "刷新 Trace" }).click();
  await expect.poll(() => traceReads).toBeGreaterThan(1);

  await page.getByRole("tab", { name: "检索诊断" }).click();
  await page.getByLabel("检索问题").fill("会话策略");
  await page.getByRole("button", { name: "运行诊断" }).click();
  await expect(page.getByText("会话策略由安全团队统一维护。", { exact: true })).toBeVisible();
  await expect(page.getByText("最终召回").locator(".." )).toContainText("1");
  await expect(page.getByText("score 0.9321", { exact: true })).toBeVisible();
  await expect.poll(() => diagnosticPayload).toMatchObject({ doc_id: "policies", query: "会话策略", top_k: 12, rerank: true });
  await page.getByRole("button", { name: "将第 1 条标为正确证据" }).click();
  await page.getByRole("button", { name: "保存到 RAG 评测" }).click();
  await expect.poll(() => diagnosticLabelPayload).toMatchObject({
    doc_id: "policies",
    query: "会话策略",
    no_answer: false,
    acceptable_evidence: [{ chunk_id: "chunk-diagnostic", source: "security.pdf", source_sha256: "sha-security" }],
  });
  await expect(page).toHaveURL(/\/knowledge\/policies\/diagnostics\?tab=rag$/);
  await expect(page.getByText("会话策略", { exact: true }).first()).toBeVisible();
});

test("ordinary account users can manage their own sessions without workspace admin permission", async ({ page }) => {
  const memberWorkspace = { ...workspace, role: "viewer" };
  const memberUser = { ...user, display_name: "Review Member" };
  const memberSession = { ...session, user: memberUser, workspace: memberWorkspace, permissions: ["read", "query"] };
  let revokedSession = "";
  await page.route("**/api/cogdoc/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/cogdoc/v1", "");
    if (path === "/auth/config") return json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: false });
    if (path === "/auth/login") return json(route, memberSession);
    if (path === "/auth/me") return json(route, { schema_version: "v1", user: memberUser, workspace: memberWorkspace, permissions: memberSession.permissions, workspaces: [memberWorkspace] });
    if (path === "/auth/sessions") return json(route, { schema_version: "v1", sessions: [
      { session_id: "session-current", created_at: "2026-08-24T08:00:00Z", last_seen_at: "2026-08-24T08:10:00Z", expires_at: "2026-08-25T08:00:00Z", current: true },
      { session_id: "session-other", created_at: "2026-08-20T08:00:00Z", last_seen_at: "2026-08-23T08:10:00Z", expires_at: "2026-08-25T08:00:00Z", current: false },
    ] });
    if (path === "/auth/sessions/session-other" && request.method() === "DELETE") { revokedSession = "session-other"; return route.fulfill({ status: 204 }); }
    if (path === "/knowledge-bases") return json(route, []);
    return json(route, { items: [] });
  });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("reviewer@example.com");
  await page.getByLabel("密码").fill("a-valid-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("button", { name: /Review Member/ }).click();
  await page.getByRole("menuitem", { name: "账号安全" }).click();
  await expect(page.getByRole("heading", { name: "账号与会话安全" })).toBeVisible();
  await expect(page.getByText("工作区会话策略", { exact: true })).toHaveCount(0);
  await expect(page.getByText("session-current", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "撤销" }).nth(1).click();
  await expect.poll(() => revokedSession).toBe("session-other");
});

test("derived knowledge revision and retrieval feedback controls preserve governance actions", async ({ page }) => {
  let revisionPayload: Record<string, unknown> | undefined;
  let retrievalAction = "";
  await page.route("**/api/cogdoc/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/cogdoc/v1", "");
    if (path === "/auth/config") return json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: false });
    if (path === "/auth/login") return json(route, session);
    if (path === "/auth/me") return json(route, { schema_version: "v1", user, workspace, permissions: session.permissions, workspaces: [workspace] });
    if (path === "/knowledge-bases") return json(route, [{ kb_id: "policies", created_at: "2026-08-01T00:00:00Z", document_count: 1, tenant_id: workspace.workspace_id, owner_id: user.user_id, embedding_profile_id: "local", embedding_model: "BAAI/bge-m3" }]);
    if (path === "/knowledge-bases/policies/documents") return json(route, [{ name: "security.pdf", sha256: "sha-security", document_id: "doc-security", source_id: "src-security", version_id: "ver-1", connector_type: "legacy-upload", media_type: "application/pdf", kind: "file" }]);
    if (path === "/knowledge" && request.method() === "GET") return json(route, { knowledge: [{ knowledge_id: "kn-old", kb_id: "policies", text: "旧的会话策略说明。", status: "approved", certainty: "medium", related_document_id: "doc-security", related_source: "security.pdf", related_source_sha256: "sha-security" }] });
    if (path === "/knowledge/kn-old/revise" && request.method() === "POST") { revisionPayload = request.postDataJSON() as Record<string, unknown>; return json(route, { knowledge: { knowledge_id: "kn-new", status: "pending" } }, 201); }
    if (path === "/retrieval-feedback" && request.method() === "GET") return json(route, { retrieval_feedback: [{ retrieval_feedback_id: "rf-1", query: "会话策略", feedback_type: "downrank", reason: "历史误召回", enabled: true }] });
    if (path === "/retrieval-feedback/rf-1/disable" && request.method() === "POST") { retrievalAction = path; return json(route, { status: "disabled", retrieval_feedback_id: "rf-1" }); }
    if (path === "/knowledge/pending-count") return json(route, { kb_id: "policies", pending: 0, stale: 0, total: 0 });
    if (path === "/knowledge/index-status") return json(route, { kb_id: "policies", state: "ready", approved_count: 1, indexed_count: 1 });
    if (path === "/feedback-loop-metrics") return json(route, { counts: { approved_knowledge_total: 1, feedback_total: 0 } });
    if (path === "/feedback" || path === "/feedback-analysis") return json(route, { items: [] });
    return json(route, { items: [] });
  });
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("reviewer@example.com");
  await page.getByLabel("密码").fill("a-valid-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/knowledge");
  await page.getByRole("button", { name: "修订派生知识" }).click();
  await page.getByLabel(/知识内容/).last().fill("新的会话策略由安全团队维护。");
  await page.getByRole("button", { name: "保存修订版本" }).click();
  await expect.poll(() => revisionPayload).toMatchObject({ text: "新的会话策略由安全团队维护。", certainty: "medium" });
  await page.getByRole("tab", { name: /检索调权/ }).click();
  await expect(page.getByText("会话策略", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "停用" }).click();
  await expect.poll(() => retrievalAction).toBe("/retrieval-feedback/rf-1/disable");
});

test("batch knowledge review confirms scope and reports partial success", async ({ page }) => {
  let batchPayload: Record<string, unknown> | undefined;
  await page.route("**/api/cogdoc/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/cogdoc/v1", "");
    if (path === "/auth/config") return json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: false });
    if (path === "/auth/login") return json(route, session);
    if (path === "/auth/me") return json(route, { schema_version: "v1", user, workspace, permissions: session.permissions, workspaces: [workspace] });
    if (path === "/knowledge-bases") return json(route, [{ kb_id: "policies", created_at: "2026-08-01T00:00:00Z", document_count: 1, tenant_id: workspace.workspace_id, owner_id: user.user_id }]);
    if (path === "/knowledge" && request.method() === "GET") return json(route, { knowledge: [{ knowledge_id: "kn-raced", kb_id: "policies", text: "待审核策略。", status: "pending", certainty: "medium" }] });
    if (path === "/knowledge/batch-approve" && request.method() === "POST") {
      batchPayload = request.postDataJSON() as Record<string, unknown>;
      return json(route, { updated: [], missing_ids: ["kn-raced"] });
    }
    if (path === "/knowledge/pending-count") return json(route, { kb_id: "policies", pending: 1, stale: 0, total: 1 });
    if (path === "/knowledge/index-status") return json(route, { kb_id: "policies", state: "ready", approved_count: 0, indexed_count: 0 });
    if (path === "/feedback-loop-metrics") return json(route, { counts: { approved_knowledge_total: 0, feedback_total: 0 } });
    if (path === "/feedback" || path === "/feedback-analysis" || path === "/retrieval-feedback") return json(route, { items: [] });
    if (path === "/knowledge-bases/policies/documents") return json(route, []);
    return json(route, { items: [] });
  });

  await page.goto("/login");
  await page.getByLabel("邮箱").fill("reviewer@example.com");
  await page.getByLabel("密码").fill("a-valid-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.goto("/knowledge/policies/knowledge");
  await page.getByRole("combobox").first().click();
  await page.getByRole("option", { name: "pending" }).click();
  await page.getByRole("button", { name: "批量通过当前列表" }).click();
  await expect(page.getByRole("heading", { name: "批量通过派生知识" })).toBeVisible();
  await page.getByRole("button", { name: "确认处理 1 条" }).click();

  await expect.poll(() => batchPayload).toMatchObject({ knowledge_ids: ["kn-raced"] });
  await expect(page.getByText("已通过 0 条，另有 1 条因列表变化未处理")).toBeVisible();
});
