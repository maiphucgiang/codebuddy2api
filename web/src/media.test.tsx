import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { api, useResource } from "./api";
import { translate } from "./i18n";
import {
  Media,
  normalizeMediaSummary,
  remainingLabel,
  taskStatusTone,
  validateMediaBaseUrl,
} from "./pages/Media";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  useResource: vi.fn(),
}));

const summary = normalizeMediaSummary({
  configured: false,
  base_url: "",
  locked: false,
  available: true,
  limit_bytes: 1073741824,
  ttl_hours: 72,
  usage: {
    total_bytes: 327155712,
    images: { count: 48, bytes: 100663296 },
    videos: { count: 6, bytes: 226492416 },
  },
  tasks: { active: 2, failed: 1 },
});

const assets = {
  items: [
    {
      id: "img_" + "a".repeat(32),
      kind: "image",
      mime: "image/png",
      size: 4096,
      sha256: "0".repeat(64),
      model: "hunyuan-image-v3.0",
      created_at: 1789000000,
      expires_at: 1789000000 + 71 * 3600,
      public_url: "https://media.example.com/v1/media/images/img_" + "a".repeat(32),
    },
    {
      id: "vid_" + "b".repeat(32),
      kind: "video",
      mime: "video/mp4",
      size: 8192,
      sha256: "1".repeat(64),
      model: "minimax-video-h3",
      created_at: 1789000000,
      expires_at: 1789000000 + 180 * 60,
    },
  ],
  has_more: false,
  next_cursor: null,
};

function mockResources(overrides: { summary?: object; tasks?: object } = {}) {
  vi.mocked(useResource).mockImplementation(((path: string) => {
    if (path === "/media")
      return {
        data: { ...summary, ...overrides.summary },
        loading: false,
        refreshing: false,
        error: null,
        reload: vi.fn(),
      };
    if (path.startsWith("/media/assets"))
      return { data: assets, loading: false, refreshing: false, error: null, reload: vi.fn() };
    if (path.startsWith("/media/tasks") && overrides.tasks)
      return {
        data: overrides.tasks,
        loading: false,
        refreshing: false,
        error: null,
        reload: vi.fn(),
      };
    return {
      data: { items: [], has_more: false, next_cursor: null },
      loading: false,
      refreshing: false,
      error: null,
      reload: vi.fn(),
    };
  }) as unknown as typeof useResource);
}

beforeEach(() => {
  vi.restoreAllMocks();
  mockResources();
});

describe("media summary normalization", () => {
  it("maps usage and tasks with safe defaults", () => {
    expect(summary.usage.images.count).toBe(48);
    expect(summary.tasks).toEqual({ active: 2, failed: 1 });
    const empty = normalizeMediaSummary({});
    expect(empty.usage.total_bytes).toBe(0);
    expect(empty.tasks).toEqual({ active: 0, failed: 0 });
    expect(empty.ttl_hours).toBe(72);
  });
});

describe("asset base url validation", () => {
  it("mirrors the gateway rules", () => {
    expect(validateMediaBaseUrl("")).toBeNull();
    expect(validateMediaBaseUrl("  ")).toBeNull();
    expect(validateMediaBaseUrl("https://media.example.com")).toBeNull();
    expect(validateMediaBaseUrl("http://10.0.0.1:8787/base")).toBeNull();
    expect(validateMediaBaseUrl("example.com")).toBe("media.settings.baseUrlInvalid");
    expect(validateMediaBaseUrl("ftp://example.com")).toBe("media.settings.baseUrlInvalid");
    expect(validateMediaBaseUrl("https://user:pw@example.com")).toBe(
      "media.settings.baseUrlInvalid",
    );
    expect(validateMediaBaseUrl("https://example.com?a=1")).toBe("media.settings.baseUrlInvalid");
  });
});

describe("retention and status mapping", () => {
  it("labels remaining time and warns under six hours", () => {
    const now = Date.now();
    expect(remainingLabel(null, translate)).toEqual({ text: "—", warn: false });
    expect(remainingLabel(now / 1000 + 90 * 3600, translate, now)).toEqual({
      text: "4 天",
      warn: false,
    });
    expect(remainingLabel(now / 1000 + 3 * 3600, translate, now)).toEqual({
      text: "3.0 小时",
      warn: true,
    });
    expect(remainingLabel(now / 1000 + 20 * 60, translate, now)).toEqual({
      text: "20 分钟",
      warn: true,
    });
  });

  it("routes the remaining-time units through the translator", () => {
    const now = Date.now();
    const echo = (key: string, vars?: Record<string, string | number>) => `${key}|${vars?.count}`;
    expect(remainingLabel(now / 1000 + 90 * 3600, echo, now).text).toBe("media.remaining.days|4");
    expect(remainingLabel(now / 1000 + 3 * 3600, echo, now).text).toBe("media.remaining.hours|3.0");
    expect(remainingLabel(now / 1000 + 20 * 60, echo, now).text).toBe("media.remaining.minutes|20");
  });

  it("maps task statuses to badge tones", () => {
    expect(taskStatusTone("completed")).toBe("good");
    expect(taskStatusTone("failed")).toBe("bad");
    expect(taskStatusTone("queued")).toBe("neutral");
    expect(taskStatusTone("in_progress")).toBe("neutral");
    expect(taskStatusTone(undefined)).toBe("neutral");
  });
});

