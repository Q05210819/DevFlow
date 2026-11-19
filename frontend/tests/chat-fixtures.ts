import type { Page } from "@playwright/test";
import { repository, session } from "./repository-fixtures";

export const stamp = "2026-11-06T00:00:00Z";
export const baseId = repository.id;
export const forkId = "22222222-2222-4222-8222-222222222222";
export const conversationId = "33333333-3333-4333-8333-333333333333";
export const secondConversationId = "34343434-3434-4434-8434-343434343434";
export const runId = "44444444-4444-4444-8444-444444444444";
export const jobId = "55555555-5555-4555-8555-555555555555";
export const cleanupJobId = "56565656-5656-4656-8656-565656565656";
export const userMessageId = "66666666-6666-4666-8666-666666666666";
export const answerMessageId = "77777777-7777-4777-8777-777777777777";
export const citationId = "c0ffee0000000001";
export const targetSha = "a".repeat(40);
export const activeSha = "b".repeat(40);

type IndexChoice = { repository_id: string; sha: string; target: "target" | "active" };
export type SelectionBody = {
  selection: { selected_repository_ids: string[]; indexes: IndexChoice[] };
  question: string;
};
export type JobState = {
  id: string;
  repository_id: string;
  requested_by: string;
  status: "queued" | "running" | "retry_wait" | "succeeded" | "failed";
  attempt: number;
  max_attempts: number;
  result: null;
  error_code: string | null;
  last_sequence: number;
  created_at: string;
  updated_at: string;
};

const sync = (id: string) => ({
  repository_id: id, requested_sequence: 1, processed_sequence: 1, generation: 1, status: "completed",
  streams: [], error_code: null, next_due_at: null, retry_at: null,
});

const catalog = [
  { ...repository, full_name: "team/base-app", sync: sync(baseId) },
  { ...repository, id: forkId, github_repository_id: 102, full_name: "team/fork-app", sync: sync(forkId) },
];

export function knowledge(id: string, options: {
  status: "ready" | "not_indexed"; target: string | null; active: string | null;
}) {
  return {
    repository_id: id,
    target_generation: 2,
    target_workspace_id: "88888888-8888-4888-8888-888888888888",
    target_sha: options.target,
    path_rule_version: "knowledge-paths-v1",
    document_paths: ["docs/base.md"],
    code_paths: [],
    corpus: {
      import_id: `${id.slice(0, 8)}-9999-4999-8999-999999999999`, kind: "workspace", status: "ready",
      target_generation: 2, included: 2, skipped: 0, unsupported: 0, over_limit: 0,
      unscanned: false, complete: true, scan_reason: null, processed_chunks: 5, created_at: stamp, deadline: stamp,
    },
    needs_reimport: false,
    index_status: options.status,
    index_reason: options.status === "ready" ? null : "target_not_indexed",
    active_index_generation: options.active ? 1 : null,
    active_index_sha: options.active,
    documents: { items: [], next_cursor: null },
    limits: { max_files: 2000, max_scan_bytes: 33554432, max_file_bytes: 1048576 },
  };
}

export const views = new Map([
  [baseId, knowledge(baseId, { status: "ready", target: targetSha, active: activeSha })],
  [forkId, knowledge(forkId, { status: "ready", target: activeSha, active: null })],
]);

export const citation = {
  citation_id: citationId,
  url: `https://github.com/team/private-app/blob/${targetSha}/docs/base.md#L12-L14`,
  repository_id: baseId,
  source_repository_id: baseId,
  path: "docs/base.md",
  sha: targetSha,
  start_line: 12,
  end_line: 14,
};

export const preview = {
  identity: {
    repository_id: baseId, source_repository_id: baseId, dependency_repository_ids: [baseId],
    document_id: "99999999-9999-4999-8999-999999999999", revision_hash: "1".repeat(64),
    workspace_id: "88888888-8888-4888-8888-888888888888", sha: targetSha, path: "docs/base.md",
  },
  text: "服务端返回的引用正文",
  location: { start_line: 12, end_line: 14, start_byte: 120, end_byte: 180 },
  is_current: true,
  partial: false,
  next_cursor: null,
  reason: null,
};

export function jobView(status: JobState["status"], errorCode: string | null = null, id = jobId): JobState {
  return {
    id, repository_id: baseId, requested_by: "member-id", status,
    attempt: status === "queued" ? 0 : 1, max_attempts: 3, result: null,
    error_code: errorCode,
    last_sequence: status === "queued" ? 1 : status === "succeeded" ? 3 : 2,
    created_at: stamp, updated_at: stamp,
  };
}

export function conversation(id = conversationId, repositories = [baseId]) {
  return {
    id, selected_repository_ids: repositories, created_at: stamp,
    active_run: null, latest_run: { run_id: runId, job_id: jobId, status: "succeeded" as const },
  };
}

