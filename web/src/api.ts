import axios from "axios";
import { installPublicDemoAdapter } from "./publicDemo";
import { create } from "zustand";
import { useCallback, useEffect, useState } from "react";

export type RecordValue = Record<string, unknown>;
export type Session = { authenticated: boolean; csrf_token: string | null; bootstrap?: boolean };
type AuthState = {
  status: "checking" | "authenticated" | "anonymous" | "error";
  csrf: string | null;
  bootstrap: boolean;
  error: string | null;
};
export const useSession = create<AuthState>(() => ({
  status: "checking",
  csrf: null,
  bootstrap: false,
  error: null,
}));
export const api = axios.create({ baseURL: "/admin", withCredentials: true, timeout: 20000 });
installPublicDemoAdapter(api);
api.interceptors.request.use((config) => {
  const csrf = useSession.getState().csrf;
  if (csrf) config.headers.set("X-CSRF-Token", csrf);
  return config;
});
api.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    if (axios.isAxiosError(error) && error.response?.status === 401) {
      useSession.setState({ status: "anonymous", csrf: null, bootstrap: false, error: null });
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
  if (value === undefined || value === null) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint")
    return `${value}`;
  return "—";
}
export function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
export function metric(value: unknown): string {
  const n = number(value);
  return n === null ? "—" : new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(n);
}
// Axis-scale abbreviation: 1234 → 1.2K, 429669080 → 429.7M; exact values stay in tooltips.
export function compact(value: unknown): string {
  const n = number(value);
  if (n === null) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(n);
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
      ) {
        const models = body.detail.models;
        return `凭证仍被模型规则引用，请先解除绑定：${Array.isArray(models) ? models.map(text).join("、") : text(models)}`;
      }
      return "保存冲突：配置已被其他操作更新。请关闭编辑并刷新后重新修改。";
    }
    if (error.response?.status === 401) return "会话已失效，请重新登录。";
    if (error.response?.status === 403)
      return serverMessage ?? "操作被拒绝：请检查管理锁定或重新登录刷新 CSRF。";
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
export async function establishSession(credentials?: {
  username: string;
  password: string;
  remember: boolean;
}) {
  try {
    const response =
      credentials === undefined
        ? await api.get<unknown>("/session")
        : await api.post<unknown>("/session", credentials);
    const data = object(response.data, "会话");
    if (data.authenticated === false) {
      useSession.setState({ status: "anonymous", csrf: null, bootstrap: false, error: null });
      return;
    }
    if (data.authenticated !== true || typeof data.csrf_token !== "string" || !data.csrf_token)
      throw new Error("会话响应缺少认证状态或 CSRF token");
    useSession.setState({
      status: "authenticated",
      csrf: data.csrf_token,
      bootstrap: data.bootstrap === true,
      error: null,
    });
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 401) {
      useSession.setState({ status: "anonymous", csrf: null, bootstrap: false, error: null });
      if (credentials !== undefined) throw new Error("用户名或密码错误");
      return;
    }
    useSession.setState({
      status: "error",
      csrf: null,
      bootstrap: false,
      error: errorMessage(error),
    });
    if (credentials !== undefined) throw error;
  }
}
export function updateCsrf(token: string) {
  useSession.setState({ csrf: token });
}
// Session-level response cache for heavy pages (model routing): reopening the
// page renders the last snapshot instantly while a fresh fetch replaces it.
const resourceCache = new Map<string, unknown>();
useSession.subscribe((state, prev) => {
  if (state.status !== "authenticated" || state.csrf !== prev.csrf) resourceCache.clear();
});
export function useResource<T>(
  path: string,
  normalize: (value: unknown) => T,
  options?: { cache?: boolean },
) {
  const cached = options?.cache ? resourceCache.get(path) : undefined;
  const [state, setState] = useState<{
    path: string;
    data: T | null;
    loading: boolean;
    refreshing: boolean;
    error: string | null;
  }>({
    path,
    data: cached === undefined ? null : normalize(cached),
    loading: cached === undefined,
    refreshing: true,
    error: null,
  });
  const [revision, setRevision] = useState(0);
  const reload = useCallback(() => setRevision((v) => v + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    const cachedForPath = options?.cache ? resourceCache.get(path) : undefined;
    // Keep same-path data (or a cached snapshot) on screen during refresh;
    // clear stale data when switching to a different path without a cache hit.
    setState((prev) => {
      const data =
        prev.path === path
          ? prev.data
          : cachedForPath === undefined
            ? null
            : normalize(cachedForPath);
      return { path, data, loading: data === null, refreshing: true, error: null };
    });
    void api
      .get<unknown>(path, { signal: controller.signal })
      .then((response) => {
        if (controller.signal.aborted) return;
        if (options?.cache) resourceCache.set(path, response.data);
        setState({
          path,
          data: normalize(response.data),
          loading: false,
          refreshing: false,
          error: null,
        });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState((prev) => ({
          path,
          data: prev.path === path ? prev.data : null,
          loading: false,
          refreshing: false,
          error: errorMessage(error),
        }));
      });
    return () => controller.abort();
  }, [path, revision, normalize, options?.cache]);
  const currentData =
    state.path === path ? state.data : cached === undefined ? null : normalize(cached);
  return {
    data: currentData,
    loading: state.path === path ? state.loading : cached === undefined,
    // True during every fetch, including reloads that keep the previous data on screen.
    refreshing: state.path === path ? state.refreshing : true,
    error: state.path === path ? state.error : null,
    reload,
  };
}
export type ModelRoute = {
  upstream_id: string;
  weight: number | null;
  enabled: boolean;
  source: "builtin" | "cloud" | "manual";
};
export type ModelRule = {
  id: string;
  public_id: string;
  upstream_id?: string;
  routes?: ModelRoute[];
  custom?: boolean;
  available?: boolean;
  enabled: boolean;
  keep_original: boolean;
  region: string;
  profile: string;
  credential_ids: string[];
  credits?: unknown;
  credits_by_profile?: unknown;
  credits_override?: Record<string, number>;
  rate_details?: unknown;
  capabilities?: unknown;
  limits?: unknown;
  metadata_by_profile?: unknown;
  media_kinds?: string[];
  media_sources?: string[];
  media_defaults?: string[];
  embedding?: boolean;
  rerank?: boolean;
};
function rateOverride(value: unknown): Record<string, number> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const entries = Object.entries(value as RecordValue).filter(
    (entry): entry is [string, number] => typeof entry[1] === "number" && Number.isFinite(entry[1]),
  );
  return entries.length ? Object.fromEntries(entries) : undefined;
}
export function modelResponse(value: unknown): {
  revision: number;
  models: ModelRule[];
  model_guard?: boolean;
  model_guard_locked?: boolean;
  model_capability_guard?: boolean;
  model_capability_guard_locked?: boolean;
} {
  const data = object(value);
  if (typeof data.revision !== "number") throw new Error("模型响应缺少 revision");
  const models = list(data.models).map((m) => {
    if (
      typeof m.id !== "string" ||
      typeof m.enabled !== "boolean" ||
      typeof m.keep_original !== "boolean" ||
      (m.upstream_id !== undefined && typeof m.upstream_id !== "string") ||
      (m.custom !== undefined && typeof m.custom !== "boolean") ||
      !Array.isArray(m.credential_ids) ||
      !m.credential_ids.every((id) => typeof id === "string")
    )
      throw new Error("模型规则字段不完整");
    return {
      ...m,
      id: m.id,
      public_id: typeof m.public_id === "string" ? m.public_id : m.id,
      upstream_id: typeof m.upstream_id === "string" ? m.upstream_id : m.id,
      custom: m.custom === true,
      available: typeof m.available === "boolean" ? m.available : undefined,
      enabled: m.enabled,
      keep_original: m.keep_original,
      region: typeof m.region === "string" ? m.region : "",
      profile: typeof m.profile === "string" ? m.profile : "",
      credential_ids: m.credential_ids as string[],
      routes: Array.isArray(m.routes)
        ? (m.routes.filter(
            (route): route is RecordValue =>
              !!route &&
              typeof route === "object" &&
              !Array.isArray(route) &&
              typeof route.upstream_id === "string" &&
              typeof route.enabled === "boolean" &&
              (route.weight === null || typeof route.weight === "number"),
          ) as ModelRoute[])
        : undefined,
      credits_override: rateOverride(m.credits_override),
    };
  });
  return {
    revision: data.revision,
    models,
    model_guard: typeof data.model_guard === "boolean" ? data.model_guard : undefined,
    model_guard_locked:
      typeof data.model_guard_locked === "boolean" ? data.model_guard_locked : undefined,
    model_capability_guard:
      typeof data.model_capability_guard === "boolean" ? data.model_capability_guard : undefined,
    model_capability_guard_locked:
      typeof data.model_capability_guard_locked === "boolean"
        ? data.model_capability_guard_locked
        : undefined,
  };
}
export type CatalogProfileVersions = {
  accounts: number;
  distinct: number;
  versions: { models: number; accounts: number }[];
};
export type CatalogScan = {
  status: "idle" | "running" | "completed" | "failed";
  total: number;
  completed: number;
  success: number;
  failed: number;
  differences: number;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  profiles: Record<string, CatalogProfileVersions>;
  accounts: RecordValue[];
};
export function catalogScanResponse(value: unknown): CatalogScan {
  const data = object(value, "目录扫描");
  const count = (field: string) => {
    const parsed = number(data[field]);
    if (parsed === null || parsed < 0) throw new Error(`目录扫描缺少 ${field}`);
    return parsed;
  };
  const status = data.status;
  if (status !== "idle" && status !== "running" && status !== "completed" && status !== "failed")
    throw new Error("目录扫描状态无效");
  return {
    status,
    total: count("total"),
    completed: count("completed"),
    success: count("success"),
    failed: count("failed"),
    differences: count("differences"),
    started_at: number(data.started_at),
    finished_at: number(data.finished_at),
    error: typeof data.error === "string" && data.error ? data.error : null,
    profiles: parseCatalogProfiles(data.profiles),
    accounts: Array.isArray(data.accounts) ? list(data.accounts, "目录扫描账号") : [],
  };
}
function parseCatalogProfiles(value: unknown): Record<string, CatalogProfileVersions> {
  if (!value || typeof value !== "object") return {};
  const profiles: Record<string, CatalogProfileVersions> = {};
  for (const [name, row] of Object.entries(value as Record<string, unknown>)) {
    const item = object(row, "目录版本");
    profiles[name] = {
      accounts: number(item.accounts) ?? 0,
      distinct: number(item.distinct) ?? 0,
      versions: list(item.versions, "目录版本列表").map((entry) => {
        const version = object(entry, "目录版本条目");
        return { models: number(version.models) ?? 0, accounts: number(version.accounts) ?? 0 };
      }),
    };
  }
  return profiles;
}
export type Credential = RecordValue & { id: string; name: string | null };
export function credentialResponse(value: unknown): Credential[] {
  return list(object(value).credentials, "凭证").map((c) => {
    const id = c.account_key ?? c.id;
    if (typeof id !== "string" || !id) throw new Error("凭证缺少公开 account_key");
    const name = c.name ?? c.filename;
    for (const field of [
      "auto_checkin",
      "auto_travel",
      "auto_daily_chat",
      "auto_growth",
      "travel_supported",
      "trial_supported",
      "daily_chat_supported",
      "growth_supported",
    ])
      if (c[field] !== undefined && typeof c[field] !== "boolean")
        throw new Error("自动任务状态必须为布尔值");
    if (c.restricted !== undefined && typeof c.restricted !== "boolean")
      throw new Error("官方限制状态必须为布尔值");
    for (const field of ["checkin", "travel", "trial", "daily_chat"])
      if (c[field] !== undefined && c[field] !== null) object(c[field], "自动任务结果");
    if (c.growth_auto !== undefined && c.growth_auto !== null)
      object(c.growth_auto, "成长任务开关");
    return { ...c, id, name: typeof name === "string" && !/[\\/]/.test(name) ? name : null };
  });
}
