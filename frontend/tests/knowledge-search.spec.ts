import { expect, test, type Page } from "@playwright/test";
import { repository, session } from "./repository-fixtures";

const stamp = "2026-11-05T00:00:00Z";
const baseId = repository.id;
const forkId = "22222222-2222-4222-8222-222222222222";
const legacyId = "33333333-3333-4333-8333-333333333333";
const workspaceBase = "44444444-4444-4444-8444-444444444444";
const workspaceFork = "55555555-5555-4555-8555-555555555555";
const workspaceLegacy = "66666666-6666-4666-8666-666666666666";
const docBase = "77777777-7777-4777-8777-777777777777";
const docFork = "88888888-8888-4888-8888-888888888888";
const docLegacy = "99999999-9999-4999-8999-999999999999";
const targetSha = "a".repeat(40);
const forkSha = "b".repeat(40);
const legacyTargetSha = "c".repeat(40);
const legacyActiveSha = "d".repeat(40);
const QIDS = [
  "10000000-0000-4000-8000-000000000001",
  "10000000-0000-4000-8000-000000000002",
  "10000000-0000-4000-8000-000000000003",
  "10000000-0000-4000-8000-000000000004",
  "10000000-0000-4000-8000-000000000005",
];
const JIDS = QIDS.map(id => id.replace(/^1000/, "2000"));

type IndexChoice = { repository_id: string; sha: string; target: "target" | "active" };
type SelectionBody = { selection: { selected_repository_ids: string[]; indexes: IndexChoice[] }; question: string };
type State = {
  submitted: Array<{ body: SelectionBody; key: string | null }>;
  listCalls: string[];
  gates: Map<string, Promise<void>>;
  denied: Set<string>;
  completed: Set<string>;
  citationCursors: Array<string | null>;
};

const sync = (id: string) => ({
  repository_id: id, requested_sequence: 1, processed_sequence: 1, generation: 1, status: "completed",
  streams: [], error_code: null, next_due_at: null, retry_at: null,
});
const catalog = [
  { ...repository, full_name: "team/base-app", sync: sync(baseId) },
  { ...repository, id: forkId, github_repository_id: 102, full_name: "team/base-app-fork", sync: sync(forkId) },
  { ...repository, id: legacyId, github_repository_id: 103, full_name: "team/legacy-app", sync: sync(legacyId) },
];

function knowledge(id: string, name: string, options: {
  generation: number; sha: string; status: "not_indexed" | "building" | "ready" | "stale";
  reason: string | null; activeGeneration?: number; activeSha?: string;
}) {
  const workspace = id === baseId ? workspaceBase : id === forkId ? workspaceFork : workspaceLegacy;
  return {
    repository_id: id,
    target_generation: options.generation,
    target_workspace_id: workspace,
    target_sha: options.sha,
    path_rule_version: "knowledge-paths-v1",
    document_paths: ["README.md"],
    code_paths: [],
    corpus: {
      import_id: `${id.slice(0, 8)}-8888-4888-8888-888888888888`, kind: "workspace", status: "ready",
      target_generation: options.generation, included: 2, skipped: 0, unsupported: 0, over_limit: 0,
      unscanned: false, complete: true, scan_reason: null, processed_chunks: 5, created_at: stamp, deadline: stamp,
    },
    needs_reimport: false,
    index_status: options.status,
    index_reason: options.reason,
    active_index_generation: options.activeGeneration ?? null,
    active_index_sha: options.activeSha ?? null,
    documents: { items: [], next_cursor: null },
    limits: { max_files: 2000, max_scan_bytes: 33554432, max_file_bytes: 1048576 },
  };
}

const views = new Map([
  [baseId, knowledge(baseId, "team/base-app", { generation: 2, sha: targetSha, status: "ready", reason: null, activeGeneration: 2, activeSha: targetSha })],
  [forkId, knowledge(forkId, "team/base-app-fork", { generation: 1, sha: forkSha, status: "not_indexed", reason: "target_not_indexed" })],
  [legacyId, knowledge(legacyId, "team/legacy-app", { generation: 5, sha: legacyTargetSha, status: "building", reason: "target_building", activeGeneration: 4, activeSha: legacyActiveSha })],
]);

