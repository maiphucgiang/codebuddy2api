import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { api, useResource } from "./api";
import { Logs } from "./pages/Logs";
import { choose } from "./test-utils";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  useResource: vi.fn(),
}));

function pending() {
  let resolve!: (value: { data: unknown }) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<{ data: unknown }>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.mocked(useResource).mockReturnValue({
    data: {
      items: [
        { id: "request-a", model: "model-a", outcome: "success", client_ip: "198.51.100.9" },
        { id: "request-b", model: "model-b", outcome: "success" },
      ],
      has_more: false,
      next_cursor: null,
    },
    loading: false,
    refreshing: false,
    error: null,
    reload: vi.fn(),
  });
});

function open(index = 0) {
  fireEvent.click(screen.getAllByRole("button", { name: "查看详情" })[index]!);
}

function close() {
  fireEvent.click(screen.getByRole("button", { name: "关闭抽屉" }));
}

describe("runtime event labels", () => {
  it("renders kind, action and outcome in Chinese with raw values on hover", () => {
    vi.mocked(useResource).mockReturnValue({
      data: {
        items: [
          { id: "event-1", kind: "runtime", action: "credential.sync", started_at: 1 },
          {
            id: "event-2",
            kind: "admin",
            action: "mystery.action",
            details: { outcome: "success" },
          },
        ],
        has_more: false,
        next_cursor: null,
      },
      loading: false,
      refreshing: false,
      error: null,
      reload: vi.fn(),
    });
    render(<Logs />);
    fireEvent.click(screen.getByRole("tab", { name: "运行事件" }));
    expect(screen.getByTitle("credential.sync").textContent).toBe("凭证同步");
    expect(screen.getByTitle("runtime").textContent).toBe("运行事件");
    expect(screen.getByText("mystery.action")).toBeTruthy();
    expect(screen.getByText("成功")).toBeTruthy();
  });
});

describe("request client metadata", () => {
  it("shows the client IP and filters requests by it", () => {
    render(<Logs />);
    expect(screen.getByText("198.51.100.9")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("筛选客户端 IP"), {
      target: { value: "198.51.100.9" },
    });
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    const call = vi.mocked(useResource).mock.calls.at(-1)![0] as string;
    expect(call).toContain("client_ip=198.51.100.9");
  });
});

describe("request API keys", () => {
  it("maps key names, marks legacy and deleted keys, and filters by key id", async () => {
    vi.spyOn(api, "get").mockResolvedValue({
      data: {
        external: null,
        items: [{ id: "k_known", name: "CI key", type: "standard", version: 1 }],
      },
    });
    vi.mocked(useResource).mockReturnValue({
      data: {
        items: [
          { id: "request-a", api_key: "k_known", outcome: "success" },
          { id: "request-b", api_key: "k_deleted", outcome: "success" },
          { id: "request-c", api_key: null, outcome: "success" },
        ],
        has_more: false,
        next_cursor: null,
      },
      loading: false,
      refreshing: false,
      error: null,
      reload: vi.fn(),
    });
    render(<Logs />);
    expect((await screen.findAllByText("CI key")).length).toBeGreaterThan(0);
    expect(screen.getByText("k_deleted（已删除）")).toBeTruthy();
    expect(screen.getByText("—（升级前）")).toBeTruthy();
    choose(screen.getByLabelText("筛选密钥"), "k_known");
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    const call = vi.mocked(useResource).mock.calls.at(-1)![0] as string;
    expect(call).toContain("api_key=k_known");
  });
});

describe("credit changes tab", () => {
  it("shows signed changes per account and filters by account and type", async () => {
    const account = "a".repeat(64);
    const get = vi.spyOn(api, "get").mockResolvedValue({
      data: { credentials: [{ account_key: account, name: "主账号.info" }] },
    });
    vi.mocked(useResource).mockReturnValue({
      data: {
        items: [
          {
            id: "credit-1",
            kind: "credit",
            action: "credit.income",
            started_at: 1,
            details: {
              credential: account,
              delta: 500,
              balance: 530,
              source: "体验积分",
              expires_at: 2e9,
            },
          },
          {
            id: "credit-2",
            kind: "credit",
            action: "credit.consume",
            started_at: 2,
            details: { credential: "b".repeat(64), delta: -70.5, balance: 30 },
          },
        ],
        has_more: false,
        next_cursor: null,
      },
      loading: false,
      refreshing: false,
      error: null,
      reload: vi.fn(),
    });
    render(<Logs />);
    await act(async () => fireEvent.click(screen.getByRole("tab", { name: "积分变动" })));
    expect(get).toHaveBeenCalledWith("/credentials", expect.anything());
    expect(screen.getByTitle("credit.income").textContent).toBe("积分入账");
    expect(screen.getByTitle(account).textContent).toBe("主账号.info");
    expect(screen.getByTitle("b".repeat(64)).textContent).toBe("bbbbbbbb");
    expect(screen.getByText("+500")).toBeTruthy();
    expect(screen.getByText("-70.5")).toBeTruthy();
    expect(screen.getByText("体验积分")).toBeTruthy();
    choose(screen.getByLabelText("筛选账号"), account);
    choose(screen.getByLabelText("筛选变动类型"), "income");
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    const call = vi.mocked(useResource).mock.calls.at(-1)![0] as string;
    expect(call).toContain("kind=credit");
    expect(call).toContain(`credential=${account}`);
    expect(call).toContain("type=income");
  });
});

