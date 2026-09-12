import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "backend.integration.ts",
  timeout: 30000,
  globalTimeout: 90000,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:5175",
    viewport: { width: 1440, height: 1050 },
    launchOptions: {
      executablePath: process.env.WEBUI_CHROMIUM,
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
    },
    trace: "retain-on-failure",
  },
  webServer: {
    command: `${process.env.WEBUI_PYTHON ?? "../.venv/bin/python"} -B ../tests/webui_fixture.py`,
    stdout: "pipe",
    stderr: "pipe",
    wait: { stderr: /Uvicorn running on http:\/\/127\.0\.0\.1:5175/ },
    reuseExistingServer: false,
    timeout: 20000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 10000 },
  },
});
