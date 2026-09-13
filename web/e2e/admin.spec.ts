import { expect, test, type Page } from "@playwright/test";
const model = {
  id: "mock-model",
  public_id: "mock-model",
  enabled: true,
  keep_original: true,
  region: null,
  profile: null,
  credential_ids: [],
  credits: 0,
  credits_by_profile: { "cn-cli": 0 },
};
const credential = {
  id: "mock-account",
  name: "mock-account.info",
  enabled: true,
  profile: "cn-cli",
  nickname: "隔离测试账号",
  health: "ready",
  fail_until: 0,
  cooldowns: [{ model: "mock-model", until: Date.now() / 1000 + 90 }],
  credits: { remaining: 250 },
  token_expires_at: Date.now() + 86400000,
  catalog_ready: true,
};
const request = {
  id: "mock-request",
  started_at: 1789200000,
  model: "mock-model",
  profile: "cn-cli",
  outcome: "success",
  status_code: 200,
  duration_ms: 1520,
  total_tokens: 420,
  credit: 0,
  attempts: [{ outcome: "success", credential: "mock-account", status_code: 200 }],
};
const settings = {
  revision: 1,
  items: [
    {
      key: "audit_retention_days",
      label: "审计明细保留天数",
      type: "integer",
      value: 30,
      stored: null,
      source: "default",
      mode: "hot",
      locked: false,
      min: 1,
      max: 36500,
    },
    {
      key: "port",
      label: "监听端口",
      type: "integer",
      value: 9000,
      stored: null,
      source: "cli",
      mode: "restart",
      locked: true,
    },
  ],
  audit: {
    logical_bytes: 1024,
    db_bytes: 32768,
    wal_bytes: 4096,
    shm_bytes: 32768,
    degraded: false,
  },
};
async function mockAPI(page: Page, authenticated = true) {
  const calls: { method: string; path: string; body: unknown }[] = [];
  let polls = 0;
  await page.route("**/admin/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const method = req.method();
    const body: unknown = req.postData() ? req.postDataJSON() : null;
    calls.push({ method, path: path + url.search, body });
    const send = (json: unknown, status = 200) => route.fulfill({ json, status });
    if (path === "/admin/session") {
      if (method === "DELETE") {
        authenticated = false;
        return send({ authenticated: false });
      }
      if (method === "POST") authenticated = true;
      return authenticated
        ? send({ authenticated: true, csrf_token: "mock-csrf" })
        : send({ error: { message: "登录失效" } }, 401);
    }
    if (!authenticated) return send({ error: { message: "未认证" } }, 401);
    if (method !== "GET" || path.endsWith("/poll"))
      expect(req.headers()["x-csrf-token"]).toBe("mock-csrf");
    if (path === "/admin/dashboard")
      return send({
        summary: { requests: 126, success_rate: 0.98, total_tokens: 45678, credit: 0 },
        series: [1, 2, 3, 4, 5, 6, 7].map((day) => ({
          date: `2026-09-0${day}`,
          requests: day * 6,
          success: day * 6,
          error: 0,
        })),
        models: [{ model: "mock-model", requests: 126, total_tokens: 45678, credit: 0 }],
        profiles: [
          { profile: "cn-cli", requests: 96 },
          { profile: "intl-work", requests: 30 },
        ],
        health: { credentials: [credential] },
        storage: {
          logical_bytes: 1024,
          max_bytes: 268435456,
          db_bytes: 32768,
          wal_bytes: 4096,
          shm_bytes: 32768,
          degraded: false,
          dropped_records: 0,
        },
        official_credits: { "mock-account.info": { credits: { remaining: 250 } } },
        generated_at: 1789200000,
        range: { days: 7 },
      });
    if (path === "/admin/models") return send({ revision: 1, models: [model] });
    if (path.endsWith("/preview"))
      return send({ candidates: [{ id: credential.id, name: credential.name }], excluded: [] });
    if (path === "/admin/models/mock-model")
      return send({ error: { message: "配置已更新，请刷新后重试" } }, 409);
    if (path === "/admin/credentials") return send({ credentials: [credential] });
    if (path === "/admin/credentials/export")
      return route.fulfill({
        body: '{"test_fixture":true}',
        contentType: "application/octet-stream",
        headers: { "Content-Disposition": 'attachment; filename="mock-account.info"' },
      });
    if (path === "/admin/credentials/upload")
      return send({ results: [{ name: "import.info", ok: true }] });
    if (path === "/admin/credentials/mock-account")
      return send({ id: credential.id, enabled: false, revision: 2 });
    if (path === "/admin/credentials/mock-account.info") return send({ ok: true });
    if (path === "/admin/oauth/start")
      return send({
        login_id: "mock-login",
        verification_uri: "https://www.codebuddy.cn/login?state=mock",
        expires_in: 120,
      });
    if (path === "/admin/oauth/poll") {
      polls++;
      return send({ done: true, imported: "mock-account.info" });
    }
    if (path === "/admin/logs/clear") return send({ ok: true });
    if (path === "/admin/logs")
      return send({
        items: [
          url.searchParams.get("kind") === "request"
            ? request
            : {
                id: "mock-event",
                started_at: 1789200000,
                action: "mock.operation",
                kind: url.searchParams.get("kind"),
              },
        ],
        has_more: !url.searchParams.has("cursor"),
        next_cursor: url.searchParams.has("cursor") ? null : "mock-cursor",
      });
    if (path === "/admin/logs/mock-request") return send(request);
    if (path === "/admin/settings") return send(settings);
    return send({ error: { message: `Unexpected mock request: ${method} ${path}` } }, 500);
  });
  return { calls, getPolls: () => polls };
}
test("isolated dashboard, deep routes, model conflict and schema settings", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const mock = await mockAPI(page);
  await page.goto("/dashboard");
  await expect(page.getByRole("heading", { name: "运行概览" })).toBeVisible();
  await expect(page.getByText("45,678", { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: "test-results/dashboard-desktop.png", fullPage: true });
  await page.getByRole("link", { name: "模型路由" }).click();
  await page.getByRole("button", { name: "编辑规则" }).click();
  await page.getByLabel("对外 ID").fill("my-alias");
  await page.getByRole("button", { name: "预览候选路由" }).click();
  await expect(page.getByRole("heading", { name: "路由预览" })).toBeVisible();
  await page.getByRole("button", { name: "保存规则" }).click();
  await expect(page.getByRole("alert")).toContainText("保存冲突");
  await page.screenshot({ path: "test-results/model-drawer.png", fullPage: true });
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByRole("link", { name: "系统设置" }).click();
  await expect(page.getByText("外部锁定", { exact: true })).toBeVisible();
  await page.getByLabel("审计明细保留天数").fill("60");
  await page.getByRole("button", { name: "保存更改" }).click();
  await expect
    .poll(() =>
      mock.calls.some((call) => call.method === "PATCH" && call.path === "/admin/settings"),
    )
    .toBe(true);
  await page.goto("/dashboard/no-such-page");
  await expect(page).toHaveURL(/\/dashboard$/);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "test-results/dashboard-mobile.png", fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});
