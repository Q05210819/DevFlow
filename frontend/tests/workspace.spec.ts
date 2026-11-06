import { expect, test, type Page } from "@playwright/test";
import { detail, facts, fail, head, health, repository, stamp, sync } from "./facts-fixtures";

const root = `/repositories/${repository.id}/code`;
const endpoint = `/api/v1/repositories/${repository.id}/workspaces`;
const id = "66666666-6666-4666-8666-666666666666";
const otherId = "77777777-7777-4777-8777-777777777777";
const sourceId = "88888888-8888-4888-8888-888888888888";
const name = "C:/空 格+%#?.tsx";
const literal = '<img src=x onerror="window.unsafe=1">\r\nsecond line\n';
const version = { id, repository_id: repository.id, source_repository_id: sourceId, commit_sha: head, tree_oid: "c".repeat(40), source: { selection: { kind: "branch", name: "main" }, ref: "refs/heads/main", source_repository_id: 102, transfer_repository_id: 102, base_sha: null, head_sha: null }, status: "ready", error: null, created_at: stamp, last_used_at: stamp };
const scope = { repository_id: repository.id, workspace_id: id, source_repository_id: sourceId, commit_sha: head, path: "" };
const tree = { ...scope, items: [{ path: name, display_name: name, kind: "file", mode: "100644", oid: "d".repeat(40), reason: null, source_url: null }, { path: null, display_name: "\\xff unsupported", kind: "unsupported", mode: "100644", oid: null, reason: "unsupported_path", source_url: null }], next_cursor: null, has_more: false, complete: true, truncated: false, scanned: 2, skipped: {}, reason: null };
const file = { ...scope, path: name, kind: "text", blob_oid: "d".repeat(40), text: literal, size_bytes: 55, requested_start_line: 1, requested_line_count: 500, start_line: 1, end_line: 2, total_lines: 2, next_line: null, source_url: `https://github.com/team/fork/blob/${head}/file#L1-L2`, complete: true, truncated: false, scanned: 2, skipped: {}, reason: null };
const search = { ...scope, query: "needle", items: [{ citation: { ...scope, path: name, blob_oid: file.blob_oid, start_line: 2, end_line: 2, source_url: `https://github.com/team/fork/blob/${head}/file#L2` }, text: null, reason: "response_limit" }], complete: true, truncated: false, scanned: { tree_entries: 2, candidates: 2, text_files: 1, blob_bytes: 55 }, skipped: {}, reason: null, snippets_truncated: true };

async function workspace(page: Page) {
  await facts(page);
  await page.route(`**${endpoint}**`, route => {
    const url = new URL(route.request().url());
    if (url.pathname === endpoint) return route.fulfill({ status: route.request().method() === "POST" ? 202 : 200, json: route.request().method() === "POST" ? version : { items: [version], next_cursor: null, has_more: false, unavailable_count: 0 } });
    if (url.pathname.endsWith("/tree")) return route.fulfill({ json: { ...tree, path: url.searchParams.get("path") ?? "" } });
    if (url.pathname.endsWith("/file")) return route.fulfill({ json: { ...file, path: url.searchParams.get("path") } });
    if (url.pathname.endsWith("/search")) return route.fulfill({ json: { ...search, query: url.searchParams.get("query"), path: url.searchParams.get("path") ?? "" } });
    if (url.pathname === `${endpoint}/${id}`) return route.fulfill({ json: version });
    return fail(route, "workspace_not_found", 404);
  });
}
async function prepared(page: Page) {
  await page.goto(root);
  await page.getByRole("button", { name: "准备版本", exact: true }).click();
  await expect(page.getByRole("region", { name: "固定版本" }).getByText(head, { exact: true })).toBeVisible();
}
async function openFile(page: Page) { await page.getByRole("button", { name, exact: true }).click(); await expect(page.locator("pre.code-text")).toHaveText(literal); }

