import axios from "axios";
import { create } from "zustand";
import { useCallback, useEffect, useState } from "react";

export type RecordValue = Record<string, unknown>;
export type Session = { authenticated: true; csrf_token: string };
type AuthState = {
  status: "checking" | "authenticated" | "anonymous" | "error";
  csrf: string | null;
  error: string | null;
};
export const useSession = create<AuthState>(() => ({
  status: "checking",
  csrf: null,
  error: null,
}));
export const api = axios.create({ baseURL: "/admin", withCredentials: true, timeout: 20000 });
api.interceptors.request.use((config) => {
  const csrf = useSession.getState().csrf;
  if (csrf) config.headers.set("X-CSRF-Token", csrf);
  return config;
});
api.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    if (axios.isAxiosError(error) && error.response?.status === 401) {
      useSession.setState({ status: "anonymous", csrf: null, error: null });
    }
    return Promise.reject(error);
  },
);
export function object(value: unknown, context = "响应"): RecordValue {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw new Error(`${context}格式不符合接口契约`);
  return value as RecordValue;
}
export function list(value: unknown, context = "列表"): RecordValue[] {
  if (!Array.isArray(value)) throw new Error(`${context}缺失或格式错误`);
  return value.map((item) => object(item, context));
}
export function text(value: unknown): string {
  if (value === undefined || value === null) return "未知";
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint")
    return `${value}`;
  return "未知";
}
export function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
export function metric(value: unknown): string {
  const n = number(value);
  return n === null
    ? "未知"
    : new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(n);
}
export function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const bodyData: unknown = error.response?.data;
    const serverMessage =
      bodyData &&
      typeof bodyData === "object" &&
      "error" in bodyData &&
      bodyData.error &&
      typeof bodyData.error === "object" &&
      "message" in bodyData.error &&
      typeof bodyData.error.message === "string"
        ? bodyData.error.message
        : null;
    if (error.response?.status === 409) {
      if (serverMessage?.includes("凭证")) return serverMessage;
      const body: unknown = error.response.data;
      if (
        body &&
        typeof body === "object" &&
        "detail" in body &&
        body.detail &&
        typeof body.detail === "object" &&
        "models" in body.detail
      )
        return `凭证仍被模型规则引用，请先解除绑定：${text(body.detail.models)}`;
      return "保存冲突：配置已被其他操作更新。请关闭编辑并刷新后重新修改。";
    }
    if (error.response?.status === 401) return "会话已失效，请重新登录。";
    if (error.response?.status === 403)
      return serverMessage ?? "操作被拒绝：请检查当前 key、管理锁定或重新登录刷新 CSRF。";
    if (serverMessage) return serverMessage;
    const data: unknown = error.response?.data;
    if (data && typeof data === "object" && "detail" in data) {
      const detail: unknown = data.detail;
      if (typeof detail === "string") return detail;
      if (
        detail &&
        typeof detail === "object" &&
        "message" in detail &&
        typeof detail.message === "string"
      )
        return detail.message;
      if (detail && typeof detail === "object" && "error" in detail) {
        const nested: unknown = detail.error;
        if (
          nested &&
          typeof nested === "object" &&
          "message" in nested &&
          typeof nested.message === "string"
        )
          return nested.message;
      }
    }
    return error.response
      ? `请求失败（HTTP ${error.response.status}），请检查服务状态。`
      : "无法连接管理 API，请检查网络或服务状态后重试。";
  }
  return error instanceof Error ? error.message : "操作失败，请重试。";
}
export async function establishSession(apiKey?: string) {
  try {
    const response =
      apiKey === undefined
        ? await api.get<unknown>("/session")
        : await api.post<unknown>("/session", { api_key: apiKey });
    const data = object(response.data, "会话");
    if (data.authenticated === false) {
      useSession.setState({ status: "anonymous", csrf: null, error: null });
      return;
    }
    if (data.authenticated !== true || typeof data.csrf_token !== "string" || !data.csrf_token)
      throw new Error("会话响应缺少认证状态或 CSRF token");
    useSession.setState({ status: "authenticated", csrf: data.csrf_token, error: null });
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 401) {
      useSession.setState({ status: "anonymous", csrf: null, error: null });
      if (apiKey !== undefined) throw new Error("API key 无效或管理界面未启用。");
      return;
    }
    useSession.setState({ status: "error", csrf: null, error: errorMessage(error) });
    if (apiKey !== undefined) throw error;
  }
}
export function useResource<T>(path: string, normalize: (value: unknown) => T) {
  const [state, setState] = useState<{ data: T | null; loading: boolean; error: string | null }>({
    data: null,
    loading: true,
    error: null,
  });
  const [revision, setRevision] = useState(0);
  const reload = useCallback(() => setRevision((v) => v + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    setState({ data: null, loading: true, error: null });
    void api
      .get<unknown>(path, { signal: controller.signal })
      .then((response) => {
        if (!controller.signal.aborted)
          setState({ data: normalize(response.data), loading: false, error: null });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setState({ data: null, loading: false, error: errorMessage(error) });
      });
    return () => controller.abort();
  }, [path, revision, normalize]);
  return { ...state, reload };
}
export type ModelRule = {
  id: string;
  public_id: string;
  enabled: boolean;
  keep_original: boolean;
  region: string;
  profile: string;
  credential_ids: string[];
  credits?: unknown;
  credits_by_profile?: unknown;
};
export function modelResponse(value: unknown): { revision: number; models: ModelRule[] } {
  const data = object(value);
  if (typeof data.revision !== "number") throw new Error("模型响应缺少 revision");
  const models = list(data.models).map((m) => {
    if (
      typeof m.id !== "string" ||
      typeof m.enabled !== "boolean" ||
      typeof m.keep_original !== "boolean" ||
      !Array.isArray(m.credential_ids) ||
      !m.credential_ids.every((id) => typeof id === "string")
    )
      throw new Error("模型规则字段不完整");
    return {
      ...m,
      id: m.id,
      public_id: typeof m.public_id === "string" ? m.public_id : m.id,
      enabled: m.enabled,
      keep_original: m.keep_original,
      region: typeof m.region === "string" ? m.region : "",
      profile: typeof m.profile === "string" ? m.profile : "",
      credential_ids: m.credential_ids as string[],
    };
  });
  return { revision: data.revision, models };
}
export type Credential = RecordValue & { id: string; name: string | null };
export function credentialResponse(value: unknown): Credential[] {
  return list(object(value).credentials, "凭证").map((c) => {
    const id = c.account_key ?? c.id;
    if (typeof id !== "string" || !id) throw new Error("凭证缺少公开 account_key");
    const name = c.name ?? c.filename;
    return { ...c, id, name: typeof name === "string" && !/[\\/]/.test(name) ? name : null };
  });
}
