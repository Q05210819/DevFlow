import { createHmac, randomUUID } from "node:crypto";
import { expect, test as base } from "@playwright/test";
import { startFullFlowBackend } from "./full-flow-setup";

const test = base.extend<object, { realBackend: void }>({
  realBackend: [async ({}, runTest, workerInfo) => {
    const stop = await startFullFlowBackend(String(workerInfo.project.use.baseURL), "workspace");
    try { await runTest(); } finally { await stop(); }
  }, { scope: "worker", auto: true, timeout: 90_000 }],
});

test("ordinary Worker prepares fixed PR code, citations and real revocation clear the view", async ({ page, baseURL }) => {
  test.setTimeout(120_000);
  await page.route("**/api/v1/auth/github/start", async route => {
    const start = await route.fetch({ maxRedirects: 0 });
    expect(start.status()).toBe(302);
    const authorize = new URL(start.headers().location);
    expect(authorize.origin + authorize.pathname).toBe("https://github.com/login/oauth/authorize");
    const callback = new URL("/api/v1/auth/github/callback", baseURL!);
    callback.searchParams.set("state", authorize.searchParams.get("state")!);
    callback.searchParams.set("code", "acceptance-admin");
    // Only external consent is replaced. Workspace responses all come from the API.
    await route.fulfill({ response: start, headers: { ...start.headers(), location: callback.href } });
  });
  await page.goto("/login");
  await page.getByRole("link", { name: "使用 GitHub 登录" }).click();
  await page.getByRole("button", { name: "接入仓库", exact: true }).click();
  const registration = page.waitForResponse(response => response.url().endsWith("/api/v1/repositories") && response.request().method() === "POST");
  await page.getByRole("button", { name: "接入 acceptance/private-repository" }).click();
  const registered = await registration;
  expect(registered.status()).toBe(201);
  const repository = await registered.json();
  const api = `/api/v1/repositories/${repository.id}`;
  await expect.poll(async () => {
    const response = await page.request.get(api + "/health");
    return (await response.json()).snapshot?.counts.open_issues;
  }, { timeout: 45_000 }).toBe(2);
  await page.getByRole("link", { name: "查看 acceptance/private-repository", exact: true }).click();
  await page.getByRole("navigation", { name: "仓库导航" }).getByRole("link", { name: "代码", exact: true }).click();
  await page.getByRole("combobox", { name: "版本来源", exact: true }).selectOption("pull");
  await page.getByLabel("PR 编号", { exact: true }).fill("2");
  await page.getByRole("combobox", { name: "PR 侧", exact: true }).selectOption("head");
  const prepare = page.waitForResponse(response => response.url().endsWith(api + "/workspaces") && response.request().method() === "POST");
  await page.getByRole("button", { name: "准备版本", exact: true }).click();
  const accepted = await prepare;
  expect(accepted.status()).toBe(202);
  expect(accepted.headers()["cache-control"]).toBe("no-store");
  const view = await accepted.json();
  expect(view.commit_sha).toMatch(/^[0-9a-f]{40}$/);
  await expect.poll(async () => (await (await page.request.get(api + "/workspaces/" + view.id)).json()).status, { timeout: 45_000 }).toBe("ready");
  await expect(page.getByRole("region", { name: "固定版本", exact: true })).toContainText(view.commit_sha);
  const directory = page.getByRole("region", { name: "目录", exact: true });
  await directory.getByRole("button", { name: "src", exact: true }).click();
  await directory.getByRole("button", { name: "example.py", exact: true }).click();
  const file = page.getByRole("region", { name: "文件", exact: true });
  await expect(file.locator("pre.code-text")).toContainText("version A");
  await expect(file.getByRole("link", { name: "查看源文件行 1–2", exact: true })).toHaveAttribute("href", `https://github.com/acceptance/private-repository/blob/${view.commit_sha}/src/example.py#L1-L2`);
  await page.getByLabel("字面文本搜索", { exact: true }).fill("needle");
  await page.getByRole("button", { name: "搜索代码", exact: true }).click();
  const search = page.getByRole("region", { name: "搜索结果", exact: true });
  await expect(search).toContainText("needle A");
  await expect(search.getByRole("link", { name: "src/example.py:2–2", exact: true })).toHaveAttribute("href", `https://github.com/acceptance/private-repository/blob/${view.commit_sha}/src/example.py#L2`);
  await page.setViewportSize({ width: 360, height: 800 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  const body = JSON.stringify({ action: "removed", installation: { id: 11001 }, repository: { id: 101001 }, repositories_removed: [{ id: 101001 }] });
  const removed = await page.request.post("/api/v1/github/webhooks", { data: body, headers: {
    "Content-Type": "application/json", "X-GitHub-Event": "installation_repositories",
    "X-GitHub-Delivery": randomUUID(), "X-Hub-Signature-256": "sha256=" + createHmac("sha256", "synthetic-acceptance-webhook-secret").update(body).digest("hex"),
  } });
  expect(removed.status()).toBe(204);
  const denied = page.waitForResponse(response => response.url().endsWith(api + "/workspaces/" + view.id) && response.status() === 404);
  await page.getByRole("button", { name: "重新确认版本", exact: true }).click();
  await denied;
  await expect(page.getByRole("navigation", { name: "仓库导航" })).toHaveCount(0);
  await expect(page.locator("pre.code-text")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "src/example.py:2–2", exact: true })).toHaveCount(0);
});