test("switching_version_hides_previous_file_before_new_sha", async ({ page }) => {
  await workspace(page); await prepared(page); await openFile(page);
  let release!: () => void, started!: () => void;
  const waiting = new Promise<void>(resolve => started = resolve);
  await page.route(`**${endpoint}`, async route => { started(); await new Promise<void>(resolve => release = resolve); await route.fulfill({ status: 202, json: { ...version, id: otherId } }).catch(() => {}); });
  await page.getByLabel("分支或标签名称").fill("other");
  await expect(page.locator("pre.code-text")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "固定版本" })).toHaveCount(0);
  await page.getByRole("button", { name: "准备版本", exact: true }).click(); await waiting;
  await page.getByLabel("分支或标签名称").fill("main");
  release();
  await expect(page.locator("pre.code-text")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "固定版本" })).toHaveCount(0);
});

test("typed selections use direct POST, CSRF and one idempotency key per logical retry", async ({ page }) => {
  await workspace(page); const bodies: unknown[] = [], keys: string[] = []; let broken = true;
  await page.route(`**${endpoint}/${id}`, route => route.fulfill({ json: { ...version, source: { ...version.source, selection: bodies.at(-1) ?? version.source.selection } } }));
  await page.route(`**${endpoint}`, route => {
    if (route.request().method() !== "POST") return route.fulfill({ json: { items: [], next_cursor: null, has_more: false, unavailable_count: 0 } });
    bodies.push(route.request().postDataJSON()); keys.push(route.request().headers()["idempotency-key"]);
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-test");
    return broken ? route.abort() : route.fulfill({ status: 202, json: { ...version, source: { ...version.source, selection: bodies.at(-1) } } });
  });
  await page.goto(root); await page.getByRole("button", { name: "准备版本", exact: true }).click();
  await expect(page.getByRole("alert").last()).toBeVisible(); broken = false;
  await page.getByRole("button", { name: "重试准备", exact: true }).click();
  await expect.poll(() => bodies.length).toBe(2); expect(keys[0]).toBe(keys[1]);
  for (const [kind, body] of [["tag", { kind: "tag", name: "v1.0" }], ["pull", { kind: "pull", number: 18, side: "base" }], ["pull", { kind: "pull", number: 18, side: "head" }], ["ci", { kind: "ci", run_id: 700, attempt: 2 }]] as const) {
    await page.getByLabel("版本来源").selectOption(kind);
    if (kind === "tag") await page.getByLabel("分支或标签名称").fill("v1.0");
    if (kind === "pull") { await page.getByLabel("PR 编号").fill("18"); await page.getByLabel("PR 侧").selectOption(body.side); }
    if (kind === "ci") { await page.getByLabel("Run ID").fill("700"); await page.getByLabel("Attempt").fill("2"); }
    await page.getByRole("button", { name: "准备版本", exact: true }).click();
    await expect.poll(() => bodies.at(-1)).toEqual(body);
  }
  expect(bodies[0]).toEqual({ kind: "branch", name: "main" }); expect(new Set(keys).size).toBe(5);
});

test("literal exact paths, LF text and omitted search snippets preserve verified citations at 360px", async ({ page }, info) => {
  await workspace(page); await page.setViewportSize({ width: 360, height: 800 });
  let filePath: string | null = null;
  await page.route(`**${endpoint}/${id}/file?**`, route => { const p = new URL(route.request().url()).searchParams; filePath = p.get("path"); expect([...p.keys()]).toEqual(["path"]); return route.fulfill({ json: file }); });
  await prepared(page);
  await expect(page.getByRole("link", { name: "\\xff unsupported", exact: true })).toHaveCount(0);
  await openFile(page); expect(filePath).toBe(name);
  expect(await page.locator("pre.code-text").textContent()).toBe(literal);
  await expect(page.locator("main img")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "查看源文件行 1–2" })).toHaveAttribute("href", file.source_url);
  await page.getByLabel("字面文本搜索").fill("needle");
  await expect(page.locator("pre.code-text")).toHaveCount(0);
  await page.getByRole("button", { name: "搜索代码", exact: true }).click();
  await expect(page.getByText("匹配行文本因响应预算省略", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: `${name}:2–2` })).toHaveAttribute("href", search.items[0].citation.source_url);
  await expect(page.getByText("搜索覆盖完整", { exact: false })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath("workspace-narrow.png"), fullPage: true });
});