function entry(citationId: string, repositoryId: string, sourceRepositoryId: string, documentId: string, workspaceId: string, sha: string, path: string, dependencies: string[], rank: number) {
  return {
    citation_id: citationId,
    identity: {
      repository_id: repositoryId, source_repository_id: sourceRepositoryId,
      dependency_repository_ids: dependencies, document_id: documentId,
      revision_hash: String(rank).repeat(64), workspace_id: workspaceId, sha, path,
    },
    location: { start_line: rank * 10, end_line: rank * 10 + 2, start_byte: rank * 100, end_byte: rank * 100 + 40 },
    url: `https://github.com/team/private-app/blob/${sha}/${path}#L${rank * 10}-L${rank * 10 + 2}`,
    content_hash: String(rank + 5).repeat(64),
    rank,
  };
}

const baseEntry = entry("c0ffee0000000001", baseId, baseId, docBase, workspaceBase, targetSha, "docs/base.md", [baseId], 1);
const forkEntry = entry("c0ffee0000000002", forkId, forkId, docFork, workspaceFork, forkSha, "docs/fork.md", [baseId, forkId], 2);
const legacyEntry = entry("c0ffee0000000003", legacyId, legacyId, docLegacy, workspaceLegacy, legacyActiveSha, "legacy/old.md", [legacyId], 1);

const baseCoverage = { repository_id: baseId, sha: targetSha, target_generation: 2, included: 3, skipped: 1, unsupported: 1, over_limit: 0, unscanned: false, complete: true };
const forkCoverage = { repository_id: forkId, sha: forkSha, target_generation: 1, included: 2, skipped: 0, unsupported: 0, over_limit: 0, unscanned: true, complete: false };
const legacyCoverage = { repository_id: legacyId, sha: legacyActiveSha, target_generation: 4, included: 4, skipped: 2, unsupported: 1, over_limit: 1, unscanned: true, complete: false };

const legacyPreview = {
  identity: legacyEntry.identity,
  text: "旧版本正文（仅返回区间）",
  location: { start_line: 10, end_line: 12, start_byte: 100, end_byte: 140 },
  is_current: false,
  partial: true,
  next_cursor: "preview-cursor",
  reason: null,
};

function resultFor(body: SelectionBody, ordinal: number) {
  const ids = body.selection.selected_repository_ids;
  const hasBase = ids.includes(baseId);
  const hasFork = ids.includes(forkId);
  const hasLegacy = ids.includes(legacyId);
  if (hasLegacy && hasBase) {
    return {
      text: "旧范围证据",
      entries: [legacyEntry],
      evidence: [{ citation_id: legacyEntry.citation_id, text: "旧范围证据", rank: 1 }],
      coverage: [legacyCoverage, baseCoverage],
    };
  }
  if (hasLegacy) {
    const text = ordinal === 0 ? "旧版本证据行" : ordinal === 2 ? "最终范围证据" : "退出前证据行";
    return {
      text,
      entries: [legacyEntry],
      evidence: [{ citation_id: legacyEntry.citation_id, text, rank: 1 }],
      coverage: [legacyCoverage],
    };
  }
  return {
    text: "基础仓库证据行",
    entries: hasFork ? [baseEntry, forkEntry] : [baseEntry],
    evidence: [
      { citation_id: baseEntry.citation_id, text: "基础仓库证据行", rank: 1 },
      ...(hasFork ? [{ citation_id: forkEntry.citation_id, text: "分支来源证据行 docs/fork.md", rank: 2 }] : []),
    ],
    coverage: hasFork ? [baseCoverage, forkCoverage] : [baseCoverage],
  };
}

