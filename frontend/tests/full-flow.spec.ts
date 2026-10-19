import { expect, test as base, type Page } from "@playwright/test";
import { startFullFlowBackend } from "./full-flow-setup";

const test = base.extend<object, { realBackend: void }>({
  realBackend: [async ({}, runTest, workerInfo) => {
    const stop = await startFullFlowBackend(String(workerInfo.project.use.baseURL));
    try { await runTest(); } finally { await stop(); }
  }, { scope: "worker", auto: true, timeout: 90_000 }],
});
test.use({ actionTimeout: 15_000, navigationTimeout: 20_000 });

async function login(page: Page, identity: "admin" | "member", origin: string) {
  await page.route("**/api/v1/auth/github/start", async route => {
    const start = await route.fetch({ maxRedirects: 0 });
    expect(start.status()).toBe(302);
    expect(start.headers()["set-cookie"]).toContain("devflow_oauth=");
    const authorize = new URL(start.headers().location);
    expect(authorize.origin + authorize.pathname).toBe("https://github.com/login/oauth/authorize");
    const state = authorize.searchParams.get("state");
    expect(state).toBeTruthy();
    // Preserve the actual start response/cookie and change only its external
    // consent redirect. Playwright does not route subsequent redirect hops.
    const callback = new URL("/api/v1/auth/github/callback", origin);
    callback.searchParams.set("state", state!);
    callback.searchParams.set("code", `acceptance-${identity}`);
    await route.fulfill({ response: start, headers: { ...start.headers(), location: callback.href } });
  });
  await page.goto("/login");
  await page.getByRole("link", { name: "使用 GitHub 登录" }).click();
  await expect(page).toHaveURL(/\/repositories$/);
  await expect(page.getByText(`acceptance-${identity}`, { exact: true })).toBeVisible();
}

test("real OAuth, allowlist, repository, Worker result, persisted events and revocation", async ({ page, browser, baseURL }) => {
  test.setTimeout(90_000);
  await login(page, "admin", baseURL!);
  await page.getByRole("link", { name: "成员管理" }).click();
  await page.getByLabel("GitHub 数字 ID").fill("42002");
  await page.getByRole("button", { name: "添加成员" }).click();
  await expect(page.getByRole("button", { name: "停用 42002" })).toBeVisible();
  await page.getByRole("link", { name: "仓库", exact: true }).click();
  await page.getByRole("button", { name: "接入仓库", exact: true }).click();
  await page.getByRole("button", { name: "接入 acceptance/private-repository" }).click();
  await expect(page.getByRole("button", { name: "检查 acceptance/private-repository" })).toBeVisible();

  const memberContext = await browser.newContext({ baseURL });
  const member = await memberContext.newPage();
  try {
    await login(member, "member", baseURL!);
    const submitted = member.waitForResponse(response => response.url().endsWith("/access-checks") && response.request().method() === "POST");
    await member.getByRole("button", { name: "检查 acceptance/private-repository" }).click();
    const response = await submitted;
    expect(response.status()).toBe(202);
    const job = await response.json();
    await expect(member).toHaveURL(new RegExp(`/jobs/${job.id}$`));
    await expect(member.getByRole("heading", { name: "检查结果" })).toBeVisible({ timeout: 20_000 });
    await expect(member.getByText("acceptance/private-repository", { exact: true })).toBeVisible();
    const detail = await member.request.get(`/api/v1/jobs/${job.id}`);
    expect(detail.status()).toBe(200);
    expect(await detail.json()).toMatchObject({ status: "succeeded", attempt: 1, last_sequence: 3, result: { github_repository_id: 101001, private: true } });
    const events = await member.request.get(`/api/v1/jobs/${job.id}/events`);
    expect(events.status()).toBe(200);
    const body = await events.text();
    expect([...body.matchAll(/^id:\s*(\d+)$/gm)].map(match => Number(match[1]))).toEqual([1, 2, 3]);
    expect([...body.matchAll(/^data:\s*(.+)$/gm)].map(match => JSON.parse(match[1]).status)).toEqual(["queued", "running", "succeeded"]);

    await page.getByRole("link", { name: "审计日志" }).click();
    await expect(page.getByRole("row").filter({ hasText: job.id }).getByRole("cell", { name: "job.succeeded", exact: true })).toBeVisible();
    await page.getByRole("link", { name: "成员管理" }).click();
    await page.getByRole("button", { name: "停用 acceptance-member" }).click();
    await expect(page.getByRole("button", { name: "启用 acceptance-member" })).toBeVisible();
    // A fresh protected read must clear the previously visible private result.
    const rejected = member.waitForResponse(response => response.url().endsWith("/api/v1/session") && response.status() === 401);
    await member.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
    await rejected;
    await expect(member.getByRole("link", { name: "使用 GitHub 登录" })).toBeVisible();
    await expect(member.getByText("acceptance/private-repository")).toHaveCount(0);
    await expect(member.getByText(job.id, { exact: true })).toHaveCount(0);
    expect((await member.request.get(`/api/v1/jobs/${job.id}`)).status()).toBe(401);
  } finally { await memberContext.close(); }
});