for (const code of ["workspace_not_found", "fork_source_unavailable", "resource_not_found"]) test(`${code} hides old code with correct repository scope`, async ({ page }) => {
  await workspace(page); await prepared(page); await openFile(page);
  await page.route(`**${endpoint}/${id}/file?**`, route => fail(route, code, 404));
  await page.getByRole("button", { name: "刷新代码", exact: true }).click();
  await expect(page.locator("pre.code-text")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "固定版本" })).toHaveCount(0);
  if (code === "resource_not_found") await expect(page.getByRole("link", { name: "PRs", exact: true })).toHaveCount(0);
  else { await page.getByRole("link", { name: "PRs", exact: true }).click(); await page.getByRole("link", { name: "#18 private pull" }).click(); await expect(page.getByRole("heading", { name: "#18 private pull" })).toBeVisible(); }
});

test("tree failed next page hides old entries and retries exact bound cursor", async ({ page }) => {
  await workspace(page); let broken = true;
  await page.route(`**${endpoint}/${id}/tree**`, route => { const p = new URL(route.request().url()).searchParams; const cursor = p.get("cursor"); expect(p.get("limit")).toBeNull(); return cursor && broken ? fail(route) : route.fulfill({ json: { ...tree, items: cursor ? [] : tree.items, next_cursor: cursor ? null : "opaque/+=cursor", has_more: !cursor } }); });
  await prepared(page); await page.getByRole("region", { name: "目录" }).getByRole("button", { name: "下一页" }).click();
  await expect(page.getByRole("button", { name, exact: true })).toHaveCount(0);
  broken = false; await page.getByRole("region", { name: "目录" }).getByRole("button", { name: "重新加载", exact: true }).click();
  await expect(page.getByRole("region", { name: "目录" }).getByText("此页没有目录条目", { exact: true })).toBeVisible();
});

test("oversized first line and unavailable file never invent body or line citation", async ({ page }) => {
  await workspace(page);
  await page.route(`**${endpoint}/${id}/file?**`, route => route.fulfill({ json: { ...file, text: "", start_line: null, end_line: null, source_url: null, complete: false, truncated: true, next_line: null, reason: "response_limit" } }));
  await prepared(page); await page.getByRole("button", { name, exact: true }).click();
  await expect(page.getByText("响应文本预算已到达", { exact: false })).toBeVisible();
  await expect(page.locator("pre.code-text")).toHaveCount(0);
  await expect(page.getByRole("link", { name: /查看源文件行/ })).toHaveCount(0);
});

test("filtered empty snapshot page continues; cached snapshot re-entry waits for fresh metadata", async ({ page }) => {
  await workspace(page); let held = false, release!: () => void;
  await page.route(`**${endpoint}`, route => route.fulfill({ json: { items: [], next_cursor: "next", has_more: true, unavailable_count: 2 } }));
  await page.goto(root); await expect(page.getByRole("region", { name: "已准备版本" }).getByRole("button", { name: "下一页" })).toBeEnabled();
  await page.route(`**${endpoint}`, route => route.fulfill({ json: { items: [version], next_cursor: null, has_more: false, unavailable_count: 0 } }));
  await page.goto(`${root}?workspace=${id}`); await openFile(page);
  await page.getByRole("link", { name: "PRs", exact: true }).click();
  await page.route(`**${endpoint}/${id}`, async route => { held = true; await new Promise<void>(resolve => release = resolve); await route.fulfill({ json: version }).catch(() => {}); });
  await page.getByRole("link", { name: "代码", exact: true }).click();
  await page.getByRole("region", { name: "已准备版本" }).getByRole("button", { name: "分支 main · 已准备", exact: true }).click();
  await expect.poll(() => held).toBe(true);
  await expect(page.locator("pre.code-text")).toHaveCount(0); await expect(page.getByRole("button", { name, exact: true })).toHaveCount(0);
  release(); await expect(page.getByRole("button", { name, exact: true })).toBeVisible();
});