async function searchRoutes(page: Page) {
  const state: State = {
    submitted: [], listCalls: [], gates: new Map(), denied: new Set(), completed: new Set(), citationCursors: [],
  };
  const queries = new Map<string, unknown>();
  await session(page, "admin");
  await page.route("**/api/v1/auth/logout", route => route.fulfill({ status: 204 }));
  await page.route("**/api/v1/repositories**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const chunks = path.split("/");
    if (path === "/api/v1/repositories") {
      state.listCalls.push(url.searchParams.get("limit") ?? "");
      return route.fulfill({ json: { items: catalog, next_cursor: null } });
    }
    const repositoryId = chunks[4];
    const overview = views.get(repositoryId);
    if (path === `/api/v1/repositories/${repositoryId}/knowledge` && overview) {
      return route.fulfill({ json: overview });
    }
    const submit = /^\/api\/v1\/repositories\/[^/]+\/knowledge\/queries$/.test(path);
    if (submit && request.method() === "POST") {
      const body = request.postDataJSON() as SelectionBody;
      const ordinal = state.submitted.length;
      state.submitted.push({ body, key: request.headers()["idempotency-key"] ?? null });
      const queryId = QIDS[ordinal];
      const result = resultFor(body, ordinal);
      queries.set(queryId, {
        query_id: queryId, job_id: JIDS[ordinal], status: "succeeded",
        selection: body.selection, question: body.question,
        manifest: { entries: result.entries }, result: { status: "evidence", evidence: result.evidence, coverage: result.coverage },
        error_code: null, created_at: stamp, completed_at: stamp,
      });
      return route.fulfill({ status: 202, json: { query_id: queryId, job_id: JIDS[ordinal] } });
    }
    const poll = path.match(/^\/api\/v1\/repositories\/[^/]+\/knowledge\/queries\/([^/]+)$/);
    if (poll) {
      const queryId = poll[1];
      const gate = state.gates.get(queryId);
      if (gate) await gate;
      state.completed.add(queryId);
      // A scope change aborts the browser request while this gated response is
      // still pending; the backend job keeps running and the late response is
      // discarded, so a canceled fulfill must not fail the test.
      try {
        if (state.denied.has(queryId)) {
          return await route.fulfill({ status: 404, json: { error: { code: "resource_not_found" } } });
        }
        const view = queries.get(queryId);
        return await (view
          ? route.fulfill({ json: view })
          : route.fulfill({ status: 404, json: { error: { code: "resource_not_found" } } }));
      } catch {
        return;
      }
    }
    const citation = path.match(/^\/api\/v1\/repositories\/[^/]+\/knowledge\/queries\/([^/]+)\/citations\/([^/]+)$/);
    if (citation) {
      const cursor = url.searchParams.get("cursor");
      state.citationCursors.push(cursor);
      return route.fulfill({ json: cursor === "preview-cursor" ? { ...legacyPreview, text: "后续区间正文", location: { start_line: 13, end_line: 14, start_byte: 141, end_byte: 180 }, partial: false, next_cursor: null } : legacyPreview });
    }
    return route.fulfill({ status: 404, json: { error: { code: "resource_not_found" } } });
  });
  return state;
}

