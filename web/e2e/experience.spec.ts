import { expect, test, type Page } from "@playwright/test";

async function experienceAPI(page: Page) {
  let revision = 1,
    enrolled = false;
  const calls: { method: string; path: string; body: Record<string, unknown> | null }[] = [];
  const credentials = [
    {
      id: "cn-account",
      name: "mainland.info",
      profile: "cn-cli",
      enabled: true,
      health: "ready",
      nickname: "隔离测试 · 大陆",
      credits: { remaining: 200 },
      cooldowns: [],
    },
    {
      id: "intl-account",
      name: "international.info",
      profile: "intl-work",
      enabled: true,
      health: "ready",
      nickname: "隔离测试 · 国际",
      credits: { remaining: 120 },
      cooldowns: [],
    },
  ];
  const models: Record<string, unknown>[] = Array.from({ length: 48 }, (_, i) => ({
    id: `fixture-model-${i}`,
    public_id: `fixture-model-${i}`,
    upstream_id: `fixture-model-${i}`,
    enabled: true,
    keep_original: false,
    custom: false,
    region: null,
    profile: null,
    credential_ids: [],
    available: true,
    credits: 0,
    credits_by_profile: { "cn-cli": 0, "intl-work": 0.5 },
  }));
  await page.route("**/admin/**", async (route) => {
    const request = route.request(),
      url = new URL(request.url()),
      method = request.method();
    const path = decodeURIComponent(url.pathname);
    const body = request.postData() ? (request.postDataJSON() as Record<string, unknown>) : null;
    calls.push({ method, path: path + url.search, body });
    const send = (json: unknown, status = 200) => route.fulfill({ json, status });
    if (path === "/admin/session")
      return send({ authenticated: true, csrf_token: "isolated-csrf" });
    if (method !== "GET" || path.endsWith("/poll"))
      expect(request.headers()["x-csrf-token"]).toBe("isolated-csrf");
    if (path === "/admin/credentials")
      return send({
        credentials: enrolled
          ? [...credentials, { ...credentials[1], id: "new-account", name: "newly-enrolled.info" }]
          : credentials,
      });
    if (path === "/admin/oauth/start")
      return send({
        login_id: "isolated",
        verification_uri: "https://www.codebuddy.cn/isolated-oauth",
        expires_in: 60,
      });
    if (path === "/admin/oauth/poll") {
      enrolled = true;
      return send({ done: true });
    }
    if (path.endsWith("/preview")) {
      const ids = body?.credential_ids as string[];
      const allowed = credentials.filter(
        (c) =>
          (!ids.length || ids.includes(c.id)) &&
          (!body?.region || (typeof body.region === "string" && c.profile.startsWith(body.region))),
      );
      return send({
        candidates: allowed,
        excluded: credentials
          .filter((c) => !allowed.includes(c))
          .map((c) => ({ ...c, reason: "不在绑定范围" })),
      });
    }
    if (path === "/admin/models") {
      if (method === "POST") {
        expect(body?.revision).toBe(revision);
        const created = { ...body, id: "custom:isolated", custom: true, available: true };
        models.push(created);
        revision++;
        return send({ revision, model: created }, 201);
      }
      return send({ revision, models });
    }
    if (path === "/admin/models/custom:isolated" && method === "DELETE") {
      expect(body?.revision).toBe(revision);
      models.splice(
        models.findIndex((m) => m.id === "custom:isolated"),
        1,
      );
      revision++;
      return send({ revision, ok: true });
    }
    if (path === "/admin/dashboard") {
      const days = Number(url.searchParams.get("days") ?? 7);
      const grain = url.searchParams.get("granularity");
      const hourly = grain === "hour" || (grain === "auto" && days === 1);
      const endDay = Date.UTC(2026, 8, 14) / 1000,
        start = endDay - (days - 1) * 86400;
      const hours: Record<number, number> = { 9: 8, 12: 17, 16: 11, 19: 4, 22: 6 };
      const series = Array.from({ length: days * (hourly ? 24 : 1) }, (_, i) => {
        const bucket = start + i * (hourly ? 3600 : 86400);
        const count = bucket < endDay ? 0 : hourly ? (hours[(bucket - endDay) / 3600] ?? 0) : 46;
        return {
          bucket,
          date: new Date(bucket * 1000)
            .toISOString()
            .slice(0, hourly ? 16 : 10)
            .replace("T", " "),
          requests: count,
          success: count,
          error: 0,
        };
      });
      return send({
        summary: { requests: 46, success_rate: 1, total_tokens: 8400, credit: 0 },
        series,
        models: [{ model: "fixture-model-0", requests: 46, total_tokens: 8400, credit: 0 }],
        profiles: [
          { profile: "cn-cli", requests: 23 },
          { profile: "intl-work", requests: 23 },
        ],
        health: { credentials },
        storage: {
          degraded: false,
          logical_bytes: 4096,
          max_bytes: 1048576,
          db_bytes: 32768,
          retention_days: 30,
          pending_cleanup: false,
        },
        range: { days, granularity: hourly ? "hour" : "day", timezone: "UTC", partial: false },
        generated_at: endDay + 23 * 3600,
      });
    }
    return send({ error: { message: `Unmocked ${method} ${path}` } }, 404);
  });
  return calls;
}

