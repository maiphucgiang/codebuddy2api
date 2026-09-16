import { useEffect, useRef, useState } from "react";
import { api, errorMessage, list, object, text, type Credential } from "./api";
import { Drawer, ErrorNotice } from "./components";
import { TravelSummary } from "./Travel";
import s from "./ui.module.scss";

type Confirmation = {
  can_claim: boolean;
  revision: string;
  title: string;
  terms: string[];
  authorization: string;
};
export function buddyConfirmation(value: unknown): Confirmation {
  const data = object(value, "首领确认");
  if (
    typeof data.can_claim !== "boolean" ||
    typeof data.revision !== "string" ||
    !/^[a-f0-9]{64}$/.test(data.revision) ||
    typeof data.title !== "string" ||
    !data.title ||
    data.title.length > 120 ||
    !Array.isArray(data.terms) ||
    !data.terms.length ||
    data.terms.length > 12 ||
    typeof data.authorization !== "string" ||
    !data.authorization.trim() ||
    data.authorization.length > 2000 ||
    data.terms.some((term) => typeof term !== "string" || !term || term.length > 2000)
  )
    throw new Error("首领确认内容无效，请刷新后重试");
  return data as Confirmation;
}

export function Buddy({
  credential,
  initial,
  confirmation,
  onClose,
  onDone,
}: {
  credential: Credential;
  initial: Record<string, unknown>;
  confirmation: Confirmation;
  onClose: () => void;
  onDone: (result: Record<string, unknown>) => void;
}) {
  const [accepted, setAccepted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const [status, setStatus] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  const confirm = async () => {
    if (controller.current || attempted || credential.enabled !== true) return;
    const abort = new AbortController();
    controller.current = abort;
    setAccepted(true);
    setBusy(true);
    setAttempted(true);
    setError(null);
    try {
      const response = await api.post(
        `/credentials/${encodeURIComponent(credential.id)}/travel`,
        { confirm_buddy: true, agreement_revision: confirmation.revision },
        { signal: abort.signal, timeout: 180000 },
      );
      const results = list(object(response.data).results, "领猫与派遣结果");
      const result = results[0];
      if (
        results.length !== 1 ||
        result.id !== credential.id ||
        typeof result.ok !== "boolean" ||
        typeof result.message !== "string"
      )
        throw new Error("领取账号或结果未确认");
      if (!abort.signal.aborted) {
        setStatus(result);
        onDone(result);
      }
    } catch (error) {
      if (!abort.signal.aborted)
        setError(
          `${errorMessage(error)}；请求失败不代表后台已停止，请关闭后查询状态，勿重复领取。`,
        );
    } finally {
      controller.current = null;
      if (!abort.signal.aborted) setBusy(false);
    }
  };
  return (
    <Drawer title="首次领取猫猫并派遣" onClose={onClose} dismissDisabled={busy}>
      <p>
        <strong>{credential.name ?? credential.id}</strong>
      </p>
      <p role="status">{text(status.message)}</p>
      <TravelSummary trip={status} />
      <p className={s.note}>{confirmation.authorization}</p>
      <h3>{confirmation.title}</h3>
      {confirmation.terms.map((term, index) => (
        <p key={index} className={s.note}>
          {term}
        </p>
      ))}
      <p className={s.note}>
        <a
          href="https://www.workbuddy.cn/profile/growth-center"
          target="_blank"
          rel="noopener noreferrer"
        >
          查看官方成长中心与活动规则
        </a>
        。首次领猫任务通过实际对话自动生效，无需单独接取；不执行其他奖励任务或切换猫猫。
      </p>
      <label>
        <input
          type="checkbox"
          checked={accepted}
          disabled={busy || attempted || credential.enabled !== true}
          onChange={(event) => {
            if (event.target.checked) void confirm();
          }}
        />
        我已阅读并同意上述协议，自动完成新手任务、领取并派遣
      </label>
      <ErrorNotice message={error} />
      {busy && <p role="status">正在核验并申请，请勿重复点击…</p>}
      <div className={s.actions}>
        <button disabled={busy} onClick={onClose}>
          {attempted ? "关闭" : "取消"}
        </button>
      </div>
      <p className={s.note}>
        勾选即保存同意并自动办理。官方状态更新延迟时，由已开启的自动旅行继续查询，不重复发送对话；关闭自动旅行可停止自动续办，关闭页面不撤销已发请求。
      </p>
    </Drawer>
  );
}
