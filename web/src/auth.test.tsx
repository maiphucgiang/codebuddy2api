import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from "axios";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";
import { AppRoutes } from "./App";
import { api, useSession } from "./api";

const originalAdapter = api.defaults.adapter;
const originalCredentials = Object.getOwnPropertyDescriptor(navigator, "credentials");
const credentialStore = vi.fn();

function response(data: unknown, config: InternalAxiosRequestConfig, status = 200) {
  return { data, status, statusText: "OK", headers: new AxiosHeaders(), config };
}

function failure(config: InternalAxiosRequestConfig, status: number, message: string) {
  return new AxiosError(
    message,
    "ERR_BAD_REQUEST",
    config,
    undefined,
    response({ error: { message } }, config, status),
  );
}

function renderLogin() {
  return render(
    <MemoryRouter initialEntries={["/dashboard/login"]}>
      <AppRoutes />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  useSession.setState({ status: "anonymous", csrf: null, bootstrap: false, error: null });
  credentialStore.mockReset();
  credentialStore.mockResolvedValue(undefined);
  Object.defineProperty(navigator, "credentials", {
    configurable: true,
    value: { store: credentialStore },
  });
  vi.stubGlobal(
    "PasswordCredential",
    class {
      constructor(public options: { id: string; password: string }) {}
    },
  );
});

afterEach(() => {
  api.defaults.adapter = originalAdapter;
  vi.unstubAllGlobals();
  if (originalCredentials) Object.defineProperty(navigator, "credentials", originalCredentials);
  else Reflect.deleteProperty(navigator, "credentials");
});

describe("account login", () => {
  it("submits username, password and remember without clearing values before success", async () => {
    let resolveSession: ((value: ReturnType<typeof response>) => void) | undefined;
    let requestBody = "";
    api.defaults.adapter = (config) => {
      if (config.url === "/session" && config.method === "post") {
        requestBody = String(config.data);
        return new Promise((resolve) => {
          resolveSession = resolve;
        });
      }
      return Promise.resolve(response({}, config));
    };
    renderLogin();
    const form = screen.getByRole("button", { name: "进入工作台" }).closest("form")!;
    expect(form).toHaveProperty("method", "post");
    expect(form).toHaveProperty("action", `${window.location.origin}/admin/session`);
    fireEvent.change(screen.getByLabelText("用户名", { exact: true }), {
      target: { value: "operator" },
    });
    fireEvent.change(screen.getByLabelText("密码", { exact: true }), {
      target: { value: "correct-horse" },
    });
    fireEvent.click(screen.getByLabelText("记住我", { exact: true }));
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));
    expect(screen.getByLabelText("密码", { exact: true })).toHaveProperty("value", "correct-horse");
    await waitFor(() => expect(resolveSession).toBeTypeOf("function"));
    resolveSession!(
      response(
        { authenticated: true, csrf_token: "csrf", bootstrap: false },
        {} as InternalAxiosRequestConfig,
      ),
    );
    await waitFor(() => expect(credentialStore).toHaveBeenCalledOnce());
    expect(JSON.parse(requestBody)).toEqual({
      username: "operator",
      password: "correct-horse",
      remember: true,
    });
    expect(credentialStore.mock.calls[0]![0].options).toEqual({
      id: "operator",
      password: "correct-horse",
    });
  });

  it("keeps the username, clears and focuses the password, and shakes after a 401", async () => {
    api.defaults.adapter = (config) => Promise.reject(failure(config, 401, "ignored"));
    const { container } = renderLogin();
    fireEvent.change(screen.getByLabelText("用户名", { exact: true }), {
      target: { value: "operator" },
    });
    fireEvent.change(screen.getByLabelText("密码", { exact: true }), {
      target: { value: "wrong-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));
    expect(await screen.findByText("用户名或密码错误")).toBeTruthy();
    expect(screen.getByLabelText("用户名", { exact: true })).toHaveProperty("value", "operator");
    const password = screen.getByLabelText("密码", { exact: true });
    expect(password).toHaveProperty("value", "");
    expect(password.getAttribute("aria-invalid")).toBe("true");
    await waitFor(() => expect(document.activeElement).toBe(password));
    expect(container.querySelector("form")!.parentElement!.className).toMatch(/loginShake/);
  });

  it("shows the server's rate-limit message", async () => {
    api.defaults.adapter = (config) => Promise.reject(failure(config, 429, "登录繁忙，请稍后重试"));
    renderLogin();
    fireEvent.change(screen.getByLabelText("密码", { exact: true }), {
      target: { value: "wrong-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "进入工作台" }));
    expect(await screen.findByText("登录繁忙，请稍后重试")).toBeTruthy();
  });

  it("toggles password visibility and reports Caps Lock", () => {
    renderLogin();
    const password = screen.getByLabelText("密码", { exact: true });
    const toggle = screen.getByRole("button", { name: "显示密码" });
    fireEvent.click(toggle);
    expect(password).toHaveProperty("type", "text");
    expect(screen.getByRole("button", { name: "隐藏密码" }).getAttribute("aria-pressed")).toBe(
      "true",
    );
    fireEvent.keyDown(password, { key: "A", modifierCapsLock: true });
    expect(screen.getByRole("status").textContent).toContain("Caps Lock");
    fireEvent.keyUp(password, { key: "A", modifierCapsLock: false });
    expect(screen.queryByRole("status")).toBeNull();
  });
});
