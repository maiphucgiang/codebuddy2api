import { expect, test, type Page } from "@playwright/test";

test.use({ hasTouch: true });
async function fixture(page: Page) {
  const calls: string[] = [];
  const credentials = [
    {
      id: "one",
      name: "one.info",
      enabled: true,
      profile: "cn-cli",
      health: "ready",
      credits: { credits: 40 },
    },
  ];
  await page.route("**/admin/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/admin/session")
      return route.fulfill({ json: { authenticated: true, csrf_token: "fixture-only" } });
    if (path === "/admin/credentials") return route.fulfill({ json: { credentials } });
    if (path === "/admin/dashboard")
      return route.fulfill({
        json: {
          summary: { requests: 17, success_rate: 15 / 17, total_tokens: 500, credit: 1 },
          series: [1, 5, 3, 8].map((requests, i) => ({
            date: `2026-09-14 0${i}:00`,
            bucket: 1789344000 + i * 3600,
            requests,
            success: requests - (i % 2),
            error: i % 2,
          })),
          models: [],
          profiles: [],
          health: { credentials },
          storage: { degraded: false },
          range: { granularity: "hour" },
        },
      });
    if (route.request().method() === "POST") {
      expect(route.request().headers()["x-csrf-token"]).toBe("fixture-only");
      calls.push(path);
      return route.fulfill({
        json: { results: [{ id: "one", name: "one.info", ok: true, message: "隔离测试操作成功" }] },
      });
    }
    return route.fulfill({
      status: 404,
      json: { error: { message: "Unmocked fixture endpoint" } },
    });
  });
  return calls;
}

test("appearance persists, follows the system, and animates modal exit", async ({ page }) => {
  await fixture(page);
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "no-preference" });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/dashboard");
  const open = page.getByRole("button", { name: "外观设置" });
  await open.click();
  const sheet = page.getByRole("dialog").locator("section");
  await expect(sheet).toHaveCSS("animation-name", /drawerIn/);
  await page.getByLabel("雾蓝").check();
  await expect(page.locator("html")).toHaveAttribute("data-palette", "blue");
  await page.getByLabel("深色", { exact: true }).check();
  const dark = await page
    .locator("body")
    .evaluate((node) => getComputedStyle(node).backgroundColor);
  await expect(page.getByLabel("青叶")).toBeDisabled();
  await page.screenshot({ path: "test-results/appearance-dark.png", fullPage: true });
  await page.getByLabel("浅色", { exact: true }).check();
  await page.getByLabel("蔷薇").check();
  await page.getByLabel("深色", { exact: true }).check();
  await expect(page.locator("body")).toHaveCSS("background-color", dark);
  await page.getByLabel("跟随系统").check();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  const phase = await page
    .getByRole("button", { name: "关闭抽屉" })
    .evaluate((node: HTMLButtonElement) => {
      node.click();
      return document.documentElement.style.overflow;
    });
  expect(phase).toBe("hidden");
  await expect(page.locator("dialog")).toHaveCount(0);
  await expect(page.locator("body")).toHaveCSS("position", "static");
  await expect(open).toBeFocused();
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("html")).toHaveAttribute("data-palette", "rose");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await open.click();
  await expect(sheet).toHaveCSS("animation-name", "none");
  await page.keyboard.press("Escape");
  await expect(page.locator("dialog")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("trend has hover, keyboard and touch values without mobile overflow", async ({ page }) => {
  await fixture(page);
  await page.goto("/dashboard");
  const chart = page.getByRole("img", { name: "按小时请求量趋势" });
  let box = (await chart.boundingBox())!;
  await page.mouse.move(box.x + box.width * 0.97, box.y + box.height * 0.5);
  await expect(page.getByRole("tooltip")).toContainText("03:00");
  await chart.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tooltip")).toContainText("01:00");
  await page.keyboard.press("End");
  await expect(page.getByRole("tooltip")).toContainText("03:00");
  await page.setViewportSize({ width: 390, height: 844 });
  box = (await chart.boundingBox())!;
  await page.touchscreen.tap(box.x + box.width * 0.65, box.y + box.height * 0.6);
  await expect(page.getByRole("tooltip")).toContainText("02:00");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/trend-mobile.png", fullPage: true });
});

test("credential refresh, checkin, sync and batch buttons stay separate", async ({ page }) => {
  const calls = await fixture(page);
  await page.goto("/dashboard/credentials");
  for (const label of [
    "刷新凭证 one.info",
    "签到 one.info",
    "同步余额 one.info",
    "批量签到",
    "同步全部余额",
  ]) {
    await page.getByRole("button", { name: label, exact: true }).click();
    await expect(page.getByText("隔离测试操作成功")).toBeVisible();
  }
  expect(calls).toEqual([
    "/admin/credentials/one/refresh",
    "/admin/credentials/one/checkin",
    "/admin/credentials/one/sync",
    "/admin/checkin",
    "/admin/sync",
  ]);
});
