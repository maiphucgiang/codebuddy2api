import { describe, expect, it } from "vite-plus/test";
import { serverToolSummary } from "./serverTools";

const tool = (model: string, extra: Record<string, unknown> = {}) => ({
  stage: "server_tool",
  model,
  outcome: "success",
  duration_ms: 100,
  ...extra,
});

describe("serverToolSummary", () => {
  it("counts searches, fetches, failures and total time from server_tool entries", () => {
    const summary = serverToolSummary([
      { stage: "send", model: "claude-a", outcome: "success", duration_ms: 900 },
      tool("web-search", { duration_ms: 812.5 }),
      tool("web-search", { outcome: "error", error_code: "upstream_429", duration_ms: 40 }),
      tool("web-fetch", { duration_ms: 150 }),
    ]);
    expect(summary).toEqual({
      total: 3,
      search: 2,
      fetch: 1,
      failed: 1,
      durationMs: 1002.5,
      truncated: false,
    });
  });

  it("flags truncation when the attempts cap marker is present", () => {
    const summary = serverToolSummary([
      tool("web-fetch"),
      { stage: "attempts_truncated", dropped: 4 },
    ]);
    expect(summary).toMatchObject({ total: 1, fetch: 1, truncated: true });
  });

  it("counts entries with an unknown tool model in the total only and skips unusable durations", () => {
    const summary = serverToolSummary([
      { stage: "server_tool", outcome: "success" },
      tool("web-other", { duration_ms: "slow" }),
      tool("web-other"),
    ]);
    expect(summary).toEqual({
      total: 3,
      search: 0,
      fetch: 0,
      failed: 0,
      durationMs: 100,
      truncated: false,
    });
  });

  it.each([
    ["not an array", undefined],
    ["empty", []],
    ["no server_tool entries", [{ stage: "send", outcome: "error" }]],
    ["only a truncation marker", [{ stage: "attempts_truncated", dropped: 2 }]],
    ["malformed entries", [null, "server_tool", 3, [tool("web-search")]]],
  ])("returns null for %s", (_name, attempts) => {
    expect(serverToolSummary(attempts)).toBeNull();
  });
});