test("nullable default branch requires explicit input and never prepares main", async ({ page }) => {
  await workspace(page); let posts = 0;
  await page.route(`**/api/v1/repositories/${repository.id}`, route => route.fulfill({ json: { ...detail, default_branch: null } }));
  page.on("request", req => { if (req.method() === "POST" && new URL(req.url()).pathname === endpoint) posts++; });
  await page.goto(root); await expect(page.getByLabel("分支或标签名称")).toHaveValue("");
  await expect(page.getByRole("button", { name: "准备版本", exact: true })).toBeDisabled(); expect(posts).toBe(0);
});

test("Health distinguishes frozen logical/physical/unknown evidence and does not prepare code", async ({ page }) => {
  await workspace(page); let posts = 0;
  page.on("request", req => { if (req.method() === "POST" && new URL(req.url()).pathname === endpoint) posts++; });
  const evidence = { selection: { kind: "branch", name: "main" }, workspace_id: id, source_repository_id: sourceId, commit_sha: head, availability: "available", reason: null, checked_at: stamp, basis: "local_pin_commit_root_tree_identity" };
  await page.route(`**/api/v1/repositories/${repository.id}/health`, route => route.fulfill({ json: { ...health, snapshot: { ...health.snapshot, workspace: "partial", manifest: { ...health.snapshot.manifest, workspace_versions: [evidence, { ...evidence, selection: { kind: "pull", number: 18, side: "base" } }, { ...evidence, selection: { kind: "pull", number: 18, side: "head" }, availability: "unknown", reason: "verification_limit", checked_at: null }], workspace_selection: { selection_order: "default; PR number/base/head; frozen CI order", basis: "frozen_health_visible_evidence", total_candidates: 3, returned_count: 3, truncated: false, checked_pins: 1, checked_count: 2, unverified_count: 1 } } } } }));
  await page.goto(`/repositories/${repository.id}`);
  await expect(page.getByRole("region", { name: "代码版本证据" })).toContainText("部分可用");
  await expect(page.getByRole("region", { name: "代码版本证据" })).toContainText("物理检查 1 · 已得结论的逻辑版本 2 · 待验证 1");
  expect(posts).toBe(0);
});

test("same-SHA snapshots retain distinct source identity and ignore a held old file", async ({ page }) => {
  await workspace(page);
  const otherSource = "99999999-9999-4999-8999-999999999999";
  const second = { ...version, id: otherId, source_repository_id: otherSource, source: { ...version.source, selection: { kind: "tag", name: "same-sha" }, source_repository_id: 103 } };
  await page.route(`**${endpoint}`, route => route.request().method() === "POST" ? route.fulfill({ status: 202, json: version }) : route.fulfill({ json: { items: [version, second], next_cursor: null, has_more: false, unavailable_count: 0 } }));
  await page.route(`**${endpoint}/${otherId}**`, route => route.fulfill({ json: new URL(route.request().url()).pathname.endsWith("/tree") ? { ...tree, workspace_id: otherId, source_repository_id: otherSource } : second }));
  await prepared(page);
  let held = false, release!: () => void;
  await page.route(`**${endpoint}/${id}/file?**`, async route => { held = true; await new Promise<void>(resolve => release = resolve); await route.fulfill({ json: { ...file, text: "old source private file" } }).catch(() => {}); });
  await page.getByRole("button", { name, exact: true }).click(); await expect.poll(() => held).toBe(true);
  await page.getByRole("button", { name: "标签 same-sha · 已准备", exact: true }).click();
  const fixed = page.getByRole("region", { name: "固定版本" });
  await expect(fixed).toContainText(otherId); await expect(fixed).toContainText(otherSource); await expect(fixed.getByText(head, { exact: true })).toBeVisible();
  release(); await expect(page.getByText("old source private file", { exact: true })).toHaveCount(0);
  await expect(fixed).not.toContainText(sourceId);
});

