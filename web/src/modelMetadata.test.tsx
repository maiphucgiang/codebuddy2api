import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { api, type ModelRule } from "./api";
import {
  CapabilityBadges,
  EffortChips,
  ModelMetadata,
  effortSummary,
  formatRate,
  formatTokens,
  modelSearchText,
  rateOf,
  specRange,
} from "./modelMetadata";
import { Models } from "./pages/Models";
import { choose } from "./test-utils";

const model: ModelRule = {
  id: "upstream",
  public_id: "public",
  upstream_id: "upstream",
  enabled: true,
  keep_original: false,
  region: "",
  profile: "",
  credential_ids: [],
  capabilities: {
    images: "mixed",
    tools: "supported",
    reasoning: "supported",
    thinking_disable: "unsupported",
  },
  limits: {
    maxInputTokens: { state: "known", value: 192000 },
    maxOutputTokens: { state: "mixed", value: null },
  },
  metadata_by_profile: {
    "cn-cli": [
      {
        id: "upstream",
        name: "Display model",
        descriptionZh: "中文说明",
        supportsImages: true,
        supportsToolCall: true,
        maxInputTokens: 192000,
        maxOutputTokens: 64000,
        iconUrl: "https://example.invalid/model.svg",
        reasoning: {
          supportedEfforts: ["low", "high"],
          defaultEffort: "high",
          canDisableThinking: false,
        },
        contextWindow: { defaultLength: 192000, supportedLengths: [64000, 192000] },
      },
    ],
    "intl-work": [
      {
        id: "upstream",
        name: "Display model",
        descriptionEn: "English description",
        supportsImages: false,
        maxOutputTokens: 32000,
      },
      { id: "upstream", supportsImages: true, maxOutputTokens: 64000 },
    ],
  },
};

afterEach(() => vi.restoreAllMocks());

describe("model declarations", () => {
  it("distinguishes unknown from unsupported and does not claim measured support", () => {
    const { rerender } = render(<CapabilityBadges model={{}} />);
    expect(screen.getByText("图片：未知")).toBeTruthy();
    expect(screen.queryByText("图片：不支持")).toBeNull();
    rerender(<CapabilityBadges model={model} />);
    expect(screen.getByText("图片：因路由而异")).toBeTruthy();
  });

  it("renders complete structured declarations, options and profile variants without loading remote icons", () => {
    const { container } = render(<ModelMetadata model={model} />);
    expect(screen.getByText("中文说明", { selector: "span" })).toBeTruthy();
    expect(screen.getByText("可选强度")).toBeTruthy();
    expect(screen.getByText("可选窗口")).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();
    choose(screen.getByRole("combobox", { name: "查看产品声明" }), "intl-work");
    expect(screen.getByText("English description")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "声明 2" })).toBeTruthy();
    expect(screen.queryByText("中文说明", { selector: "span" })).toBeNull();
  });

  it("keeps upstream markup inert and gracefully handles old API responses", () => {
    const value = {
      metadata_by_profile: { "intl-cli": [{ descriptionZh: "<img src=x onerror=alert(1)>" }] },
    };
    const { container, rerender } = render(<ModelMetadata model={value} />);
    expect(screen.getByText("<img src=x onerror=alert(1)>", { selector: "span" })).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();
    rerender(<ModelMetadata model={{}} />);
    expect(screen.getByText("尚无模型元数据")).toBeTruthy();
  });

  it("distinguishes inherited declarations and exposes safe source variants", () => {
    const inherited = {
      metadata_by_profile: {
        "intl-cli": [
          {
            id: "model",
            credits: "x0.3",
            catalog_source: { kind: "shared", profiles: ["intl-work"] },
            source_variants: [
              {
                profile: "intl-work",
                metadata: { descriptionZh: "共享原始声明", credits: "x0.3" },
              },
            ],
          },
        ],
      },
    };
    const { rerender } = render(<ModelMetadata model={inherited} />);
    expect(screen.getByText("国际共享目录")).toBeTruthy();
    expect(screen.getByText(/当前账号未直接声明此模型/)).toBeTruthy();
    fireEvent.click(screen.getByText(/来源版本 1/));
    expect(screen.getByText("共享原始声明", { selector: "span" })).toBeTruthy();
    rerender(
      <ModelMetadata
        model={{
          metadata_by_profile: {
            "intl-cli": [
              { id: "model", catalog_source: { kind: "direct", profiles: ["intl-cli"] } },
            ],
          },
        }}
      />,
    );
    expect(screen.getByText("目录来源：当前产品直接声明。")).toBeTruthy();
    expect(screen.queryByText("国际共享目录")).toBeNull();
    rerender(<ModelMetadata model={{ metadata_by_profile: { "intl-cli": [{ id: "model" }] } }} />);
    expect(screen.queryByText("目录来源：当前产品直接声明。")).toBeNull();
  });

  it("exposes details from the model table and supports display-name search", async () => {
    vi.spyOn(api, "get").mockImplementation(
      async (path) =>
        ({
          data:
            path === "/models"
              ? {
                  revision: 1,
                  models: [
                    model,
                    {
                      ...model,
                      id: "hy-image-3.5",
                      public_id: "hy-image-3.5",
                      upstream_id: "hy-image-3.5",
                      capabilities: {},
                      limits: {},
                      metadata_by_profile: {},
                      media_kinds: ["image", "edit"],
                    },
                    {
                      ...model,
                      id: "minimax-video-h3",
                      public_id: "minimax-video-h3",
                      upstream_id: "minimax-video-h3",
                      capabilities: {},
                      limits: {},
                      metadata_by_profile: {},
                      media_kinds: ["video"],
                    },
                  ],
                  model_capability_guard: false,
                }
              : { credentials: [] },
        }) as never,
    );
    render(<Models />);
    await screen.findByText("能力预检：关闭");
    expect(screen.getByText("文生图")).toBeTruthy();
    expect(screen.getByText("图生图")).toBeTruthy();
    const capability = screen.getByRole("combobox", { name: "筛选能力" });
    choose(capability, "media_image");
    expect(screen.getByText("hy-image-3.5", { selector: "strong" })).toBeTruthy();
    expect(screen.queryByText("minimax-video-h3", { selector: "strong" })).toBeNull();
    expect(screen.queryByText("public", { selector: "strong" })).toBeNull();
    choose(capability, "media_video");
    expect(screen.getByText("minimax-video-h3", { selector: "strong" })).toBeTruthy();
    expect(screen.queryByText("hy-image-3.5", { selector: "strong" })).toBeNull();
    choose(capability, "");
    fireEvent.change(screen.getByRole("textbox", { name: "搜索模型" }), {
      target: { value: "Display" },
    });
    expect(screen.getByText("192K")).toBeTruthy();
    expect(screen.getByText("32K–64K")).toBeTruthy();
    expect(screen.getByText("图片因路由而异")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "模型详情" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("combobox", { name: "查看产品声明" })).toBeTruthy();
    expect(within(dialog).getByRole("heading", { name: "能力概要" })).toBeTruthy();
  });
});

