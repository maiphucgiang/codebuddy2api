import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vite-plus/test";
import { api, useResource } from "./api";
import { Keys } from "./pages/Keys";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  useResource: vi.fn(),
}));

const standard = {
  id: "k_abcdefghijkl",
  name: "CI key",
  type: "standard",
  prefix: "cb-abcd",
  masked: "cb-ab…ef90",
  enabled: true,
  expires_at: null,
  ip_allow: null,
  rpm: null,
  models: null,
  created_at: 1761600000,
  last_used_at: 1761600100,
  version: 3,
  usage: { requests: 7, success: 7, error: 0, total_tokens: 100, credit: 2 },
};
const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const writeText = vi.fn();

function mockResource(data: unknown, models: unknown = []) {
  vi.mocked(useResource).mockImplementation((path) => ({
    data: path === "/models" ? models : data,
    loading: false,
    refreshing: false,
    error: null,
    reload: vi.fn(),
  }));
}

beforeEach(() => {
  mockResource({ external: null, items: [standard] }, [
    { id: "auto", label: "自动" },
    { id: "source-a", label: "公开模型 A" },
  ]);
  writeText.mockReset();
  writeText.mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  if (originalClipboard) Object.defineProperty(navigator, "clipboard", originalClipboard);
  else Reflect.deleteProperty(navigator, "clipboard");
});

describe("API key management", () => {
  it("shows external occupancy, status and 24-hour usage", () => {
    mockResource({
      external: { type: "admin", source: "external", origin: "cli", occupied: true },
      items: [{ ...standard, expires_at: 1 }],
    });
    render(<Keys />);
    expect(screen.getByText(/已被外部配置占用（命令行）/)).toBeTruthy();
    expect(screen.getByText("已过期")).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "24 小时请求数" })).toBeTruthy();
    expect(screen.getByText("7")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "新建密钥" }));
    expect(screen.getByRole("option", { name: "管理密钥" })).toHaveProperty("disabled", true);
    expect(screen.getByText(/已被外部配置占用或已存在/)).toBeTruthy();
  });

  it("creates a restricted standard key and shows the returned secret", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({
      data: { ...standard, secret: "cb-complete-secret" },
    });
    render(<Keys />);
    fireEvent.click(screen.getByRole("button", { name: "新建密钥" }));
    fireEvent.change(screen.getByLabelText("名称", { exact: true }), {
      target: { value: "Deploy key" },
    });
    fireEvent.change(screen.getByLabelText("IP 白名单", { exact: true }), {
      target: { value: "198.51.100.0/24\n2001:db8::/32" },
    });
    fireEvent.change(screen.getByLabelText("RPM", { exact: true }), {
      target: { value: "60" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "自动" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "公开模型 A" }));
    fireEvent.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/keys", {
        name: "Deploy key",
        type: "standard",
        expires_at: null,
        ip_allow: ["198.51.100.0/24", "2001:db8::/32"],
        rpm: 60,
        models: ["auto", "source-a"],
      }),
    );
    expect(await screen.findByText("cb-complete-secret")).toBeTruthy();
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "复制" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("cb-complete-secret"));
  });

  it("emphasizes that an admin secret cannot be revealed again", async () => {
    vi.spyOn(api, "post").mockResolvedValue({
      data: { ...standard, type: "admin", secret: "cb-admin-secret" },
    });
    render(<Keys />);
    fireEvent.click(screen.getByRole("button", { name: "新建密钥" }));
    fireEvent.change(screen.getByLabelText("名称", { exact: true }), {
      target: { value: "Admin key" },
    });
    fireEvent.change(screen.getByLabelText("类型", { exact: true }), {
      target: { value: "admin" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建" }));
    expect(await screen.findByText("关闭后无法再次查看")).toBeTruthy();
  });

  it("edits restrictions and rotates with the current version", async () => {
    const patch = vi.spyOn(api, "patch").mockResolvedValue({ data: standard });
    const post = vi.spyOn(api, "post").mockResolvedValue({
      data: { ...standard, version: 4, secret: "cb-rotated-secret" },
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Keys />);
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("名称", { exact: true }), {
      target: { value: "Renamed key" },
    });
    fireEvent.click(screen.getByLabelText("启用", { exact: true }));
    fireEvent.click(screen.getByRole("button", { name: "保存更改" }));
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith(`/keys/${standard.id}`, {
        version: 3,
        name: "Renamed key",
        enabled: false,
        expires_at: null,
        ip_allow: null,
        rpm: null,
        models: null,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "轮换" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(`/keys/${standard.id}/rotate`, { version: 3 }),
    );
    expect(await screen.findByText("cb-rotated-secret")).toBeTruthy();
  });
});
