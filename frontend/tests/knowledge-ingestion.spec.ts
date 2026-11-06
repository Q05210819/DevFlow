import { expect, test } from "@playwright/test";
import { repository, session } from "./repository-fixtures";

const stamp = "2026-10-27T00:00:00Z";
const sha = "b".repeat(40);
const workspaceId = "55555555-5555-4555-8555-555555555555";
const documentId = "66666666-6666-4666-8666-666666666666";
const revisionId = "77777777-7777-4777-8777-777777777777";

const sync = { repository_id: repository.id, requested_sequence: 1, processed_sequence: 1, generation: 1, status: "completed", streams: [], error_code: null, next_due_at: null, retry_at: null };
const detail = { ...repository, sync };
const corpus = { import_id: "88888888-8888-4888-8888-888888888888", kind: "workspace", status: "ready_partial", target_generation: 2, included: 3, skipped: 1, unsupported: 1, over_limit: 0, unscanned: false, complete: true, scan_reason: null, processed_chunks: 12, created_at: stamp, deadline: stamp };
const document = { document_id: documentId, kind: "workspace", path: "docs/guide.md", filename: null, deleted: false, published_revisions: 1, latest_published_revision_id: revisionId, latest_published_at: stamp };
const knowledge = {
  repository_id: repository.id, target_generation: 2, target_workspace_id: workspaceId, target_sha: sha,
  path_rule_version: "knowledge-paths-v1", document_paths: ["README.md", "docs"], code_paths: [],
  corpus, needs_reimport: false, index_status: "not_indexed",
  documents: { items: [document], next_cursor: null },
  limits: { max_files: 2000, max_scan_bytes: 33554432, max_file_bytes: 1048576 },
};
const version = { id: workspaceId, repository_id: repository.id, source_repository_id: repository.id, commit_sha: sha, tree_oid: "c".repeat(40), source: { selection: { kind: "branch", name: "main" }, ref: "refs/heads/main", source_repository_id: 101, transfer_repository_id: 101, base_sha: null, head_sha: null }, status: "ready", error: null, created_at: stamp, last_used_at: stamp };
const paths = {
  items: [
    { path: "README.md", mode: "100644", size: 120, outcome: "selected", reason: null },
    { path: "docs/guide.md", mode: "100644", size: 900, outcome: "selected", reason: null },
    { path: "docs/logo.png", mode: "100644", size: 4096, outcome: "unsupported", reason: "unsupported" },
    { path: "docs/old.md", mode: "100644", size: 12, outcome: "excluded", reason: "source_deleted" },
  ],
  next_cursor: null, exhausted: false, truncated: true,
};
const revision = { revision_id: revisionId, revision_hash: "d".repeat(64), published_at: stamp, is_current: true, availability: "available" };
const preview = { identity: { repository_id: repository.id, source_repository_id: repository.id, dependency_repository_ids: [repository.id], document_id: documentId, revision_hash: "d".repeat(64), workspace_id: workspaceId, sha, path: "docs/guide.md" }, text: "# 使用说明\n\n正文内容", location: { start_line: 1, end_line: 3, start_byte: 0, end_byte: 24 }, is_current: true, partial: false, next_cursor: null, reason: null };

async function knowledgeRoutes(page: import("@playwright/test").Page, options: { admitted?: unknown[] } = {}) {
  await session(page, "admin");
  await page.route("**/api/v1/repositories**", route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/repositories") return route.fulfill({ json: { items: [{ ...repository, sync }], next_cursor: null } });
    if (path.endsWith("/knowledge/paths")) return route.fulfill({ json: paths });
    if (path.endsWith("/knowledge/documents/" + documentId)) return route.fulfill({ json: { ...document, repository_id: repository.id, revisions: [revision], revisions_next_cursor: null, preview } });
    if (path.endsWith("/knowledge")) return route.fulfill({ json: knowledge });
    if (path.endsWith("/workspaces/" + workspaceId)) return route.fulfill({ json: version });
    if (path.endsWith("/knowledge/imports")) { options.admitted?.push(route.request().postDataJSON()); return route.fulfill({ status: 202, json: { import_id: corpus.import_id, job_id: "99999999-9999-4999-8999-999999999999", target_generation: 3 } }); }
    if (path.endsWith("/knowledge/uploads")) return route.fulfill({ status: 201, json: { staging_id: "aaaa1111-1111-4111-8111-111111111111", expires_at: stamp } });
    if (path.endsWith("/sync")) return route.fulfill({ json: sync });
    return route.fulfill({ json: detail });
  });
}

