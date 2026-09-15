import { expect, test } from "@playwright/test";

test("per-account automation persists with scoped actions, dark mode and mobile containment", async ({
  page,
}) => {
  const credentials = [
    {
      id: "cn",
      name: "cn.info",
      enabled: true,
      profile: "cn-cli",
      health: "ready",
      auto_checkin: true,
      auto_travel: true,
      travel_supported: true,
      checkin: { state: "already", date: "2026-09-15", message: "今日已签到" },
      travel: { state: "traveling", message: "Buddy 旅行中" },
    },
    {
      id: "intl",
      name: "intl.info",
      enabled: true,
      profile: "intl-work",
      health: "ready",
      auto_checkin: false,
      auto_travel: false,
      travel_supported: false,
      checkin: { state: "inactive", date: "2026-09-15", message: "签到活动未开放或已结束" },
      travel: { state: "unknown", message: "尚未查询" },
    },
  ];
  const writes: string[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/admin/**", (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    if (path === "/admin/session")
      return route.fulfill({ json: { authenticated: true, csrf_token: "fixture-only" } });
    if (path === "/admin/credentials") return route.fulfill({ json: { credentials } });
    if (["PATCH", "POST"].includes(req.method())) {
      expect(req.headers()["x-csrf-token"]).toBe("fixture-only");
      writes.push(`${req.method()} ${path}`);
      if (req.method() === "PATCH") {
        const target = credentials.find((c) => path.endsWith(`/${c.id}`))!;
        Object.assign(target, req.postDataJSON());
        return route.fulfill({
          json: { id: target.id, ...req.postDataJSON(), revision: writes.length },
        });
      }
      return route.fulfill({
        json: {
          results: [
            {
              id: "cn",
              name: "cn.info",
              ok: false,
              claimed: true,
              message: "领取已确认，后续状态查询失败，未派出",
            },
          ],
        },
      });
    }
    return route.fulfill({ status: 404, json: {} });
  });
  await page.goto("/dashboard/credentials");
  await expect(page.getByRole("switch", { name: "自动签到 cn.info" })).toBeChecked();
  await expect(page.getByRole("switch", { name: "自动旅行 cn.info" })).toBeChecked();
  await expect(page.getByRole("switch", { name: "自动签到 intl.info" })).not.toBeChecked();
  await expect(page.getByRole("switch", { name: "自动旅行 intl.info" })).toBeDisabled();
  await page.getByRole("switch", { name: "自动签到 intl.info" }).click();
  await expect(page.getByText(/保存不会立即领取/)).toBeVisible();
  expect(writes).toEqual(["PATCH /admin/credentials/intl"]);
  await page.reload();
  await expect(page.getByRole("switch", { name: "自动签到 intl.info" })).toBeChecked();
  await page.getByRole("switch", { name: "自动旅行 cn.info" }).click();
  await expect(page.getByText(/自动旅行已关闭/)).toBeVisible();
  await page.getByRole("button", { name: "旅行状态 cn.info" }).click();
  await expect(page.getByText("部分完成")).toBeVisible();
  await page.getByRole("button", { name: "旅行领派 cn.info" }).click();
  await expect(page.getByText(/领取已确认，后续状态查询失败/)).toBeVisible();
  expect(writes).toEqual([
    "PATCH /admin/credentials/intl",
    "PATCH /admin/credentials/cn",
    "POST /admin/credentials/cn/travel-status",
    "POST /admin/credentials/cn/travel",
  ]);
  await page.screenshot({ path: "test-results/automation-desktop.png", fullPage: true });
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByRole("switch", { name: "自动签到 cn.info" }).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/automation-mobile-dark.png", fullPage: true });
  expect(errors).toEqual([]);
});
