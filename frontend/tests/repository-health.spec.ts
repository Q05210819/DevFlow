import { expect, test } from "@playwright/test";
import { detail, facts, health, repository, stamp, sync } from "./facts-fixtures";

test("health preserves evidence time and unknown counts when refresh fails", async ({ page }, info) => {
  await facts(page); let failed = false;
  await page.route(`**/api/v1/repositories/${repository.id}/health**`, route => {
    if (route.request().method() === "POST") { failed = true; return route.fulfill({ status: 202, json: { ...health, refresh_status: "failed", error_code: "internal_error" } }); }
    return route.fulfill({ json: failed ? { ...health, refresh_status: "failed", error_code: "internal_error" } : health });
  });
  await page.goto(`/repositories/${repository.id}`);
  const region = page.getByRole("region", { name: "健康事实" });
  await expect(region.getByText(stamp, { exact: true }).first()).toBeVisible();
  await expect(region.getByText("未知", { exact: true }).first()).toBeVisible();
  await expect(region).toContainText("记录创建/更新");
  await expect(region.getByRole("region", { name: "代码版本证据" })).toContainText("不可用");
  await expect(region).toContainText("该历史摘要没有记录逐版本验证清单，检查数量未知");
  await page.getByRole("button", { name: "刷新健康摘要" }).click();
  await expect(region.getByRole("alert")).toContainText("刷新失败");
  await expect(region.getByText(stamp, { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: info.outputPath("health-evidence.png"), fullPage: true });
});

test("sync transport retry preserves logical UUID and CSRF; successful new request renews key", async ({ page }) => {
  await facts(page); const keys: string[] = [];
  await page.route(`**/api/v1/repositories/${repository.id}/sync/refresh`, route => {
    const request = route.request(); keys.push(request.headers()["idempotency-key"]);
    expect(request.headers()["x-csrf-token"]).toBe("csrf-test"); expect(request.postDataJSON()).toEqual({ resume: false });
    return keys.length === 1 ? route.abort() : route.fulfill({ status: 202, json: sync });
  });
  await page.goto(`/repositories/${repository.id}`);
  await expect(page.getByRole("button", { name: "刷新同步", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "恢复同步", exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "管理员回填" })).toHaveCount(0);
  await page.getByRole("button", { name: "刷新同步", exact: true }).click();
  await expect(page.getByRole("main").getByRole("alert")).toBeVisible();
  await page.getByRole("button", { name: "重试同步提交" }).click();
  await expect(page.getByRole("button", { name: "刷新同步", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "刷新同步", exact: true }).click();
  await expect.poll(() => keys.length).toBe(3);
  expect(keys[0]).toMatch(/^[\da-f-]{36}$/); expect(keys[1]).toBe(keys[0]); expect(keys[2]).not.toBe(keys[0]);
});

test("admin backfill is explicit UTC, validates bounds and resume is admin-only", async ({ page }) => {
  await facts(page, "admin"); const bodies: unknown[] = []; const keys: string[] = [];
  await page.route(`**/api/v1/repositories/${repository.id}/sync/backfills`, route => {
    bodies.push(route.request().postDataJSON()); keys.push(route.request().headers()["idempotency-key"]);
    return bodies.length === 1 ? route.abort() : route.fulfill({ status: 202, json: sync });
  });
  await page.route(`**/api/v1/repositories/${repository.id}/sync/refresh`, route => { expect(route.request().postDataJSON()).toEqual({ resume: true }); return route.fulfill({ status: 202, json: sync }); });
  await page.goto(`/repositories/${repository.id}`);
  for (const [start, end] of [["2026-10-03T00:00", "2026-10-02T00:00"], ["2024-01-01T00:00", "2026-10-02T00:00"], ["2999-01-01T00:00", "2999-01-02T00:00"]]) {
    await page.getByLabel("开始时间（UTC）").fill(start);
    await page.getByLabel("结束时间（UTC）").fill(end);
    await page.getByRole("button", { name: "提交回填" }).click();
    await expect(page.getByRole("region", { name: "管理员回填" }).getByRole("alert")).toContainText("请检查 UTC");
    expect(bodies).toHaveLength(0);
  }
  await page.getByLabel("开始时间（UTC）").fill("2026-10-01T00:00");
  await page.getByLabel("结束时间（UTC）").fill("2026-10-02T00:00");
  for (const stream of ["issues", "pulls", "actions"]) await page.getByLabel(stream, { exact: true }).uncheck();
  await page.getByRole("button", { name: "提交回填" }).click();
  await expect(page.getByRole("region", { name: "管理员回填" }).getByRole("alert")).toContainText("采集流");
  expect(bodies).toHaveLength(0);
  for (const stream of ["issues", "pulls", "actions"]) await page.getByLabel(stream, { exact: true }).check();
  await page.getByRole("button", { name: "提交回填" }).click();
  await expect(page.getByRole("button", { name: "重试回填提交" })).toBeVisible();
  await page.getByLabel("结束时间（UTC）").fill("2026-10-03T00:00");
  await page.getByRole("button", { name: "重试回填提交" }).click();
  await expect.poll(() => bodies.length).toBe(2);
  expect(bodies[0]).toEqual({ start: "2026-10-01T00:00:00.000Z", end: "2026-10-02T00:00:00.000Z", streams: ["issues", "pulls", "actions"] });
  expect(keys[1]).not.toBe(keys[0]);
  await page.getByRole("button", { name: "恢复同步" }).click();
});

test("visible polling starts at two seconds, stops after three transport errors and manually resumes", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-10-15T00:00:00Z") }); await page.clock.pauseAt(new Date("2026-10-15T00:00:01Z")); await facts(page); let calls = 0; let broken = false;
  await page.route(`**/api/v1/repositories/${repository.id}/sync`, route => { calls++; return broken ? route.abort() : route.fulfill({ json: { ...sync, status: "running" } }); });
  await page.route(`**/api/v1/repositories/${repository.id}`, route => route.fulfill({ json: { ...detail, sync: { ...sync, status: "running" } } }));
  await page.goto(`/repositories/${repository.id}`); await expect.poll(async () => { await page.clock.runFor(1); return calls; }).toBe(1);
  await page.clock.fastForward(1900); expect(calls).toBe(1);
  broken = true;
  for (let n = 2; n <= 4; n++) { await page.clock.fastForward(2000); await expect.poll(async () => { await page.clock.runFor(1); return calls; }).toBe(n); }
  await page.clock.runFor(1);
  await expect(page.getByRole("button", { name: "恢复状态轮询" })).toBeVisible();
  await page.clock.fastForward(20_000); expect(calls).toBe(4);
  broken = false; await page.getByRole("button", { name: "恢复状态轮询" }).click();
  await page.clock.fastForward(2000); await expect.poll(async () => { await page.clock.runFor(1); return calls; }).toBe(5);
});

test("retry_at and Retry-After are polling floors; five minute deadline remains bounded", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-10-15T00:00:00Z") }); await page.clock.pauseAt(new Date("2026-10-15T00:00:01Z")); await facts(page); let calls = 0;
  const retry = "2026-10-15T00:00:10Z";
  await page.route(`**/api/v1/repositories/${repository.id}`, route => route.fulfill({ json: { ...detail, sync: { ...sync, status: "retry_wait", retry_at: retry } } }));
  await page.route(`**/api/v1/repositories/${repository.id}/sync`, route => { calls++; return calls === 1 ? route.fulfill({ status: 429, headers: { "Retry-After": "Thu, 15 Oct 2026 00:00:20 GMT" }, json: { error: { code: "github_rate_limited" } } }) : route.fulfill({ json: { ...sync, status: "running", retry_at: null } }); });
  await page.goto(`/repositories/${repository.id}`); await expect.poll(async () => { await page.clock.runFor(1); return calls; }).toBe(1);
  await expect.poll(async () => { await page.clock.runFor(1); return page.getByRole("main").getByRole("alert").count(); }).toBe(1);
  const untilFloor = Date.parse("2026-10-15T00:00:20Z") - await page.evaluate(() => Date.now());
  await page.clock.fastForward(untilFloor - 1); expect(calls).toBe(1);
  await page.clock.fastForward(1); await expect.poll(async () => { await page.clock.runFor(1); return calls; }).toBe(2);
  await page.clock.fastForward(300_000);
  await expect(page.getByRole("button", { name: "恢复状态轮询" })).toBeVisible();
  const stopped = calls; await page.clock.fastForward(30_000); expect(calls).toBe(stopped);
});

