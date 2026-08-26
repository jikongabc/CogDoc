import { expect, test, type Page, type Route } from "@playwright/test";

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function installLocalApi(page: Page) {
  await page.route("**/api/cogdoc/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace("/api/cogdoc/v1", "");
    if (path === "/auth/config") {
      return json(route, {
        schema_version: "v1",
        account_auth_enabled: false,
        self_registration_enabled: false,
        oidc_enabled: false,
        oidc_display_name: "",
        scim_enabled: false,
      });
    }
    if (path === "/knowledge-bases") {
      return json(route, [{
        kb_id: "company-handbook",
        created_at: "2026-08-20T09:00:00Z",
        document_count: 12,
        tenant_id: "local",
        owner_id: "local",
      }]);
    }
    if (path === "/knowledge-bases/company-handbook/documents") return json(route, []);
    if (path === "/sessions") return json(route, { schema_version: "v1", doc_id: "company-handbook", sessions: [{ session_id: "last-session", title: "上次对话", message_count: 4 }] });
    if (path.endsWith("/pending") || path.includes("pending-knowledge")) return json(route, { pending_count: 0, items: [] });
    return json(route, { items: [], jobs: [], summaries: [], events: [] });
  });
}

test("local deployment has an explicit login gate and complete product navigation", async ({ page }) => {
  await installLocalApi(page);
  await page.goto("/login");

  await expect(page.getByText("本地部署模式")).toBeVisible();
  await page.getByRole("button", { name: /进入本地工作区/ }).click();
  await expect(page).toHaveURL(/\/knowledge\/company-handbook\/chat\/last-session$/);
  await expect(page.getByLabel("选择知识库")).toHaveValue("company-handbook");
  const workbench = page.getByRole("navigation", { name: "知识库主视图" });
  await expect(workbench).toContainText("对话研究文档派生知识调试");
  await expect(workbench).not.toContainText("证据审核");
  await expect(page.getByRole("button", { name: "新对话" }).first()).toBeVisible();
  await expect(page.getByText("文档", { exact: true }).first()).toBeVisible();
  await workbench.getByRole("link", { name: "文档", exact: true }).click();
  await expect(page).toHaveURL(/\/knowledge\/company-handbook\?kb=company-handbook$/);
  await page.getByRole("link", { name: "接入", exact: true }).click();
  await expect(page).toHaveURL(/\/integrations\?kb=company-handbook$/);
  await expect(page.locator("#main-content").getByRole("heading", { name: "数据接入", exact: true })).toBeVisible();
  await expect(page.getByLabel("目标知识库")).toHaveValue("company-handbook");
  await page.goto("/knowledge/company-handbook/sources");
  await expect(page).toHaveURL(/\/integrations\?kb=company-handbook$/);
  await page.getByRole("link", { name: "任务", exact: true }).click();
  await expect(page.getByRole("heading", { name: "任务", exact: true }).first()).toBeVisible();
});
