import { expect, test } from "@playwright/test";

const admin = { id: "user-admin", github_id: 41, login: "leader", role: "admin", enabled: true };
const member = { id: "user-member", github_id: 42, login: "colleague", role: "member", enabled: true };

test("anonymous visitor sees only login entry; returning session shows current user", async ({ page }) => {
  let signedIn = false;
  await page.route("**/api/v1/session", (route) =>
    route.fulfill(signedIn
      ? { status: 200, json: { user: member, csrf_token: "csrf-member" } }
      : { status: 401, json: { error: { code: "unauthenticated", message: "Sign in", request_id: "r1" } } }),
  );

  await page.goto("/");
  await expect(page.getByRole("link", { name: /GitHub.*登录/ })).toHaveAttribute("href", "/api/v1/auth/github/start");
  await expect(page.getByText("colleague")).not.toBeVisible();

  signedIn = true;
  await page.reload();
  await expect(page.getByText("colleague")).toBeVisible();
});

test("logout removes private cached content and returns to login", async ({ page }) => {
  let signedIn = true;
  await page.route("**/api/v1/session", (route) =>
    route.fulfill(signedIn
      ? { status: 200, json: { user: admin, csrf_token: "csrf-admin" } }
      : { status: 401, json: { error: { code: "unauthenticated", message: "Sign in", request_id: "r2" } } }),
  );
  await page.route("**/api/v1/admin/users?**", (route) =>
    route.fulfill({ status: 200, json: { items: [admin, member], next_cursor: null } }),
  );
  await page.route("**/api/v1/auth/logout", async (route) => {
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-admin");
    signedIn = false;
    await route.fulfill({ status: 204 });
  });

  await page.goto("/admin/users");
  const privateCachedText = page.getByText("colleague");
  await expect(privateCachedText).toBeVisible();
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(privateCachedText).not.toBeVisible();
  await expect(page.getByRole("link", { name: /GitHub.*登录/ })).toBeVisible();
  await page.goto("/admin/users");
  await expect(privateCachedText).not.toBeVisible();
});
