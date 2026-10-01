import { expect, test } from "@playwright/test";
import { completed, disconnect, emit, eventTransport, job, repository, session, streamCount } from "./repository-fixtures";

test.beforeEach(async ({ page }) => { await session(page); await eventTransport(page); });

test("deduplicates progress, resumes after last sequence, closes terminal and obtains REST result", async ({ page }) => {
  let finished = false;
  await page.route(`**/api/v1/jobs/${job.id}`, route => route.fulfill({ json: finished ? completed : job }));
  await page.goto(`/jobs/${job.id}`);
  await expect.poll(() => streamCount(page)).toBe(1);
  await emit(page, 1); await emit(page, 2); await emit(page, 2);
  await expect(page.getByRole("list", { name: "检查进度" }).getByRole("listitem")).toHaveCount(2);
  await disconnect(page);
  await expect.poll(() => streamCount(page)).toBe(2);
  expect(await page.evaluate(() => (window as unknown as { streams: { url: string }[] }).streams.at(-1)?.url)).toContain("after=2");
  finished = true;
  await emit(page, 3, "succeeded");
  await expect(page.getByText("team/private-result")).toBeVisible();
  await expect(page.getByRole("list", { name: "检查进度" }).getByRole("listitem")).toHaveCount(3);
  expect(await page.evaluate(() => (window as unknown as { streams: { closed: boolean }[] }).streams.every(s => s.closed))).toBe(true);
  const before = await streamCount(page);
  await page.waitForTimeout(1500);
  expect((await streamCount(page)) - before).toBe(0);
});

test("terminal initial REST creates no connection", async ({ page }) => {
  await page.route(`**/api/v1/jobs/${job.id}`, route => route.fulfill({ json: completed }));
  await page.goto(`/jobs/${job.id}`);
  await expect(page.getByText("team/private-result")).toBeVisible();
  expect(await streamCount(page)).toBe(0);
});

test("access_lost closes connection and removes private details", async ({ page }) => {
  await page.route(`**/api/v1/jobs/${job.id}`, route => route.fulfill({ json: { ...job, result: completed.result } }));
  await page.goto(`/jobs/${job.id}`);
  await expect.poll(() => streamCount(page)).toBe(1);
  await expect(page.getByText(job.id, { exact: true })).toBeVisible();
  await emit(page, 1);
  await expect(page.getByRole("list", { name: "检查进度" })).toBeVisible();
  await page.evaluate(() => (window as unknown as { revoke: () => void }).revoke());
  await expect(page.getByText("检查不可访问")).toBeVisible();
  await expect(page.getByText("team/private-result")).toHaveCount(0);
  await expect(page.getByText(job.id, { exact: true })).toHaveCount(0);
  await expect(page.getByRole("list", { name: "检查进度" })).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { streams: { closed: boolean }[] }).streams.every(s => s.closed))).toBe(true);
});

for (const status of [401, 404]) test(`stream error followed by ${status} stops permanently`, async ({ page }) => {
  let denied = false;
  await page.route(`**/api/v1/jobs/${job.id}`, route => route.fulfill(denied ? { status, json: { error: { code: "denied" } } } : { json: job }));
  await page.goto(`/jobs/${job.id}`);
  await expect.poll(() => streamCount(page)).toBe(1);
  denied = true;
  await disconnect(page);
  await expect(page.getByText("检查不可访问")).toBeVisible();
  await page.waitForTimeout(1500);
  expect(await streamCount(page)).toBe(1);
  await expect(page.getByRole("button", { name: "恢复连接" })).toHaveCount(0);
});

test("temporary failures have bounded retries and manual recovery", async ({ page }) => {
  let unavailable = false;
  let requests = 0;
  await page.route(`**/api/v1/jobs/${job.id}`, route => { requests++; return unavailable ? route.abort() : route.fulfill({ json: job }); });
  await page.goto(`/jobs/${job.id}`);
  await expect.poll(() => streamCount(page)).toBe(1);
  unavailable = true;
  await disconnect(page);
  await expect(page.getByRole("button", { name: "恢复连接" })).toBeVisible({ timeout: 15000 });
  const stopped = requests;
  await page.waitForTimeout(1500);
  expect(requests).toBe(stopped);
  unavailable = false;
  await page.getByRole("button", { name: "恢复连接" }).click();
  await expect.poll(() => streamCount(page)).toBe(2);
});

test("logout closes stream and late detail cannot restore private result", async ({ page }) => {
  let resolveDetail: (() => void) | undefined;
  let hold = false;
  await page.route(`**/api/v1/jobs/${job.id}`, async route => {
    if (hold) await new Promise<void>(resolve => { resolveDetail = resolve; });
    await route.fulfill({ json: hold ? completed : job }).catch(() => {});
  });
  await page.route("**/api/v1/auth/logout", async route => {
    await page.route("**/api/v1/session", r => r.fulfill({ status: 401, json: { error: { code: "unauthenticated" } } }));
    await route.fulfill({ status: 204 });
  });
  await page.goto(`/jobs/${job.id}`);
  await expect.poll(() => streamCount(page)).toBe(1);
  hold = true;
  await emit(page, 3, "succeeded");
  await expect.poll(() => !!resolveDetail).toBe(true);
  await page.getByRole("button", { name: "退出登录" }).click();
  resolveDetail?.();
  await expect(page.getByRole("link", { name: "使用 GitHub 登录" })).toBeVisible();
  await expect(page.getByText("team/private-result")).toHaveCount(0);
});