for (const resource of ["tree", "file", "search"] as const) test(`late ${resource} after A-B-A cannot restore departed code`, async ({ page }) => {
  await workspace(page); await prepared(page);
  let held = false, release!: () => void;
  await page.route(`**${endpoint}/${id}/${resource}**`, async route => {
    held = true; await new Promise<void>(resolve => release = resolve);
    const data = resource === "tree" ? { ...tree, items: [{ ...tree.items[0], display_name: "late private tree", path: "late-private.ts" }] } : resource === "file" ? { ...file, text: "late private file" } : { ...search, items: [{ ...search.items[0], text: "late private search" }] };
    await route.fulfill({ json: data }).catch(() => {});
  });
  if (resource === "tree") await page.getByRole("button", { name: "根目录", exact: true }).click();
  if (resource === "file") await page.getByRole("button", { name, exact: true }).click();
  if (resource === "search") { await page.getByLabel("字面文本搜索").fill("needle"); await page.getByRole("button", { name: "搜索代码", exact: true }).click(); }
  await expect.poll(() => held).toBe(true);
  await page.getByLabel("分支或标签名称").fill("B"); await page.getByLabel("分支或标签名称").fill("main");
  release();
  await expect(page.getByText(`late private ${resource}`, { exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "固定版本" })).toHaveCount(0);
});

for (const resource of ["tree", "file", "search"] as const) test(`${resource} scope echo mismatch hides the whole code version`, async ({ page }) => {
  await workspace(page); await prepared(page);
  const data = resource === "tree" ? tree : resource === "file" ? file : search;
  await page.route(`**${endpoint}/${id}/${resource}**`, route => route.fulfill({ json: { ...data, commit_sha: "e".repeat(40) } }));
  if (resource === "tree") await page.getByRole("button", { name: "根目录", exact: true }).click();
  if (resource === "file") await page.getByRole("button", { name, exact: true }).click();
  if (resource === "search") { await page.getByLabel("字面文本搜索").fill("needle"); await page.getByRole("button", { name: "搜索代码", exact: true }).click(); }
  await expect(page.getByRole("alert").last()).toContainText("返回的代码版本与当前选择不一致");
  await expect(page.getByRole("region", { name: "固定版本" })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "已准备版本" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "PRs", exact: true })).toBeVisible();
});

test("pending preparation polls only exact metadata and does not read content until ready", async ({ page }) => {
  await workspace(page); await page.clock.install(); let ready = false, reads = 0, metadata = 0;
  await page.route(`**${endpoint}/${id}`, route => { metadata++; return route.fulfill({ json: { ...version, status: ready ? "ready" : "queued" } }); });
  page.on("request", req => { if (/\/(tree|file|search)$/.test(new URL(req.url()).pathname)) reads++; });
  await page.goto(root); await page.getByRole("button", { name: "准备版本", exact: true }).click();
  await expect(page.getByText("等待准备", { exact: true })).toBeVisible(); expect(reads).toBe(0);
  ready = true; await page.clock.fastForward(2000);
  await expect.poll(async () => { await page.clock.runFor(1); return metadata; }).toBeGreaterThan(1);
  await expect(page.getByRole("button", { name, exact: true })).toBeVisible();
});

