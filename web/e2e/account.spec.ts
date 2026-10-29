import { expect, test, type Page } from "@playwright/test";
import { mockDashboard, mockSettings } from "./mock";

async function mockAccount(page: Page) {
  let authenticated = true;
  let username = "admin";
  let password = "old-password";
  let csrf = "old-csrf";
  let bootstrap = true;
  let putCount = 0;
  let revoked = false;
  await page.route("**/admin/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/admin/session") {
      if (request.method() === "POST") {
        const body = request.postDataJSON();
        if (body.username !== username || body.password !== password) {
          return route.fulfill({ status: 401, json: { error: { message: "用户名或密码错误" } } });
        }
        authenticated = true;
        return route.fulfill({ json: { authenticated: true, csrf_token: csrf, bootstrap } });
      }
      if (request.method() === "DELETE") authenticated = false;
      return route.fulfill({
        json: authenticated
          ? { authenticated: true, csrf_token: csrf, bootstrap }
          : { authenticated: false, csrf_token: null },
      });
    }
    if (!authenticated)
      return route.fulfill({ status: 401, json: { error: { message: "未认证" } } });
    if (path === "/admin/settings") return route.fulfill({ json: mockSettings });
    if (path === "/admin/dashboard") return route.fulfill({ json: mockDashboard });
    if (path === "/admin/account" && request.method() === "GET") {
      return route.fulfill({
        json: { username, password_source: "user", bootstrap, changed_at: 1789200000 },
      });
    }
    if (path === "/admin/account" && request.method() === "PUT") {
      expect(request.headers()["x-csrf-token"]).toBe(csrf);
      const body = request.postDataJSON();
      if (body.current_password !== password) {
        return route.fulfill({ status: 403, json: { error: { message: "当前管理密码错误" } } });
      }
      putCount++;
      username = body.username ?? username;
      password = body.new_password ?? password;
      bootstrap = false;
      csrf = "new-csrf";
      return route.fulfill({ json: { username, bootstrap, csrf_token: csrf } });
    }
    if (path === "/admin/sessions" && request.method() === "DELETE") {
      expect(request.headers()["x-csrf-token"]).toBe(csrf);
      revoked = true;
      authenticated = false;
      return route.fulfill({ json: { ok: true } });
    }
    return route.fulfill({ status: 404, json: { error: { message: `Unexpected ${path}` } } });
  });
  return { getPutCount: () => putCount, wasRevoked: () => revoked };
}

test("account changes adopt the new CSRF and revoked sessions require the new password", async ({
  page,
}) => {
  const mock = await mockAccount(page);
  await page.goto("/dashboard/settings?tab=account");
  await page.getByLabel("用户名", { exact: true }).fill("root");
  await page.getByLabel("当前管理密码", { exact: true }).fill("old-password");
  await page.getByLabel("新密码", { exact: true }).fill("new-password");
  await page.getByLabel("再次输入新密码", { exact: true }).fill("new-password");
  await page.getByRole("button", { name: "保存账户更改" }).click();
  await expect(page.getByText("账户已保存，其他已登录会话已退出。", { exact: true })).toBeVisible();
  page.on("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "退出所有会话" }).click();
  await expect(page).toHaveURL(/\/dashboard\/login$/);
  expect(mock.wasRevoked()).toBe(true);
  await page.getByLabel("用户名", { exact: true }).fill("root");
  await page.getByLabel("密码", { exact: true }).fill("old-password");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page.getByRole("alert")).toContainText("用户名或密码错误");
  await page.getByLabel("密码", { exact: true }).fill("new-password");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page.getByRole("heading", { name: "运行概览" })).toBeVisible();
});

test("mismatched password confirmation does not write the account", async ({ page }) => {
  const mock = await mockAccount(page);
  await page.goto("/dashboard/settings?tab=account");
  await page.getByLabel("当前管理密码", { exact: true }).fill("old-password");
  await page.getByLabel("新密码", { exact: true }).fill("new-password");
  await page.getByLabel("再次输入新密码", { exact: true }).fill("different-password");
  await page.getByRole("button", { name: "保存账户更改" }).click();
  await expect(page.getByRole("alert")).toContainText("两次输入的新密码不一致");
  expect(mock.getPutCount()).toBe(0);
});
