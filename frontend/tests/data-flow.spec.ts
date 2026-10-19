import { createHmac, randomUUID } from "node:crypto";
import { expect, test as base, type Page } from "@playwright/test";
import { startFullFlowBackend } from "./full-flow-setup";

const test = base.extend<object, { realBackend: void }>({
  realBackend: [async ({}, runTest, workerInfo) => {
    const stop = await startFullFlowBackend(String(workerInfo.project.use.baseURL));
    try { await runTest(); } finally { await stop(); }
  }, { scope: "worker", auto: true, timeout: 90_000 }],
});

async function login(page: Page, origin: string) {
  await page.route("**/api/v1/auth/github/start", async route => {
    const start = await route.fetch({ maxRedirects: 0 });
    expect(start.status()).toBe(302);
    expect(start.headers()["set-cookie"]).toContain("devflow_oauth=");
    const authorize = new URL(start.headers().location);
    expect(authorize.origin + authorize.pathname).toBe("https://github.com/login/oauth/authorize");
    const callback = new URL("/api/v1/auth/github/callback", origin);
    callback.searchParams.set("state", authorize.searchParams.get("state")!);
    callback.searchParams.set("code", "acceptance-admin");
    // Only replace external consent. Every private data response remains real.
    await route.fulfill({ response: start, headers: { ...start.headers(), location: callback.href } });
  });
  await page.goto("/login");
  await page.getByRole("link", { name: "使用 GitHub 登录" }).click();
  await expect(page).toHaveURL(/\/repositories$/);
}

test("real collected Issue PR CI health, missing patch and repository revocation", async ({ page, baseURL }) => {
  test.setTimeout(90_000);
  await login(page, baseURL!);
  await page.getByRole("button", { name: "接入仓库", exact: true }).click();
  const registration = page.waitForResponse(response => response.url().endsWith("/api/v1/repositories") && response.request().method() === "POST");
  await page.getByRole("button", { name: "接入 acceptance/private-repository" }).click();
  const registered = await registration;
  expect(registered.status()).toBe(201);
  const repository = await registered.json();
  const api = `/api/v1/repositories/${repository.id}`;
  const root = `/repositories/${repository.id}`;
  await expect.poll(async () => {
    const response = await page.request.get(api + "/health");
    expect(response.status()).toBe(200);
    return (await response.json()).snapshot?.counts.open_issues;
  }, { timeout: 45_000 }).toBe(2);
  await page.getByRole("link", { name: "查看 acceptance/private-repository", exact: true }).click();
  await expect(page.getByRole("region", { name: "健康事实", exact: true })).toContainText("Workspace 暂不可用");
  await expect(page.getByText("记录创建/更新活动（不是事件历史）", { exact: true })).toBeVisible();
  const health = await page.request.get(api + "/health");
  expect((await health.json()).snapshot.manifest.actions[0]).toMatchObject({ source_id: 81001, attempt: 2, head_sha: "b".repeat(40), pr_numbers: [2] });

  await page.getByRole("navigation", { name: "仓库导航" }).getByRole("link", { name: "PRs", exact: true }).click();
  await page.getByRole("link", { name: "#2 Acceptance pull", exact: true }).click();
  await expect(page.getByRole("heading", { name: "#2 Acceptance pull", exact: true })).toBeVisible();
  const files = await page.request.get(api + "/pulls/2?collection=files");
  expect(files.status()).toBe(200);
  expect((await files.json()).items[0].payload.patch_status).toBe("missing");
  await expect(page.getByRole("region", { name: "文件与补丁", exact: true })).toContainText("补丁：缺失");
  await expect(page.getByText("b".repeat(40), { exact: true }).first()).toBeVisible();

  await page.getByRole("navigation", { name: "仓库导航" }).getByRole("link", { name: "Actions", exact: true }).click();
  await page.getByRole("link", { name: "Acceptance CI · 81001 / 2", exact: true }).click();
  await expect(page).toHaveURL(root + "/actions/81001/attempts/2");
  await expect(page.getByRole("region", { name: "任务", exact: true })).toContainText("Acceptance tests");
  await page.getByRole("link", { name: "打开 Attempt 1", exact: true }).click();
  await expect(page).toHaveURL(root + "/actions/81001/attempts/1");
  await expect(page.getByRole("region", { name: "任务", exact: true })).toContainText("failure");

  await page.getByRole("navigation", { name: "仓库导航" }).getByRole("link", { name: "Issues", exact: true }).click();
  const issues = await page.request.get(api + "/issues");
  expect((await issues.json()).items.map((item: { source_id: number }) => item.source_id).sort()).toEqual([51001, 51003]);
  await page.getByRole("link", { name: "#1 Acceptance issue", exact: true }).click();
  await expect(page.getByText("Private issue evidence", { exact: true })).toBeVisible();
  const body = JSON.stringify({ action: "removed", installation: { id: 11001 }, repository: { id: 101001 }, repositories_removed: [{ id: 101001 }] });
  const revoked = await page.request.post("/api/v1/github/webhooks", { data: body, headers: {
    "Content-Type": "application/json", "X-GitHub-Event": "installation_repositories",
    "X-GitHub-Delivery": randomUUID(), "X-Hub-Signature-256": "sha256=" + createHmac("sha256", "synthetic-acceptance-webhook-secret").update(body).digest("hex"),
  } });
  expect(revoked.status()).toBe(204);
  const denied = page.waitForResponse(response => response.url().endsWith(api + "/issues/1") && response.status() === 404);
  await page.getByRole("button", { name: "刷新事实", exact: true }).click();
  expect((await (await denied).json()).error.code).toBe("resource_not_found");
  await expect(page.getByRole("main").getByRole("alert")).toHaveText("仓库不可访问，请确认权限或重新登录。");
  await expect(page.getByRole("navigation", { name: "仓库导航" })).toHaveCount(0);
  await expect(page.getByText("Private issue evidence", { exact: true })).toHaveCount(0);
  await expect(page.getByText("#1 Acceptance issue", { exact: true })).toHaveCount(0);
  for (const suffix of ["/sync", "/health", "/actions/runs"]) expect((await page.request.get(api + suffix)).status()).toBe(404);
});
