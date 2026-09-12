import { act, renderHook, waitFor } from "@testing-library/react";
import {
  AxiosError,
  AxiosHeaders,
  type AxiosAdapter,
  type InternalAxiosRequestConfig,
} from "axios";
import { afterEach, describe, expect, it } from "vite-plus/test";
import {
  api,
  credentialResponse,
  establishSession,
  errorMessage,
  metric,
  modelResponse,
  object,
  useResource,
  useSession,
} from "./api";
const adapter = api.defaults.adapter;
afterEach(() => {
  api.defaults.adapter = adapter;
  useSession.setState({ status: "checking", csrf: null, error: null });
});
function response(data: unknown, config: InternalAxiosRequestConfig) {
  return { data, status: 200, statusText: "OK", headers: new AxiosHeaders(), config };
}
describe("API and session state", () => {
  it("keeps unknown distinct from explicit zero", () => {
    expect(metric(undefined)).toBe("未知");
    expect(metric(null)).toBe("未知");
    expect(metric(0)).toBe("0");
  });
  it("rejects malformed DTO instead of fabricating empty success", () => {
    expect(() => modelResponse({ models: [] })).toThrow("revision");
    expect(() => credentialResponse({})).toThrow();
    expect(() => credentialResponse({ credentials: [{ name: "x.info" }] })).toThrow("account_key");
  });
  it("uses account_key and never uses an absolute filename for deletion", () => {
    const rows = credentialResponse({
      credentials: [{ account_key: "account-1", name: "/private/x.info" }],
    });
    expect(rows[0]).toMatchObject({ id: "account-1", name: null });
  });
  it("reads CSRF from session and includes credentials / CSRF in OAuth poll", async () => {
    const seen: InternalAxiosRequestConfig[] = [];
    api.defaults.adapter = ((config) => {
      seen.push(config);
      return Promise.resolve(
        response(
          config.url === "/session"
            ? { authenticated: true, csrf_token: "mock-csrf" }
            : { done: false },
          config,
        ),
      );
    }) satisfies AxiosAdapter;
    await establishSession("isolated-test-key");
    await api.get("/oauth/poll", { params: { login_id: "mock-login" } });
    expect(seen[0].withCredentials).toBe(true);
    expect(seen[1].headers.get("X-CSRF-Token")).toBe("mock-csrf");
    expect(useSession.getState().csrf).toBe("mock-csrf");
    expect(JSON.stringify(useSession.getState())).not.toContain("isolated-test-key");
    expect(localStorage.length).toBe(0);
  });
  it("reports session schema errors without authenticating", async () => {
    api.defaults.adapter = (config) => Promise.resolve(response({ authenticated: true }, config));
    await establishSession();
    expect(useSession.getState().status).toBe("error");
    expect(useSession.getState().csrf).toBeNull();
  });
  it("turns a 401 into anonymous and drops csrf", async () => {
    useSession.setState({ status: "authenticated", csrf: "old" });
    api.defaults.adapter = (config) =>
      Promise.reject(
        new AxiosError("unauthorized", "ERR_BAD_REQUEST", config, undefined, {
          ...response({}, config),
          status: 401,
        }),
      );
    await establishSession();
    expect(useSession.getState().status).toBe("anonymous");
    expect(useSession.getState().csrf).toBeNull();
  });
  it("exposes failed resource requests and allows retry", async () => {
    let failure = true;
    api.defaults.adapter = (config) =>
      failure
        ? Promise.reject(new Error("mock unavailable"))
        : Promise.resolve(response({ count: 0 }, config));
    const { result } = renderHook(() => useResource("/dashboard", object));
    await waitFor(() => expect(result.current.error).toBe("mock unavailable"));
    expect(result.current.data).toBeNull();
    failure = false;
    act(() => result.current.reload());
    await waitFor(() => expect(result.current.data).toEqual({ count: 0 }));
    expect(result.current.error).toBeNull();
  });
  it("identifies revision conflicts", () => {
    const config = { headers: new AxiosHeaders() } as InternalAxiosRequestConfig;
    expect(
      errorMessage(
        new AxiosError("conflict", "ERR_BAD_REQUEST", config, undefined, {
          ...response({}, config),
          status: 409,
        }),
      ),
    ).toContain("保存冲突");
  });
});
