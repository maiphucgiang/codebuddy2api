import { describe, expect, it, vi } from "vite-plus/test";
import axios from "axios";
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from "axios";
import { createPublicDemoAdapter, installPublicDemoAdapter } from "./publicDemo";

function config(method: string, url: string, data?: unknown): InternalAxiosRequestConfig {
  return {
    method,
    url,
    data: data === undefined ? undefined : JSON.stringify(data),
    headers: {},
  } as InternalAxiosRequestConfig;
}

function reply(config: InternalAxiosRequestConfig, data: unknown, status = 200): AxiosResponse {
  return {
    data,
    status,
    statusText: String(status),
    headers: { "x-codebuddy-preview": "public" },
    config,
  };
}

const image = {
  id: "img_demo",
  kind: "image",
  mime: "image/png",
  size: 400,
  sha256: "a".repeat(64),
  model: "hy-image-3.5",
  created_at: 100,
  expires_at: 200,
};
const video = {
  ...image,
  id: "vid_demo",
  kind: "video",
  mime: "video/mp4",
  size: 200,
  model: "minimax-video-h3",
};
const modelRule = {
  enabled: true,
  keep_original: false,
  region: "",
  profile: "",
  credential_ids: [],
};

describe("public preview browser-local adapter", () => {
  it("keeps media deletion, settings and task refresh local while allowing reads", async () => {
    const calls: InternalAxiosRequestConfig[] = [];
    const delegate = vi.fn(async (request: InternalAxiosRequestConfig) => {
      calls.push(request);
      const path = new URL(request.url || "", "http://preview.local").pathname;
      if (path === "/media") {
        return reply(request, {
          configured: false,
          base_url: "",
          available: true,
          limit_bytes: 1000,
          ttl_hours: 72,
          usage: {
            total_bytes: 600,
            images: { count: 1, bytes: 400 },
            videos: { count: 1, bytes: 200 },
          },
          tasks: { active: 1, failed: 0 },
        });
      }
      if (path === "/media/assets")
        return reply(
          request,
          JSON.stringify({ items: [image, video], has_more: false, next_cursor: null }),
        );
      if (path === "/media/tasks") {
        return reply(request, {
          items: [
            {
              task_id: "task_demo",
              model: "minimax-video-h3",
              status: "in_progress",
              created: 100,
              media_id: null,
              error: null,
            },
          ],
          has_more: false,
          next_cursor: null,
        });
      }
      if (path === "/settings") {
        return reply(request, {
          revision: 4,
          items: [{ key: "media_public_base_url", value: "", locked: false }],
          audit: {},
          session: {},
        });
      }
      if (path === "/media/assets/vid_demo") return reply(request, video);
      return reply(request, { ok: true });
    }) as AxiosAdapter;
    const adapter = createPublicDemoAdapter(delegate);

    await adapter(config("get", "/media"));
    await adapter(config("get", "/media/assets?kind=image"));
    await adapter(config("get", "/media/tasks"));
    await adapter(config("delete", "/media/assets/img_demo"));
    expect(calls.filter((request) => request.method === "delete")).toHaveLength(0);

    const assets = await adapter(config("get", "/media/assets"));
    expect(assets.data.items.map((item: { id: string }) => item.id)).toEqual(["vid_demo"]);
    const summary = await adapter(config("get", "/media"));
    expect(summary.data.usage).toEqual({
      total_bytes: 200,
      images: { count: 0, bytes: 0 },
      videos: { count: 1, bytes: 200 },
    });

    const refreshed = await adapter(config("post", "/media/tasks/task_demo/refresh"));
    expect(refreshed.data.status).toBe("completed");
    expect(refreshed.data.media_id).toBe("vid_demo");
    expect(calls.filter((request) => request.method === "post")).toHaveLength(0);
    expect((await adapter(config("get", "/media"))).data.tasks.active).toBe(0);

    const patched = await adapter(
      config("patch", "/settings", {
        revision: 4,
        values: { media_public_base_url: "https://demo.example" },
      }),
    );
    expect(patched.data.revision).toBe(5);
    expect(calls.filter((request) => request.method === "patch")).toHaveLength(0);
    const settings = await adapter(config("get", "/settings"));
    expect(settings.data.items[0].value).toBe("https://demo.example");
    expect((await adapter(config("get", "/media"))).data.configured).toBe(true);
    expect((await adapter(config("get", "/media/assets/vid_demo"))).data.public_url).toBe(
      "https://demo.example/v1/media/videos/vid_demo",
    );

    await adapter(config("delete", "/credentials/demo"));
    expect(calls.at(-1)?.url).toBe("/credentials/demo");
  });

  it("keeps model edits in the tab and resets from a fresh adapter", async () => {
    const calls: InternalAxiosRequestConfig[] = [];
    const models = [
      {
        id: "hy-image-3.5",
        ...modelRule,
        public_id: "hy-image-3.5",
        upstream_id: "hy-image-3.5",
        media_kinds: ["image", "edit"],
      },
      {
        id: "minimax-video-h3",
        ...modelRule,
        public_id: "minimax-video-h3",
        upstream_id: "minimax-video-h3",
        media_kinds: ["video"],
      },
    ];
    const delegate = vi.fn(async (request: InternalAxiosRequestConfig) => {
      calls.push(request);
      const path = new URL(request.url || "", "http://preview.local").pathname;
      if (path === "/models") return reply(request, { revision: 7, models, model_guard: true });
      return reply(request, { candidates: [], excluded: [] });
    }) as AxiosAdapter;
    const adapter = createPublicDemoAdapter(delegate);
    await adapter(config("get", "/models"));

    await adapter(
      config("put", "/models/hy-image-3.5", {
        ...modelRule,
        revision: 7,
        public_id: "hy-local",
        upstream_id: "hy-image-3.5",
      }),
    );
    let view = await adapter(config("get", "/models"));
    expect(view.data.revision).toBe(8);
    expect(view.data.models[0].public_id).toBe("hy-local");
    expect(view.data.models[0].media_kinds).toEqual(["image", "edit"]);

    const created = await adapter(
      config("post", "/models", {
        ...modelRule,
        revision: 8,
        public_id: "local-video",
        upstream_id: "minimax-video-h3",
      }),
    );
    expect(created.status).toBe(201);
    view = await adapter(config("get", "/models"));
    expect(view.data.models.at(-1)).toMatchObject({
      public_id: "local-video",
      media_kinds: ["video"],
      custom: true,
    });
    await adapter(config("delete", "/models/custom:browser-1", { revision: 9 }));
    view = await adapter(config("get", "/models"));
    expect(view.data.models).toHaveLength(2);

    await adapter(config("patch", "/settings", { revision: 10, values: { model_guard: false } }));
    view = await adapter(config("get", "/models"));
    expect(view.data.model_guard).toBe(false);
    expect(
      calls
        .filter((request) => ["put", "post", "patch", "delete"].includes(request.method || ""))
        .map((request) => request.url),
    ).toEqual([]);

    const fresh = createPublicDemoAdapter(delegate);
    const initial = await fresh(config("get", "/models"));
    expect(initial.data.revision).toBe(7);
    expect(initial.data.models[0].public_id).toBe("hy-image-3.5");
    expect(initial.data.models).toHaveLength(2);
  });
  it("supports account, session and API key workflows locally", async () => {
    const delegate = vi.fn(async (request: InternalAxiosRequestConfig) =>
      reply(request, { authenticated: true, csrf_token: "initial-csrf", bootstrap: true }),
    ) as AxiosAdapter;
    const adapter = createPublicDemoAdapter(delegate);
    await adapter(config("get", "/session"));

    const account = await adapter(config("get", "/account"));
    expect(account.data).toMatchObject({ username: "admin", bootstrap: true });
    const changed = await adapter(
      config("put", "/account", { current_password: "preview", username: "operator" }),
    );
    expect(changed.data).toMatchObject({
      username: "operator",
      bootstrap: false,
      csrf_token: "local-account-csrf",
    });

    const external = await adapter(config("get", "/keys"));
    expect(external.data.external).toMatchObject({ origin: "cli", occupied: true });
    const created = await adapter(
      config("post", "/keys", {
        name: "Demo key",
        type: "standard",
        expires_at: null,
        ip_allow: null,
        rpm: 60,
        models: ["auto"],
      }),
    );
    const id = created.data.id as string;
    const firstSecret = created.data.secret as string;
    expect(firstSecret).toMatch(/^cb-/);
    expect(created.data.rpm).toBe(60);
    expect((await adapter(config("post", `/keys/${id}/reveal`))).data.secret).toBe(firstSecret);
    const updated = await adapter(
      config("patch", `/keys/${id}`, { version: 1, name: "Renamed", enabled: false }),
    );
    expect(updated.data).toMatchObject({ name: "Renamed", enabled: false, version: 2 });
    const rotated = await adapter(config("post", `/keys/${id}/rotate`, { version: 2 }));
    expect(rotated.data.secret).not.toBe(firstSecret);
    expect(rotated.data.version).toBe(3);
    expect((await adapter(config("delete", `/keys/${id}`, { version: 3 }))).data.ok).toBe(true);
    expect((await adapter(config("get", "/keys"))).data.items).toEqual([]);
    expect((await adapter(config("delete", "/sessions"))).data.ok).toBe(true);
    expect(delegate).toHaveBeenCalledTimes(1);
  });

  it("installs around the default adapter array", async () => {
    const delegate = vi.fn(async (request: InternalAxiosRequestConfig) =>
      reply(request, { configured: false, base_url: "", usage: {}, tasks: {} }),
    );
    const api = axios.create({ adapter: [delegate] });
    installPublicDemoAdapter(api);
    await api.get("/media");
    await api.delete("/media/assets/img_demo");
    expect(delegate).toHaveBeenCalledTimes(1);
  });
});
