import { expect, test } from "@playwright/test";
import { completed, job, session } from "./repository-fixtures";

test("native access_lost clears private details even when REST recovery is unavailable", async ({ page }) => {
  await session(page);
  let revoked = false;
  let recoveryReads = 0;
  let releaseStream: () => void = () => {};
  const release = new Promise<void>(resolve => { releaseStream = resolve; });
  await page.route(`**/api/v1/jobs/${job.id}`, route => {
    if (revoked) recoveryReads++;
    return route.fulfill(revoked
      ? { status: 503, json: { error: { code: "service_unavailable" } } }
      : { json: job });
  });
  await page.route(`**/api/v1/jobs/${job.id}/events?**`, async route => {
    await release;
    await route.fulfill({ contentType: "text/event-stream", body: "event: access_lost\ndata: {}\n\n" });
  });
  try {
    await page.goto(`/jobs/${job.id}`);
    await expect(page.getByText(job.id, { exact: true })).toBeVisible();
    revoked = true;
    releaseStream();
    await expect(page.getByRole("heading", { name: "检查不可访问" })).toBeVisible();
    await expect(page.getByText(job.id, { exact: true })).toHaveCount(0);
    await expect(page.getByRole("region", { name: "检查详情" })).toHaveCount(0);
    expect(recoveryReads).toBe(0);
  } finally { releaseStream(); }
});

test("native EventSource replays duplicates, recovers with after and loads final result from REST", async ({ page }) => {
  await session(page);
  let finished = false;
  let connections = 0;
  await page.route(`**/api/v1/jobs/${job.id}`, route => route.fulfill({ json: finished ? completed : job }));
  await page.route(`**/api/v1/jobs/${job.id}/events?**`, route => {
    connections++;
    const after = new URL(route.request().url()).searchParams.get("after");
    expect(after).toBe(connections === 1 ? "0" : "2");
    finished = connections > 1;
    const sequences = finished ? [3] : [1, 2, 2];
    return route.fulfill({ contentType: "text/event-stream", body: ": heartbeat\n\n" + sequences.map(sequence => `id: ${sequence}\nevent: job.status\ndata: ${JSON.stringify({ status: finished ? "succeeded" : "running", attempt: 1, error_code: null, result: { full_name: "untrusted-event-result" } })}\n\n`).join("") });
  });
  await page.goto(`/jobs/${job.id}`);
  await expect(page.getByText("team/private-result")).toBeVisible();
  await expect(page.getByRole("list", { name: "检查进度" }).getByRole("listitem")).toHaveCount(3);
  await expect(page.getByText("untrusted-event-result")).toHaveCount(0);
  await page.waitForTimeout(1500);
  expect(connections).toBe(2);
});