export function history() {
  return [
    {
      id: answerMessageId, role: "assistant", body: "上一轮的结论，引用了服务端证据。",
      ordinal: 1, created_at: stamp, citations: [citation],
    },
    {
      id: userMessageId, role: "user", body: "上一轮的问题", ordinal: 0, created_at: stamp, citations: [],
    },
  ];
}

export type ChatState = {
  conversations: ReturnType<typeof conversation>[];
  messages: Map<string, ReturnType<typeof history>>;
  jobs: Map<string, JobState>;
  creates: Array<{ body: { selected_repository_ids: string[] }; key: string | null }>;
  submitted: Array<{ conversationId: string; body: SelectionBody; key: string | null }>;
  cancels: string[];
  deletions: string[];
  citations: string[];
  denied: Set<string>;
  gate: { promise: Promise<void>; release: () => void } | null;
};

export function state(): ChatState {
  const messages = new Map<string, ReturnType<typeof history>>();
  messages.set(conversationId, history());
  const jobs = new Map<string, JobState>();
  jobs.set(jobId, jobView("succeeded"));
  return {
    conversations: [conversation()],
    messages, jobs, creates: [], submitted: [], cancels: [], deletions: [], citations: [],
    denied: new Set(), gate: null,
  };
}

function notFound(code = "resource_not_found") {
  return { status: 404, json: { error: { code } } };
}

export async function chatRoutes(page: Page, current = state()) {
  await session(page, "admin");
  await page.route("**/api/v1/auth/logout", route => route.fulfill({ status: 204 }));
  await page.route("**/api/v1/repositories**", route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/repositories") {
      return route.fulfill({ json: { items: catalog, next_cursor: null } });
    }
    const match = path.match(/^\/api\/v1\/repositories\/([^/]+)\/knowledge$/);
    const overview = match ? views.get(match[1]) : undefined;
    return overview
      ? route.fulfill({ json: overview })
      : route.fulfill(notFound());
  });
  await page.route("**/api/v1/conversations**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (path === "/api/v1/conversations") {
      if (method === "POST") {
        const body = request.postDataJSON() as { selected_repository_ids: string[] };
        current.creates.push({ body, key: request.headers()["idempotency-key"] ?? null });
        const created = conversation(
          current.creates.length === 1 ? conversationId : secondConversationId,
          body.selected_repository_ids,
        );
        current.conversations.push(created);
        current.messages.set(created.id, []);
        return route.fulfill({ status: 201, json: {
          id: created.id, selected_repository_ids: created.selected_repository_ids,
          archived: false, created_at: created.created_at,
        } });
      }
      return route.fulfill({ json: { conversations: current.conversations } });
    }
    const messages = path.match(/^\/api\/v1\/conversations\/([^/]+)\/messages$/);
    if (messages) {
      const id = messages[1];
      if (current.denied.has(id)) return route.fulfill(notFound());
      if (method === "POST") {
        const body = request.postDataJSON() as SelectionBody;
        current.submitted.push({ conversationId: id, body, key: request.headers()["idempotency-key"] ?? null });
        const queued = jobView("queued");
        current.jobs.set(jobId, queued);
        const list = current.messages.get(id) ?? [];
        const messageId = crypto.randomUUID();
        list.unshift({
          id: messageId, role: "user", body: body.question, ordinal: list.length,
          created_at: stamp, citations: [],
        });
        current.messages.set(id, list);
        return route.fulfill({ status: 202, json: { run_id: runId, job_id: jobId, message_id: messageId } });
      }
      if (current.gate) await current.gate.promise;
      return route.fulfill({ json: { messages: current.messages.get(id) ?? [], next_cursor: null } });
    }
    const citationMatch = path.match(/^\/api\/v1\/conversations\/([^/]+)\/messages\/([^/]+)\/citations\/([^/]+)$/);
    if (citationMatch) {
      current.citations.push(path);
      if (current.denied.has(citationMatch[3])) return route.fulfill(notFound());
      return route.fulfill({ json: preview });
    }
    const deletion = path.match(/^\/api\/v1\/conversations\/([^/]+)\/deletions$/);
    if (deletion && method === "POST") {
      current.deletions.push(deletion[1]);
      current.conversations = current.conversations.filter(item => item.id !== deletion[1]);
      current.messages.set(deletion[1], []);
      return route.fulfill({ status: 202, json: { conversation_id: deletion[1], job_id: cleanupJobId } });
    }
    return route.fulfill(notFound());
  });
  await page.route("**/api/v1/jobs/**", route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const cancel = path.match(/^\/api\/v1\/jobs\/([^/]+)\/cancel$/);
    if (cancel && request.method() === "POST") {
      current.cancels.push(cancel[1]);
      const cancelled = jobView("failed", "cancelled");
      current.jobs.set(cancel[1], cancelled);
      return route.fulfill({ json: cancelled });
    }
    const detail = path.match(/^\/api\/v1\/jobs\/([^/]+)$/);
    if (detail) {
      const view = current.jobs.get(detail[1]);
      return view ? route.fulfill({ json: view }) : route.fulfill(notFound());
    }
    return route.fulfill(notFound());
  });
  return current;
}
