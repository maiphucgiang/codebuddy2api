import { useEffect, useRef, useState } from "react";
import { api, errorMessage, number, object, text } from "./api";
import { Drawer, ErrorNotice, Icon } from "./components";
import { useExiting } from "./presence";
import s from "./ui.module.scss";

export function safeOAuthUrl(value: unknown): string {
  if (typeof value !== "string") throw new Error("OAuth 响应缺少验证链接");
  const url = new URL(value);
  if (
    url.protocol !== "https:" ||
    (url.port !== "" && url.port !== "443") ||
    url.username ||
    url.password ||
    ![
      "www.codebuddy.cn",
      "www.codebuddy.ai",
      "www.workbuddy.cn",
      "www.workbuddy.ai",
      "copilot.tencent.com",
    ].includes(url.hostname)
  )
    throw new Error("服务返回了不在官方白名单中的 OAuth 链接");
  return url.href;
}
export function OAuth({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const exiting = useExiting();
  const [site, setSite] = useState("cn");
  const [login, setLogin] = useState<{ id: string; url: string; until: number } | null>(null);
  const [message, setMessage] = useState("选择站点后发起授权。");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    if (exiting) controller.current?.abort();
    return () => controller.current?.abort();
  }, [exiting]);
  useEffect(() => {
    if (!login || exiting) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (Date.now() > login.until) {
        setLogin(null);
        setMessage("授权已过期，请重新发起。");
        return;
      }
      // Keep this bounded flow active while the official authorization tab has focus.
      try {
        const response = await api.get<unknown>("/oauth/poll", {
          params: { login_id: login.id },
          signal: abort.signal,
        });
        if (abort.signal.aborted) return;
        const data = object(response.data);
        if (typeof data.done !== "boolean") throw new Error("OAuth 轮询响应缺少 done 状态");
        if (data.done) {
          setLogin(null);
          if (data.error) setError(text(data.error));
          else onDone();
          return;
        }
        timer = setTimeout(() => {
          void poll();
        }, 4000);
      } catch (err) {
        if (!abort.signal.aborted) {
          setError(errorMessage(err));
          setLogin(null);
          setMessage("请重新发起授权。");
        }
      }
    };
    timer = setTimeout(() => {
      void poll();
    }, 4000);
    return () => {
      abort.abort();
      clearTimeout(timer);
    };
  }, [login, onDone, exiting]);
  return (
    <Drawer title="OAuth 添加凭证" onClose={onClose}>
      <p className={s.note}>
        仅在官方站点完成授权。入库成功后自动关闭此抽屉并刷新列表；官方标签页保持安全隔离，请在授权后手动关闭。
      </p>
      <label className={s.field}>
        登录站点
        <select value={site} disabled={!!login || busy} onChange={(e) => setSite(e.target.value)}>
          <option value="cn">中国大陆 · CN</option>
          <option value="intl">国际 · WorkBuddy</option>
          <option value="intl-codebuddy">国际 · CodeBuddy</option>
        </select>
      </label>
      <ErrorNotice message={error} />
      <p className={s.oauthStatus} role="status">
        {message}
      </p>
      {login ? (
        <div className={s.actions}>
          <a className={s.linkButton} href={login.url} target="_blank" rel="noopener noreferrer">
            打开官方授权页面 <Icon name="arrow" />
          </a>
          <button
            onClick={() => {
              setLogin(null);
              setMessage("已停止查询。");
            }}
          >
            停止轮询
          </button>
        </div>
      ) : (
        <button
          className={s.primary}
          disabled={busy}
          onClick={() => {
            setError(null);
            setBusy(true);
            const request = new AbortController();
            controller.current = request;
            void api
              .post<unknown>("/oauth/start", null, { params: { site }, signal: request.signal })
              .then((res) => {
                if (request.signal.aborted) return;
                const data = object(res.data);
                if (typeof data.login_id !== "string" || !data.login_id)
                  throw new Error("OAuth 响应缺少 login_id");
                const seconds = number(data.expires_in);
                setLogin({
                  id: data.login_id,
                  url: safeOAuthUrl(data.verification_uri),
                  until: Date.now() + Math.min(seconds ?? 300, 600) * 1000,
                });
                setMessage("等待官方授权完成…");
              })
              .catch((err: unknown) => {
                if (!request.signal.aborted) setError(errorMessage(err));
              })
              .finally(() => {
                if (!request.signal.aborted) setBusy(false);
              });
          }}
        >
          {busy ? "正在发起…" : "发起 OAuth 授权"}
        </button>
      )}
    </Drawer>
  );
}
