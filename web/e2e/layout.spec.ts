import { expect, test, type Page } from "@playwright/test";

// Guards narrow layouts: tab rows wrap and dropdown menus escape scrolling tables.
const credentials = Array.from({ length: 12 }, (_, i) => ({
  id: `account-${i}`,
  name: `very-long-credential-name-${i}.info`,
  enabled: true,
  profile: "cn-cli",
  nickname: `隔离测试账号 ${i}`,
  health: "ready",
  credits: { remaining: 100 + i },
  cooldowns: [],
  token_expires_at: Date.now() + 86400000,
}));

const settings = {
  revision: 1,
  items: [
    {
      key: "stream_mode",
      label: "流式模式",
      type: "string",
      value: "compatible",
      stored: null,
      source: "default",
      mode: "hot",
      locked: false,
      choices: ["compatible", "realtime"],
    },
    {
      key: "host",
      label: "监听地址",
      type: "string",
      value: "0.0.0.0",
      stored: null,
      source: "env",
      mode: "restart",
      locked: true,
    },
    {
      key: "audit_budget",
      label: "审计预算",
      type: "integer",
      value: 8192,
      stored: null,
      source: "internal",
      mode: "restart",
      locked: true,
    },
  ],
  audit: { logical_bytes: 0, db_bytes: 0, wal_bytes: 0, shm_bytes: 0, degraded: false },
};

async function mockAPI(page: Page) {
  await page.route("**/admin/**", async (route) => {
    const url = new URL(route.request().url());
    const send = (json: unknown) => route.fulfill({ json, status: 200 });
    if (url.pathname === "/admin/session") return send({ authenticated: true, csrf_token: "cs" });
    if (url.pathname === "/admin/settings") return send(settings);
    if (url.pathname === "/admin/credentials") return send({ credentials });
    return send({});
  });
}

async function openDashboard(page: Page) {
  await mockAPI(page);
  await page.goto("/dashboard/settings");
}

test.describe("narrow layout", () => {
  test("settings tabs wrap instead of pushing the page sideways", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await openDashboard(page);
    await expect(page.getByRole("tab", { name: /系统只读/ })).toBeVisible();
    const numbers = await page.evaluate(() => {
      const scroller = document.scrollingElement!;
      const tabs = document.querySelector<HTMLElement>("[class*=tabs]");
      return {
        overflow: scroller.scrollWidth - scroller.clientWidth,
        tabOverflow: tabs ? tabs.scrollWidth - tabs.clientWidth : 0,
      };
    });
    expect(numbers.overflow).toBeLessThanOrEqual(1);
    expect(numbers.tabOverflow).toBeLessThanOrEqual(1);
  });

  test("row menu escapes the scrolling table and stays inside the viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 820 });
    await openDashboard(page);
    await page.goto("/dashboard/credentials");
    await page.getByRole("button", { name: "列表视图" }).first().click();
    const menus = page.getByRole("button", { name: /更多/ });
    await expect(menus.first()).toBeVisible();
    await menus.nth((await menus.count()) - 1).click();
    const menu = page.locator("[role=menu]").first();
    await expect(menu).toBeVisible();
    const box = await menu.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(390);
    expect(box!.y + box!.height).toBeLessThanOrEqual(820);
    // Portaled to <body>, so a `overflow: auto` table wrapper cannot clip it.
    const portaled = await menu.evaluate((el) => el.parentElement === document.body);
    expect(portaled).toBe(true);
  });
});

test.describe("media page", () => {
  const mediaSummary = {
    configured: false,
    base_url: "",
    locked: false,
    available: true,
    limit_bytes: 1073741824,
    ttl_hours: 72,
    usage: {
      total_bytes: 327155712,
      images: { count: 2, bytes: 100663296 },
      videos: { count: 1, bytes: 226492416 },
    },
    tasks: { active: 1, failed: 0 },
    models: [],
  };
  const mediaAssets = {
    items: [
      {
        id: `img_${"a".repeat(32)}`,
        kind: "image",
        mime: "image/png",
        size: 4096,
        sha256: "0".repeat(64),
        model: "hunyuan-image-v3.0",
        created: 1789000000,
        expires: 1789000000 + 71 * 3600,
      },
    ],
    has_more: false,
    next_cursor: null,
  };

  async function openMedia(page: Page) {
    await page.route("**/admin/**", async (route) => {
      const url = new URL(route.request().url());
      const send = (json: unknown) => route.fulfill({ json, status: 200 });
      if (url.pathname === "/admin/session") return send({ authenticated: true, csrf_token: "cs" });
      if (url.pathname === "/admin/settings") return send(settings);
      if (url.pathname === "/admin/media") return send(mediaSummary);
      if (url.pathname === "/admin/media/assets") return send(mediaAssets);
      return send({});
    });
    await page.goto("/dashboard/media");
  }

  test("cards reflow inside a 600px viewport", async ({ page }) => {
    await page.setViewportSize({ width: 600, height: 900 });
    await openMedia(page);
    const overflow = await page.evaluate(() => {
      const scroller = document.scrollingElement!;
      return scroller.scrollWidth - scroller.clientWidth;
    });
    expect(overflow).toBeLessThanOrEqual(1);
    await expect(page.getByRole("button", { name: /hunyuan-image-v3\.0/ })).toBeVisible();
    await page.screenshot({ path: "test-results/media-600px.png", fullPage: true });
  });

  test("dark mode keeps the card grid readable", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "dark" });
    await openMedia(page);
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await expect(page.getByRole("button", { name: /hunyuan-image-v3\.0/ })).toBeVisible();
    await page.screenshot({ path: "test-results/media-dark.png", fullPage: true });
  });
});
