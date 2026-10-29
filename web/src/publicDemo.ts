import {
  AxiosError,
  getAdapter,
  type AxiosAdapter,
  type AxiosInstance,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from "axios";

type Json = Record<string, any>;
type State = {
  active: boolean;
  revision: number | null;
  serverBase: string;
  assets: Map<string, Json>;
  deletedAssets: Map<string, Json>;
  tasks: Map<string, Json>;
  taskOverrides: Map<string, Json>;
  activeDelta: number;
  settings: Json | null;
  settingValues: Json;
  serverModels: Json[];
  modelRules: Map<string, Json>;
  deletedModels: Set<string>;
  customSequence: number;
  account: Json;
  keys: Json[];
  keySequence: number;
};

const PREVIEW_HEADER = "x-codebuddy-preview";
const LOCAL_SETTINGS = new Set([
  "media_public_base_url",
  "media_max_total_mb",
  "media_ttl_hours",
  "model_guard",
  "model_capability_guard",
]);
const MEDIA_FIELDS = [
  "media_kinds",
  "media_sources",
  "media_defaults",
  "capabilities",
  "limits",
  "metadata_by_profile",
];
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
const initialState = (): State => ({
  active: false,
  revision: null,
  serverBase: "",
  assets: new Map(),
  deletedAssets: new Map(),
  tasks: new Map(),
  taskOverrides: new Map(),
  activeDelta: 0,
  settings: null,
  settingValues: {},
  serverModels: [],
  modelRules: new Map(),
  deletedModels: new Set(),
  customSequence: 0,
  account: {
    username: "admin",
    password_source: "external",
    bootstrap: true,
    changed_at: 1761600000,
  },
  keys: [],
  keySequence: 0,
});

function response(config: InternalAxiosRequestConfig, data: unknown, status = 200): AxiosResponse {
  return {
    data,
    status,
    statusText: String(status),
    headers: { [PREVIEW_HEADER]: "public" },
    config,
  };
}

function requestBody(config: InternalAxiosRequestConfig): Json {
  if (typeof config.data === "string") return JSON.parse(config.data);
  return (config.data || {}) as Json;
}

function notePublic(result: AxiosResponse, state: State) {
  const headers = result.headers as any;
  const marker = headers?.get?.(PREVIEW_HEADER) ?? headers?.[PREVIEW_HEADER];
  if (marker === "public") state.active = true;
  return result;
}

function noteRevision(state: State, value: unknown) {
  if (state.revision === null && Number.isInteger(value)) state.revision = Number(value);
}

function nextRevision(state: State, fallback: unknown) {
  const base = state.revision ?? (Number.isInteger(fallback) ? Number(fallback) : 0);
  state.revision = base + 1;
  return state.revision;
}

function effectiveBase(state: State): string {
  return typeof state.settingValues.media_public_base_url === "string"
    ? state.settingValues.media_public_base_url
    : state.serverBase;
}

function assetView(item: Json, state: State): Json {
  const next = clone(item);
  const base = effectiveBase(state).replace(/\/+$/, "");
  if (base)
    next.public_url = `${base}/v1/media/${next.kind === "video" ? "videos" : "images"}/${next.id}`;
  else delete next.public_url;
  return next;
}

function settingsView(state: State): Json {
  const data = clone(state.settings || { items: [], audit: {}, session: {} });
  data.revision = state.revision ?? data.revision ?? 0;
  data.items = (data.items || []).map((item: Json) =>
    Object.hasOwn(state.settingValues, item.key)
      ? { ...item, value: state.settingValues[item.key] }
      : item,
  );
  return data;
}

function summaryView(raw: Json, state: State): Json {
  const data = clone(raw);
  data.base_url = effectiveBase(state);
  data.configured = Boolean(data.base_url);
  if (Object.hasOwn(state.settingValues, "media_max_total_mb"))
    data.limit_bytes = Number(state.settingValues.media_max_total_mb) * 1048576;
  if (Object.hasOwn(state.settingValues, "media_ttl_hours"))
    data.ttl_hours = state.settingValues.media_ttl_hours;
  const usage = data.usage || {
    total_bytes: 0,
    images: { count: 0, bytes: 0 },
    videos: { count: 0, bytes: 0 },
  };
  for (const item of state.deletedAssets.values()) {
    const group = item.kind === "video" ? usage.videos : usage.images;
    if (group) {
      group.count = Math.max(0, Number(group.count || 0) - 1);
      group.bytes = Math.max(0, Number(group.bytes || 0) - Number(item.size || 0));
    }
    usage.total_bytes = Math.max(0, Number(usage.total_bytes || 0) - Number(item.size || 0));
  }
  data.usage = usage;
  if (data.tasks)
    data.tasks.active = Math.max(0, Number(data.tasks.active || 0) + state.activeDelta);
  return data;
}

function inheritedFacts(state: State, upstream: string): Json {
  const source = state.serverModels.find((item) => item.id === upstream);
  if (!source) return {};
  return Object.fromEntries(
    MEDIA_FIELDS.filter((key) => key in source).map((key) => [key, clone(source[key])]),
  );
}

function modelsView(raw: Json, state: State): Json {
  const data = clone(raw);
  const output = [];
  const present = new Set<string>();
  for (const item of data.models || []) {
    if (state.deletedModels.has(item.id)) continue;
    present.add(item.id);
    const local = state.modelRules.get(item.id);
    output.push(local ? { ...item, ...clone(local), id: item.id } : item);
  }
  for (const [id, rule] of state.modelRules) {
    if (present.has(id) || state.deletedModels.has(id)) continue;
    output.push({ ...inheritedFacts(state, String(rule.upstream_id || "")), ...clone(rule), id });
  }
  data.models = output;
  data.revision = state.revision ?? data.revision;
  for (const key of ["model_guard", "model_capability_guard"]) {
    if (Object.hasOwn(state.settingValues, key)) data[key] = state.settingValues[key];
  }
  return data;
}

function notFound(config: InternalAxiosRequestConfig) {
  const result = response(
    config,
    { error: { message: "媒体资源不存在或已清理", type: "not_found_error" } },
    404,
  );
  throw new AxiosError("媒体资源不存在或已清理", "ERR_BAD_REQUEST", config, undefined, result);
}

function keyView(item: Json): Json {
  const result = clone(item);
  delete result.secret;
  if (item.type === "standard") result.masked = `${item.prefix}…${item.secret.slice(-4)}`;
  result.usage = null;
  return result;
}

function nextKey(state: State) {
  const sequence = ++state.keySequence;
  return {
    id: `k_${String(sequence).padStart(12, "0")}`,
    secret: `cb-${String(sequence).padStart(32, "0")}`,
  };
}

function localWrite(
  config: InternalAxiosRequestConfig,
  path: string,
  method: string,
  state: State,
): AxiosResponse | null {
  const body = requestBody(config);
  if (path === "/session") {
    if (method === "GET" || method === "POST")
      return response(config, {
        authenticated: true,
        csrf_token: "local-session-csrf",
        bootstrap: state.account.bootstrap,
      });
    if (method === "DELETE") return response(config, { authenticated: false });
  }
  if (path === "/sessions" && method === "DELETE") return response(config, { ok: true });
  if (path === "/account") {
    if (method === "GET") return response(config, clone(state.account));
    if (method === "PUT") {
      if (typeof body.username === "string") state.account.username = body.username;
      if (typeof body.new_password === "string") state.account.password_source = "user";
      state.account.bootstrap = false;
      state.account.changed_at = Date.now() / 1000;
      return response(config, {
        username: state.account.username,
        bootstrap: false,
        csrf_token: "local-account-csrf",
      });
    }
  }
  if (path === "/keys") {
    if (method === "GET")
      return response(config, {
        external: { type: "admin", source: "external", origin: "cli", occupied: true },
        items: state.keys.map(keyView),
      });
    if (method === "POST") {
      const generated = nextKey(state);
      const item: Json = {
        id: generated.id,
        name: body.name,
        type: body.type,
        secret: generated.secret,
        prefix: generated.secret.slice(0, 8),
        enabled: true,
        expires_at: body.expires_at ?? null,
        ip_allow: body.ip_allow ?? null,
        rpm: body.rpm ?? null,
        models: body.models ?? null,
        created_at: Date.now() / 1000,
        last_used_at: null,
        version: 1,
      };
      state.keys.push(item);
      return response(config, { ...keyView(item), secret: generated.secret });
    }
  }
  const keyMatch = path.match(/^\/keys\/([^/]+)(?:\/(rotate|reveal))?$/);
  if (keyMatch) {
    const id = decodeURIComponent(keyMatch[1]!);
    const index = state.keys.findIndex((item) => item.id === id);
    if (index < 0) notFound(config);
    const item = state.keys[index]!;
    const action = keyMatch[2];
    if (method === "PATCH") {
      for (const field of ["name", "enabled", "expires_at", "ip_allow", "rpm", "models"]) {
        if (Object.hasOwn(body, field)) item[field] = clone(body[field]);
      }
      item.version += 1;
      return response(config, keyView(item));
    }
    if (method === "DELETE") {
      state.keys.splice(index, 1);
      return response(config, { ok: true });
    }
    if (method === "POST" && action === "rotate") {
      const generated = nextKey(state);
      item.secret = generated.secret;
      item.prefix = generated.secret.slice(0, 8);
      item.version += 1;
      return response(config, { ...keyView(item), secret: generated.secret });
    }
    if (method === "POST" && action === "reveal" && item.type === "standard")
      return response(config, { secret: item.secret });
  }
  if (method === "DELETE" && path.startsWith("/media/assets/")) {
    const id = decodeURIComponent(path.slice("/media/assets/".length));
    if (!id || id.includes("/")) return null;
    state.deletedAssets.set(id, state.assets.get(id) || { id });
    return response(config, { deleted: id });
  }
  const taskMatch = path.match(/^\/media\/tasks\/([^/]+)\/refresh$/);
  if (method === "POST" && taskMatch) {
    const id = decodeURIComponent(taskMatch[1]!);
    const current = state.tasks.get(id) || { task_id: id, model: "", created: Date.now() / 1000 };
    const previous = state.taskOverrides.get(id) || current;
    if (["queued", "in_progress"].includes(previous.status)) state.activeDelta -= 1;
    const video = [...state.assets.values()].find(
      (item) => item.kind === "video" && !state.deletedAssets.has(item.id),
    );
    const next: Json = {
      ...clone(previous),
      status: "completed",
      error: null,
      media_id: video?.id || null,
    };
    state.taskOverrides.set(id, next);
    return response(config, { ...next, id, object: "video", created_at: next.created });
  }
  if (method === "PATCH" && path === "/settings") {
    const values = body.values || {};
    const keys = Object.keys(values);
    if (!keys.length || keys.some((key) => !LOCAL_SETTINGS.has(key))) return null;
    Object.assign(state.settingValues, clone(values));
    nextRevision(state, body.revision);
    return response(config, settingsView(state));
  }
  if (method === "POST" && path === "/models") {
    const id = `custom:browser-${++state.customSequence}`;
    const rule: Json = { ...clone(body), id, custom: true };
    delete rule.revision;
    state.modelRules.set(id, rule);
    const revision = nextRevision(state, body.revision);
    return response(config, { revision, model: rule }, 201);
  }
  const modelMatch = path.match(/^\/models\/([^/]+)$/);
  if (modelMatch && method === "PUT") {
    const id = decodeURIComponent(modelMatch[1]!);
    const rule: Json = { ...clone(body), id };
    delete rule.revision;
    state.modelRules.set(id, rule);
    const revision = nextRevision(state, body.revision);
    return response(config, { revision, model: rule });
  }
  if (modelMatch && method === "DELETE") {
    const id = decodeURIComponent(modelMatch[1]!);
    state.deletedModels.add(id);
    const revision = nextRevision(state, body.revision);
    return response(config, { revision, ok: true });
  }
  return null;
}

function localRead(
  config: InternalAxiosRequestConfig,
  result: AxiosResponse,
  path: string,
  method: string,
  state: State,
): AxiosResponse {
  if (method !== "GET" || !result.data) return result;
  let data: unknown = result.data;
  if (typeof data === "string") {
    try {
      data = JSON.parse(data);
    } catch {
      return result;
    }
  }
  if (!data || typeof data !== "object") return result;
  const raw = data as Json;
  if (path === "/media") {
    state.serverBase = typeof raw.base_url === "string" ? raw.base_url : state.serverBase;
    return { ...result, data: summaryView(raw, state) };
  }
  if (path === "/media/assets") {
    for (const item of raw.items || []) state.assets.set(item.id, clone(item));
    const visibleItems = (raw.items || [])
      .filter((item: Json) => !state.deletedAssets.has(item.id))
      .map((item: Json) => assetView(item, state));
    return { ...result, data: { ...raw, items: visibleItems } };
  }
  if (path === "/media/tasks") {
    for (const item of raw.items || []) state.tasks.set(item.task_id, clone(item));
    return {
      ...result,
      data: {
        ...raw,
        items: (raw.items || []).map((item: Json) => {
          const next = { ...item, ...clone(state.taskOverrides.get(item.task_id) || {}) };
          if (state.deletedAssets.has(next.media_id)) next.media_id = null;
          return next;
        }),
      },
    };
  }
  if (path === "/settings") {
    noteRevision(state, raw.revision);
    state.settings = clone(raw);
    return { ...result, data: settingsView(state) };
  }
  if (path === "/models") {
    noteRevision(state, raw.revision);
    state.serverModels = clone(raw.models || []);
    return { ...result, data: modelsView(raw, state) };
  }
  if (path.startsWith("/media/assets/") && !path.endsWith("/content")) {
    const id = decodeURIComponent(path.slice("/media/assets/".length));
    if (state.deletedAssets.has(id)) notFound(config);
    state.assets.set(id, clone(raw));
    return { ...result, data: assetView(raw, state) };
  }
  return result;
}

/** Run shared-preview mutations in this tab; a reload creates a fresh state. */
export function createPublicDemoAdapter(delegate: AxiosAdapter): AxiosAdapter {
  const state = initialState();
  return async (config) => {
    const path = new URL(config.url || "", "http://preview.local").pathname;
    const method = (config.method || "get").toUpperCase();
    if (state.active) {
      const local = localWrite(config, path, method, state);
      if (local) return notePublic(local, state);
    }
    const result = await delegate(config);
    const wasActive = state.active;
    notePublic(result, state);
    return wasActive ? localRead(config, result, path, method, state) : result;
  };
}

export function installPublicDemoAdapter(api: AxiosInstance) {
  const delegate = api.defaults.adapter;
  if (delegate) api.defaults.adapter = createPublicDemoAdapter(getAdapter(delegate));
}
