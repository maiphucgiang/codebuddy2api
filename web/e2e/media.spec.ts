import { expect, test, type Page } from "@playwright/test";

// Mocked management API: sidebar entry, asset grid, preview drawer and the settings drawer.
const now = Date.now() / 1000;

function summary(configured: boolean, locked = false) {
  return {
    configured,
    base_url: configured ? "https://media.example.com" : "",
    locked,
    available: true,
    limit_bytes: 1073741824,
    ttl_hours: 72,
    usage: {
      total_bytes: 327155712,
      images: { count: 2, bytes: 100663296 },
      videos: { count: 1, bytes: 226492416 },
    },
    tasks: { active: 1, failed: 1 },
    models: [
      {
        id: "hunyuan-image-v3.0",
        kinds: ["image"],
        sources: ["static"],
        default: ["image"],
        accounts: 2,
      },
      {
        id: "minimax-video-h3",
        kinds: ["video"],
        sources: ["static"],
        default: ["video"],
        accounts: 0,
      },
    ],
  };
}

function asset(id: string, kind: "image" | "video", model: string, configured: boolean) {
  return {
    id,
    kind,
    mime: kind === "video" ? "video/mp4" : "image/png",
    size: 4096,
    sha256: "0".repeat(64),
    model,
    created_at: now - 3600,
    expires_at: now + 71 * 3600,
    ...(configured ? { public_url: `https://media.example.com/v1/media/${kind}s/${id}` } : {}),
  };
}

const settingsItems = (locked: boolean) => [
  {
    key: "media_public_base_url",
    label: "资源访问地址",
    type: "string",
    value: "",
    stored: null,
    source: locked ? "env" : "default",
    mode: "hot",
    locked,
  },
  {
    key: "media_max_total_mb",
    label: "媒体容量上限（MB）",
    type: "integer",
    value: 1024,
    stored: null,
    source: "default",
    mode: "hot",
    locked: false,
    min: 64,
    max: 1048576,
  },
  {
    key: "media_ttl_hours",
    label: "媒体保留时长（小时）",
    type: "integer",
    value: 72,
    stored: null,
    source: "default",
    mode: "hot",
    locked: false,
    min: 1,
    max: 8760,
  },
];

async function mockMedia(page: Page, options: { configured?: boolean; locked?: boolean } = {}) {
  const configured = options.configured === true;
  const patches: unknown[] = [];
  await page.route("**/admin/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const send = (json: unknown, status = 200) => route.fulfill({ json, status });
    if (path === "/admin/session") return send({ authenticated: true, csrf_token: "cs" });
    if (path === "/admin/media") return send(summary(configured, options.locked === true));
    if (path === "/admin/media/assets") {
      const items = [
        asset("img_" + "a".repeat(32), "image", "hunyuan-image-v3.0", configured),
        asset("img_" + "b".repeat(32), "image", "hy-image-3.5", configured),
        asset("vid_" + "c".repeat(32), "video", "minimax-video-h3", configured),
      ];
      return send({ items, has_more: false, next_cursor: null });
    }
    if (path === "/admin/media/tasks") {
      return send({
        items: [
          {
            task_id: "task-in-progress",
            account_key: "fingerprint",
            profile: "cn-cli",
            model: "minimax-video-h3",
            created: now - 600,
            updated: now - 300,
            status: "in_progress",
            media_id: null,
            error: null,
            credit: null,
          },
        ],
        has_more: false,
        next_cursor: null,
      });
    }
    if (path === "/admin/settings" && route.request().method() === "PATCH") {
      patches.push(route.request().postDataJSON());
      return send({
        revision: 2,
        items: settingsItems(false),
        audit: { db_bytes: 0 },
        session: {},
      });
    }
    if (path === "/admin/settings")
      return send({
        revision: 1,
        items: settingsItems(options.locked === true),
        audit: { db_bytes: 0 },
        session: {},
      });
    if (path.startsWith("/admin/media/assets/img_") && !path.endsWith("content")) {
      return send(asset("img_" + "a".repeat(32), "image", "hunyuan-image-v3.0", configured));
    }
    return send({});
  });
  return { patches };
}

test("media page: grid, preview drawer and settings drawer validation", async ({ page }) => {
  const { patches } = await mockMedia(page);
  await page.goto("/dashboard/media");
  await expect(page.getByRole("heading", { name: "媒体生成", exact: true })).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("未配置资源访问地址");
  await expect(page.getByRole("button", { name: /图片/ }).first()).toBeVisible();

  // Cards open the preview drawer; copy-link stays hidden until a base URL exists.
  await page
    .getByRole("button", { name: /hunyuan-image-v3\.0/ })
    .first()
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("资源详情");
  await expect(dialog.getByRole("button", { name: "复制链接" })).toHaveCount(0);
  const href = await dialog.getByRole("link", { name: "下载" }).getAttribute("href");
  expect(href).toContain("/admin/media/assets/img_");
  await dialog.getByRole("button", { name: "关闭抽屉" }).click();

  await page.getByRole("tab", { name: "视频任务" }).click();
  await expect(page.getByRole("cell", { name: "task-in-progress" })).toBeVisible();
  await expect(page.getByRole("button", { name: "查询状态" })).toBeVisible();
  await page.getByRole("tab", { name: "模型" }).click();
  await expect(page.getByText("无可用账号")).toBeVisible();

  await page.getByRole("button", { name: "媒体设置" }).click();
  const drawer = page.getByRole("dialog");
  await drawer.getByLabel("资源访问地址").fill("example.com");
  await expect(drawer.getByRole("alert")).toContainText("必须以 http:// 或 https:// 开头");
  await expect(drawer.getByRole("button", { name: "保存" })).toBeDisabled();
  expect(patches).toHaveLength(0);
  await drawer.getByLabel("资源访问地址").fill("https://cdn.example.com/prefix/");
  await drawer.getByRole("button", { name: "保存" }).click();
  await expect(drawer).toHaveCount(0);
  expect(patches[0]).toMatchObject({
    revision: 1,
    values: { media_public_base_url: "https://cdn.example.com/prefix" },
  });
});

test("media page hides the banner and offers public links once configured", async ({ page }) => {
  await mockMedia(page, { configured: true });
  await page.goto("/dashboard/media");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page
    .getByRole("button", { name: /hunyuan-image-v3\.0/ })
    .first()
    .click();
  const link = page.getByRole("dialog").getByRole("button", { name: "复制链接" });
  await expect(link).toBeVisible();
});

test("media settings stay editable only when not locked by startup", async ({ page }) => {
  await mockMedia(page, { locked: true });
  await page.goto("/dashboard/media");
  await expect(page.getByRole("alert")).toContainText("由启动配置锁定");
  await page.getByRole("button", { name: "媒体设置" }).click();
  const field = page.getByRole("dialog").getByLabel("资源访问地址");
  await expect(field).toBeDisabled();
});
