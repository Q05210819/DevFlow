import { expect, test, type Page } from "@playwright/test";
import { repository, session } from "./repository-fixtures";

const stamp = "2026-11-05T00:00:00Z";
const baseId = repository.id;
const sourceId = "22222222-2222-4222-8222-222222222222";
const queryId = "30000000-3000-4000-8000-000000000001";
const jobId = "40000000-4000-4000-8000-000000000001";
const entryId = "c0ffee0000000001";
const unknownId = "c0ffee0000000002";
const targetSha = "a".repeat(40);
const sourceSha = "b".repeat(40);
const baseWorkspace = "55555555-5555-4555-8555-555555555555";
const sourceWorkspace = "66666666-6666-4666-8666-666666666666";
const baseQueryPath = `/api/v1/repositories/${baseId}/knowledge/queries`;
const citationPath = `${baseQueryPath}/${queryId}/citations/${entryId}`;

type IndexChoice = { repository_id: string; sha: string; target: "target" | "active" };
type Body = { selection: { selected_repository_ids: string[]; indexes: IndexChoice[] }; question: string };
type State = {
  submitted: Body[];
  citations: string[];
  denied: Set<string>;
  versionChanged: boolean;
};

const sync = (id: string) => ({
  repository_id: id, requested_sequence: 1, processed_sequence: 1, generation: 1, status: "completed",
  streams: [], error_code: null, next_due_at: null, retry_at: null,
});
const catalog = [
  { ...repository, full_name: "team/base-app", sync: sync(baseId) },
  { ...repository, id: sourceId, github_repository_id: 102, full_name: "team/source-app", sync: sync(sourceId) },
];

function knowledge(id: string, status: "ready" | "not_indexed", sha: string, workspace: string) {
  return {
    repository_id: id,
    target_generation: 1,
    target_workspace_id: workspace,
    target_sha: sha,
    path_rule_version: "knowledge-paths-v1",
    document_paths: ["docs/base.md"],
    code_paths: [],
    corpus: {
      import_id: `${id.slice(0, 8)}-8888-4888-8888-888888888888`, kind: "workspace", status: "ready",
      target_generation: 1, included: 1, skipped: 0, unsupported: 0, over_limit: 0,
      unscanned: false, complete: true, scan_reason: null, processed_chunks: 2, created_at: stamp, deadline: stamp,
    },
    needs_reimport: false,
    index_status: status,
    index_reason: status === "ready" ? null : "target_not_indexed",
    active_index_generation: null,
    active_index_sha: null,
    documents: { items: [], next_cursor: null },
    limits: { max_files: 2000, max_scan_bytes: 33554432, max_file_bytes: 1048576 },
  };
}

const views = new Map([
  [baseId, knowledge(baseId, "ready", targetSha, baseWorkspace)],
  [sourceId, knowledge(sourceId, "not_indexed", sourceSha, sourceWorkspace)],
]);

const entry = {
  citation_id: entryId,
  identity: {
    repository_id: baseId, source_repository_id: baseId, dependency_repository_ids: [baseId],
    document_id: "77777777-7777-4777-8777-777777777777", revision_hash: "1".repeat(64),
    workspace_id: baseWorkspace, sha: targetSha, path: "docs/base.md",
  },
  location: { start_line: 12, end_line: 14, start_byte: 120, end_byte: 180 },
  url: `https://github.com/team/private-app/blob/${targetSha}/docs/base.md#L12-L14`,
  content_hash: "2".repeat(64),
  rank: 1,
};

const preview = {
  identity: entry.identity,
  text: "服务端返回的引用正文",
  location: entry.location,
  is_current: false,
  partial: true,
  next_cursor: null,
  reason: null,
};

const baseCoverage = {
  repository_id: baseId, sha: targetSha, target_generation: 1,
  included: 1, skipped: 0, unsupported: 0, over_limit: 0, unscanned: false, complete: true,
};

function succeededView(body: Body) {
  return {
    query_id: queryId, job_id: jobId, status: "succeeded",
    selection: body.selection, question: body.question,
    manifest: { entries: [entry] },
    result: {
      status: "evidence",
      evidence: [
        { citation_id: entryId, text: "服务端返回的证据正文", rank: 1 },
        { citation_id: unknownId, text: "未知引用正文", rank: 2 },
      ],
      coverage: [baseCoverage],
    },
    error_code: null, created_at: stamp, completed_at: stamp,
  };
}

function failedView(body: Body) {
  return {
    query_id: queryId, job_id: jobId, status: "failed",
    selection: body.selection, question: body.question,
    manifest: null, result: null,
    error_code: "version_changed", created_at: stamp, completed_at: stamp,
  };
}

