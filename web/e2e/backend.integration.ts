import { test, expect } from "@playwright/test";

test("real management API: session, model alias, audit, clear, and file credentials", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page).toHaveURL(/\/dashboard\/login$/);
  await page.getByLabel("API key", { exact: true }).fill("synthetic-e2e-key");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page.getByRole("heading", { name: "运行概览" })).toBeVisible();
  await expect(page.getByText("125", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "模型路由" }).click();
  await page.getByRole("button", { name: "编辑规则" }).click();
  await page.getByLabel("对外 ID").fill("garden-fixture");
  await page.getByRole("button", { name: "保存规则" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const denied = await page.request.post("/v1/chat/completions", {
    data: { model: "garden-fixture", messages: [{ role: "user", content: "synthetic" }] },
  });
  expect(denied.status()).toBe(401);
  const generated = await page.request.post("/v1/chat/completions", {
    headers: { Authorization: "Bearer synthetic-e2e-key" },
    data: {
      model: "garden-fixture",
      stream: false,
      messages: [{ role: "user", content: "synthetic" }],
    },
  });
  expect(generated.status()).toBe(200);
  expect((await generated.json()).model).toBe("garden-fixture");
  await page.getByRole("link", { name: "日志审计" }).click();
  await expect(page.getByText("garden-fixture", { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: "test-results/backend-real-audit.png", fullPage: true });
  const summaryBefore = await (await page.request.get("/admin/dashboard?days=1")).json();
  const session = await (await page.request.get("/admin/session")).json();
  const cleared = await page.request.post("/admin/logs/clear", {
    headers: { Origin: "http://127.0.0.1:5175", "X-CSRF-Token": session.csrf_token },
    data: { scope: "details" },
  });
  expect(cleared.status()).toBe(200);
  const summaryAfter = await (await page.request.get("/admin/dashboard?days=1")).json();
  expect(summaryAfter.summary.requests).toBe(summaryBefore.summary.requests);
  expect(summaryAfter.summary.credit).toBe(0);
  expect((await (await page.request.get("/admin/credentials")).json()).credentials).toHaveLength(1);
  await page.goto("/dashboard");
  await expect(page.getByRole("heading", { name: "运行概览" })).toBeVisible();
  await expect(page.getByText("125", { exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/backend-real-dashboard.png", fullPage: true });
  await page.goto("/dashboard/credentials");
  await expect(page.getByText("fixture.info", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: /^125(?:\s|$)/ })).toBeVisible();
  await page.screenshot({ path: "test-results/backend-real-credentials.png", fullPage: true });
  expect((await page.request.get("/v1/missing")).status()).toBe(404);
  expect((await page.request.get("/admin/missing")).status()).toBe(404);
  expect(errors).toEqual([]);
});
