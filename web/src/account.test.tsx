import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, afterEach, describe, expect, it, vi } from "vite-plus/test";
import { api, useResource, useSession } from "./api";
import { AppRoutes } from "./App";
import { Settings } from "./pages/Settings";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  useResource: vi.fn(),
}));

function renderAccount() {
  window.history.replaceState({}, "", "/dashboard/settings?tab=account");
  return render(
    <MemoryRouter initialEntries={["/dashboard/settings?tab=account"]}>
      <Settings />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(useResource).mockReturnValue({
    data: { revision: 1, items: [], audit: {}, session: {} },
    loading: false,
    refreshing: false,
    error: null,
    reload: vi.fn(),
  });
  useSession.setState({ status: "authenticated", csrf: "old-csrf", bootstrap: true, error: null });
  vi.spyOn(api, "get").mockImplementation((path) => {
    if (path === "/account") {
      return Promise.resolve({
        data: {
          username: "admin",
          password_source: "generated",
          bootstrap: true,
          changed_at: 1761600000,
        },
      });
    }
    return Promise.reject(new Error(`Unexpected GET ${path}`));
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState({}, "", "/");
});

describe("management account settings", () => {
  it("opens ?tab=account, saves a username and adopts the new CSRF token", async () => {
    const put = vi.spyOn(api, "put").mockResolvedValue({
      data: { username: "operator", bootstrap: false, csrf_token: "new-csrf" },
    });
    renderAccount();
    expect(await screen.findByRole("heading", { name: "管理账户" })).toBeTruthy();
    fireEvent.change(await screen.findByLabelText("用户名", { exact: true }), {
      target: { value: "operator" },
    });
    fireEvent.change(screen.getByLabelText("当前管理密码", { exact: true }), {
      target: { value: "old-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存账户更改" }));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/account", {
        current_password: "old-password",
        username: "operator",
      }),
    );
    await waitFor(() => expect(useSession.getState().csrf).toBe("new-csrf"));
    expect(useSession.getState().bootstrap).toBe(false);
    expect(screen.getByText(/其他已登录会话已退出/)).toBeTruthy();
  });

  it("rejects mismatched new passwords without calling the API", async () => {
    const put = vi.spyOn(api, "put");
    renderAccount();
    fireEvent.change(await screen.findByLabelText("当前管理密码", { exact: true }), {
      target: { value: "old-password" },
    });
    fireEvent.change(screen.getByLabelText("新密码", { exact: true }), {
      target: { value: "new-password" },
    });
    fireEvent.change(screen.getByLabelText("再次输入新密码", { exact: true }), {
      target: { value: "different-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存账户更改" }));
    expect(await screen.findByText("两次输入的新密码不一致")).toBeTruthy();
    expect(put).not.toHaveBeenCalled();
  });

  it("revokes every session after confirmation and returns to anonymous", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const remove = vi.spyOn(api, "delete").mockResolvedValue({ data: { ok: true } });
    renderAccount();
    fireEvent.click(await screen.findByRole("button", { name: "退出所有会话" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("/sessions"));
    await waitFor(() => expect(useSession.getState().status).toBe("anonymous"));
    expect(useSession.getState().csrf).toBeNull();
    expect(useSession.getState().bootstrap).toBe(false);
  });

  it("dismisses the bootstrap banner without changing session state", async () => {
    window.history.replaceState({}, "", "/dashboard/settings?tab=account");
    render(
      <MemoryRouter initialEntries={["/dashboard/settings?tab=account"]}>
        <AppRoutes />
      </MemoryRouter>,
    );
    expect(await screen.findByText("当前使用初始密码，请尽快修改。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "关闭通知" }));
    expect(screen.queryByText("当前使用初始密码，请尽快修改。")).toBeNull();
    expect(await screen.findByRole("heading", { name: "管理账户" })).toBeTruthy();
    expect(useSession.getState().bootstrap).toBe(true);
  });
});