async function routes(page: Page) {
  const state: State = { submitted: [], citations: [], denied: new Set(), versionChanged: false };
  const queries = new Map<string, unknown>();
  await session(page, "admin");
  await page.route("**/api/v1/auth/logout", route => route.fulfill({ status: 204 }));
  await page.route("**/api/v1/repositories**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const chunks = path.split("/");
    if (path === "/api/v1/repositories") {
      return route.fulfill({ json: { items: catalog, next_cursor: null } });
    }
    const repositoryId = chunks[4];
    const overview = views.get(repositoryId);
    if (path === `/api/v1/repositories/${repositoryId}/knowledge` && overview) {
      return route.fulfill({ json: overview });
    }
    const submit = /^\/api\/v1\/repositories\/[^/]+\/knowledge\/queries$/.test(path);
    if (submit && request.method() === "POST") {
      const body = request.postDataJSON() as Body;
      state.submitted.push(body);
      queries.set(queryId, state.versionChanged ? failedView(body) : succeededView(body));
      return route.fulfill({ status: 202, json: { query_id: queryId, job_id: jobId } });
    }
    const poll = path.match(/^\/api\/v1\/repositories\/[^/]+\/knowledge\/queries\/([^/]+)$/);
    if (poll) {
      const view = queries.get(poll[1]);
      return route.fulfill(view
        ? { json: view }
        : { status: 404, json: { error: { code: "resource_not_found" } } });
    }
    const citation = path.match(/^\/api\/v1\/repositories\/[^/]+\/knowledge\/queries\/([^/]+)\/citations\/([^/]+)$/);
    if (citation) {
      state.citations.push(path);
      if (state.denied.has(citation[2])) {
        return route.fulfill({ status: 404, json: { error: { code: "resource_not_found" } } });
      }
      return route.fulfill({ json: preview });
    }
    return route.fulfill({ status: 404, json: { error: { code: "resource_not_found" } } });
  });
  return state;
}

async function ask(page: Page, names: string[], question: string) {
  await page.goto("/knowledge");
  const scope = page.getByRole("region", { name: "检索范围" });
  for (const name of names) {
    await scope.getByRole("checkbox", { name: `选择仓库 ${name}`, exact: true }).check();
  }
  await page.getByRole("textbox", { name: "问题", exact: true }).fill(question);
  await page.getByRole("button", { name: "开始检索" }).click();
}

test("test_citation_preview_uses_manifest_identity_and_location", async ({ page }) => {
  const state = await routes(page);
  await ask(page, ["team/base-app"], "部署流程是什么？");
  const result = page.getByRole("region", { name: "检索结果" });
  await expect(result).toContainText("服务端返回的证据正文");
  const items = result.locator("ol.fact-list > li");
  await expect(items).toHaveCount(2);
  // Only evidence backed by a manifest entry offers a preview. The second
  // citation ID is not in the manifest and cannot be opened.
  await expect(items.nth(1)).toContainText("未知引用正文");
  await expect(items.nth(1).getByRole("button", { name: "查看引用" })).toHaveCount(0);

  await items.nth(0).getByRole("button", { name: "查看引用" }).click();
  const previewRegion = page.getByRole("region", { name: "引用预览" });
  await expect(previewRegion).toContainText("docs/base.md");
  await expect(previewRegion).toContainText(targetSha);
  await expect(previewRegion).toContainText("仅显示返回区间（第 12–14 行）");
  await expect(previewRegion).toContainText("服务端返回的引用正文");
  await expect(previewRegion).not.toContainText("未知引用正文");
  expect(state.citations.length).toBeGreaterThan(0);
  expect(state.citations.every(path => path === citationPath)).toBe(true);
  expect(state.submitted[0].selection).toEqual({
    selected_repository_ids: [baseId],
    indexes: [{ repository_id: baseId, sha: targetSha, target: "target" }],
  });
});

test("test_revoked_citation_preview_renders_no_body", async ({ page }) => {
  const state = await routes(page);
  state.denied.add(entryId);
  await ask(page, ["team/base-app"], "撤权后的引用预览");
  const result = page.getByRole("region", { name: "检索结果" });
  await result.getByRole("button", { name: "查看引用" }).click();
  const previewRegion = page.getByRole("region", { name: "引用预览" });
  await expect(previewRegion).toContainText("服务暂时不可用，请稍后重试。");
  await expect(previewRegion).not.toContainText("服务端返回的引用正文");
  await expect(result).toContainText("服务端返回的证据正文");
});

test("test_version_change_is_reported_without_stale_evidence", async ({ page }) => {
  const state = await routes(page);
  state.versionChanged = true;
  await ask(page, ["team/base-app", "team/source-app"], "切换语料版本后的问题");
  const sourceCard = page.getByRole("region", { name: "索引选择 team/source-app", exact: true });
  await expect(sourceCard).toContainText("仅来源（无索引）");
  await expect(page.getByRole("region", { name: "检索进度" }))
    .toContainText("检索失败 · 语料版本已变化，请重新选择检索范围。");
  await expect(page.getByRole("region", { name: "检索结果" })).toHaveCount(0);
  expect(state.submitted[0].selection).toEqual({
    selected_repository_ids: [baseId, sourceId],
    indexes: [{ repository_id: baseId, sha: targetSha, target: "target" }],
  });
});
