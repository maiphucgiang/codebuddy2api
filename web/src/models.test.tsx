import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { api, type ModelRule } from "./api";
import { ModelEditor, validateRule } from "./pages/Models";

const rule: ModelRule = {
  id: "source",
  public_id: "public",
  upstream_id: "source",
  enabled: true,
  keep_original: false,
  region: "",
  profile: "",
  credential_ids: [],
};
const credentials = [{ id: "one", name: "one.info", profile: "intl-work", enabled: true }];
afterEach(() => vi.restoreAllMocks());
function editor(model = rule, creating = false) {
  const saved = vi.fn(),
    close = vi.fn();
  const view = render(
    <ModelEditor
      model={model}
      models={[rule]}
      credentials={credentials}
      revision={4}
      creating={creating}
      onSaved={saved}
      onClose={close}
    />,
  );
  return { ...view, saved, close };
}
describe("independent model routes", () => {
  it("creates a model with separate public and upstream IDs", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({ data: { revision: 5 } });
    const { saved, close } = editor({ ...rule, id: "", public_id: "", upstream_id: "" }, true);
    fireEvent.change(screen.getByLabelText("对外 ID"), { target: { value: "my-client-model" } });
    fireEvent.change(screen.getByLabelText("上游 ID"), { target: { value: "provider-model" } });
    fireEvent.click(screen.getByRole("radio", { name: /指定区域/ }));
    fireEvent.change(screen.getByLabelText("区域"), { target: { value: "intl" } });
    fireEvent.click(screen.getByRole("button", { name: "创建模型" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/models",
        expect.objectContaining({
          revision: 4,
          public_id: "my-client-model",
          upstream_id: "provider-model",
          region: "intl",
          credential_ids: [],
        }),
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    expect(saved).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
  });
  it("clears opposite constraints and does not silently save an empty account scope", async () => {
    const put = vi.spyOn(api, "put").mockResolvedValue({ data: { revision: 5 } });
    editor({ ...rule, credential_ids: ["one"] });
    fireEvent.click(screen.getByRole("radio", { name: /指定区域/ }));
    expect(screen.queryByRole("checkbox", { name: /one.info/ })).toBeNull();
    fireEvent.change(screen.getByLabelText("区域"), { target: { value: "intl" } });
    fireEvent.change(screen.getByLabelText("产品"), { target: { value: "intl-work" } });
    fireEvent.click(screen.getByRole("radio", { name: /指定账号/ }));
    expect(screen.queryByLabelText("区域")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "保存规则" }));
    expect(await screen.findByText("请选择至少一个账号")).toBeTruthy();
    expect(put).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox", { name: /one.info/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存规则" }));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith(
        "/models/source",
        expect.objectContaining({
          region: null,
          profile: null,
          credential_ids: ["one"],
        }),
        expect.anything(),
      ),
    );
  });
  it("retains legacy combined scopes until an explicit mode is selected", async () => {
    const put = vi.spyOn(api, "put").mockResolvedValue({ data: { revision: 5 } });
    editor({ ...rule, region: "intl", profile: "intl-work", credential_ids: ["one"] });
    expect(screen.getByText(/当前仍按原交集执行/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "保存规则" }));
    expect(put).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("radio", { name: /指定账号/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存规则" }));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith(
        "/models/source",
        expect.objectContaining({
          region: null,
          profile: null,
          credential_ids: ["one"],
        }),
        expect.anything(),
      ),
    );
  });
  it("aborts an unmounted preview and prevents edits or dismissal while submitting", async () => {
    let resolve!: (value: { data: unknown }) => void;
    const post = vi.spyOn(api, "post").mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const { unmount, close, saved } = editor();
    fireEvent.click(screen.getByRole("button", { name: "预览候选路由" }));
    expect(screen.getByRole("button", { name: "关闭抽屉" })).toHaveProperty("disabled", true);
    expect(screen.getByLabelText("上游 ID").closest("fieldset")).toHaveProperty("disabled", true);
    fireEvent(screen.getByRole("dialog"), new Event("cancel", { cancelable: true }));
    expect(close).not.toHaveBeenCalled();
    const signal = post.mock.calls[0][2]!.signal as AbortSignal;
    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => resolve({ data: { candidates: [], excluded: [] } }));
    expect(saved).not.toHaveBeenCalled();
  });
  it("does not accept a save without a new server revision", async () => {
    vi.spyOn(api, "put").mockResolvedValue({ data: { ok: false } });
    const { close, saved } = editor();
    fireEvent.click(screen.getByRole("button", { name: "保存规则" }));
    expect(await screen.findByText(/后端未确认模型保存/)).toBeTruthy();
    expect(saved).not.toHaveBeenCalled();
    expect(close).not.toHaveBeenCalled();
  });
  it("validates both identifiers and mutual exclusion", () => {
    expect(validateRule({ ...rule, upstream_id: "" }, [])).not.toBeNull();
    expect(validateRule({ ...rule, public_id: "bad id" }, [])).not.toBeNull();
    expect(validateRule({ ...rule, region: "cn", credential_ids: ["one"] }, [])).not.toBeNull();
    expect(
      validateRule({ ...rule, profile: "cn-cli", credential_ids: ["one"] }, []),
    ).not.toBeNull();
  });
});