for (const location of ["directory", "range", "search"] as const) test(`ready ${location} survives unrelated sync polling and still hides on revocation`, async ({ page }) => {
  await workspace(page); await page.clock.install();
  let syncReads = 0, metadataReads = 0, denied = false;
  await page.route(`**/api/v1/repositories/${repository.id}/sync`, route => {
    syncReads++; return denied ? fail(route, "resource_not_found", 404) : route.fulfill({ json: { ...sync, status: "running" } });
  });
  await page.route(`**${endpoint}/${id}`, route => { metadataReads++; return route.fulfill({ json: version }); });
  await page.route(`**${endpoint}/${id}/tree**`, route => {
    const path = new URL(route.request().url()).searchParams.get("path") ?? "";
    return route.fulfill({ json: { ...tree, path, items: path ? tree.items : [{ ...tree.items[0], path: "src", display_name: "src", kind: "directory", mode: "040000" }] } });
  });
  await page.route(`**${endpoint}/${id}/search**`, route => route.fulfill({ json: { ...search, path: "src", items: [{ ...search.items[0], citation: { ...search.items[0].citation, path: `src/${name}` } }] } }));
  await prepared(page); await page.getByRole("button", { name: "src", exact: true }).click();
  await expect(page.getByRole("heading", { name: "目录 src", exact: true })).toBeVisible();
  if (location === "range") {
    await openFile(page);
    await page.route(`**${endpoint}/${id}/file?**`, route => route.fulfill({ json: { ...file, requested_start_line: 2, requested_line_count: 1, start_line: 2, end_line: 2, text: "second line\n", source_url: search.items[0].citation.source_url } }));
    await page.getByLabel("起始行（留空使用 API 默认）").fill("2");
    await page.getByLabel("行数（最多 500）").fill("1");
    await page.getByRole("button", { name: "读取行范围", exact: true }).click();
    await expect(page.locator("pre.code-text")).toHaveText("second line\n");
  }
  if (location === "search") {
    await page.getByLabel("字面文本搜索").fill("needle");
    await page.getByRole("button", { name: "搜索代码", exact: true }).click();
    await expect(page.getByText("匹配行文本因响应预算省略", { exact: true })).toBeVisible();
  }
  const initialMetadata = metadataReads;
  for (let tick = 0; tick < 3; tick++) {
    const before = syncReads;
    await page.clock.runFor(2100);
    await expect.poll(() => syncReads).toBeGreaterThan(before);
    await expect.poll(() => metadataReads).toBeGreaterThan(initialMetadata + tick);
    if (location === "directory") await expect(page.getByRole("heading", { name: "目录 src", exact: true })).toBeVisible();
    if (location === "range") {
      await expect(page.locator("pre.code-text")).toHaveText("second line\n");
      await expect(page.getByLabel("起始行（留空使用 API 默认）")).toHaveValue("2");
      await expect(page.getByLabel("行数（最多 500）")).toHaveValue("1");
    }
    if (location === "search") {
      await expect(page.getByLabel("字面文本搜索")).toHaveValue("needle");
      await expect(page.getByText("匹配行文本因响应预算省略", { exact: true })).toBeVisible();
      await expect(page.getByText("范围：src。", { exact: false })).toBeVisible();
    }
  }
  denied = true; await page.clock.runFor(2100);
  await expect(page.getByRole("region", { name: "固定版本" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "PRs", exact: true })).toHaveCount(0);
});

test("manual reconfirm hides ready content while its response is held", async ({ page }) => {
  await workspace(page); await prepared(page); await openFile(page);
  let held = false, release!: () => void;
  await page.route(`**${endpoint}/${id}`, async route => { held = true; await new Promise<void>(resolve => release = resolve); await route.fulfill({ json: version }); });
  await page.getByRole("button", { name: "重新确认版本", exact: true }).click();
  await expect.poll(() => held).toBe(true);
  await expect(page.getByRole("region", { name: "固定版本" })).toHaveCount(0);
  release(); await expect(page.getByRole("region", { name: "固定版本" })).toBeVisible();
});

