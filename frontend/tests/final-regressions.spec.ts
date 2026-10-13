import { expect, test } from "@playwright/test";
import { completed, disconnect, emit, eventTransport, job, repository, session, streamCount } from "./repository-fixtures";

for (const screen of ["users", "audit", "repositories", "candidates"]) {
  test(`${screen} failed second page preserves retry and previous without stale rows`, async ({ page }) => {
    await session(page, "admin");
    const endpoint = screen === "users" ? "admin/users" : screen === "audit" ? "admin/audit-logs" : screen === "candidates" ? "github/repositories" : "repositories";
    if (screen === "candidates") await page.route("**/api/v1/repositories?**", r => r.fulfill({ json: { items: [], next_cursor: null } }));
    let failed = true;
    let secondRequests = 0;
    await page.route(`**/api/v1/${endpoint}?**`, route => {
      const second = new URL(route.request().url()).searchParams.has("cursor");
      if (second) secondRequests++;
      if (second && failed) return route.fulfill({ status: 403, json: { error: { code: "forbidden" } } });
      const item = screen === "users" ? { id: "test-user", github_id: 999, login: "private-row", role: "member", enabled: true }
        : screen === "audit" ? { id: "test-audit", actor_id: "private-row", action: "member.created", resource_type: "user", resource_id: null, outcome: "success", reason_code: null, created_at: "2026-10-13T00:00:00Z" }
        : { ...repository, full_name: "private-row" };
      return route.fulfill({ json: { items: [item], next_cursor: second ? null : "second-page" } });
    });
    await page.goto(screen === "users" || screen === "audit" ? `/admin/${screen}` : "/repositories");
    if (screen === "candidates") await page.getByRole("button", { name: "接入仓库", exact: true }).click();
    const scope = screen === "candidates" ? page.getByRole("region", { name: "候选仓库" }) : page.getByRole("main");
    await scope.getByRole("button", { name: "下一页" }).click();
    await expect(scope.getByRole("alert")).toBeVisible();
    await expect(scope.getByText("private-row", { exact: true })).toHaveCount(0);
    await expect(scope.getByRole("button", { name: "下一页" })).toBeDisabled();
    await expect(scope.getByRole("button", { name: "上一页" })).toBeEnabled();
    const retry = scope.getByRole("button", { name: screen === "candidates" ? "重新加载候选" : "重新加载", exact: true });
    const before = secondRequests;
    await retry.click();
    await expect.poll(() => secondRequests).toBeGreaterThan(before);
    await scope.getByRole("button", { name: "上一页" }).click();
    await expect(scope.getByText("private-row", { exact: true })).toBeVisible();
    await scope.getByRole("button", { name: "下一页" }).click();
    await expect(retry).toBeVisible();
    failed = false;
    await retry.click();
    await expect(scope.getByText("private-row", { exact: true })).toBeVisible();
  });
}

test("terminal REST attempt supersedes an older SSE attempt and retains history", async ({ page }) => {
  await session(page); await eventTransport(page);
  let terminal = false;
  await page.route(`**/api/v1/jobs/${job.id}`, route => route.fulfill({ json: terminal ? { ...completed, attempt: 3 } : job }));
  await page.goto(`/jobs/${job.id}`);
  await expect.poll(() => streamCount(page)).toBe(1);
  await emit(page, 1);
  terminal = true;
  await disconnect(page);
  await expect(page.getByText("team/private-result")).toBeVisible();
  await expect(page.getByText("3 / 3", { exact: true })).toBeVisible();
  await expect(page.getByRole("list", { name: "检查进度" })).toContainText("第 1 次尝试");
});

for (const [code, message] of [
  ["github_unavailable", "GitHub 服务暂时不可用"],
  ["github_rate_limited", "GitHub 请求额度已用尽"],
  ["github_configuration_error", "请联系管理员检查 GitHub App 配置"],
  ["attempt_timeout", "本次检查超时"],
  ["lease_expired", "检查执行中断"],
  ["reauth_required", "请重新登录并授权 GitHub"],
  ["secret-unknown-code", "检查未完成，请稍后重试或联系管理员"],
  [null, "检查未完成，请稍后重试或联系管理员"],
]) test(`terminal failure reason is safe and actionable: ${code}`, async ({ page }) => {
  await session(page);
  await page.route(`**/api/v1/jobs/${job.id}`, route => route.fulfill({ json: { ...job, status: "failed", error_code: code } }));
  await page.goto(`/jobs/${job.id}`);
  await expect(page.getByRole("main").getByRole("alert")).toContainText(message!);
  await expect(page.getByText("secret-unknown-code")).toHaveCount(0);
});