test("test_source_only_fork_selection_is_explicit", async ({ page }) => {
  const state = await searchRoutes(page);
  await page.goto("/knowledge");
  const scope = page.getByRole("region", { name: "检索范围" });
  await expect(scope).toContainText("team/base-app");
  await expect(scope).toContainText("team/base-app-fork");
  await expect(page.getByRole("combobox", { name: "每页数量", exact: true })).toHaveValue("30");
  await page.getByRole("combobox", { name: "每页数量", exact: true }).selectOption("100");
  await expect.poll(() => state.listCalls.at(-1)).toBe("100");

  await scope.getByRole("checkbox", { name: "选择仓库 team/base-app-fork", exact: true }).check();
  const forkCard = page.getByRole("region", { name: "索引选择 team/base-app-fork", exact: true });
  await expect(forkCard).toContainText(`来源 SHA ${forkSha}`);
  await expect(forkCard).toContainText("仅来源（无索引）");
  await expect(forkCard).toContainText("当前目标尚未建立索引");
  await scope.getByRole("checkbox", { name: "选择仓库 team/base-app-fork", exact: true }).uncheck();

  await scope.getByRole("checkbox", { name: "选择仓库 team/base-app", exact: true }).check();
  const baseCard = page.getByRole("region", { name: "索引选择 team/base-app", exact: true });
  await expect(baseCard).toContainText(`目标 SHA ${targetSha}`);
  await page.getByRole("textbox", { name: "问题", exact: true }).fill("部署流程是什么？");
  await page.getByRole("button", { name: "开始检索" }).click();
  await expect(page.getByRole("region", { name: "检索结果" })).toContainText("基础仓库证据行");
  await expect(page.getByText("分支来源证据行")).toHaveCount(0);
  expect(state.submitted[0].body).toEqual({
    selection: { selected_repository_ids: [baseId], indexes: [{ repository_id: baseId, sha: targetSha, target: "target" }] },
    question: "部署流程是什么？",
  });
  expect(state.submitted[0].key).toBeTruthy();

  await scope.getByRole("checkbox", { name: "选择仓库 team/base-app-fork", exact: true }).check();
  await expect(page.getByRole("region", { name: "检索结果" })).toHaveCount(0);
  await page.getByRole("textbox", { name: "问题", exact: true }).fill("部署流程是什么？");
  await page.getByRole("button", { name: "开始检索" }).click();
  await expect(page.getByRole("region", { name: "检索结果" })).toContainText("分支来源证据行");
  expect(state.submitted[1].body.selection).toEqual({
    selected_repository_ids: [baseId, forkId],
    indexes: [{ repository_id: baseId, sha: targetSha, target: "target" }],
  });
  const coverage = page.getByRole("region", { name: "覆盖范围" });
  await expect(coverage).toContainText("team/base-app-fork");
  await expect(coverage).toContainText("范围不完整");
});

test("test_search_without_cloud_configuration", async ({ page }) => {
  const state = await searchRoutes(page);
  await page.goto("/knowledge");
  await expect(page.getByText("云未配置不会阻止检索")).toBeVisible();
  const scope = page.getByRole("region", { name: "检索范围" });
  await scope.getByRole("checkbox", { name: "选择仓库 team/base-app", exact: true }).check();
  await page.getByRole("textbox", { name: "问题", exact: true }).fill("本地检索");
  await page.getByRole("button", { name: "开始检索" }).click();
  await expect(page.getByRole("region", { name: "检索结果" })).toContainText("基础仓库证据行");
  // Scope to the app region: Next's route announcer is also role="alert".
  await expect(page.getByRole("region", { name: "检索与引用" }).getByRole("alert")).toHaveCount(0);
  expect(state.submitted).toHaveLength(1);
});

