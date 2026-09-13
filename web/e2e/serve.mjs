import { createServer } from "vite-plus";
const server = await createServer();
await server.listen();
console.log(
  `Isolated WebUI test server PID=${process.pid}; port=5174; owned and stopped by Playwright`,
);
for (const signal of ["SIGTERM", "SIGINT"])
  process.on(signal, () => {
    void server.close().then(() => process.exit(0));
  });