test("hidden pages pause polling and departing cancels further work", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-10-15T00:00:00Z") }); await page.clock.pauseAt(new Date("2026-10-15T00:00:01Z")); await facts(page); let calls = 0;
  await page.route(`**/api/v1/repositories/${repository.id}/sync`, route => { calls++; return route.fulfill({ json: { ...sync, status: "running" } }); });
  await page.goto(`/repositories/${repository.id}`); await expect.poll(async () => { await page.clock.runFor(1); return calls; }).toBe(1);
  await expect.poll(async () => { await page.clock.runFor(1); return page.getByRole("region", { name: "同步状态" }).getByRole("status").textContent(); }).toContain("正在同步");
  await page.evaluate(() => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" }); document.dispatchEvent(new Event("visibilitychange")); });
  await page.clock.fastForward(10_000); expect(calls).toBe(1);
  await page.evaluate(() => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" }); document.dispatchEvent(new Event("visibilitychange")); });
  await page.clock.fastForward(2000); await expect.poll(async () => { await page.clock.runFor(1); return calls; }).toBe(2);
  await page.getByRole("link", { name: "仓库", exact: true }).click();
  await expect.poll(async () => { await page.clock.runFor(1); return page.getByRole("link", { name: "查看 team/private-app" }).count(); }).toBe(1);
  const departed = calls; await page.clock.fastForward(10_000); expect(calls).toBe(departed);
});

test("an initial transport failure counts toward the three-failure polling bound", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-10-15T00:00:00Z") }); await page.clock.pauseAt(new Date("2026-10-15T00:00:01Z")); await facts(page); let calls = 0;
  await page.route(`**/api/v1/repositories/${repository.id}`, route => route.fulfill({ json: { ...detail, sync: { ...sync, status: "running" } } }));
  await page.route(`**/api/v1/repositories/${repository.id}/sync`, route => { calls++; return route.abort(); });
  await page.goto(`/repositories/${repository.id}`);
  await expect.poll(async () => { await page.clock.runFor(1); return page.getByRole("main").getByRole("alert").count(); }).toBe(1);
  expect(calls).toBe(1);
  for (const n of [2, 3]) { await page.clock.fastForward(2000); await expect.poll(async () => { await page.clock.runFor(1); return calls; }).toBe(n); await page.clock.runFor(10); }
  await expect(page.getByRole("button", { name: "恢复状态轮询" })).toBeVisible();
  await page.clock.fastForward(20_000); expect(calls).toBe(3);
});

