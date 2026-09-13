import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { MemoryRouter } from "react-router";
import { AppRoutes } from "./App";
import { api, useResource, useSession } from "./api";
import { Empty, PageTitle } from "./components";
import { Dashboard } from "./pages/Dashboard";
import { Settings } from "./pages/Settings";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  useResource: vi.fn(),
}));
beforeEach(() => vi.restoreAllMocks());
function resource(data: unknown) {
  vi.mocked(useResource).mockReturnValue({ data, error: null, loading: false, reload: vi.fn() });
}
const settings = {
  revision: 7,
  items: [
    {
      key: "api_key",
      label: "管理密钥",
      value: null,
      stored: null,
      source: "environment",
      mode: "startup",
      type: "secret",
      locked: true,
    },
    {
      key: "host",
      label: "监听地址",
      value: "127.0.0.1",
      stored: "127.0.0.1",
      source: "cli",
      mode: "restart",
      type: "string",
      locked: true,
    },
    {
      key: "audit_retention_days",
      label: "日志保留天数",
      value: 30,
      stored: 60,
      source: "management",
      mode: "hot",
      type: "integer",
      locked: false,
    },
  ],
  audit: { degraded: false, logical_bytes: 0 },
};

describe("concise user-facing copy", () => {
  it("keeps the login card free of storage and implementation notes", () => {
    useSession.setState({ status: "anonymous", csrf: null, error: null });
    const { container } = render(
      <MemoryRouter initialEntries={["/dashboard/login"]}>
        <AppRoutes />
      </MemoryRouter>,
    );
    const card = screen.getByRole("heading", { name: "登录管理控制台" }).parentElement!;
    expect(Array.from(card.children, (node) => node.tagName.toLowerCase())).toEqual([
      "svg",
      "h2",
      "form",
    ]);
    expect(screen.getByLabelText("API key", { exact: true })).toBeTruthy();
    expect(screen.getByRole("button", { name: "进入工作台" })).toBeTruthy();
    expect(container.querySelectorAll("p")).toHaveLength(0);
  });

  it("renders titles and empty states without developer explanations", () => {
    const { container } = render(
      <>
        <PageTitle title="模型路由" />
        <Empty title="暂无数据" />
      </>,
    );
    expect(screen.getByRole("heading", { name: "模型路由" })).toBeTruthy();
    expect(screen.getByText("暂无数据")).toBeTruthy();
    expect(container.textContent).not.toMatch(/WORKSPACE|真实数据|演示数据|接口契约/);
    expect(container.querySelectorAll("p")).toHaveLength(0);
  });

  it("keeps optional task-specific guidance", () => {
    render(<Empty title="没有匹配结果">请调整筛选条件。</Empty>);
    expect(screen.getByText("请调整筛选条件。")).toBeTruthy();
  });

  it("keeps configuration source, locks, and pending effective values clear", () => {
    resource(settings);
    const { container } = render(<Settings />);
    expect(screen.getByText("环境变量")).toBeTruthy();
    expect(screen.getByText("命令行")).toBeTruthy();
    expect(screen.getByText("重启后生效")).toBeTruthy();
    expect(screen.getAllByText("外部锁定")).toHaveLength(2);
    expect(screen.getByText("当前生效：30")).toBeTruthy();
    expect(screen.getByText("已保存：60")).toBeTruthy();
    expect(screen.queryByText("当前生效：未知")).toBeNull();
    expect(container.textContent).not.toMatch(
      /revision|schema|WebUI 不写入|服务端校验为准|内部常量/,
    );
    expect(screen.getByText("查看存储详情").closest("details")?.open).toBe(false);
  });

  it("still submits the revision and only edited settings", async () => {
    resource(settings);
    const patch = vi.spyOn(api, "patch").mockResolvedValue({ data: {} });
    render(<Settings />);
    fireEvent.change(screen.getByLabelText("日志保留天数"), { target: { value: "90" } });
    expect(screen.getByText("1 项待保存")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "保存更改" }));
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith("/settings", {
        revision: 7,
        values: { audit_retention_days: 90 },
      }),
    );
  });

  it("retains degraded-state warnings, cooldowns, and unknown versus zero metrics", () => {
    resource({
      summary: { requests: 1, success_rate: 1, total_tokens: null, credit: 0 },
      series: [],
      models: [],
      profiles: [],
      health: {
        credentials: [
          { name: "合成凭证", profile: "cn-cli", enabled: false, health: "ready", cooldowns: [{}] },
        ],
      },
      storage: { degraded: true },
      official_credits: {},
      generated_at: 1,
    });
    const { container } = render(<Dashboard />);
    expect(screen.getByRole("alert").textContent).toBe("统计可能不完整，请检查日志存储状态。");
    expect(screen.getByText("人工停用")).toBeTruthy();
    expect(screen.getByText("1 个模型冷却")).toBeTruthy();
    expect(screen.getAllByText("未知").length).toBeGreaterThan(0);
    expect(screen.getByText("网关已知 Credit").closest("section")?.textContent).toContain("0");
    expect(container.textContent).not.toMatch(
      /一个客户端请求只统计一次|未知用量不计为零|人工状态与熔断独立|未导入的旧文本日志/,
    );
  });
});
