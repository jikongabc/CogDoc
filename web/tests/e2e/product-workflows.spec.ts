import { expect, test, type Route } from "@playwright/test";

const workspace = { workspace_id: "ws-enterprise", name: "Enterprise Knowledge", role: "owner", revision: 1, created_at: "2026-08-01T00:00:00Z" };
const user = { user_id: "usr-reviewer", email: "reviewer@example.com", display_name: "Review Owner" };
const session = { schema_version: "v1", access_token: "workflow-token", token_type: "bearer", expires_at: "2026-09-01T00:00:00Z", user, workspace, permissions: ["read", "query", "write", "delete", "review", "publish", "manage_access", "manage_tenant"] };

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

test("research lifecycle and retrieval review use authoritative backend transitions", async ({ page }) => {
  let researchAction = "";
  let retrievalReview: Record<string, unknown> | undefined;
  await page.route("**/api/cogdoc/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/cogdoc/v1", "");
    if (path === "/auth/config") return json(route, { schema_version: "v1", account_auth_enabled: true, self_registration_enabled: false, oidc_enabled: false, oidc_display_name: "", scim_enabled: false });
    if (path === "/auth/login") return json(route, session);
    if (path === "/auth/me") return json(route, { schema_version: "v1", user, workspace, permissions: session.permissions, workspaces: [workspace] });
    if (path === "/knowledge-bases") return json(route, [{ kb_id: "policies", created_at: "2026-08-01T00:00:00Z", document_count: 8, tenant_id: workspace.workspace_id, owner_id: user.user_id }]);
    if (path === "/research-jobs/summaries") return json(route, { items: [] });
    if (path === "/research-jobs" && request.method() === "GET") return json(route, { items: [{ job_id: "research-1", kb_id: "policies", title: "Policy review", objective: "Review security policy evidence", status: "draft", revision: 3 }] });
    if (path === "/research-jobs/research-1" && request.method() === "GET") return json(route, { job_id: "research-1", kb_id: "policies", title: "Policy review", objective: "Review security policy evidence", status: "draft", revision: 3, sections: [{ section_id: "s1", title: "Password policy", objective: "Find password controls" }] });
    if (path === "/research-jobs/research-1/report" || path === "/research-jobs/research-1/provenance") return json(route, { items: [] });
    if (path === "/research-jobs/research-1/start" && request.method() === "POST") { researchAction = path; return json(route, { status: "running", revision: 4 }); }
    if (path === "/knowledge") return json(route, { items: [] });
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

  await page.goto("/research");
  await expect(page.getByRole("heading", { name: "Policy review" })).toBeVisible();
  await page.getByRole("button", { name: "开始", exact: true }).click();
  await expect.poll(() => researchAction).toBe("/research-jobs/research-1/start");

  await page.goto("/reviews");
  await page.getByRole("tab", { name: /检索证据/ }).click();
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