test("knowledge shows canonical coverage, honest index state and preview scope", async ({ page }) => {
  await knowledgeRoutes(page);
  await page.goto(`/repositories/${repository.id}/knowledge`);
  const region = page.getByRole("region", { name: "语料状态" });
  await expect(region).toContainText("部分就绪");
  await expect(region).toContainText("已导入 3");
  await expect(region).toContainText("尚未建立检索索引");
  await expect(page.getByRole("region", { name: "来源文档" })).toContainText("docs/guide.md");
  await page.getByRole("button", { name: "docs/guide.md" }).click();
  const detailRegion = page.getByRole("region", { name: "文档详情" });
  await expect(detailRegion).toContainText("正文内容");
  await expect(detailRegion).toContainText("当前语料修订");
});

test("workspace import confirms the resolved SHA and real path outcomes before submitting", async ({ page }) => {
  const admitted: unknown[] = [];
  await knowledgeRoutes(page, { admitted });
  await page.goto(`/repositories/${repository.id}/knowledge`);
  const region = page.getByRole("region", { name: "导入固定版本" });
  await region.getByLabel("Workspace 版本 ID").fill(workspaceId);
  await expect(region).toContainText(sha);
  await expect(region).toContainText("目录已被截断");
  await expect(region).toContainText("将导入");
  await expect(region).toContainText("来源文档已删除");
  await expect(region).toContainText("不支持");
  const submit = region.getByRole("button", { name: "确认并开始导入" });
  await expect(submit).toBeDisabled();
  await region.getByLabel("我已核对完整 SHA、来源仓库与路径范围").check();
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(region.getByRole("status").filter({ hasText: "已提交导入" })).toBeVisible();
  expect(admitted).toEqual([{ kind: "workspace", repository_id: repository.id, workspace_id: workspaceId, document_paths: ["README", "README.md", "README.txt", "README.rst", "docs"], code_paths: [] }]);
});

test("workspace import requires a non-empty path selection", async ({ page }) => {
  await knowledgeRoutes(page);
  await page.goto(`/repositories/${repository.id}/knowledge`);
  const region = page.getByRole("region", { name: "导入固定版本" });
  await region.getByLabel("Workspace 版本 ID").fill(workspaceId);
  await region.getByLabel("文档路径（逗号分隔）").fill("");
  await region.getByLabel("代码目录（逗号分隔，可留空）").fill("");
  await expect(region.getByText("至少选择一个文档路径或代码目录")).toBeVisible();
  await region.getByLabel("我已核对完整 SHA、来源仓库与路径范围").check();
  await expect(region.getByRole("button", { name: "确认并开始导入" })).toBeDisabled();
});

test("uploading states the shared scope and reuses one stage across admission retries", async ({ page }) => {
  await knowledgeRoutes(page);
  let stages = 0;
  let admissions = 0;
  await page.route("**/knowledge/uploads**", route => { stages += 1; return route.fulfill({ status: 201, json: { staging_id: "aaaa1111-1111-4111-8111-111111111111", expires_at: stamp } }); });
  await page.route("**/knowledge/imports", route => {
    admissions += 1;
    return admissions === 1 ? route.abort() : route.fulfill({ status: 202, json: { import_id: corpus.import_id, job_id: "99999999-9999-4999-8999-999999999999", target_generation: 3 } });
  });
  await page.goto(`/repositories/${repository.id}/knowledge`);
  const region = page.getByRole("region", { name: "上传文档" });
  await expect(region).toContainText("将供该仓库已获准成员");
  await region.getByLabel("选择文件").setInputFiles({ name: "note.md", mimeType: "text/markdown", buffer: Buffer.from("# note\n") });
  await region.getByRole("button", { name: "上传并提交导入" }).click();
  await expect(region.getByRole("alert")).toBeVisible();
  await region.getByRole("button", { name: "上传并提交导入" }).click();
  await expect(region.getByRole("status").filter({ hasText: "已提交导入" })).toBeVisible();
  expect(stages).toBe(1);
  expect(admissions).toBe(2);
});

