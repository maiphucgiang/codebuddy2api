import { expect, test, type Page } from "@playwright/test";
import { mockSettings } from "./mock";

const adminSecret = `cb-${"a".repeat(32)}`;
const standardSecret = `cb-${"b".repeat(32)}`;

async function mockKeys(page: Page, external = false) {
  const items: Record<string, unknown>[] = [];
  const secrets = new Map<string, string>();
  const created: Record<string, unknown>[] = [];
  await page.route("**/admin/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/admin/session") {
      return route.fulfill({ json: { authenticated: true, csrf_token: "csrf", bootstrap: false } });
    }
    if (path === "/admin/settings") return route.fulfill({ json: mockSettings });
    if (path === "/admin/models") {
      return route.fulfill({ json: { models: [{ id: "upstream", public_id: "Public model" }] } });
    }
    if (path === "/admin/keys" && request.method() === "GET") {
      return route.fulfill({
        json: { external: external ? { origin: "environment" } : null, items },
      });
    }
    if (path === "/admin/keys" && request.method() === "POST") {
      const body = request.postDataJSON();
      created.push(body);
      const secret = body.type === "admin" ? adminSecret : standardSecret;
      const id = `k_e2e_${items.length + 1}`;
      secrets.set(id, secret);
      const item = {
        id,
        name: body.name,
        type: body.type,
        prefix: secret.slice(0, 8),
        enabled: true,
        expires_at: body.expires_at ?? null,
        ip_allow: body.ip_allow ?? null,
        rpm: body.rpm ?? null,
        models: body.models ?? null,
        created_at: 1789200000,
        last_used_at: null,
        version: 1,
        usage: { requests: 0 },
        overridden: false,
        ...(body.type === "standard"
          ? { masked: `${secret.slice(0, 6)}…${secret.slice(-4)}` }
          : {}),
      };
      items.push(item);
      return route.fulfill({ json: { ...item, secret } });
    }
    const reveal = path.match(/^\/admin\/keys\/([^/]+)\/reveal$/);
    if (reveal && request.method() === "POST") {
      const secret = secrets.get(decodeURIComponent(reveal[1]));
      return secret
        ? route.fulfill({ json: { secret } })
        : route.fulfill({ status: 404, json: { error: { message: "密钥不存在" } } });
    }
    return route.fulfill({ status: 404, json: { error: { message: `Unexpected ${path}` } } });
  });
  return created;
}

test("admin key is shown once and an existing admin key disables another", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await mockKeys(page);
  await page.goto("/dashboard/keys");
  await page.getByRole("button", { name: "新建密钥" }).click();
  await page.getByLabel("名称", { exact: true }).fill("deployment-admin");
  await page.getByRole("combobox", { name: "类型", exact: true }).selectOption("admin");
  await expect(page.getByLabel("IP 白名单")).toHaveCount(0);
  await expect(page.getByLabel("RPM", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "创建", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "密钥只显示一次" })).toBeVisible();
  await expect(dialog.getByText("关闭后无法再次查看", { exact: true })).toBeVisible();
  await expect(dialog.locator("code")).toHaveText(adminSecret);
  await dialog.getByRole("button", { name: "复制", exact: true }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(adminSecret);
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(page.getByRole("row", { name: /deployment-admin/ })).toBeVisible();
  await page.getByRole("button", { name: "新建密钥" }).click();
  await expect(page.getByRole("option", { name: "管理密钥" })).toHaveJSProperty("disabled", true);
});

test("restricted standard key can be revealed and copied again", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const created = await mockKeys(page);
  await page.goto("/dashboard/keys");
  await page.getByRole("button", { name: "新建密钥" }).click();
  await page.getByLabel("名称", { exact: true }).fill("ci-key");
  await page.getByRole("combobox", { name: "有效期", exact: true }).selectOption("7");
  await page.getByLabel("IP 白名单", { exact: true }).fill("127.0.0.1\n198.51.100.0/24");
  await page.getByLabel("RPM", { exact: true }).fill("60");
  await page.getByLabel("Public model", { exact: true }).check();
  await page.getByRole("button", { name: "创建", exact: true }).click();
  let dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "完整密钥" })).toBeVisible();
  await expect(dialog.locator("code")).toHaveText(standardSecret);
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  expect(created[0].ip_allow).toEqual(["127.0.0.1", "198.51.100.0/24"]);
  expect(created[0].rpm).toBe(60);
  expect(created[0].models).toEqual(["upstream"]);
  expect(created[0].expires_at as number).toBeGreaterThan(Date.now() / 1000 + 6 * 86400);
  const row = page.getByRole("row", { name: /ci-key/ });
  await row.getByRole("button", { name: "查看", exact: true }).click();
  dialog = page.getByRole("dialog");
  await expect(dialog.locator("code")).toHaveText(standardSecret);
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await row.getByRole("button", { name: "复制", exact: true }).click();
  await expect(page.getByText("密钥已复制", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(standardSecret);
});

test("external admin key is occupied without exposing a secret", async ({ page }) => {
  await mockKeys(page, true);
  await page.goto("/dashboard/keys");
  await expect(page.getByText(/管理密钥 · 已被外部配置占用（环境变量）/)).toBeVisible();
  await expect(page.locator("code")).toHaveCount(0);
  await page.getByRole("button", { name: "新建密钥" }).click();
  await expect(page.getByRole("option", { name: "管理密钥" })).toHaveJSProperty("disabled", true);
});
