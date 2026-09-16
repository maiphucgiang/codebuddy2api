import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { api, useResource } from "./api";
import { Logs } from "./pages/Logs";

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
        { id: "request-a", model: "model-a", outcome: "success" },
        { id: "request-b", model: "model-b", outcome: "success" },
      ],
      has_more: false,
      next_cursor: null,
    },
    loading: false,
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

describe("log detail request lifetime", () => {
  it("does not reopen a closed drawer when its response arrives", async () => {
    const request = pending();
    const get = vi.spyOn(api, "get").mockReturnValue(request.promise);
    render(<Logs />);
    open();
    expect(screen.getByRole("status").textContent).toContain("正在加载详情");
    close();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(get.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
    await act(async () => request.resolve({ data: { id: "late-a" } }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it.each(["success", "failure"])(
    "ignores stale %s without changing the next request's loading state",
    async (outcome) => {
      const first = pending();
      const second = pending();
      vi.spyOn(api, "get").mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
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
    const get = vi.spyOn(api, "get").mockReturnValue(request.promise);
    const view = render(<Logs />);
    open();
    view.unmount();
    expect(get.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
    await act(async () => request.reject(new Error("late failure")));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("cancels detail loading when switching log kind", async () => {
    const request = pending();
    const get = vi.spyOn(api, "get").mockReturnValue(request.promise);
    render(<Logs />);
    open();
    fireEvent.click(screen.getByRole("tab", { name: "运行事件" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(get.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
    open();
    expect(get).toHaveBeenCalledTimes(1);
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
    error: null,
    reload: vi.fn(),
  });
  render(<Logs />);
  fireEvent.click(screen.getByRole("tab", { name: "运行事件" }));
  expect(screen.getByText("警告")).toBeTruthy();
});