test("switching repositories never restores the previous knowledge body", async ({ page }) => {
  await knowledgeRoutes(page);
  await page.goto(`/repositories/${repository.id}/knowledge`);
  await page.getByRole("button", { name: "docs/guide.md" }).click();
  await expect(page.getByRole("region", { name: "文档详情" })).toContainText("正文内容");
  const other = "12121212-1212-4212-8212-121212121212";
  await page.route(`**/api/v1/repositories/${other}**`, route => {
    const path = new URL(route.request().url()).pathname;
    if (path === `/api/v1/repositories/${other}`) return route.fulfill({ json: { ...detail, id: other, full_name: "team/other" } });
    if (path.endsWith("/sync")) return route.fulfill({ json: { ...sync, repository_id: other } });
    if (path.endsWith("/knowledge")) return route.fulfill({ json: { ...knowledge, repository_id: other, corpus: null, documents: { items: [], next_cursor: null }, target_sha: null, target_workspace_id: null, target_generation: 0 } });
    return route.fulfill({ status: 404, json: { error: { code: "resource_not_found" } } });
  });
  await page.goto(`/repositories/${other}/knowledge`);
  await expect(page.getByRole("region", { name: "来源文档" })).toContainText("此页没有来源文档");
  await expect(page.getByText("正文内容")).toHaveCount(0);
});

test("a successful mutation returns later document pages to page one", async ({ page }) => {
  let mutated = false;
  const cursors: (string | null)[] = [];
  await session(page, "admin");
  await page.route("**/api/v1/repositories**", route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/repositories") return route.fulfill({ json: { items: [{ ...repository, sync }], next_cursor: null } });
    if (path.endsWith("/knowledge/uploads")) return route.fulfill({ status: 201, json: { staging_id: "aaaa1111-1111-4111-8111-111111111111", expires_at: stamp } });
    if (path.endsWith("/knowledge/imports")) { mutated = true; return route.fulfill({ status: 202, json: { import_id: corpus.import_id, job_id: "99999999-9999-4999-8999-999999999999", target_generation: 3 } }); }
    if (path.endsWith("/knowledge")) {
      const cursor = url.searchParams.get("cursor");
      cursors.push(cursor);
      if (mutated && cursor) return route.fulfill({ status: 400, json: { error: { code: "invalid_cursor", message: "Expired cursor" } } });
      return route.fulfill({ json: { ...knowledge, documents: { items: [document], next_cursor: cursor ? null : "page-2" } } });
    }
    if (path.endsWith("/sync")) return route.fulfill({ json: sync });
    return route.fulfill({ json: detail });
  });
  await page.goto(`/repositories/${repository.id}/knowledge`);
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByText("第 2 页")).toBeVisible();
  const region = page.getByRole("region", { name: "上传文档" });
  await region.getByLabel("选择文件").setInputFiles({ name: "note.md", mimeType: "text/markdown", buffer: Buffer.from("# note\n") });
  await region.getByRole("button", { name: "上传并提交导入" }).click();
  await expect(region.getByRole("status")).toContainText("已提交导入");
  await expect(page.getByText("第 1 页")).toBeVisible();
  expect(cursors.at(-1)).toBeNull();
  await expect(page.getByText("Expired cursor")).toHaveCount(0);
});

test("a pending corpus status polls without manual refresh", async ({ page }) => {
  let calls = 0;
  let ready = false;
  await session(page, "admin");
  await page.route("**/api/v1/repositories**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/repositories") return route.fulfill({ json: { items: [{ ...repository, sync }], next_cursor: null } });
    if (path.endsWith("/knowledge")) {
      calls += 1;
      return route.fulfill({ json: { ...knowledge, corpus: { ...corpus, status: ready ? "ready" : "building" } } });
    }
    if (path.endsWith("/sync")) return route.fulfill({ json: sync });
    return route.fulfill({ json: detail });
  });
  await page.goto(`/repositories/${repository.id}/knowledge`);
  const region = page.getByRole("region", { name: "语料状态" });
  await expect(region).toContainText("正在构建");
  ready = true;
  await expect.poll(() => calls, { timeout: 10_000 }).toBeGreaterThan(1);
  await expect(region).toContainText("已就绪");
});

