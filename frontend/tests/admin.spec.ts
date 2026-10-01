import { expect, test } from "@playwright/test";

const admin = { id: "user-admin", github_id: 41, login: "leader", role: "admin", enabled: true };
const member = { id: "user-member", github_id: 42, login: "colleague", role: "member", enabled: true };

test("member has no admin entry and direct admin route is refused", async ({ page }) => {
  await page.route("**/api/v1/session", (route) => route.fulfill({ status: 200, json: { user: member, csrf_token: "csrf-member" } }));
  await page.route("**/api/v1/admin/users?**", (route) => route.fulfill({ status: 403, json: { error: { code: "forbidden", message: "Denied", request_id: "r3" } } }));

  await page.goto("/");
  await expect(page.getByRole("link", { name: "成员管理" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "审计日志" })).toHaveCount(0);
  await page.goto("/admin/users");
  await expect(page.getByText("无权访问管理页面")).toBeVisible();
  await expect(page.getByText("leader")).toHaveCount(0);
});

test("last active admin cannot be disabled and sees an actionable error", async ({ page }) => {
  await page.route("**/api/v1/session", (route) => route.fulfill({ status: 200, json: { user: admin, csrf_token: "csrf-admin" } }));
  await page.route("**/api/v1/admin/users?**", (route) => route.fulfill({ status: 200, json: { items: [admin], next_cursor: null } }));
  await page.route("**/api/v1/admin/users/user-admin", async (route) => {
    expect(route.request().method()).toBe("PATCH");
    expect(route.request().postDataJSON()).toEqual({ enabled: false });
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-admin");
    await route.fulfill({ status: 409, json: { error: { code: "last_admin", message: "Cannot disable last admin", request_id: "r4" } } });
  });

  await page.goto("/admin/users");
  await page.getByRole("button", { name: "停用 leader" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("必须保留至少一位已启用的管理员");
  await expect(page.getByRole("cell", { name: "已启用", exact: true })).toBeVisible();
});

test("audit list follows opaque cursor pagination", async ({ page }) => {
  await page.route("**/api/v1/session", (route) => route.fulfill({ status: 200, json: { user: admin, csrf_token: "csrf-admin" } }));
  await page.route("**/api/v1/admin/audit-logs?**", (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    return route.fulfill({ status: 200, json: cursor
      ? { items: [{ id: "a2", actor_id: "user-admin", action: "user.update", resource_type: "user", resource_id: "user-member", outcome: "success", request_id: "r6", job_id: null, reason_code: null, created_at: "2026-09-30T08:00:00Z" }], next_cursor: null }
      : { items: [{ id: "a1", actor_id: "user-admin", action: "user.create", resource_type: "user", resource_id: "user-member", outcome: "success", request_id: "r5", job_id: null, reason_code: null, created_at: "2026-09-30T07:00:00Z" }], next_cursor: "opaque-cursor" } });
  });

  await page.goto("/admin/audit");
  await expect(page.getByText("user.create")).toBeVisible();
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByText("user.update")).toBeVisible();
  await expect(page.getByText("user.create")).not.toBeVisible();
});

test("admin creates a member by keyboard on narrow screen, changes role and follows user pagination", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  let created = false;
  let promoted = false;
  await page.route("**/api/v1/session", (route) => route.fulfill({ json: { user: admin, csrf_token: "csrf-admin" } }));
  await page.route("**/api/v1/admin/users**", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      expect(request.headers()["x-csrf-token"]).toBe("csrf-admin");
      expect(request.postDataJSON()).toEqual({ github_id: 42, role: "member" });
      created = true;
      return route.fulfill({ status: 201, json: member });
    }
    if (request.method() === "PATCH") {
      expect(request.postDataJSON()).toEqual({ role: "admin" });
      promoted = true;
      return route.fulfill({ json: { ...member, role: "admin" } });
    }
    const cursor = new URL(request.url()).searchParams.get("cursor");
    if (cursor) expect(cursor).toBe("users-next/+=");
    return route.fulfill({ json: cursor
      ? { items: [{ ...member, login: "second-page" }], next_cursor: null }
      : { items: [admin, ...(created ? [{ ...member, role: promoted ? "admin" : "member" }] : [])], next_cursor: "users-next/+=" } });
  });
  await page.goto("/admin/users");
  await page.getByLabel("GitHub 数字 ID").fill("42");
  await page.getByLabel("GitHub 数字 ID").press("Tab");
  await page.getByLabel("初始角色").press("Tab");
  await expect(page.getByRole("button", { name: "添加成员" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByText("colleague", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("members-narrow.png"), fullPage: true });
  await page.getByRole("button", { name: "设为管理员 colleague" }).click();
  await expect(page.getByRole("button", { name: "设为普通成员 colleague" })).toBeVisible();
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByText("second-page", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "上一页" }).click();
  await expect(page.getByText("colleague", { exact: true })).toBeVisible();
});

test("audit failure reasons and API errors never echo untrusted messages", async ({ page }) => {
  await page.route("**/api/v1/session", (route) => route.fulfill({ json: { user: admin, csrf_token: "csrf-admin" } }));
  await page.route("**/api/v1/admin/audit-logs?**", (route) => route.fulfill({ json: {
    items: [{ id: "a1", actor_id: null, action: "user.update", resource_type: "user", resource_id: null, outcome: "failure", request_id: "r5", job_id: null, reason_code: "secret-in-unknown-reason", created_at: "2026-09-30T07:00:00Z" }], next_cursor: null,
  } }));
  await page.goto("/admin/audit");
  await expect(page.getByText("操作未完成")).toBeVisible();
  await expect(page.getByText("secret-in-unknown-reason")).toHaveCount(0);
  await page.route("**/api/v1/admin/users?**", (route) => route.fulfill({ status: 503, json: { error: { code: "unknown", message: "secret-in-error", request_id: "r6" } } }));
  await page.getByRole("link", { name: "成员管理" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("暂时不可用");
  await expect(page.getByText("secret-in-error")).toHaveCount(0);
});
