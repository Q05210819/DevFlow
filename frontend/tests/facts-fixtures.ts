import type { Page, Route } from "@playwright/test";
import { repository, session } from "./repository-fixtures";

export { repository };
export const stamp = "2026-10-14T00:00:00Z";
export const base = "a".repeat(40), head = "b".repeat(40);
export const coverage = { stream: "issues", status: "partial", start: stamp, end: stamp, completed_watermark: null, last_success_at: stamp, error_code: "not_collected" };
export const sync = { repository_id: repository.id, requested_sequence: 2, processed_sequence: 1, generation: 1, status: "completed", streams: ["issues", "pulls", "actions"].map(stream => ({ stream, status: "partial", coverage: { start: stamp, end: stamp, completed_watermark: null }, last_success_at: stamp, remaining: null, error_code: "not_collected" })), error_code: "not_collected", next_due_at: null, retry_at: null };
export const detail = { ...repository, sync };
export const issue = { id: "33333333-3333-4333-8333-333333333333", kind: "issue", source_id: 501, number: 17, version_key: "", source_updated_at: stamp, observed_at: stamp, source_url: "https://github.com/team/private-app/issues/17", unavailable: false, payload: { title: "private issue", body: '<img src=x onerror="window.unsafe=1">\nsecond line', state: "open", labels: ["bug"], assignees: [], author: { id: 42, login: "colleague" }, created_at: stamp, closed_at: null, normalization_reasons: [] } };
export const pull = { ...issue, kind: "pull", source_id: 502, number: 18, source_url: null, payload: { ...issue.payload, title: "private pull", issue_id: 503, base_repository_id: 101, head_repository_id: 102, base_ref: "main", head_ref: "feature", base_sha: base, head_sha: head, draft: false, merged: false, merge_commit_sha: null, merged_at: null } };
export const run = { ...issue, kind: "ci_run", source_id: 700, number: null, version_key: "2", source_url: null, payload: { normalization_reasons: [], attempt: 2, workflow_id: 30, workflow_path: ".github/workflows/test.yml", name: "CI", event: "push", branch: "main", head_sha: head, status: "completed", conclusion: "failure", created_at: stamp, updated_at: stamp, run_started_at: stamp } };
export const collection = { collection: "comments", version_key: "", complete: false, reason: "not_collected", observed_at: null };
export const snapshot = { id: "44444444-4444-4444-8444-444444444444", generation: 2, rule_version: "health-v1", generated_at: stamp, thresholds: { stale_days: 7, activity_short_days: 7, activity_long_days: 30 }, counts: { basis: "observed_persisted_population", open_issues: 1, open_pulls: null, unassigned_issues: 1, draft_pulls: null, stale_issues: 0, stale_pulls: null, activity_basis: "observed_record_creation_update_not_event_history", activity: ["issues", "pulls", "actions"].flatMap(stream => [7, 30].map(days => ({ stream, days, requested_start: stamp, requested_end: stamp, actual_start: null, actual_end: null, complete: false, created_records: null, updated_records: null }))), actions_categories: { failure: 1 } }, coverage: [coverage], manifest: { rule_version: "health-v1", frozen_at: stamp, sync_generation: 1, requested_sequence: 2, processed_sequence: 1, populations: [{ kind: "issue", count: 1, observed_from: stamp, observed_until: stamp }], coverage: [coverage], pulls: [{ source_id: 502, number: 18, base_sha: base, head_sha: head, source_url: null, observed_at: stamp }], pulls_selection: { selection_order: "number_asc", total_candidates: 1, returned_count: 1, truncated: false }, actions: [{ source_id: 700, attempt: 2, workflow_id: 30, head_sha: head, branch: "main", status: "completed", conclusion: "failure", source_url: null, source_updated_at: stamp, observed_at: stamp, default_branch_latest: true, pr_numbers: [18] }], actions_selection: { selection_order: "updated_desc", total_candidates: 1, returned_count: 1, truncated: false }, unlisted_evidence: "unknown" }, workspace: "unavailable" };
export const health = { snapshot, refresh_status: "completed", error_code: null, retry_at: null };
export function fail(route: Route, code = "service_unavailable", status = 503) { return route.fulfill({ status, json: { error: { code } } }); }
export async function facts(page: Page, role = "member") {
  await session(page, role);
  await page.route("**/api/v1/repositories**", route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/repositories") return route.fulfill({ json: { items: [{ ...repository, sync }], next_cursor: null } });
    if (path.endsWith("/health")) return route.fulfill({ json: health });
    if (path.endsWith("/sync")) return route.fulfill({ json: sync });
    if (path.endsWith(repository.id)) return route.fulfill({ json: detail });
    const stream = path.includes("/pulls") ? "pulls" : path.includes("/actions/") ? "actions" : "issues";
    const item = stream === "pulls" ? pull : stream === "actions" ? run : issue;
    const cov = { ...coverage, stream };
    if (url.searchParams.has("collection")) return route.fulfill({ json: { items: [], next_cursor: null, coverage: cov, collection: { ...collection, collection: url.searchParams.get("collection") } } });
    if (/\/(17|18|attempts\/2)$/.test(path)) return route.fulfill({ json: { item, collections: [], coverage: cov } });
    return route.fulfill({ json: { items: [item], next_cursor: null, coverage: cov, collection: null } });
  });
}
