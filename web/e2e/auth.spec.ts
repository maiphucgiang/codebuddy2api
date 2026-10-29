import { expect, test, type Page } from "@playwright/test";
import { mockDashboard, mockSettings } from "./mock";

async function mockAuth(page: Page) {
  let authenticated = false;
  const passwords = new Set(["initial-password"]);
  await page.route("**/admin/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/admin/session") {
      if (request.method() === "POST") {
        const body = request.postDataJSON();
        if (!passwords.has(body.password)) {
          return route.fulfill({ status: 401, json: { error: { message: "用户名或密码错误" } } });
        }
        authenticated = true;
        const persistent = body.remember === true;
        return route.fulfill({
          json: { authenticated: true, csrf_token: "csrf", bootstrap: true },
          headers: {
            "set-cookie": `cb_admin_session=session-id; Path=/admin; HttpOnly; SameSite=Strict${
              persistent ? "; Max-Age=34560000" : ""
            }`,
          },
        });
      }
      if (request.method() === "DELETE") {
        authenticated = false;
        return route.fulfill({
          json: { ok: true },
          headers: {
            "set-cookie": "cb_admin_session=; Max-Age=0; Path=/admin; HttpOnly; SameSite=Strict",
          },
        });
      }
      return route.fulfill({
        json: authenticated
          ? { authenticated: true, csrf_token: "csrf", bootstrap: true }
          : { authenticated: false, csrf_token: null },
      });
    }
    if (!authenticated)
      return route.fulfill({ status: 401, json: { error: { message: "未认证" } } });
    if (path === "/admin/settings") return route.fulfill({ json: mockSettings });
    if (path === "/admin/dashboard") return route.fulfill({ json: mockDashboard });
    if (path === "/admin/account") {
      return route.fulfill({
        json: {
          username: "admin",
          password_source: "external",
          bootstrap: true,
          changed_at: 1789200000,
        },
      });
    }
    return route.fulfill({ status: 404, json: { error: { message: `Unexpected ${path}` } } });
  });
}

async function signIn(page: Page, remember = false) {
  await page.getByLabel("用户名", { exact: true }).fill("admin");
  await page.getByLabel("密码", { exact: true }).fill("initial-password");
  if (remember) await page.getByLabel("记住我", { exact: true }).check();
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page.getByRole("heading", { name: "运行概览" })).toBeVisible();
}

test("session login survives reload and logout returns to the login page", async ({
  page,
  context,
}) => {
  await mockAuth(page);
  await page.goto("/");
  await expect(page).toHaveURL(/\/dashboard\/login$/);
  await signIn(page);
  const cookie = (await context.cookies()).find((item) => item.name === "cb_admin_session");
  expect(cookie?.expires).toBe(-1);
  await page.reload();
  await expect(page.getByRole("heading", { name: "运行概览" })).toBeVisible();
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(/\/dashboard\/login$/);
});

test("remember me creates a long-lived cookie", async ({ page, context }) => {
  await mockAuth(page);
  await page.goto("/dashboard/login");
  await signIn(page, true);
  const cookie = (await context.cookies()).find((item) => item.name === "cb_admin_session");
  const now = Date.now() / 1000;
  expect(cookie?.expires).toBeGreaterThan(now + 399 * 86400);
  expect(cookie?.expires).toBeLessThan(now + 401 * 86400);
});

test("bootstrap warning links to the account tab", async ({ page }) => {
  await mockAuth(page);
  await page.goto("/dashboard/login");
  await signIn(page);
  await expect(
    page.getByRole("status").filter({ hasText: "当前使用初始密码，请尽快修改。" }),
  ).toBeVisible();
  await page.getByRole("link", { name: "修改管理账户" }).click();
  await expect(page).toHaveURL(/\/dashboard\/settings\?tab=account$/);
  await expect(page.getByRole("heading", { name: "管理账户", exact: true }).first()).toBeVisible();
});

test("login renders in light, dark and a single-column mobile layout", async ({ page }) => {
  await mockAuth(page);
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await page.goto("/dashboard/login");
  await expect(page.getByLabel("密码", { exact: true })).toBeVisible();
  await page.getByLabel("密码", { exact: true }).fill("initial-password");
  await expect(page.locator("p")).toHaveCount(0);
  await page.screenshot({ path: "test-results/auth-login-light.png", fullPage: true });
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await page.screenshot({ path: "test-results/auth-login-dark.png", fullPage: true });
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/auth-login-mobile.png", fullPage: true });
});
