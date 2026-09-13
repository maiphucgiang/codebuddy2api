import { defineConfig } from "vite-plus";

export default defineConfig(({ command }) => ({
  base: command === "serve" ? "/" : "/dashboard/",
  server: {
    host: "127.0.0.1",
    port: 5174,
    strictPort: true,
    proxy: process.env.CODEBUDDY_WEBUI_PROXY
      ? { "/admin": { target: process.env.CODEBUDDY_WEBUI_PROXY, changeOrigin: false } }
      : undefined,
  },
  lint: { options: { typeCheck: true, typeAware: true } },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./src/test-setup.ts"],
  },
}));