test("credential OAuth terminates, upload/export and safe-name deletion are wired", async ({
  page,
}) => {
  const mock = await mockAPI(page);
  await page.goto("/dashboard/credentials");
  await expect(page.getByText("认证正常")).toBeVisible();
  await page.getByRole("button", { name: "添加凭证" }).click();
  await page.getByRole("button", { name: "发起 OAuth 授权" }).click();
  await expect(page.getByRole("link", { name: /打开官方授权页面/ })).toBeVisible();
  await expect(page.getByText("授权完成，凭证已添加。")).toBeVisible({ timeout: 8000 });
  await page.waitForTimeout(4500);
  expect(mock.getPolls()).toBe(1);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "导入文件" }).click();
  await page.getByLabel("选择 .info 或 ZIP").setInputFiles({
    name: "import.info",
    mimeType: "application/octet-stream",
    buffer: Buffer.from('{"test_fixture":true}'),
  });
  await expect(page.getByText("已导入", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByLabel("选择 mock-account.info", { exact: true }).check();
  await page.getByRole("button", { name: "导出已选 (1)" }).click();
  await expect(page.getByText("文件包含明文认证信息")).toBeVisible();
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "我理解明文风险，下载已选凭证" }).click();
  expect((await downloadPromise).suggestedFilename()).toBe("mock-account.info");
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await page.getByRole("button", { name: "确认删除凭证" }).click();
  await expect
    .poll(() =>
      mock.calls.some(
        (call) => call.method === "DELETE" && call.path === "/admin/credentials/mock-account.info",
      ),
    )
    .toBe(true);
});
test("log tabs, cursor, details and guarded destructive clear", async ({ page }) => {
  const mock = await mockAPI(page);
  await page.goto("/dashboard/logs");
  await page.getByRole("button", { name: "查看详情" }).click();
  await expect(page.getByRole("heading", { name: "实际尝试", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByText("第 2 页", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "运行事件" }).click();
  await expect(page.getByText("mock.operation")).toBeVisible();
  await page.getByRole("tab", { name: "管理操作" }).click();
  await page.getByRole("button", { name: "清理日志" }).click();
  await page.getByLabel("清理范围").selectOption("all");
  await expect(page.getByRole("button", { name: "确认不可撤销的清理" })).toBeDisabled();
  await page.getByLabel("输入“清空全部日志与统计”").fill("清空全部日志与统计");
  await page.getByLabel("当前 API key").fill("mock-revalidation-key");
  await page.getByRole("button", { name: "确认不可撤销的清理" }).click();
  await expect.poll(() => mock.calls.some((call) => call.path === "/admin/logs/clear")).toBe(true);
});
test("anonymous login never persists API key and API failure is not zero-data success", async ({
  page,
}) => {
  await mockAPI(page, false);
  await page.goto("/dashboard/settings");
  await expect(page).toHaveURL(/\/dashboard\/login$/);
  await expect(page.getByText("不会在浏览器中保存 API key。", { exact: true })).toHaveCount(0);
  await expect(page.locator("p")).toHaveCount(0);
  await page.getByLabel("API key", { exact: true }).fill("mock-login-key");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  expect(
    await page.evaluate(() => ({
      local: localStorage.length,
      session: sessionStorage.length,
      url: location.href,
    })),
  ).toEqual({ local: 0, session: 0, url: "http://127.0.0.1:5174/dashboard" });
  await page.route("**/admin/dashboard?**", (route) =>
    route.fulfill({ status: 503, json: { error: { message: "隔离测试：存储不可用" } } }),
  );
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("隔离测试：存储不可用");
  await expect(page.getByText("请求总量", { exact: true })).toHaveCount(0);
});