describe("media page", () => {
  it("shows the unconfigured banner with a configure action", () => {
    render(<Media />);
    expect(screen.getByRole("alert").textContent).toContain("未配置资源访问地址");
    expect(screen.getByRole("button", { name: "去配置" })).toBeTruthy();
  });

  it("shows the locked banner without an action when locked by startup", () => {
    mockResources({ summary: { locked: true } });
    render(<Media />);
    expect(screen.getByRole("alert").textContent).toContain("由启动配置锁定");
    expect(screen.queryByRole("button", { name: "去配置" })).toBeNull();
  });

  it("renders asset cards and hides copy-link until a base URL exists", () => {
    render(<Media />);
    fireEvent.click(screen.getAllByRole("button", { name: /hunyuan-image-v3\.0/ })[0]!);
    const dialog = screen.getByRole("dialog");
    expect(dialog).toBeTruthy();
    expect(dialog.textContent).toContain("创建时间");
    expect(dialog.textContent).toContain("到期时间"); // Derived from the current TTL setting.
    expect(screen.queryByRole("button", { name: "复制链接" })).toBeNull();
    expect(screen.getByRole("link", { name: "下载" })).toHaveProperty(
      "href",
      expect.stringContaining("/admin/media/assets/img_"),
    );
  });

  it("offers copy-link once the base URL is configured", () => {
    mockResources({ summary: { configured: true, base_url: "https://media.example.com" } });
    render(<Media />);
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getAllByRole("button", { name: /hunyuan-image-v3\.0/ })[0]!);
    expect(screen.getByRole("button", { name: "复制链接" })).toBeTruthy();
  });

  it("reports a copy failure through i18n", async () => {
    mockResources({ summary: { configured: true, base_url: "https://media.example.com" } });
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    Object.assign(navigator, { clipboard: { writeText } });
    render(<Media />);
    fireEvent.click(screen.getAllByRole("button", { name: /hunyuan-image-v3\.0/ })[0]!);
    fireEvent.click(screen.getByRole("button", { name: "复制链接" }));
    expect(writeText).toHaveBeenCalledWith(
      "https://media.example.com/v1/media/images/img_" + "a".repeat(32),
    );
    await waitFor(() =>
      expect(screen.getByRole("dialog").textContent).toContain("复制失败，请手动复制链接"),
    );
  });

  it("flags active tasks whose download is deferred", () => {
    const task = (id: string, status: string, error: string | null) => ({
      task_id: id,
      status,
      model: "minimax-video-h3",
      profile: "cn-cli",
      account_key: "acct",
      created: 1789000000,
      error,
      credit: null,
      media_id: null,
    });
    mockResources({
      tasks: {
        items: [
          task(
            "task-deferred",
            "in_progress",
            "资源下载暂缓，下次查询重试：资源下载失败（HTTP 429）",
          ),
          task("task-queued", "queued", null),
          task(
            "task-failed",
            "failed",
            "视频资源下载失败：资源下载失败（HTTP 404，签名可能已过期）",
          ),
        ],
        has_more: false,
        next_cursor: null,
      },
    });
    render(<Media />);
    fireEvent.click(screen.getByRole("tab", { name: "视频任务" }));
    expect(screen.getAllByText("下载暂缓，查询时重试")).toHaveLength(1);
  });

  it("keeps model management in model routing", () => {
    render(<Media />);
    expect(screen.queryByRole("tab", { name: "模型" })).toBeNull();
    expect(screen.getByRole("tab", { name: "资源库" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "视频任务" })).toBeTruthy();
  });
  it("confirms deletion in a dialog and styles the download button", async () => {
    const remove = vi.spyOn(api, "delete").mockResolvedValue({ data: {} } as never);
    render(<Media />);
    fireEvent.click(screen.getAllByRole("button", { name: /hunyuan-image-v3\.0/ })[0]!);
    const download = screen.getByRole("link", { name: "下载" });
    expect(download.className).toContain("linkButton");
    fireEvent.click(screen.getByRole("button", { name: /^删除$/ }));
    expect(remove).not.toHaveBeenCalled();
    const confirmation = within(screen.getByRole("dialog"));
    expect(confirmation.getByText(/不可恢复/)).toBeTruthy();
    fireEvent.click(confirmation.getByRole("button", { name: "取消" }));
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /^删除$/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(remove).toHaveBeenCalled());
    expect(remove.mock.calls[0]![0]).toContain("/media/assets/img_");
  });
});
