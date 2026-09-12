import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { api, errorMessage, object, text, type RecordValue } from "./api";
import s from "./ui.module.scss";

export function Icon({ name = "grid" }: { name?: string }) {
  const paths: Record<string, string> = {
    grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
    model: "m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5",
    key: "M14 4a6 6 0 1 1-3 11l-6 6H2v-3l6-6a6 6 0 0 1 6-8Z M16 8h.01",
    logs: "M6 3h12v18H6z M9 7h6M9 11h6M9 15h4",
    settings: "M4 7h16M4 17h16M8 4v6M16 14v6",
    arrow: "M5 12h14m-5-5 5 5-5 5",
    refresh: "M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1",
    close: "m6 6 12 12M6 18 18 6",
    shield: "m12 2 8 3v6c0 5-8 11-8 11S4 16 4 11V5l8-3Zm-4 9 3 3 5-6",
    leaf: "M20 3C7 2 1 8 5 16s17 4 15-13ZM5 20 16 8",
    alert: "m12 3 10 18H2L12 3Zm0 5v6m0 3v1",
  };
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name] ?? paths.grid} />
    </svg>
  );
}
export function PageTitle({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className={s.pageTitle}>
      <div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      <div className={s.actions}>{actions}</div>
    </div>
  );
}
export function ErrorNotice({ message, retry }: { message: string | null; retry?: () => void }) {
  return message ? (
    <div className={s.error} role="alert">
      <Icon name="alert" />
      <span>{message}</span>
      {retry && <button onClick={retry}>重试</button>}
    </div>
  ) : null;
}
export function Empty({ title = "暂无数据", children }: { title?: string; children?: ReactNode }) {
  return (
    <div className={s.empty}>
      <Icon name="leaf" />
      <h3>{title}</h3>
      {children && <p>{children}</p>}
    </div>
  );
}
export function ResourceState({
  loading,
  error,
  reload,
}: {
  loading: boolean;
  error: string | null;
  reload: () => void;
}) {
  return (
    <>
      {loading && (
        <div className={s.loading} role="status">
          <span />
          加载中…
        </div>
      )}
      <ErrorNotice message={error} retry={reload} />
    </>
  );
}
export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "good" | "bad" | "neutral" | "warn";
}) {
  return <span className={`${s.badge} ${s[tone]}`}>{children}</span>;
}
export function Panel({
  title,
  hint,
  children,
  className = "",
}: {
  title: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`${s.panel} ${className}`}>
      <div className={s.panelHeading}>
        <h2>{title}</h2>
        {hint && <span>{hint}</span>}
      </div>
      {children}
    </section>
  );
}
export function Fields({ data }: { data: RecordValue }) {
  return (
    <dl className={s.details}>
      {Object.entries(data).map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>{text(value)}</dd>
        </div>
      ))}
    </dl>
  );
}
export function Drawer({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => {
    const dialog = ref.current;
    const active = document.activeElement;
    dialog?.showModal();
    return () => {
      dialog?.close();
      if (active instanceof HTMLElement) active.focus();
    };
  }, []);
  return (
    <dialog
      className={s.drawer}
      ref={ref}
      aria-labelledby={id}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div className={s.drawerHead}>
        <div>
          <h2 id={id}>{title}</h2>
        </div>
        <button aria-label="关闭抽屉" onClick={onClose}>
          <Icon name="close" />
        </button>
      </div>
      <div className={s.drawerBody}>{children}</div>
    </dialog>
  );
}
export const CLEAR_CONFIRMATION = "清空全部日志与统计";
export function ClearLogs({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [scope, setScope] = useState<"details" | "all">("details");
  const [confirmation, setConfirmation] = useState("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Drawer title="清理日志" onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          const currentKey = key;
          setKey("");
          void api
            .post("/logs/clear", {
              scope,
              confirmation: scope === "all" ? confirmation : "",
              ...(scope === "all" ? { api_key: currentKey } : {}),
            })
            .then((response) => {
              if (object(response.data).ok !== true)
                throw new Error("后端未确认清理成功，请检查存储状态");
              onDone();
              onClose();
            })
            .catch((err: unknown) => setError(errorMessage(err)))
            .finally(() => setBusy(false));
        }}
      >
        <label className={s.field}>
          清理范围
          <select
            value={scope}
            onChange={(e) => {
              setScope(e.target.value as "details" | "all");
              setConfirmation("");
              setKey("");
            }}
          >
            <option value="details">仅清空明细 · 保留统计</option>
            <option value="all">全部日志与统计</option>
          </select>
        </label>
        <div className={s.warning}>
          <Icon name="alert" />
          <div>
            <strong>{scope === "all" ? CLEAR_CONFIRMATION : "仅清空日志明细，保留累计统计"}</strong>
            <p>
              {scope === "all"
                ? "将永久删除全部请求明细、事件日志和历史统计，不可撤销。建议先备份；凭证及网关配置不受影响。"
                : "将删除请求明细和事件日志，保留历史统计。不可撤销，建议先备份。"}
            </p>
          </div>
        </div>
        {scope === "all" && (
          <>
            <label className={s.field}>
              输入“{CLEAR_CONFIRMATION}”
              <input
                value={confirmation}
                onChange={(e) => setConfirmation(e.target.value)}
                autoComplete="off"
              />
            </label>
            <label className={s.field}>
              当前 API key
              <input
                type="password"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                autoComplete="off"
                required
              />
            </label>
          </>
        )}
        <ErrorNotice message={error} />
        <button
          className={s.danger}
          disabled={busy || (scope === "all" && (confirmation !== CLEAR_CONFIRMATION || !key))}
        >
          {busy ? "正在清理…" : "确认不可撤销的清理"}
        </button>
      </form>
    </Drawer>
  );
}
