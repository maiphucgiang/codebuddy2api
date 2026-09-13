import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  timeout: 30000,
  globalTimeout: 90000,
  workers: 1,
  fullyParallel: false,
  use: {
    baseURL: "http://127.0.0.1:5174",
    viewport: { width: 1440, height: 1050 },
    browserName: "chromium",
    launchOptions: {
      executablePath: process.env.WEBUI_CHROMIUM,
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
    },
    trace: "retain-on-failure",
  },
  webServer: {
    command: "node e2e/serve.mjs",
    stdout: "pipe",
    stderr: "pipe",
    wait: { stdout: /Isolated WebUI test server PID=/ },
    reuseExistingServer: false,
    timeout: 30000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 5000 },
  },
});