test("deleting a document clears its cached preview", async ({ page }) => {
  let deleted = false;
  await session(page, "admin");
  await page.route("**/api/v1/repositories**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/repositories") return route.fulfill({ json: { items: [{ ...repository, sync }], next_cursor: null } });
    if (path.endsWith("/knowledge/documents/" + documentId + "/deletions")) { deleted = true; return route.fulfill({ status: 202, json: { import_id: corpus.import_id, job_id: "99999999-9999-4999-8999-999999999999", target_generation: 3 } }); }
    if (path.endsWith("/knowledge/documents/" + documentId)) {
      return route.fulfill({ json: deleted ? { ...document, repository_id: repository.id, deleted: true, revisions: [], revisions_next_cursor: null, preview: null } : { ...document, repository_id: repository.id, revisions: [revision], revisions_next_cursor: null, preview } });
    }
    if (path.endsWith("/knowledge")) return route.fulfill({ json: { ...knowledge, documents: { items: [{ ...document, deleted }], next_cursor: null } } });
    if (path.endsWith("/sync")) return route.fulfill({ json: sync });
    return route.fulfill({ json: detail });
  });
  await page.goto(`/repositories/${repository.id}/knowledge`);
  await page.getByRole("button", { name: "docs/guide.md" }).click();
  await expect(page.getByRole("region", { name: "文档详情" })).toContainText("正文内容");
  await page.getByRole("button", { name: "删除此来源文档" }).click();
  await page.getByRole("button", { name: "确认删除" }).click();
  await expect(page.getByText("此来源文档已逻辑删除")).toBeVisible();
  await expect(page.getByText("正文内容")).toHaveCount(0);
});

test("in-app repository navigation ignores a delayed old-repository response", async ({ page }) => {
  const other = "12121212-1212-4212-8212-121212121212";
  let slow = false;
  let delivered = false;
  const otherDetail = { ...detail, id: other, full_name: "team/other" };
  const otherSync = { ...sync, repository_id: other };
  const otherKnowledge = { ...knowledge, repository_id: other, corpus: null, documents: { items: [], next_cursor: null }, target_sha: null, target_workspace_id: null, target_generation: 0 };
  await knowledgeRoutes(page);
  await page.route("**/api/v1/repositories**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/repositories") return route.fulfill({ json: { items: [{ ...repository, sync }, { ...repository, id: other, full_name: "team/other", sync: otherSync }], next_cursor: null } });
    if (path.startsWith(`/api/v1/repositories/${other}`)) {
      if (path === `/api/v1/repositories/${other}`) return route.fulfill({ json: otherDetail });
      if (path.endsWith("/sync")) return route.fulfill({ json: otherSync });
      if (path.endsWith("/knowledge")) return route.fulfill({ json: otherKnowledge });
      return route.fallback();
    }
    if (slow && path === `/api/v1/repositories/${repository.id}/knowledge`) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      delivered = true;
    }
    return route.fallback();
  });
  await page.goto(`/repositories/${repository.id}/knowledge`);
  await page.getByRole("button", { name: "docs/guide.md" }).click();
  await expect(page.getByRole("region", { name: "文档详情" })).toContainText("正文内容");
  slow = true;
  await page.getByRole("button", { name: "刷新知识状态" }).click();
  await page.getByRole("link", { name: "仓库" }).click();
  await page.getByRole("link", { name: "查看 team/other" }).click();
  await page.getByRole("link", { name: "知识库" }).click();
  await expect(page.getByRole("region", { name: "来源文档" })).toContainText("此页没有来源文档");
  await expect.poll(() => delivered, { timeout: 5_000 }).toBe(true);
  await expect(page.getByText("正文内容")).toHaveCount(0);
});
