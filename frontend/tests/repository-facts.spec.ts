import { expect, test } from "@playwright/test";
import { base, collection, coverage, detail, fail, facts, head, issue, pull, repository, run, stamp } from "./facts-fixtures";

test("repository entry opens authorized Issue facts with literal body and source identities", async ({ page }) => {
  await facts(page);
  await page.goto("/repositories");
  await page.getByRole("link", { name: "查看 team/private-app" }).click();
  await page.getByRole("link", { name: "Issues", exact: true }).click();
  await page.getByRole("link", { name: "#17 private issue" }).click();
  await expect(page.getByRole("heading", { name: "#17 private issue" })).toBeVisible();
  await expect(page.getByText("GitHub ID：501", { exact: true })).toBeVisible();
  await expect(page.getByText(issue.payload.body, { exact: true })).toBeVisible();
  await expect(page.locator("main img")).toHaveCount(0);
  await expect(page.getByText("Markdown 暂不排版", { exact: false })).toBeVisible();
  await expect(page.getByText("尚未采集", { exact: false }).first()).toBeVisible();
});

test("PR full SHA and exact CI attempt remain usable at 360px", async ({ page }, info) => {
  await facts(page); await page.setViewportSize({ width: 360, height: 800 });
  await page.goto(`/repositories/${repository.id}/pulls/18`);
  await expect(page.getByText(base, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(head, { exact: true }).first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath("pull-narrow.png"), fullPage: true });
  await page.getByRole("link", { name: "Actions", exact: true }).click();
  await page.getByRole("link", { name: "CI · 700 / 2" }).click();
  await expect(page.getByRole("heading", { name: "CI · 700 / 2" })).toBeVisible();
  await expect(page.getByText("failure", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("second page failure hides prior rows and retries the bound cursor", async ({ page }) => {
  await facts(page); let broken = true;
  await page.route(`**/api/v1/repositories/${repository.id}/issues?**`, route => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    if (cursor && broken) return fail(route);
    return route.fulfill({ json: { items: [{ ...issue, payload: { ...issue.payload, title: cursor ? "second issue" : "first issue" } }], coverage, collection: null, next_cursor: cursor ? null : "opaque/+=cursor" } });
  });
  await page.goto(`/repositories/${repository.id}/issues`);
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toBeVisible();
  await expect(page.getByText("first issue", { exact: false })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "上一页" })).toBeEnabled();
  broken = false; await page.getByRole("button", { name: "重新加载", exact: true }).click();
  await expect(page.getByText("#17 second issue", { exact: true })).toBeVisible();
});

test("fact_not_found is local and does not revoke repository navigation", async ({ page }) => {
  await facts(page); await page.route(`**/api/v1/repositories/${repository.id}/issues/99`, route => fail(route, "fact_not_found", 404));
  await page.goto(`/repositories/${repository.id}/issues/99`);
  await expect(page.getByRole("main").getByRole("alert")).toContainText("事实不存在");
  await page.getByRole("link", { name: "Issues", exact: true }).click();
  await expect(page.getByText("#17 private issue", { exact: true })).toBeVisible();
});

test("a newly missing cached fact hides its old body while repository stays accessible", async ({ page }) => {
  await facts(page); let missing = false;
  await page.route(`**/api/v1/repositories/${repository.id}/issues/17`, route => missing ? fail(route, "fact_not_found", 404) : route.fulfill({ json: { item: issue, collections: [], coverage } }));
  await page.goto(`/repositories/${repository.id}/issues/17`);
  await expect(page.getByText(issue.payload.body, { exact: true })).toBeVisible();
  missing = true; await page.getByRole("button", { name: "刷新事实", exact: true }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("事实不存在");
  await expect(page.getByText(issue.payload.body, { exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Issues", exact: true })).toBeVisible();
});

test("test_revoked_cached_repository_is_hidden", async ({ page }) => {
  await facts(page); let denied = false;
  await page.route(`**/api/v1/repositories/${repository.id}/issues/17`, route => denied ? fail(route, "resource_not_found", 404) : route.fulfill({ json: { item: issue, collections: [], coverage } }));
  await page.goto(`/repositories/${repository.id}/issues/17`);
  await expect(page.getByText(issue.payload.body, { exact: true })).toBeVisible();
  denied = true; await page.getByRole("button", { name: "刷新事实", exact: true }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("仓库不可访问");
  await expect(page.getByText(issue.payload.body, { exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Issues", exact: true })).toHaveCount(0);
});

test("late responses from a departed repository cannot restore private text", async ({ page }) => {
  await facts(page); let release!: () => void; let started!: () => void;
  const waiting = new Promise<void>(resolve => started = resolve);
  await page.route(`**/api/v1/repositories/${repository.id}/issues/17`, async route => {
    started(); await new Promise<void>(resolve => release = resolve);
    await route.fulfill({ json: { item: issue, collections: [], coverage } }).catch(() => {});
  });
  await page.goto(`/repositories/${repository.id}/issues/17`); await waiting;
  await page.getByRole("link", { name: "仓库", exact: true }).click(); release();
  await expect(page.getByRole("link", { name: "查看 team/private-app" })).toBeVisible();
  await expect(page.getByText(issue.payload.body, { exact: true })).toHaveCount(0);
});

test("collection complete empty differs from uncollected and exact older attempts stay bound", async ({ page }) => {
  await facts(page);
  await page.route(`**/api/v1/repositories/${repository.id}/actions/runs/700/attempts/1**`, route => {
    const selected = new URL(route.request().url()).searchParams.get("collection");
    const job = { ...issue, kind: "ci_job", source_id: 800, number: null, source_url: null, payload: { normalization_reasons: [], run_id: 700, attempt: 1, head_sha: base, name: "older attempt job", status: "completed", conclusion: "success", started_at: stamp, completed_at: stamp, steps: [{ number: 1, name: "older step", status: "completed", conclusion: "success", started_at: stamp, completed_at: stamp }] } };
    return route.fulfill({ json: selected ? { items: selected === "jobs" ? [job] : [], next_cursor: null, coverage: { ...coverage, stream: "actions" }, collection: { ...collection, collection: selected, complete: true, reason: null } } : { item: { ...run, version_key: "1", payload: { ...run.payload, attempt: 1, conclusion: "success" } }, collections: [], coverage: { ...coverage, stream: "actions" } } });
  });
  await page.goto(`/repositories/${repository.id}/actions/runs/700/attempts/1`.replace("/actions/runs/", "/actions/"));
  await expect(page.getByRole("heading", { name: "CI · 700 / 1" })).toBeVisible();
  await expect(page.getByText("已完整采集，暂无记录", { exact: false }).first()).toBeVisible();
  await expect(page.getByText("success", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "older attempt job" })).toBeVisible();
  await expect(page.getByRole("list", { name: "任务步骤" })).toContainText("older step");
});

test("PR child cursor resets with base/head version and old reviews keep their commit evidence", async ({ page }) => {
  await facts(page); let changed = false; let mixed = 0;
  const oldCommit = "d".repeat(40), newHead = "c".repeat(40);
  await page.route(`**/api/v1/repositories/${repository.id}/pulls/18**`, route => {
    const params = new URL(route.request().url()).searchParams;
    const kind = params.get("collection"), cursor = params.get("cursor");
    if (!kind) return route.fulfill({ json: { item: changed ? { ...pull, observed_at: "2026-10-14T01:00:00Z", payload: { ...pull.payload, head_sha: newHead } } : pull, collections: [], coverage: { ...coverage, stream: "pulls" } } });
    if (changed && cursor) mixed++;
    const file = { ...issue, kind: "pr_file", source_id: 502, number: null, version_key: `${base}:${changed ? newHead : head}:${"a".repeat(64)}`, source_url: null, payload: { normalization_reasons: [], base_sha: base, head_sha: changed ? newHead : head, filename: changed ? "new-version.ts" : cursor ? "second-file.ts" : "first-file.ts", previous_filename: null, sha: null, status: "modified", additions: 1, deletions: 0, changes: 1, patch: "+<script>window.unsafe=1</script>", patch_status: "truncated" } };
    const review = { ...issue, kind: "review", source_id: 900, number: null, source_url: null, payload: { normalization_reasons: [], body: "old head review", author: null, created_at: stamp, state: "approved", commit_id: oldCommit, submitted_at: stamp } };
    return route.fulfill({ json: { items: kind === "files" ? [file] : kind === "reviews" ? [review] : [], next_cursor: kind === "files" && !changed && !cursor ? "old-version-cursor" : null, coverage: { ...coverage, stream: "pulls" }, collection: { ...collection, collection: kind, version_key: `${base}:${changed ? newHead : head}`, complete: false, reason: "partial" } } });
  });
  await page.goto(`/repositories/${repository.id}/pulls/18`);
  const files = page.getByRole("region", { name: "文件与补丁" });
  await expect(files.getByText("first-file.ts", { exact: true })).toBeVisible();
  await expect(files).toContainText("已截断，不完整");
  await expect(files.getByText("+<script>window.unsafe=1</script>", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "评审", exact: true })).toContainText(oldCommit);
  await files.getByRole("button", { name: "下一页" }).click();
  await expect(files.getByText("second-file.ts", { exact: true })).toBeVisible();
  changed = true; await page.getByRole("button", { name: "刷新事实", exact: true }).click();
  await expect(files.getByText("new-version.ts", { exact: true })).toBeVisible();
  await expect(files.getByRole("button", { name: "上一页" })).toBeDisabled();
  expect(mixed).toBe(0); expect(await page.evaluate(() => (window as unknown as { unsafe?: number }).unsafe)).toBeUndefined();
});

test("repository switch rejects a held old repository response", async ({ page }) => {
  await facts(page); const other = { ...repository, id: "55555555-5555-4555-8555-555555555555", full_name: "team/other-app" };
  let hold = false; let release!: () => void; let waiting!: () => void; const started = new Promise<void>(resolve => waiting = resolve);
  await page.route("**/api/v1/repositories?**", route => route.fulfill({ json: { items: [{ ...repository, sync: detail.sync }, { ...other, sync: detail.sync }], next_cursor: null } }));
  await page.route(`**/api/v1/repositories/${repository.id}/issues/17`, async route => {
    if (hold) { waiting(); await new Promise<void>(resolve => release = resolve); }
    await route.fulfill({ json: { item: issue, collections: [], coverage } }).catch(() => {});
  });
  await page.route(`**/api/v1/repositories/${other.id}**`, route => route.fulfill({ json: route.request().url().endsWith("/sync") ? { ...detail.sync, repository_id: other.id } : route.request().url().endsWith("/health") ? { snapshot: null, refresh_status: "idle", error_code: null, retry_at: null } : { ...other, sync: { ...detail.sync, repository_id: other.id } } }));
  await page.goto(`/repositories/${repository.id}/issues/17`); await expect(page.getByText(issue.payload.body, { exact: true })).toBeVisible();
  hold = true; await page.getByRole("button", { name: "刷新事实", exact: true }).click(); await started;
  await page.getByRole("link", { name: "仓库", exact: true }).click(); await page.getByRole("link", { name: "查看 team/other-app" }).click();
  await expect(page.getByRole("heading", { name: "team/other-app" })).toBeVisible(); release();
  await expect(page.getByText(issue.payload.body, { exact: true })).toHaveCount(0);
});

test("logout aborts a pending repository command and hides its later result", async ({ page }) => {
  await facts(page); let signedIn = true; let release!: () => void; let waiting!: () => void; const started = new Promise<void>(resolve => waiting = resolve);
  await page.route("**/api/v1/session", route => signedIn ? route.fulfill({ json: { user: { id: "member-id", github_id: 42, login: "colleague", role: "member", enabled: true }, csrf_token: "csrf-test" } }) : fail(route, "unauthenticated", 401));
  await page.route("**/api/v1/auth/logout", route => { signedIn = false; return route.fulfill({ status: 204 }); });
  await page.route(`**/api/v1/repositories/${repository.id}/sync/refresh`, async route => { waiting(); await new Promise<void>(resolve => release = resolve); await route.fulfill({ status: 202, json: detail.sync }).catch(() => {}); });
  await page.goto(`/repositories/${repository.id}`); await page.getByRole("button", { name: "刷新同步", exact: true }).click(); await started;
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page.getByRole("link", { name: /GitHub.*登录/ })).toBeVisible(); release();
  await expect(page.getByRole("heading", { name: "team/private-app" })).toHaveCount(0);
});

test("cached repository re-entry waits for a fresh authorization confirmation", async ({ page }) => {
  await facts(page); let reads = 0; let hold = false; let release!: () => void;
  await page.route(`**/api/v1/repositories/${repository.id}`, async route => {
    reads++; if (hold) await new Promise<void>(resolve => release = resolve);
    await route.fulfill({ json: detail }).catch(() => {});
  });
  await page.goto(`/repositories/${repository.id}/issues/17`);
  await expect(page.getByText(issue.payload.body, { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "仓库", exact: true }).click();
  const before = reads; hold = true;
  await page.getByRole("link", { name: "查看 team/private-app" }).click();
  await expect.poll(() => reads).toBeGreaterThan(before);
  await expect(page.getByText("team/private-app", { exact: true })).toHaveCount(0);
  release(); await expect(page.getByRole("heading", { name: "team/private-app" })).toBeVisible();
});

test("session identity change clears old repository state and ignores its held response", async ({ page }) => {
  await facts(page); let otherUser = false; let reads = 0; let hold = false; let release!: () => void;
  await page.route("**/api/v1/session", route => route.fulfill({ json: { user: { id: otherUser ? "other-user" : "member-id", github_id: otherUser ? 43 : 42, login: otherUser ? "new-colleague" : "colleague", role: "member", enabled: true }, csrf_token: otherUser ? "csrf-new" : "csrf-test" } }));
  await page.route(`**/api/v1/repositories/${repository.id}/issues/17`, async route => {
    reads++;
    const old = !otherUser;
    if (hold && old) await new Promise<void>(resolve => release = resolve);
    await route.fulfill({ json: { item: { ...issue, payload: { ...issue.payload, body: old ? "old private body" : "new private body" } }, collections: [], coverage } }).catch(() => {});
  });
  await page.goto(`/repositories/${repository.id}/issues/17`);
  await expect(page.getByText("old private body", { exact: true })).toBeVisible();
  const before = reads; hold = true;
  await page.getByRole("button", { name: "刷新事实", exact: true }).click(); await expect.poll(() => reads).toBeGreaterThan(before);
  otherUser = true; await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" }); document.dispatchEvent(new Event("visibilitychange", { bubbles: true }));
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" }); document.dispatchEvent(new Event("visibilitychange", { bubbles: true }));
  });
  await expect(page.getByText("new-colleague", { exact: true })).toBeVisible();
  release(); await expect(page.getByText("new private body", { exact: true })).toBeVisible();
  await expect(page.getByText("old private body", { exact: true })).toHaveCount(0);
});