describe("log detail request lifetime", () => {
  it("does not reopen a closed drawer when its response arrives", async () => {
    const request = pending();
    const get = vi
      .spyOn(api, "get")
      .mockImplementation((path) =>
        path === "/keys"
          ? Promise.resolve({ data: { external: null, items: [] } })
          : request.promise,
      );
    render(<Logs />);
    open();
    expect(screen.getByRole("status").textContent).toContain("正在加载详情");
    close();
    expect(screen.queryByRole("dialog")).toBeNull();
    const detail = get.mock.calls.find(([path]) => path === "/logs/request-a")!;
    expect(detail[1]?.signal?.aborted).toBe(true);
    await act(async () => request.resolve({ data: { id: "late-a" } }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it.each(["success", "failure"])(
    "ignores stale %s without changing the next request's loading state",
    async (outcome) => {
      const first = pending();
      const second = pending();
      let detailCount = 0;
      vi.spyOn(api, "get").mockImplementation((path) => {
        if (path === "/keys") return Promise.resolve({ data: { external: null, items: [] } });
        detailCount += 1;
        return detailCount === 1 ? first.promise : second.promise;
      });
      render(<Logs />);
      open();
      close();
      expect(screen.getAllByRole("button", { name: "查看详情" })[1]).toHaveProperty(
        "disabled",
        false,
      );
      open(1);
      await act(async () => {
        if (outcome === "success") first.resolve({ data: { id: "stale-a" } });
        else first.reject(new Error("stale failure"));
      });
      expect(screen.getByRole("status").textContent).toContain("正在加载详情");
      expect(screen.queryByText("stale-a")).toBeNull();
      expect(screen.queryByText("stale failure")).toBeNull();
      await act(async () => second.resolve({ data: { id: "current-b", attempts: [] } }));
      expect(within(screen.getByRole("dialog")).getByText("current-b")).toBeTruthy();
      expect(screen.queryByRole("status")).toBeNull();
    },
  );

  it("aborts on unmount and ignores a late rejection", async () => {
    const request = pending();
    const get = vi
      .spyOn(api, "get")
      .mockImplementation((path) =>
        path === "/keys"
          ? Promise.resolve({ data: { external: null, items: [] } })
          : request.promise,
      );
    const view = render(<Logs />);
    open();
    view.unmount();
    const detail = get.mock.calls.find(([path]) => path === "/logs/request-a")!;
    expect(detail[1]?.signal?.aborted).toBe(true);
    await act(async () => request.reject(new Error("late failure")));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("cancels detail loading when switching log kind", async () => {
    const request = pending();
    const get = vi
      .spyOn(api, "get")
      .mockImplementation((path) =>
        path === "/keys"
          ? Promise.resolve({ data: { external: null, items: [] } })
          : request.promise,
      );
    render(<Logs />);
    open();
    fireEvent.click(screen.getByRole("tab", { name: "运行事件" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    const detail = get.mock.calls.find(([path]) => path === "/logs/request-a")!;
    expect(detail[1]?.signal?.aborted).toBe(true);
    open();
    expect(get.mock.calls.filter(([path]) => path.startsWith("/logs/"))).toHaveLength(1);
    expect(screen.queryByRole("status")).toBeNull();
    await act(async () => request.resolve({ data: { id: "stale-request" } }));
    expect(screen.queryByText("stale-request")).toBeNull();
  });

  it("keeps current request failures visible", async () => {
    const request = pending();
    vi.spyOn(api, "get").mockReturnValue(request.promise);
    render(<Logs />);
    open();
    await act(async () => request.reject(new Error("current failure")));
    expect(screen.getByRole("alert").textContent).toBe("current failure");
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("gateway web tool calls", () => {
  const tool = (model: string, extra: Record<string, unknown> = {}) => ({
    stage: "server_tool",
    model,
    outcome: "success",
    duration_ms: 100,
    ...extra,
  });

  it("marks requests that ran web tools with a count badge, in error style when one failed", () => {
    vi.mocked(useResource).mockReturnValue({
      data: {
        items: [
          { id: "ok", model: "model-ok", attempts: [tool("web-search"), tool("web-fetch")] },
          {
            id: "bad",
            model: "model-bad",
            attempts: [tool("web-search", { outcome: "error" }), tool("web-fetch")],
          },
          {
            id: "capped",
            model: "model-capped",
            attempts: [tool("web-fetch"), { stage: "attempts_truncated", dropped: 3 }],
          },
          { id: "plain", model: "model-plain", attempts: [{ stage: "send", outcome: "error" }] },
          { id: "none", model: "model-none" },
        ],
        has_more: false,
        next_cursor: null,
      },
      loading: false,
      refreshing: false,
      error: null,
      reload: vi.fn(),
    });
    render(<Logs />);
    const row = (id: string) => screen.getByText(id).closest("tr")!;
    expect(within(row("ok")).getByText("联网 2").className).not.toContain("_bad_");
    expect(within(row("bad")).getByText("联网 2").className).toContain("_bad_");
    expect(within(row("capped")).getByText("联网 1+")).toBeTruthy();
    expect(within(row("plain")).queryByText(/联网/)).toBeNull();
    expect(within(row("none")).queryByText(/联网/)).toBeNull();
    expect(screen.getAllByText(/^联网 /)).toHaveLength(3);
  });

  it("localizes the gateway tool model names in the list and keeps the raw name on hover", () => {
    vi.mocked(useResource).mockReturnValue({
      data: {
        items: [
          { id: "search", model: "web-search" },
          { id: "fetch", model: "web-fetch" },
          { id: "chat", model: "model-a" },
        ],
        has_more: false,
        next_cursor: null,
      },
      loading: false,
      refreshing: false,
      error: null,
      reload: vi.fn(),
    });
    render(<Logs />);
    expect(screen.getByTitle("web-search").textContent).toBe("联网搜索");
    expect(screen.getByTitle("web-fetch").textContent).toBe("网页抓取");
    expect(screen.getByText("model-a").getAttribute("title")).toBeNull();
  });

  it("summarizes web tool calls above the attempts and labels each entry", async () => {
    vi.spyOn(api, "get").mockResolvedValue({
      data: {
        id: "request-a",
        attempts: [
          { stage: "send", model: "model-a", outcome: "success" },
          tool("web-search", { duration_ms: 812.5 }),
          tool("web-search", { outcome: "error", error_code: "upstream_429", duration_ms: 40 }),
          tool("web-fetch", { duration_ms: 150 }),
        ],
      },
    });
    render(<Logs />);
    open();
    await act(async () => {});
    const dialog = within(screen.getByRole("dialog"));
    const summary = within(dialog.getByRole("group", { name: "联网工具汇总" }));
    expect(summary.getByText("搜索次数").nextElementSibling!.textContent).toBe("2");
    expect(summary.getByText("抓取次数").nextElementSibling!.textContent).toBe("1");
    expect(summary.getByText("失败次数").nextElementSibling!.textContent).toBe("1");
    expect(summary.getByText("工具总耗时").nextElementSibling!.textContent).toBe("1,002.5 ms");
    expect(dialog.queryByText(/已截断/)).toBeNull();
    expect(dialog.getAllByTitle("server_tool")).toHaveLength(3);
    expect(dialog.getAllByTitle("server_tool")[0]!.textContent).toBe("联网工具");
    expect(dialog.getAllByTitle("web-search")[0]!.textContent).toBe("联网搜索");
    expect(dialog.getByTitle("web-fetch").textContent).toBe("网页抓取");
  });

  it("warns that the counts may be incomplete when attempts were truncated", async () => {
    vi.spyOn(api, "get").mockResolvedValue({
      data: {
        id: "request-a",
        attempts: [tool("web-fetch"), { stage: "attempts_truncated", dropped: 5 }],
      },
    });
    render(<Logs />);
    open();
    await act(async () => {});
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText(/统计可能不完整/)).toBeTruthy();
    expect(dialog.getByTitle("attempts_truncated").textContent).toBe("尝试记录已截断");
  });

  it("renders no summary without web tool entries", async () => {
    vi.spyOn(api, "get").mockResolvedValue({
      data: { id: "request-a", attempts: [{ stage: "send", model: "model-a", outcome: "error" }] },
    });
    render(<Logs />);
    open();
    await act(async () => {});
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText("尝试 1")).toBeTruthy();
    expect(dialog.queryByRole("group", { name: "联网工具汇总" })).toBeNull();
    expect(dialog.queryByText(/统计可能不完整/)).toBeNull();
  });
});

it("shows the persisted daily Buddy warning from event details", () => {
  vi.mocked(useResource).mockReturnValue({
    data: {
      items: [
        {
          id: "buddy-warning",
          action: "buddy.attention_required",
          kind: "runtime",
          details: { outcome: "warning", stage: "buddy_tasks" },
        },
      ],
      has_more: false,
      next_cursor: null,
    },
    loading: false,
    refreshing: false,
    error: null,
    reload: vi.fn(),
  });
  render(<Logs />);
  fireEvent.click(screen.getByRole("tab", { name: "运行事件" }));
  expect(screen.getByText("警告")).toBeTruthy();
});
