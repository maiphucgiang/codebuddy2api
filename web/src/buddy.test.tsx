import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vite-plus/test";
import { Buddy, buddyConfirmation } from "./Buddy";
import { Credentials } from "./pages/Credentials";
import { api, useResource } from "./api";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  useResource: vi.fn(),
}));
beforeEach(() => vi.restoreAllMocks());
const confirmation = {
  can_claim: true,
  revision: "a".repeat(64),
  title: "首领奖励领取确认协议",
  terms: ["官方确认条款"],
  authorization:
    "自动接取并完成 first_buddy 新手任务，必要时发送一次真实对话，最多请求 32 个输出 token，可能消耗少量积分。",
};
const credential = {
  id: "cn",
  name: "cn.info",
  enabled: true,
  profile: "cn-work",
  travel_supported: true,
};
const initial = {
  id: "cn",
  ok: false,
  message: "请确认首次领猫",
  buddy_confirmation: confirmation,
};
function mount(canClaim = true, enabled = true) {
  const onClose = vi.fn();
  const onDone = vi.fn();
  const view = render(
    <Buddy
      credential={{ ...credential, enabled }}
      initial={initial}
      confirmation={{ ...confirmation, can_claim: canClaim }}
      onClose={onClose}
      onDone={onDone}
    />,
  );
  return { ...view, onClose, onDone };
}
it("does not submit on opening or cancelling an unchecked agreement", () => {
  const post = vi.spyOn(api, "post");
  const { onClose } = mount();
  expect(screen.getByRole("checkbox")).toHaveProperty("checked", false);
  expect(screen.getByRole("checkbox")).toHaveProperty("disabled", false);
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(onClose).toHaveBeenCalledOnce();
  expect(post).not.toHaveBeenCalled();
});
it("checking consent submits one account and revision without a second confirmation button", async () => {
  let finish!: (value: unknown) => void;
  const post = vi.spyOn(api, "post").mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const { onDone } = mount();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("checkbox"));
  expect(post).toHaveBeenCalledTimes(1);
  expect(post).toHaveBeenCalledWith(
    "/credentials/cn/travel",
    { confirm_buddy: true, agreement_revision: confirmation.revision },
    expect.objectContaining({ timeout: 180000 }),
  );
  expect(screen.queryByRole("button", { name: "确认领取并派遣" })).toBeNull();
  expect(screen.getByRole("button", { name: "关闭抽屉" })).toHaveProperty("disabled", true);
  await act(async () =>
    finish({
      data: {
        results: [
          {
            id: "cn",
            ok: false,
            buddy_consent_accepted: true,
            buddy_claimed: true,
            message: "猫猫已领取，派遣未确认",
          },
        ],
      },
    }),
  );
  expect(onDone).toHaveBeenCalledOnce();
  expect(screen.getByText("猫猫已领取")).toBeTruthy();
  expect(screen.getByText("首领同意已保存，无需重复确认")).toBeTruthy();
  expect(screen.getByRole("checkbox")).toHaveProperty("disabled", true);
});
it("allows consent while eligibility is false and shows the stored pending result", async () => {
  const post = vi.spyOn(api, "post").mockResolvedValue({
    data: {
      results: [
        {
          id: "cn",
          ok: false,
          buddy_consent_accepted: true,
          reason: "buddy_not_eligible",
          message: "同意已保存；等待官方条件满足",
        },
      ],
    },
  });
  const { onDone } = mount(false);
  expect(screen.getByRole("checkbox")).toHaveProperty("disabled", false);
  await act(async () => fireEvent.click(screen.getByRole("checkbox")));
  expect(post).toHaveBeenCalledOnce();
  expect(onDone).toHaveBeenCalledOnce();
  expect(screen.getByText("首领同意已保存，无需重复确认")).toBeTruthy();
});
it("does not submit consent for a disabled account", () => {
  const post = vi.spyOn(api, "post");
  mount(false, false);
  expect(screen.getByRole("checkbox")).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("checkbox"));
  expect(post).not.toHaveBeenCalled();
});
it("does not retry ambiguous failures or accept another account's result", async () => {
  const post = vi.spyOn(api, "post").mockRejectedValueOnce(new Error("network failed"));
  const { onDone, unmount } = mount();
  await act(async () => fireEvent.click(screen.getByRole("checkbox")));
  expect(screen.getByText(/后台已停止/)).toBeTruthy();
  expect(screen.getByRole("checkbox")).toHaveProperty("disabled", true);
  expect(onDone).not.toHaveBeenCalled();
  expect(post).toHaveBeenCalledTimes(1);
  unmount();
  post.mockResolvedValueOnce({ data: { results: [{ id: "other", ok: true, message: "done" }] } });
  const second = mount();
  await act(async () => fireEvent.click(screen.getByRole("checkbox")));
  expect(screen.getByText(/领取账号或结果未确认/)).toBeTruthy();
  expect(second.onDone).not.toHaveBeenCalled();
});
it("opens confirmation only from a scoped manual travel result", async () => {
  vi.mocked(useResource).mockReturnValue({
    data: [credential],
    loading: false,
    error: null,
    reload: vi.fn(),
  });
  const post = vi.spyOn(api, "post").mockResolvedValue({
    data: {
      results: [{ ...initial, buddy_confirmation: { ...confirmation, can_claim: false } }],
    },
  });
  render(<Credentials />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "旅行领派 cn.info" })));
  expect(screen.getByRole("dialog")).toBeTruthy();
  expect(screen.getByRole("checkbox", { name: /我已阅读并同意/ })).toHaveProperty(
    "disabled",
    false,
  );
  expect(post).toHaveBeenCalledTimes(1);
  expect(post).toHaveBeenCalledWith("/credentials/cn/travel", undefined, expect.anything());
});
it("discloses automatic tasks and possible credit usage before consent", () => {
  const post = vi.spyOn(api, "post");
  mount(false);
  expect(screen.getByText(confirmation.authorization)).toBeTruthy();
  expect(screen.getByText(/无需单独接取/)).toBeTruthy();
  expect(screen.getByRole("checkbox", { name: /自动完成新手任务/ })).toHaveProperty(
    "disabled",
    false,
  );
  expect(post).not.toHaveBeenCalled();
});

it("rejects incomplete consent text instead of accepting it", () => {
  for (const value of [
    {},
    { ...confirmation, revision: "old" },
    { ...confirmation, authorization: undefined },
    { ...confirmation, authorization: "" },
    { ...confirmation, authorization: "x".repeat(2001) },
    { ...confirmation, terms: [] },
    { ...confirmation, can_claim: "true" },
  ])
    expect(() => buddyConfirmation(value)).toThrow();
});
it("shows partial completion when checkin succeeds but Buddy prerequisites block travel", async () => {
  vi.mocked(useResource).mockReturnValue({
    data: [credential],
    loading: false,
    error: null,
    reload: vi.fn(),
  });
  vi.spyOn(api, "post").mockResolvedValue({
    data: {
      results: [
        {
          id: "cn",
          name: "cn.info",
          ok: true,
          checkin_ok: true,
          message: "签到成功；请先完成首领任务",
          travel: { ok: false, buddy_blocked: true, phase: "buddy_tasks" },
        },
      ],
    },
  });
  render(<Credentials />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "签到 cn.info" })));
  expect(screen.getByText("部分完成")).toBeTruthy();
  expect(screen.queryByText("已完成")).toBeNull();
  expect(screen.queryByRole("dialog")).toBeNull();
});
