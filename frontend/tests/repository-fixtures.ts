import type { Page } from "@playwright/test";

export const repository = { id: "11111111-1111-4111-8111-111111111111", github_repository_id: 101, installation_id: 10, full_name: "team/private-app", private: true, default_branch: "main" };
export const job = { id: "22222222-2222-4222-8222-222222222222", repository_id: repository.id, requested_by: "member-id", status: "running", attempt: 1, max_attempts: 3, result: null, error_code: null, last_sequence: 0, created_at: "2026-09-30T08:00:00Z", updated_at: "2026-09-30T08:00:00Z" };
export const completed = { ...job, status: "succeeded", last_sequence: 3, result: { github_repository_id: 101, full_name: "team/private-result", private: true, default_branch: "main", checked_at: "2026-09-30T08:01:00Z" } };
export async function session(page: Page, role = "member") {
  await page.route("**/api/v1/session", route => route.fulfill({ json: { user: { id: "member-id", github_id: 42, login: "colleague", role, enabled: true }, csrf_token: "csrf-test" } }));
}

// Only the external transport is replaced. Real pages, hook, REST, timers and session run normally.
export async function eventTransport(page: Page) {
  await page.addInitScript(() => {
    const streams: Array<{ url: string; closed: boolean; target: EventTarget }> = [];
    class FixtureSource extends EventTarget {
      onerror: (() => void) | null = null;
      onopen: (() => void) | null = null;
      entry: typeof streams[number];
      constructor(url: string) { super(); this.entry = { url, closed: false, target: this }; streams.push(this.entry); }
      close() { this.entry.closed = true; }
    }
    Object.assign(window, { EventSource: FixtureSource, streams,
      emit: (sequence: number, status = "running") => streams.at(-1)?.target.dispatchEvent(new MessageEvent("job.status", { lastEventId: String(sequence), data: JSON.stringify({ status, attempt: 1, error_code: null }) })),
      disconnect: () => (streams.at(-1)?.target as FixtureSource).onerror?.(),
      revoke: () => streams.at(-1)?.target.dispatchEvent(new MessageEvent("access_lost")),
    });
  });
}
export async function streamCount(page: Page) { return page.evaluate(() => (window as unknown as { streams: unknown[] }).streams.length); }
export async function emit(page: Page, sequence: number, status = "running") { await page.evaluate(([seq, state]) => (window as unknown as { emit: (n: number, s: string) => void }).emit(Number(seq), String(state)), [sequence, status]); }
export async function disconnect(page: Page) { await page.evaluate(() => (window as unknown as { disconnect: () => void }).disconnect()); }
