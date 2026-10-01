import { expect, test } from "@playwright/test";
import { repository, completed, session } from "./repository-fixtures";

test("admin discovers through empty candidate pages and connects numeric identity", async ({ page }, info) => {
  await session(page, "admin");
  let connected = false;
  await page.route("**/api/v1/repositories**", async route => {
    if (route.request().method() === "POST") {
      expect(route.request().postDataJSON()).toEqual({ installation_id: 10, github_repository_id: 101 });
      expect(route.request().headers()["x-csrf-token"]).toBe("csrf-test");
      connected = true;
      return route.fulfill({ status: 201, json: repository });
    }
    return route.fulfill({ json: { items: connected ? [repository] : [], next_cursor: null } });
  });
  await page.route("**/api/v1/github/repositories?**", route => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    if (cursor) expect(cursor).toBe("next/+=");
    return route.fulfill({ json: { items: cursor ? [repository] : [], next_cursor: cursor ? null : "next/+=" } });
  });
  await page.goto("/repositories");
  await expect(page.getByText("尚未接入仓库")).toBeVisible();
  await page.getByRole("button", { name: "接入仓库", exact: true }).click();
  await page.getByRole("region", { name: "候选仓库" }).getByRole("button", { name: "下一页" }).click();
  await page.getByRole("button", { name: "接入 team/private-app" }).click();
  await expect(page.getByRole("button", { name: "检查 team/private-app" })).toBeVisible();
  await page.screenshot({ path: info.outputPath("repositories-desktop.png"), fullPage: true });
});

test("member only sees authorized repositories and retry reuses the same idempotency key", async ({ page }, info) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await session(page);
  let discoveryCalls = 0;
  const keys: string[] = [];
  await page.route("**/api/v1/github/**", route => { discoveryCalls++; return route.abort(); });
  await page.route("**/api/v1/repositories?**", route => route.fulfill({ json: { items: [repository], next_cursor: null } }));
  await page.route(`**/api/v1/repositories/${repository.id}/access-checks`, route => {
    keys.push(route.request().headers()["idempotency-key"]);
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-test");
    return keys.length === 1 ? route.abort() : route.fulfill({ status: 202, json: completed });
  });
  await page.route(`**/api/v1/jobs/${completed.id}`, route => route.fulfill({ json: completed }));
  await page.goto("/repositories");
  await expect(page.getByRole("button", { name: "接入仓库", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "检查 team/private-app" }).click();
  await expect(page.getByRole("button", { name: "重试提交" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath("repositories-error-narrow.png"), fullPage: true });
  await page.getByRole("button", { name: "重试提交" }).click();
  await expect(page.getByText("team/private-result")).toBeVisible();
  expect(keys[0]).toMatch(/^[\da-f-]{36}$/);
  expect(keys[1]).toBe(keys[0]);
  await page.getByRole("link", { name: "仓库", exact: true }).click();
  await page.getByRole("button", { name: "检查 team/private-app" }).click();
  await expect(page.getByText("team/private-result")).toBeVisible();
  expect(keys[2]).not.toBe(keys[0]);
  expect(discoveryCalls).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath("job-narrow.png"), fullPage: true });
});

test("member empty authorization differs from temporary failure and can recover", async ({ page }) => {
  await session(page);
  let unavailable = false;
  await page.route("**/api/v1/repositories?**", route => route.fulfill(unavailable ? { status: 503, json: { error: { code: "unknown", message: "private-upstream-secret" } } } : { json: { items: [], next_cursor: null } }));
  await page.goto("/repositories");
  await expect(page.getByText("暂无可访问的仓库")).toBeVisible();
  unavailable = true;
  await page.reload();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("暂时不可用");
  await expect(page.getByText("private-upstream-secret")).toHaveCount(0);
  unavailable = false;
  await page.getByRole("button", { name: "重新加载" }).click();
  await expect(page.getByText("暂无可访问的仓库")).toBeVisible();
});