test("test_old_result_cannot_replace_new_scope", async ({ page }) => {
  const state = await searchRoutes(page);
  await page.goto("/knowledge");
  const scope = page.getByRole("region", { name: "检索范围" });
  await scope.getByRole("checkbox", { name: "选择仓库 team/legacy-app", exact: true }).check();
  const legacyCard = page.getByRole("region", { name: "索引选择 team/legacy-app", exact: true });
  await expect(legacyCard).toContainText(`目标 SHA ${legacyTargetSha}`);
  await expect(legacyCard).toContainText(`当前 active SHA ${legacyActiveSha}`);
  await expect(legacyCard).toContainText("目标版本索引正在构建");
  await expect(page.getByRole("button", { name: "开始检索" })).toBeDisabled();
  await legacyCard.getByRole("radio", { name: "使用旧 active SHA", exact: true }).check();
  await expect(legacyCard).toContainText(`已明确选择旧 active SHA ${legacyActiveSha}`);
  await page.getByRole("textbox", { name: "问题", exact: true }).fill("旧版本行为是什么？");
  await page.getByRole("button", { name: "开始检索" }).click();

  const result = page.getByRole("region", { name: "检索结果" });
  await expect(result).toContainText("旧版本证据行");
  await expect(result).toContainText("不代表事实概率");
  const coverage = page.getByRole("region", { name: "覆盖范围" });
  await expect(coverage).toContainText("team/legacy-app");
  await expect(coverage).toContainText("已导入 4");
  await expect(coverage).toContainText("跳过 2");
  await expect(coverage).toContainText("范围不完整");
  expect(state.submitted[0].body.selection.indexes).toEqual([
    { repository_id: legacyId, sha: legacyActiveSha, target: "active" },
  ]);

  await result.getByRole("button", { name: "查看引用" }).click();
  const preview = page.getByRole("region", { name: "引用预览" });
  await expect(preview).toContainText("旧版本正文（仅返回区间）");
  await expect(preview).toContainText("仅显示返回区间（第 10–12 行）");
  await expect(preview).toContainText("历史修订（结果生成时的版本）");
  await preview.getByRole("button", { name: "继续读取后续内容" }).click();
  await expect(preview).toContainText("后续区间正文");
  // The App Router mounts in StrictMode, so the first preview read can be
  // repeated before its response lands. Cursor progression is the invariant.
  expect(state.citationCursors[0]).toBeNull();
  expect(state.citationCursors.at(-1)).toBe("preview-cursor");
  expect(state.citationCursors.every(cursor => cursor === null || cursor === "preview-cursor")).toBe(true);

  // Changing the explicit scope discards the old result before a new submit.
  await scope.getByRole("checkbox", { name: "选择仓库 team/base-app", exact: true }).check();
  await expect(page.getByRole("region", { name: "检索结果" })).toHaveCount(0);

  let release!: () => void;
  state.gates.set(QIDS[1], new Promise<void>(resolve => { release = resolve; }));
  await page.getByRole("textbox", { name: "问题", exact: true }).fill("旧范围问题");
  await page.getByRole("button", { name: "开始检索" }).click();
  await expect.poll(() => state.submitted.length).toBe(2);
  await scope.getByRole("checkbox", { name: "选择仓库 team/base-app", exact: true }).uncheck();
  await page.getByRole("textbox", { name: "问题", exact: true }).fill("最终范围问题");
  await page.getByRole("button", { name: "开始检索" }).click();
  await expect(page.getByRole("region", { name: "检索结果" })).toContainText("最终范围证据");
  release();
  await expect.poll(() => state.completed.has(QIDS[1]), { timeout: 5_000 }).toBe(true);
  await expect(page.getByText("旧范围证据")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "检索结果" })).toContainText("最终范围证据");

  // Leaving the page clears the query and citation cache.
  await page.getByRole("link", { name: "概览" }).click();
  await page.getByRole("link", { name: "检索", exact: true }).click();
  await expect(page.getByRole("region", { name: "检索结果" })).toHaveCount(0);

  // Revocation during polling removes the visible result and cached scopes.
  await page.getByRole("region", { name: "检索范围" }).getByRole("checkbox", { name: "选择仓库 team/legacy-app", exact: true }).check();
  await page.getByRole("region", { name: "索引选择 team/legacy-app", exact: true }).getByRole("radio", { name: "使用旧 active SHA", exact: true }).check();
  await page.getByRole("textbox", { name: "问题", exact: true }).fill("撤权检查");
  state.denied.add(QIDS[3]);
  await page.getByRole("button", { name: "开始检索" }).click();
  await expect(page.getByText("权限已撤销或会话已失效")).toBeVisible();
  await expect(page.getByRole("region", { name: "检索结果" })).toHaveCount(0);

  // Logout clears the whole query cache before the login screen renders.
  state.denied.clear();
  await page.getByRole("region", { name: "检索范围" }).getByRole("checkbox", { name: "选择仓库 team/legacy-app", exact: true }).check();
  await page.getByRole("region", { name: "索引选择 team/legacy-app", exact: true }).getByRole("radio", { name: "使用旧 active SHA", exact: true }).check();
  await page.getByRole("textbox", { name: "问题", exact: true }).fill("退出前问题");
  await page.getByRole("button", { name: "开始检索" }).click();
  await expect(page.getByRole("region", { name: "检索结果" })).toContainText("退出前证据行");
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "登录 DevFlow" })).toBeVisible();
});
