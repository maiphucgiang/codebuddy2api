import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vite-plus/test";
import { Credentials } from "./pages/Credentials";
import { api, credentialResponse, useResource } from "./api";
import { TravelSummary } from "./Travel";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  useResource: vi.fn(),
}));
beforeEach(() => vi.restoreAllMocks());
function fixture() {
  const rows = [
    {
      id: "cn",
      name: "cn.info",
      profile: "cn-cli",
      enabled: true,
      health: "ready",
      auto_checkin: true,
      auto_travel: true,
      travel_supported: true,
      checkin: { state: "success", date: "2026-09-15", message: "签到成功" },
      travel: { state: "traveling", message: "Buddy 旅行中" },
    },
    {
      id: "intl",
      name: "intl.info",
      profile: "intl-work",
      enabled: true,
      health: "ready",
      auto_checkin: false,
      auto_travel: false,
      travel_supported: false,
      checkin: { state: "inactive", date: "2026-09-15", message: "签到活动未开放或已结束" },
      travel: { state: "unknown", message: "尚未查询" },
    },
  ];
  const reload = vi.fn();
  vi.mocked(useResource).mockReturnValue({ data: rows, reload, loading: false, error: null });
  return { rows, reload };
}

it("renders server preferences, allows international checkin opt-in, but never offers domestic travel there", async () => {
  const { rows, reload } = fixture();
  const patch = vi.spyOn(api, "patch").mockImplementation(async (url, body) => {
    const target = rows.find((r) => url === `/credentials/${r.id}`)!;
    Object.assign(target, body);
    return { data: { ...target, revision: 1 } };
  });
  const post = vi.spyOn(api, "post");
  render(<Credentials />);
  expect(screen.getByRole("switch", { name: "自动签到 cn.info" })).toHaveProperty("checked", true);
  expect(screen.getByRole("switch", { name: "自动旅行 cn.info" })).toHaveProperty("checked", true);
  expect(screen.getByRole("switch", { name: "自动签到 intl.info" })).toHaveProperty(
    "checked",
    false,
  );
  expect(screen.getByRole("switch", { name: "自动旅行 intl.info" })).toHaveProperty(
    "disabled",
    true,
  );
  expect(screen.queryByRole("button", { name: "旅行领派 intl.info" })).toBeNull();
  await act(async () =>
    fireEvent.click(screen.getByRole("switch", { name: "自动签到 intl.info" })),
  );
  expect(patch).toHaveBeenCalledWith("/credentials/intl", { auto_checkin: true });
  expect(screen.getByRole("switch", { name: "自动签到 intl.info" })).toHaveProperty(
    "checked",
    true,
  );
  expect(screen.getByText(/保存不会立即领取/)).toBeTruthy();
  expect(reload).toHaveBeenCalled();
  post.mockClear();
  await act(async () => fireEvent.click(screen.getByRole("switch", { name: "自动旅行 cn.info" })));
  expect(patch).toHaveBeenLastCalledWith("/credentials/cn", { auto_travel: false });
  expect(screen.getByRole("switch", { name: "自动签到 cn.info" })).toHaveProperty("checked", true);
  expect(post).not.toHaveBeenCalled();
});

it("does not pretend a failed or malformed save succeeded", async () => {
  fixture();
  const patch = vi.spyOn(api, "patch").mockResolvedValue({ data: { id: "intl", revision: 1 } });
  render(<Credentials />);
  await act(async () =>
    fireEvent.click(screen.getByRole("switch", { name: "自动签到 intl.info" })),
  );
  expect(screen.getByText(/设置保存结果未确认/)).toBeTruthy();
  expect(screen.getByRole("switch", { name: "自动签到 intl.info" })).toHaveProperty(
    "checked",
    false,
  );
  patch.mockRejectedValueOnce(new Error("network unavailable"));
  await act(async () => fireEvent.click(screen.getByRole("switch", { name: "自动旅行 cn.info" })));
  expect(screen.getByRole("switch", { name: "自动旅行 cn.info" })).toHaveProperty("checked", true);
});

it("keeps manual checkin and travel separate from the automation preference writes", async () => {
  fixture();
  const post = vi.spyOn(api, "post").mockResolvedValue({
    data: {
      results: [
        {
          id: "cn",
          name: "cn.info",
          ok: false,
          claimed: true,
          message: "领取已确认，后续状态查询失败，未派出",
        },
      ],
    },
  });
  render(<Credentials />);
  for (const [label, path] of [
    ["旅行状态 cn.info", "/credentials/cn/travel-status"],
    ["旅行领派 cn.info", "/credentials/cn/travel"],
    ["签到 intl.info", "/credentials/intl/checkin"],
  ]) {
    await act(async () => fireEvent.click(screen.getByRole("button", { name: label })));
    expect(post).toHaveBeenLastCalledWith(
      path,
      undefined,
      expect.objectContaining({ timeout: 300000 }),
    );
  }
  expect(screen.getByText("部分完成")).toBeTruthy();
  expect(screen.getByText(/签到活动未开放或已结束/)).toBeTruthy();
});