test("visibility cancellation does not count as a transport failure", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-10-15T00:00:00Z") }); await page.clock.pauseAt(new Date("2026-10-15T00:00:01Z")); await facts(page); let calls = 0; let hold = false; let release!: () => void;
  await page.route(`**/api/v1/repositories/${repository.id}/sync`, async route => {
    calls++; if (hold) await new Promise<void>(resolve => release = resolve);
    await route.fulfill({ json: { ...sync, status: "running" } }).catch(() => {});
  });
  await page.goto(`/repositories/${repository.id}`);
  await expect.poll(async () => { await page.clock.runFor(1); return calls; }).toBe(1); await page.clock.runFor(10);
  hold = true;
  for (const n of [2, 3, 4]) {
    await page.clock.fastForward(2000); await expect.poll(async () => { await page.clock.runFor(1); return calls; }).toBe(n);
    await page.evaluate(() => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" }); document.dispatchEvent(new Event("visibilitychange")); });
    release(); await page.clock.runFor(10);
    await page.evaluate(() => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" }); document.dispatchEvent(new Event("visibilitychange")); });
  }
  await expect(page.getByRole("button", { name: "恢复状态轮询" })).toHaveCount(0);
  hold = false; await page.clock.fastForward(2000); await expect.poll(async () => { await page.clock.runFor(1); return calls; }).toBe(5);
});

for (const resource of ["sync", "health"]) {
  test(`five-minute watchdog cancels a held ${resource} poll and recovery respects its retry floor`, async ({ page }) => {
    await page.clock.install({ time: new Date("2026-10-15T00:00:00Z") }); await page.clock.pauseAt(new Date("2026-10-15T00:00:01Z")); await facts(page);
    let calls = 0, aborted = 0; let hold = false, held = false, recovered = false; let release!: () => void;
    const path = `/api/v1/repositories/${repository.id}/${resource}`, floor = "2026-10-15T00:05:20Z";
    page.on("requestfailed", request => { if (new URL(request.url()).pathname === path) aborted++; });
    await page.route(`**${path}`, async route => {
      calls++; const shouldHold = hold && !held;
      if (shouldHold) { held = true; await new Promise<void>(resolve => release = resolve); }
      const result = resource === "sync" ? { ...sync, status: recovered || shouldHold ? "completed" : "running", processed_sequence: shouldHold ? 999 : 1 } : { ...health, refresh_status: recovered || shouldHold ? "completed" : "running", snapshot: { ...health.snapshot, generated_at: shouldHold ? "late-held-time" : stamp } };
      await route.fulfill({ json: result }).catch(() => {});
    });
    await page.route(`**${path}/refresh`, route => route.fulfill({ status: 202, json: resource === "sync" ? { ...sync, status: "retry_wait", retry_at: floor } : { ...health, refresh_status: "retry_wait", retry_at: floor } }));
    await page.goto(`/repositories/${repository.id}`);
    await expect.poll(async () => { await page.clock.runFor(1); return page.getByRole("region", { name: "健康事实" }).getByText(stamp, { exact: true }).count(); }).toBeGreaterThan(0); await page.clock.runFor(10);
    const initial = calls; hold = true;
    await page.clock.fastForward(2000); await expect.poll(async () => { await page.clock.runFor(1); return held; }).toBe(true); aborted = 0;
    await page.getByRole("button", { name: resource === "sync" ? "刷新同步" : "刷新健康摘要", exact: true }).click();
    await expect.poll(async () => { await page.clock.runFor(1); return page.getByText(floor, { exact: false }).count(); }).toBeGreaterThan(0);
    await page.clock.fastForward(300_000); await page.clock.runFor(10);
    await expect(page.getByRole("button", { name: "恢复状态轮询" })).toBeVisible();
    await expect.poll(() => aborted).toBeGreaterThan(0);
    release(); await page.clock.runFor(10);
    await expect(page.getByRole("button", { name: "恢复状态轮询" })).toBeVisible();
    await expect(page.getByText("late-held-time", { exact: false })).toHaveCount(0);
    await expect(page.getByText("已处理 999", { exact: false })).toHaveCount(0);
    recovered = true; await page.getByRole("button", { name: "恢复状态轮询" }).click();
    const remaining = Date.parse(floor) - await page.evaluate(() => Date.now());
    await page.clock.fastForward(remaining - 1); expect(calls).toBe(initial + 1);
    await page.clock.fastForward(1); await expect.poll(async () => { await page.clock.runFor(1); return calls; }).toBe(initial + 2);
    await expect(page.getByRole("region", { name: "健康事实" }).getByText(stamp, { exact: true }).first()).toBeVisible();
  });
}