describe("model table summaries", () => {
  it("formats token windows, rates and effort options from declarations", () => {
    expect(formatTokens(1_000_000)).toBe("1M");
    expect(formatTokens(192_000)).toBe("192K");
    expect(specRange(model, "maxInputTokens")).toEqual([192000]);
    expect(specRange(model, "maxOutputTokens")).toEqual([32000, 64000]);
    expect(effortSummary(model)).toEqual({
      options: ["low", "high"],
      defaults: ["high"],
      closable: false,
      only: false,
    });
    expect(rateOf(0)).toBe(0);
    expect(formatRate(0.114)).toBe("0.11x");
    expect(rateOf("x0.5 credits")).toBeNull();
    expect(rateOf(-1)).toBeNull();
  });

  it("falls back to contextWindow lengths when input tokens are missing", () => {
    const windowOnly = {
      metadata_by_profile: {
        "cn-cli": [
          {
            id: "m",
            contextWindow: { defaultLength: 300000, supportedLengths: [300000, 1000000] },
          },
        ],
      },
    };
    expect(specRange(windowOnly, "maxInputTokens")).toEqual([300000]);
    expect(specRange(windowOnly, "maxOutputTokens")).toEqual([]);
  });
});

describe("effort and search summaries", () => {
  it("marks the default effort and describes fixed or absent thinking", () => {
    render(<EffortChips model={model} />);
    expect(screen.getByTitle("默认档")).toBeTruthy();
    render(
      <EffortChips
        model={{
          capabilities: { reasoning: "supported" },
          metadata_by_profile: {
            "cn-cli": [{ id: "m", reasoning: { defaultEffort: "medium" } }],
          },
        }}
      />,
    );
    expect(screen.getByText("固定档 · 默认 medium")).toBeTruthy();
    render(<EffortChips model={{ capabilities: { reasoning: "unsupported" } }} />);
    expect(screen.getByText("不支持思考")).toBeTruthy();
  });

  it("searches across identifiers, names, descriptions and vendors", () => {
    const text = modelSearchText(model);
    expect(text).toContain("display model");
    expect(text).toContain("中文说明");
    expect(modelSearchText({ ...model, public_id: "" })).toContain("upstream");
  });
});

describe("capability guard settings on model routing", () => {
  function modelsPayload(locked: boolean) {
    return {
      revision: 1,
      models: [model],
      model_guard: true,
      model_guard_locked: locked,
      model_capability_guard: true,
      model_capability_guard_locked: locked,
    };
  }

  it("saves an explicit false without changing other settings", async () => {
    vi.spyOn(api, "get").mockImplementation((url) =>
      Promise.resolve({
        data: url === "/models" ? modelsPayload(false) : { credentials: [] },
      }),
    );
    const save = vi.spyOn(api, "patch").mockResolvedValue({ data: { revision: 2 } });
    render(<Models />);
    const toggle = await screen.findByRole("switch", { name: /模型能力预检/ });
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith("/settings", {
        revision: 1,
        values: { model_capability_guard: false },
      }),
    );
    expect(screen.getByText(/国际图片归并及其他安全限制不变/)).toBeTruthy();
  });

  it("disables the switches when locked by external configuration", async () => {
    vi.spyOn(api, "get").mockImplementation((url) =>
      Promise.resolve({
        data: url === "/models" ? modelsPayload(true) : { credentials: [] },
      }),
    );
    render(<Models />);
    const capSwitch = await screen.findByRole("switch", { name: /模型能力预检/ });
    const guardSwitch = await screen.findByRole("switch", { name: /表外模型拦截/ });
    expect(capSwitch).toHaveProperty("disabled", true);
    expect(guardSwitch).toHaveProperty("disabled", true);
  });
});