test("refresh rejects changed snapshot provenance even when the SHA is unchanged", async ({ page }) => {
  await workspace(page); await prepared(page); await openFile(page);
  await page.route(`**${endpoint}/${id}`, route => route.fulfill({ json: { ...version, source: { ...version.source, transfer_repository_id: 999 } } }));
  await page.getByRole("button", { name: "重新确认版本", exact: true }).click();
  await expect(page.getByRole("alert").last()).toContainText("返回的代码版本与当前选择不一致");
  await expect(page.locator("pre.code-text")).toHaveCount(0);
});

test("file range edits hide the old body and continuation uses exact returned next_line", async ({ page }) => {
  await workspace(page); await prepared(page); await openFile(page);
  await page.getByLabel("起始行（留空使用 API 默认）").fill("2");
  await expect(page.locator("pre.code-text")).toHaveCount(0);
  await page.route(`**${endpoint}/${id}/file?**`, route => {
    const params = new URL(route.request().url()).searchParams;
    expect(params.get("path")).toBe(name); expect(params.get("line_count")).toBe("1");
    const start = params.get("start_line"); expect(["2", "3"]).toContain(start);
    return route.fulfill({ json: { ...file, requested_start_line: Number(start), requested_line_count: 1, start_line: Number(start), end_line: Number(start), text: start === "2" ? "second line\n" : "third line\n", source_url: `https://github.com/team/fork/blob/${head}/file#L${start}`, total_lines: 3, complete: false, truncated: true, next_line: start === "2" ? 3 : null, reason: "line_limit" } });
  });
  await page.getByLabel("行数（最多 500）").fill("1"); await page.getByRole("button", { name: "读取行范围", exact: true }).click();
  await expect(page.locator("pre.code-text")).toHaveText("second line\n");
  await expect(page.getByRole("link", { name: "查看源文件行 2–2" })).toHaveAttribute("href", search.items[0].citation.source_url);
  await page.getByRole("button", { name: "继续读取第 3 行", exact: true }).click();
  await expect(page.locator("pre.code-text")).toHaveText("third line\n");
});

for (const kind of ["lfs_pointer", "too_large", "binary", "non_utf8", "symlink", "gitlink"] as const) test(`${kind} never displays a text body or line citation`, async ({ page }) => {
  await workspace(page);
  await page.route(`**${endpoint}/${id}/file?**`, route => route.fulfill({ json: { ...file, kind, text: "", blob_oid: kind === "gitlink" ? null : file.blob_oid, size_bytes: null, total_lines: null, start_line: null, end_line: null, source_url: null, complete: false, reason: kind } }));
  await prepared(page); await page.getByRole("button", { name, exact: true }).click();
  await expect(page.getByRole("region", { name: "文件" })).toContainText(kind);
  await expect(page.locator("pre.code-text")).toHaveCount(0);
  await expect(page.getByRole("link", { name: /查看源文件行/ })).toHaveCount(0);
});

test("logout while preparation is held cannot restore private code or metadata", async ({ page }) => {
  await workspace(page); let held = false, release!: () => void, signedIn = true;
  await page.route("**/api/v1/session", route => signedIn ? route.fulfill({ json: { user: { id: "member-id", github_id: 42, login: "colleague", role: "member", enabled: true }, csrf_token: "csrf-test" } }) : fail(route, "unauthenticated", 401));
  await page.route("**/api/v1/auth/logout", route => { signedIn = false; return route.fulfill({ status: 204 }); });
  await page.route(`**${endpoint}`, async route => {
    if (route.request().method() !== "POST") return route.fulfill({ json: { items: [version], next_cursor: null, has_more: false, unavailable_count: 0 } });
    held = true; await new Promise<void>(resolve => release = resolve); await route.fulfill({ status: 202, json: version }).catch(() => {});
  });
  await page.goto(root); await page.getByRole("button", { name: "准备版本", exact: true }).click(); await expect.poll(() => held).toBe(true);
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(page.getByRole("link", { name: /GitHub.*登录/ })).toBeVisible(); release();
  await expect(page.getByRole("region", { name: "固定版本" })).toHaveCount(0);
  await expect(page.getByText(head, { exact: true })).toHaveCount(0);
});