test("disconnect recovery replays from last received event despite newer REST snapshot", async ({ page }) => {
  let newer = false;
  await page.route(`**/api/v1/jobs/${job.id}`, route => route.fulfill({ json: { ...job, last_sequence: newer ? 5 : 0 } }));
  await page.goto(`/jobs/${job.id}`);
  await expect.poll(() => streamCount(page)).toBe(1);
  await emit(page, 1);
  newer = true;
  await disconnect(page);
  await expect.poll(() => streamCount(page)).toBe(2);
  expect(await page.evaluate(() => (window as unknown as { streams: { url: string }[] }).streams.at(-1)?.url)).toContain("after=1");
  await emit(page, 2);
  await expect(page.getByRole("list", { name: "检查进度" }).getByRole("listitem")).toHaveCount(2);
});

test("navigation closes current stream and a different job starts with its own cursor", async ({ page }) => {
  const secondJob = { ...job, id: "33333333-3333-4333-8333-333333333333", last_sequence: 4 };
  await page.route(`**/api/v1/jobs/${job.id}`, route => route.fulfill({ json: { ...job, last_sequence: 7 } }));
  await page.route(`**/api/v1/jobs/${secondJob.id}`, route => route.fulfill({ json: secondJob }));
  await page.route(`**/api/v1/repositories/${repository.id}/access-checks`, route => route.fulfill({ status: 202, json: secondJob }));
  await page.route("**/api/v1/repositories?**", route => route.fulfill({ json: { items: [repository], next_cursor: null } }));
  await page.goto(`/jobs/${job.id}`);
  await expect.poll(() => streamCount(page)).toBe(1);
  await emit(page, 8);
  await page.getByRole("link", { name: "返回仓库" }).click();
  await expect(page.getByRole("button", { name: "检查 team/private-app" })).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { streams: { closed: boolean }[] }).streams.every(s => s.closed))).toBe(true);
  await page.getByRole("button", { name: "检查 team/private-app" }).click();
  await expect.poll(() => streamCount(page)).toBe(2);
  expect(await page.evaluate(() => (window as unknown as { streams: { url: string }[] }).streams.at(-1)?.url)).toBe(`/api/v1/jobs/${secondJob.id}/events?after=4`);
  await expect(page.getByRole("list", { name: "检查进度" })).toHaveCount(0);
});

test("actor change cannot display previous actor private data while new detail is pending", async ({ page }) => {
  let changed = false;
  let release: (() => void) | undefined;
  await page.route("**/api/v1/session", route => route.fulfill({ json: { user: { id: changed ? "other-member" : "member-id", github_id: changed ? 43 : 42, login: changed ? "other-user" : "colleague", role: "member", enabled: true }, csrf_token: "csrf-test" } }));
  await page.route(`**/api/v1/jobs/${job.id}`, async route => {
    if (changed) { await new Promise<void>(resolve => { release = resolve; }); return route.fulfill({ status: 404, json: { error: { code: "not_found" } } }).catch(() => {}); }
    return route.fulfill({ json: completed });
  });
  await page.goto(`/jobs/${job.id}`);
  await expect(page.getByText("team/private-result")).toBeVisible();
  changed = true;
  await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
  await expect(page.getByText("other-user")).toBeVisible();
  try { await expect(page.getByText("team/private-result")).toHaveCount(0); }
  finally { release?.(); }
});

test("logout hides private state while login navigation is delayed", async ({ page }) => {
  await page.route(`**/api/v1/jobs/${job.id}`, route => route.fulfill({ json: completed }));
  await page.route("**/api/v1/auth/logout", route => route.fulfill({ status: 204 }));
  let resumeNavigation: (() => void) | undefined;
  await page.route("**/login", async route => { await new Promise<void>(resolve => { resumeNavigation = resolve; }); await route.continue(); });
  await page.goto(`/jobs/${job.id}`);
  await expect(page.getByText("team/private-result")).toBeVisible();
  let privateRemoved = false;
  await page.exposeFunction("recordPrivateRemoval", () => { privateRemoved = true; });
  await page.evaluate(() => {
    new MutationObserver(() => {
      if (!document.body.textContent?.includes("team/private-result")) void (window as unknown as { recordPrivateRemoval: () => Promise<void> }).recordPrivateRemoval();
    }).observe(document.body, { childList: true, subtree: true });
  });
  await page.getByRole("button", { name: "退出登录" }).click({ noWaitAfter: true });
  await expect.poll(() => !!resumeNavigation).toBe(true);
  try {
    await expect.poll(() => privateRemoved).toBe(true);
  } finally { resumeNavigation?.(); }
});