test("glass workspace folds, creates a scoped model and keeps dialogs isolated", async ({
  page,
}) => {
  const calls = await experienceAPI(page);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/dashboard/models");
  const opener = page.getByRole("button", { name: "编辑规则" }).nth(24);
  await opener.scrollIntoViewIfNeeded();
  const before = await page.evaluate(() => scrollY);
  await opener.click();
  await expect(page.locator("body")).toHaveCSS("position", "fixed");
  await expect(page.locator("html")).toHaveCSS("overflow", "hidden");
  await expect(page.locator("body")).toHaveCSS("top", `${-before}px`);
  await page.mouse.move(100, 400);
  await page.mouse.wheel(0, 600);
  await expect(page.locator("body")).toHaveCSS("top", `${-before}px`);
  await page.getByRole("dialog").click({ position: { x: 100, y: 400 } });
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(await page.evaluate(() => scrollY)).toBe(before);
  await expect(opener).toBeFocused();
  await page.getByRole("button", { name: "收起侧栏" }).click();
  await expect(page.locator("aside")).toHaveCSS("width", "76px");
  await page.reload();
  await expect(page.getByRole("button", { name: "展开侧栏" })).toBeVisible();
  await page.getByRole("button", { name: "新增模型" }).click();
  await page.getByLabel("对外 ID").fill("team-coding");
  await page.getByLabel("上游 ID").fill("fixture-model-0");
  await page.getByRole("radio", { name: /指定区域/ }).check();
  await page.getByRole("combobox", { name: "区域", exact: true }).selectOption("intl");
  await page.getByRole("radio", { name: /指定账号/ }).check();
  await expect(page.getByRole("combobox", { name: "区域", exact: true })).toHaveCount(0);
  await page.getByRole("checkbox", { name: /international.info/ }).check();
  await page.getByRole("button", { name: "预览候选路由" }).click();
  await expect(page.getByRole("heading", { name: "路由预览" })).toBeVisible();
  await page.screenshot({ path: "test-results/glass-model-editor.png" });
  await page.getByRole("button", { name: "创建模型" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const created = calls.find((call) => call.path === "/admin/models" && call.method === "POST")!;
  expect(created.body).toMatchObject({
    public_id: "team-coding",
    upstream_id: "fixture-model-0",
    credential_ids: ["intl-account"],
    region: null,
    profile: null,
  });
  await page.getByLabel("搜索模型").fill("team-coding");
  await expect(page.getByText("team-coding", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await page.getByRole("button", { name: "确认删除模型" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByText("没有匹配的模型")).toBeVisible();
  expect(errors).toEqual([]);
});

test("daily range uses hourly points, with desktop and mobile glass layouts", async ({ page }) => {
  await experienceAPI(page);
  await page.goto("/dashboard");
  await page.getByLabel("统计时间范围").selectOption("1");
  await expect(page.getByRole("img", { name: "按小时请求量趋势" }).locator("circle")).toHaveCount(
    24,
  );
  await page.getByRole("button", { name: "收起侧栏" }).click();
  await expect(page.locator("aside")).toHaveCSS("width", "76px");
  await page.screenshot({ path: "test-results/glass-overview-hourly.png" });
  await page.getByLabel("统计粒度").selectOption("day");
  await expect(page.getByRole("img", { name: "按日请求量趋势" }).locator("circle")).toHaveCount(1);
  await page.getByLabel("统计粒度").selectOption("hour");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("img", { name: "按小时请求量趋势" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/glass-overview-mobile.png" });
  await page.getByRole("link", { name: "模型路由", exact: true }).click();
  await page.getByRole("button", { name: "新增模型" }).click();
  await page.getByRole("radio", { name: /指定账号/ }).check();
  const dialog = page.getByRole("dialog");
  const bounds = await dialog.boundingBox();
  expect(bounds!.width).toBeLessThanOrEqual(390);
  await expect(page.locator("body")).toHaveCSS("position", "fixed");
  await page.screenshot({ path: "test-results/glass-editor-mobile.png" });
  await dialog.click({ position: { x: 2, y: 2 } });
  await expect(dialog).toHaveCount(0);
});

test("OAuth enrollment closes the drawer and refreshes without exposing the opener", async ({
  page,
  context,
}) => {
  const calls = await experienceAPI(page);
  await context.route("https://www.codebuddy.cn/**", (route) =>
    route.fulfill({ contentType: "text/html", body: "<h1>Isolated authorization fixture</h1>" }),
  );
  await page.goto("/dashboard/credentials");
  await page.getByRole("button", { name: "添加凭证" }).click();
  await page.getByRole("button", { name: "发起 OAuth 授权" }).click();
  const opened = page.waitForEvent("popup");
  await page.getByRole("link", { name: /打开官方授权页面/ }).click();
  const popup = await opened;
  await popup.waitForURL("https://www.codebuddy.cn/isolated-oauth");
  expect(await popup.evaluate(() => window.opener === null)).toBe(true);
  await expect(page.getByRole("dialog")).toHaveCount(0, { timeout: 8000 });
  await expect(page.getByText("newly-enrolled.info", { exact: true })).toBeVisible();
  expect(popup.isClosed()).toBe(false); // The external tab remains isolated, not controlled by the admin page.
  expect(calls.filter((call) => call.path === "/admin/credentials")).toHaveLength(2);
});
