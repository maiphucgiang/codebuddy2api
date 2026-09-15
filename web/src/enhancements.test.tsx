import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { Appearance, chooseAppearance, parseAppearance, useAppearance } from "./appearance";
import { Drawer, DrawerPresence } from "./components";
import { Trend } from "./Trend";
import { Credentials } from "./pages/Credentials";
import { api, useResource } from "./api";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  useResource: vi.fn(),
}));
function media() {
  const targets = new Map<string, EventTarget>();
  const values = new Map<string, boolean>();
  vi.stubGlobal("matchMedia", (query: string) => {
    if (!targets.has(query)) targets.set(query, new EventTarget());
    const target = targets.get(query)!;
    return {
      get matches() {
        return values.get(query) ?? false;
      },
      media: query,
      addEventListener: target.addEventListener.bind(target),
      removeEventListener: target.removeEventListener.bind(target),
    };
  });
  return (query: string, matches: boolean) => {
    values.set(query, matches);
    targets.get(query)?.dispatchEvent(new Event("change"));
  };
}
beforeEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  useAppearance.setState({ mode: "system", palette: "green", dark: false, persisted: true });
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("appearance and presence", () => {
  it("follows live system changes, keeps dark separate, and restores the light palette", () => {
    const change = media();
    render(<Appearance />);
    fireEvent.click(screen.getByRole("button", { name: "外观设置" }));
    fireEvent.click(screen.getByLabelText("雾蓝"));
    expect(document.documentElement.dataset.palette).toBe("blue");
    act(() => change("(prefers-color-scheme: dark)", true));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(screen.getByRole("group", { name: "浅色配色" })).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByLabelText("浅色"));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.documentElement.dataset.palette).toBe("blue");
    expect(JSON.parse(localStorage.getItem("codebuddy.appearance")!)).toEqual({
      mode: "light",
      palette: "blue",
    });
    act(() => change("(prefers-color-scheme: dark)", false));
    act(() => change("(prefers-color-scheme: dark)", true));
    expect(document.documentElement.dataset.theme).toBe("light");
  });
  it("handles broken or unavailable storage without losing the in-session choice", () => {
    media();
    expect(parseAppearance("broken")).toEqual({ mode: "system", palette: "green" });
    expect(parseAppearance('{"mode":"invalid","palette":"invalid"}')).toEqual({
      mode: "system",
      palette: "green",
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    render(<Appearance />);
    act(() => chooseAppearance({ mode: "dark" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(useAppearance.getState().persisted).toBe(false);
  });
  it("holds the scroll lock through exit and releases it after the animation", () => {
    media();
    vi.useFakeTimers();
    function Harness() {
      const [open, setOpen] = useState(true);
      return (
        <DrawerPresence>
          {open && (
            <Drawer title="测试" onClose={() => setOpen(false)}>
              内容
            </Drawer>
          )}
        </DrawerPresence>
      );
    }
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "关闭抽屉" }));
    expect(document.querySelector("dialog")?.dataset.phase).toBe("exiting");
    expect(document.documentElement.style.overflow).toBe("hidden");
    act(() => {
      vi.advanceTimersByTime(180);
    });
    expect(document.querySelector("dialog")).toBeNull();
    expect(document.documentElement.style.overflow).not.toBe("hidden");
  });
  it("skips exit delay when reduced motion is requested", () => {
    const change = media();
    change("(prefers-reduced-motion: reduce)", true);
    function Harness() {
      const [open, setOpen] = useState(true);
      return (
        <DrawerPresence>
          {open && (
            <Drawer title="测试" onClose={() => setOpen(false)}>
              内容
            </Drawer>
          )}
        </DrawerPresence>
      );
    }
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "关闭抽屉" }));
    expect(document.querySelector("dialog")).toBeNull();
  });
});

describe("trend values", () => {
  const rows = [
    { bucket: 0, date: "00:00", requests: 3, success: 2, error: 1 },
    { bucket: 3600, date: "01:00", requests: 7, success: 6, error: 1 },
  ];
  it("shows pointer and keyboard data, then discards stale selections on new data", () => {
    const view = render(<Trend rows={rows} granularity="hour" />);
    const chart = screen.getByRole("img");
    vi.spyOn(chart, "getBoundingClientRect").mockReturnValue({ left: 0, width: 780 } as DOMRect);
    fireEvent(chart, new MouseEvent("pointermove", { bubbles: true, clientX: 740 }));
    expect(screen.getByRole("tooltip").textContent).toContain("01:00");
    expect(within(screen.getByRole("tooltip")).getByText("7")).toBeTruthy();
    fireEvent.keyDown(chart, { key: "Home" });
    expect(screen.getByRole("tooltip").textContent).toContain("00:00");
    fireEvent.keyDown(chart, { key: "ArrowRight" });
    expect(screen.getByRole("tooltip").textContent).toContain("01:00");
    view.rerender(<Trend rows={[{ ...rows[0], date: "new" }]} />);
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
  it("does not bridge a missing hourly span", () => {
    render(<Trend rows={[rows[0], { ...rows[1], bucket: 36000 }]} granularity="hour" partial />);
    const chart = screen.getByRole("img");
    vi.spyOn(chart, "getBoundingClientRect").mockReturnValue({ left: 0, width: 780 } as DOMRect);
    fireEvent(chart, new MouseEvent("pointermove", { bubbles: true, clientX: 390 }));
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(chart.querySelector("polyline")).toBeNull();
  });
});

describe("scoped credential buttons", () => {
  it("sends only the selected id and separates batch operations", async () => {
    vi.mocked(useResource).mockReturnValue({
      data: [{ id: "one", name: "one.info", enabled: true, profile: "cn-cli", health: "ready" }],
      reload: vi.fn(),
      loading: false,
      error: null,
    });
    const post = vi.spyOn(api, "post").mockResolvedValue({
      data: { results: [{ id: "one", name: "one.info", ok: true, message: "完成" }] },
    });
    render(<Credentials />);
    for (const [label, path] of [
      ["刷新凭证 one.info", "/credentials/one/refresh"],
      ["签到 one.info", "/credentials/one/checkin"],
      ["同步余额 one.info", "/credentials/one/sync"],
      ["批量签到", "/checkin"],
      ["同步全部余额", "/sync"],
    ]) {
      await act(async () => fireEvent.click(screen.getByRole("button", { name: label })));
      expect(post).toHaveBeenLastCalledWith(
        path,
        undefined,
        expect.objectContaining({ timeout: 300000 }),
      );
    }
    expect(post).toHaveBeenCalledTimes(5);
  });
});
