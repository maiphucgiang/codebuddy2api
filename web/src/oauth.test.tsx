import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { api } from "./api";
import { Credentials } from "./pages/Credentials";

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});
const old = { id: "old", name: "old.info", enabled: true, profile: "cn-cli", health: "ready" };
const added = { id: "new", name: "new.info", enabled: true, profile: "intl-work", health: "ready" };
async function begin() {
  await act(async () => {
    render(<Credentials />);
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "添加凭证" }));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "发起 OAuth 授权" }));
  });
}
function startResponse() {
  vi.spyOn(api, "post").mockResolvedValue({
    data: {
      login_id: "synthetic-login",
      verification_uri: "https://www.codebuddy.cn/auth",
      expires_in: 60,
    },
  });
}
describe("OAuth enrollment completion", () => {
  it("polls while the official tab is active, closes its drawer and refreshes credentials", async () => {
    let enrolled = false;
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const get = vi.spyOn(api, "get").mockImplementation(async (path) => {
      if (path === "/oauth/poll") {
        enrolled = true;
        return { data: { done: true } };
      }
      return { data: { credentials: enrolled ? [old, added] : [old] } };
    });
    startResponse();
    await begin();
    const link = screen.getByRole("link", { name: /打开官方授权页面/ });
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("new.info")).toBeTruthy();
    expect(screen.getByText("授权完成，凭证已添加。")).toBeTruthy();
    expect(get.mock.calls.filter(([path]) => path === "/credentials")).toHaveLength(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    expect(get.mock.calls.filter(([path]) => path === "/oauth/poll")).toHaveLength(1);
  });
  it("keeps the dialog open and stops polling when enrollment fails", async () => {
    const get = vi.spyOn(api, "get").mockImplementation(async (path) => ({
      data: path === "/oauth/poll" ? { done: true, error: "save failed" } : { credentials: [old] },
    }));
    startResponse();
    await begin();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText("save failed")).toBeTruthy();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(12000);
    });
    expect(get.mock.calls.filter(([path]) => path === "/oauth/poll")).toHaveLength(1);
    expect(get.mock.calls.filter(([path]) => path === "/credentials")).toHaveLength(1);
  });
  it("ignores a late success after dismissal, including a newly reopened dialog", async () => {
    let resolve!: (value: { data: unknown }) => void;
    const get = vi.spyOn(api, "get").mockImplementation((path) =>
      path === "/oauth/poll"
        ? new Promise((done) => {
            resolve = done;
          })
        : Promise.resolve({ data: { credentials: [old] } }),
    );
    startResponse();
    await begin();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    const signal = get.mock.calls.find(([path]) => path === "/oauth/poll")![1]!
      .signal as AbortSignal;
    fireEvent.click(screen.getByRole("button", { name: "关闭抽屉" }));
    expect(signal.aborted).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "添加凭证" }));
    await act(async () => resolve({ data: { done: true } }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.queryByText("授权完成，凭证已添加。")).toBeNull();
    expect(get.mock.calls.filter(([path]) => path === "/credentials")).toHaveLength(1);
  });
});