it("shows partial completion when checkin is unavailable but departure succeeded", async () => {
  fixture();
  vi.spyOn(api, "post").mockResolvedValue({
    data: {
      results: [
        {
          id: "cn",
          name: "cn.info",
          ok: false,
          skipped: true,
          checkin_ok: false,
          travel: { departed: true },
          message: "活动未开放；Buddy 已派出",
        },
      ],
    },
  });
  render(<Credentials />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "签到 cn.info" })));
  expect(screen.getByText("部分完成")).toBeTruthy();
});

it("disables switches when an older backend omits them and rejects malformed types", () => {
  vi.mocked(useResource).mockReturnValue({
    data: [{ id: "old", name: "old.info", enabled: true }],
    reload: vi.fn(),
    loading: false,
    error: null,
  });
  render(<Credentials />);
  expect(screen.getByRole("switch", { name: "自动签到 old.info" })).toHaveProperty(
    "disabled",
    true,
  );
  expect(() => credentialResponse({ credentials: [{ id: "bad", auto_checkin: "false" }] })).toThrow(
    "自动任务状态必须为布尔值",
  );
});

it("shows server-provided travel locations and snapshot durations without triggering requests", () => {
  const { rows } = fixture();
  Object.assign(rows[0].travel, {
    location_name: "海边书店",
    remaining_seconds: 3661,
    phase: "after_depart",
    stale: false,
  });
  const post = vi.spyOn(api, "post");
  render(<Credentials />);
  expect(screen.getByText("旅行地点：海边书店")).toBeTruthy();
  expect(screen.getByText("查询时剩余：1 小时 2 分钟")).toBeTruthy();
  expect(screen.getByText("派遣后核验")).toBeTruthy();
  expect(post).not.toHaveBeenCalled();
});

it("keeps confirmed departure visible when its status read fails", async () => {
  fixture();
  vi.spyOn(api, "post").mockResolvedValue({
    data: {
      results: [
        {
          id: "cn",
          name: "cn.info",
          action: "travel",
          ok: false,
          departed: true,
          stale: true,
          state: "unknown",
          phase: "after_depart",
          error_kind: "http",
          http_status: 503,
          code: 123,
          remaining_seconds: 300,
          message: "派遣已确认，后续状态查询失败；勿重复派出",
        },
      ],
    },
  });
  render(<Credentials />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "旅行领派 cn.info" })));
  expect(screen.getByText("部分完成")).toBeTruthy();
  expect(screen.getByText("派遣后核验")).toBeTruthy();
  expect(screen.getByText("上游 HTTP 错误")).toBeTruthy();
  expect(screen.getByText(/HTTP 503/)).toBeTruthy();
  expect(screen.getByText(/业务码 123/)).toBeTruthy();
  expect(screen.queryByText(/查询时剩余/)).toBeNull();
});

it("shows nested travel diagnostics independently from checkin", async () => {
  fixture();
  vi.spyOn(api, "post").mockResolvedValue({
    data: {
      results: [
        {
          id: "cn",
          name: "cn.info",
          action: "checkin",
          ok: false,
          checkin_ok: true,
          message: "签到成功；地点配置查询失败，未派出",
          travel: { phase: "config", http_status: 404, error_kind: "http", code: 12 },
        },
      ],
    },
  });
  render(<Credentials />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "签到 cn.info" })));
  expect(screen.getByText("部分完成")).toBeTruthy();
  expect(screen.getByText("地点配置")).toBeTruthy();
  expect(screen.getByText(/HTTP 404/)).toBeTruthy();
});

it("does not invent a duration or a claimed credit amount", () => {
  const { rerender } = render(<TravelSummary trip={{ state: "traveling", claimed: true }} />);
  expect(screen.getByText("领取已确认，积分数额未返回")).toBeTruthy();
  expect(screen.queryByText(/查询时剩余/)).toBeNull();
  for (const remaining of [null, true, "300", -1, NaN, Infinity]) {
    rerender(<TravelSummary trip={{ state: "traveling", remaining_seconds: remaining }} />);
    expect(screen.queryByText(/查询时剩余/)).toBeNull();
  }
  rerender(<TravelSummary trip={{ state: "traveling", remaining_seconds: 300, stale: true }} />);
  expect(screen.queryByText(/查询时剩余/)).toBeNull();
  rerender(<TravelSummary trip={{ state: "arrived", remaining_seconds: 300 }} />);
  expect(screen.queryByText(/查询时剩余/)).toBeNull();
  rerender(<TravelSummary trip={{ claimed: true, claimed_credit: 0 }} />);
  expect(screen.queryByText(/积分数额未返回/)).toBeNull();
  rerender(<TravelSummary trip={{ state: "traveling", remaining_seconds: 0 }} />);
  expect(screen.getByText(/预计已到达，请查询核验/)).toBeTruthy();
});
