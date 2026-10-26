import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { api, errorMessage, object } from "./api";
import { lockPage } from "./modal";
import { useExiting } from "./presence";
export { DrawerPresence } from "./presence";
import s from "./ui.module.scss";
import { useT } from "./i18n";
import { Select } from "./fields";
import { ToastRegion } from "./toast";

export function Icon({ name = "grid" }: { name?: string }) {
  const paths: Record<string, string> = {
    grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
    model: "m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5",
    key: "M14 4a6 6 0 1 1-3 11l-6 6H2v-3l6-6a6 6 0 0 1 6-8Z M16 8h.01",
    logs: "M6 3h12v18H6z M9 7h6M9 11h6M9 15h4",
    settings: "M4 7h16M4 17h16M8 4v6M16 14v6",
    arrow: "M5 12h14m-5-5 5 5-5 5",
    plus: "M12 5v14M5 12h14",
    collapse: "M4 4h16v16H4zM9 4v16m7-12-3 4 3 4",
    expand: "M4 4h16v16H4zM9 4v16m4-12 3 4-3 4",
    refresh: "M23 4v6h-6M1 20v-6h6M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 0 20.5 15",
    close: "m6 6 12 12M6 18 18 6",
    shield: "m12 2 8 3v6c0 5-8 11-8 11S4 16 4 11V5l8-3Zm-4 9 3 3 5-6",
    leaf: "M20 3C7 2 1 8 5 16s17 4 15-13ZM5 20 16 8",
    alert: "m12 3 10 18H2L12 3Zm0 5v6m0 3v1",
    appearance:
      "M12 3a9 9 0 1 0 0 18h1a2 2 0 0 0 1-3.7 1.5 1.5 0 0 1 1-2.8h2a4 4 0 0 0 4-4C21 6.4 17 3 12 3ZM7 10h.01M10 7h.01M15 7h.01M17 10h.01",
    sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5",
    moon: "M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11Z",
    system: "M3 4h18v13H3zM8 21h8m-4-4v4",
    down: "M12 3v12m0 0 4-4m-4 4-4-4M5 21h14",
    up: "M12 21V9m0 0 4 4m-4-4-4 4M5 3h14",
    clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4.5V12l3 2",
    restore: "M3 12a9 9 0 1 0 2.6-6.4L3 8m0-5v5h5",
    globe:
      "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm-9 9h18M12 3c2.3 2.4 3.5 5.4 3.5 9s-1.2 6.6-3.5 9c-2.3-2.4-3.5-5.4-3.5-9s1.2-6.6 3.5-9Z",
    // Feather's "github" mark (MIT), drawn in the same stroke style as the rest of the set.
    github:
      "M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22",
    cache:
      "M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3Zm8 3v12c0 1.7-3.6 3-8 3s-8-1.3-8-3V6m16 6c0 1.7-3.6 3-8 3s-8-1.3-8-3",
    database:
      "M12 8c4.4 0 8-1.3 8-3s-3.6-3-8-3-8 1.3-8 3 3.6 3 8 3Zm-8 2v9c0 1.7 3.6 3 8 3s8-1.3 8-3v-9M4 14.5c0 1.7 3.6 3 8 3s8-1.3 8-3",
    image:
      "M3 5.5h18v13H3v-13Zm3.5 10 3-3.5 2.5 3 3.5-4.5 3.5 5M8.8 9.2a1 1 0 1 1-2 0 1 1 0 0 1 2 0Z",
    play: "M8 5.5v13l11-6.5-11-6.5Z",
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
/** A refresh icon that turns while `spinning` and always finishes the turn it is in. */
export function RefreshIcon({ spinning }: { spinning: boolean }) {
  const [turning, setTurning] = useState(false);
  useEffect(() => {
    if (spinning) setTurning(true);
  }, [spinning]);
  return (
    <span
      className={`${s.spinner} ${turning ? s.spinning : ""}`}
      onAnimationIteration={() => {
        if (!spinning) setTurning(false);
      }}
    >
      <Icon name="refresh" />
    </span>
  );
}
/** A glass block that glides under the selected (`aria-pressed` / `aria-selected`) item of its bar. */
export function SegmentThumb() {
  const ref = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const thumb = ref.current;
    const bar = thumb?.parentElement;
    if (!thumb || !bar) return;
    const place = () => {
      const active = bar.querySelector<HTMLElement>(
        '[aria-pressed="true"], [aria-selected="true"]',
      );
      thumb.style.opacity = active ? "1" : "0";
      if (!active) return;
      thumb.style.width = `${active.offsetWidth}px`;
      thumb.style.height = `${active.offsetHeight}px`;
      thumb.style.transform = `translate(${active.offsetLeft}px, ${active.offsetTop}px)`;
    };
    place();
    // Glide only after the first placement, so the thumb never sweeps in on mount.
    const ready = window.setTimeout(() => thumb.classList.add(s.segmentReady), 50);
    const changes = new MutationObserver(place);
    changes.observe(bar, {
      subtree: true,
      childList: true,
      characterData: true,
      attributeFilter: ["aria-pressed", "aria-selected"],
    });
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(place);
    resize?.observe(bar);
    return () => {
      window.clearTimeout(ready);
      changes.disconnect();
      resize?.disconnect();
    };
  }, []);
  return <span ref={ref} className={s.segmentThumb} aria-hidden="true" />;
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
  // The header breadcrumb already names the page; keep the h1 for screen readers only.
  return (
    <div className={s.pageTitle}>
      <div>
        <h1 className={s.srOnly}>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      <div className={s.actions}>{actions}</div>
      <ToastRegion />
    </div>
  );
}
export function ErrorNotice({ message, retry }: { message: string | null; retry?: () => void }) {
  const t = useT();
  return message ? (
    <div className={s.error} role="alert">
      <Icon name="alert" />
      <span>{message}</span>
      {retry && <button onClick={retry}>{t("common.retry")}</button>}
    </div>
  ) : null;
}
export function Empty({ title, children }: { title?: string; children?: ReactNode }) {
  const t = useT();
  const heading = title ?? t("common.empty");
  return (
    <div className={s.empty}>
      <Icon name="leaf" />
      <h3>{heading}</h3>
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
  const t = useT();
  return (
    <>
      {loading && (
        <div className={s.loading} role="status">
          <span />
          {t("common.loading")}
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
  action,
  children,
  className = "",
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`${s.panel} ${className}`}>
      <div className={s.panelHeading}>
        <h2>{title}</h2>
        {hint && <span>{hint}</span>}
        {action}
      </div>
      {children}
    </section>
  );
}
export { Fields, DataValue, profileLabel } from "./values";
export function Drawer({
  title,
  children,
  onClose,
  dismissDisabled = false,
  className = "",
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  dismissDisabled?: boolean;
  className?: string;
}) {
  const t = useT();
  const exiting = useExiting();
  const ref = useRef<HTMLDialogElement>(null);
  const outside = useRef(false);
  const id = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    const active = document.activeElement;
    dialog.showModal();
    const unlock = lockPage();
    return () => {
      dialog.close();
      unlock();
      if (active instanceof HTMLElement && active.isConnected)
        active.focus({ preventScroll: true });
    };
  }, []);
  return (
    <dialog
      className={`${s.modalOverlay} ${exiting ? s.exiting : ""}`}
      data-phase={exiting ? "exiting" : "open"}
      ref={ref}
      aria-labelledby={id}
      onPointerDown={(e) => {
        outside.current = e.button === 0 && e.target === e.currentTarget;
      }}
      onClick={(e) => {
        const dismiss = outside.current && e.target === e.currentTarget;
        outside.current = false;
        if (dismiss && !dismissDisabled && !exiting) onClose();
      }}
      onCancel={(e) => {
        e.preventDefault();
        if (!dismissDisabled && !exiting) onClose();
      }}
    >
      <section className={`${s.drawer} ${className}`} inert={exiting}>
        <div className={s.drawerHead}>
          <h2 id={id}>{title}</h2>
          <button
            type="button"
            aria-label={t("common.closeDrawer")}
            disabled={dismissDisabled}
            onClick={onClose}
          >
            <Icon name="close" />
          </button>
        </div>
        <div className={s.drawerBody}>{children}</div>
        {/* Toasts raised while the drawer is open sit over it; a closing drawer hands them back. */}
        {!exiting && <ToastRegion />}
      </section>
    </dialog>
  );
}
export const CLEAR_CONFIRMATION = "清空全部日志与统计";
export function ClearLogs({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const t = useT();
  const [scope, setScope] = useState<"details" | "all">("details");
  const [confirmation, setConfirmation] = useState("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Drawer title={t("logs.clear")} onClose={onClose} dismissDisabled={busy}>
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
              if (object(response.data).ok !== true) throw new Error(t("logs.clear.unconfirmed"));
              onDone();
              onClose();
            })
            .catch((err: unknown) => setError(errorMessage(err)))
            .finally(() => setBusy(false));
        }}
      >
        <label className={s.field}>
          {t("logs.clear.scope")}
          <Select
            value={scope}
            onChange={(value) => {
              setScope(value as "details" | "all");
              setConfirmation("");
              setKey("");
            }}
          >
            <option value="details">{t("logs.clear.detailsOnly")}</option>
            <option value="all">{t("logs.clear.everything")}</option>
          </Select>
        </label>
        <div className={s.warning}>
          <Icon name="alert" />
          <div>
            <strong>{scope === "all" ? CLEAR_CONFIRMATION : t("logs.clear.detailsStrong")}</strong>
            <p>{scope === "all" ? t("logs.clear.allWarning") : t("logs.clear.detailsWarning")}</p>
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
              {t("common.currentApiKey")}
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
          {busy ? t("logs.clear.busy") : t("logs.clear.confirm")}
        </button>
      </form>
    </Drawer>
  );
}
