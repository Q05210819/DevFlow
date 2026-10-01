import { expect, test } from "@playwright/test";
import { completed, job, session } from "./repository-fixtures";

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
