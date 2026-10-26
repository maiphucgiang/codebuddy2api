import { useEffect, useState } from "react";
import {
  api,
  errorMessage,
  list,
  number,
  object,
  text,
  useResource,
  type RecordValue,
} from "../api";
import {
  Badge,
  Drawer,
  Empty,
  ErrorNotice,
  Fields,
  Icon,
  PageTitle,
  RefreshIcon,
  ResourceState,
  SegmentThumb,
} from "../components";
import { bytes } from "../values";
import { notify } from "../toast";
import { Select } from "../fields";
import { useT } from "../i18n";
import s from "../ui.module.scss";

const TTL_PRESETS = [24, 72, 168, 720];

/** Client-side mirror of the gateway's media_public_base_url validator. */
export function validateMediaBaseUrl(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  try {
    const parsed = new URL(trimmed);
    if (!["http:", "https:"].includes(parsed.protocol) || !parsed.hostname)
      return "media.settings.baseUrlInvalid";
    if (parsed.username || parsed.password || parsed.search || parsed.hash)
      return "media.settings.baseUrlInvalid";
    return null;
  } catch {
    return "media.settings.baseUrlInvalid";
  }
}

export type MediaModel = {
  id: string;
  kinds: string[];
  sources: string[];
  defaults: string[];
  accounts: number;
};

export type MediaSummary = {
  configured: boolean;
  base_url: string;
  locked: boolean;
  available: boolean;
  limit_bytes: number;
  ttl_hours: number;
  usage: {
    total_bytes: number;
    images: { count: number; bytes: number };
    videos: { count: number; bytes: number };
  };
  tasks: { active: number; failed: number };
  models: MediaModel[];
};

export function normalizeMediaSummary(value: unknown): MediaSummary {
  const data = object(value, "媒体概览");
  const group = (raw: unknown) => ({
    count: number((raw as RecordValue)?.count) ?? 0,
    bytes: number((raw as RecordValue)?.bytes) ?? 0,
  });
  return {
    configured: data.configured === true,
    base_url: typeof data.base_url === "string" ? data.base_url : "",
    locked: data.locked === true,
    available: data.available !== false,
    limit_bytes: number(data.limit_bytes) ?? 0,
    ttl_hours: number(data.ttl_hours) ?? 72,
    usage: {
      total_bytes: number((data.usage as RecordValue)?.total_bytes) ?? 0,
      images: group((data.usage as RecordValue)?.images),
      videos: group((data.usage as RecordValue)?.videos),
    },
    tasks: {
      active: number((data.tasks as RecordValue)?.active) ?? 0,
      failed: number((data.tasks as RecordValue)?.failed) ?? 0,
    },
    models: Array.isArray(data.models)
      ? list(data.models, "媒体模型").map((item) => ({
          id: text(item.id),
          kinds: Array.isArray(item.kinds) ? item.kinds.map(text) : [],
          sources: Array.isArray(item.sources) ? item.sources.map(text) : [],
          defaults: Array.isArray(item.default) ? item.default.map(text) : [],
          accounts: number(item.accounts) ?? 0,
        }))
      : [],
  };
}

/** Remaining retention as a short label; under six hours it turns warning-coloured. */
export function remainingLabel(
  expires: unknown,
  t: (key: string, vars?: Record<string, string | number>) => string,
  now = Date.now(),
): { text: string; warn: boolean } {
  const end = number(expires);
  if (end === null) return { text: "—", warn: false };
  const seconds = Math.max(0, end - now / 1000);
  const hours = seconds / 3600;
  if (hours >= 48)
    return { text: t("media.remaining.days", { count: Math.round(hours / 24) }), warn: false };
  if (hours >= 1)
    return { text: t("media.remaining.hours", { count: hours.toFixed(1) }), warn: hours < 6 };
  return {
    text: t("media.remaining.minutes", { count: Math.max(1, Math.round(seconds / 60)) }),
    warn: true,
  };
}

export function taskStatusTone(status: unknown): "good" | "bad" | "neutral" {
  if (status === "completed") return "good";
  if (status === "failed") return "bad";
  return "neutral";
}

function pageNormalize(value: unknown) {
  const data = object(value, "媒体列表");
  if (typeof data.has_more !== "boolean" || (data.has_more && typeof data.next_cursor !== "string"))
    throw new Error("媒体列表分页响应不完整");
  return {
    items: list(data.items),
    next_cursor: typeof data.next_cursor === "string" ? data.next_cursor : null,
    has_more: data.has_more,
  };
}

function AssetPreview({
  item,
  configured,
  onClose,
  onDeleted,
}: {
  item: RecordValue;
  configured: boolean;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const t = useT();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const assetId = text(item.id);
  const content = `/admin/media/assets/${encodeURIComponent(assetId)}/content`;
  const isVideo = item.kind === "video";
  const link = typeof item.public_url === "string" ? item.public_url : "";
  return (
    <Drawer title={t("media.preview")} onClose={onClose} dismissDisabled={busy}>
      <ErrorNotice message={error} />
      <div className={s.mediaPreviewWrap}>
        {isVideo ? (
          <video className={s.mediaPreview} controls preload="metadata" src={content}>
            <track kind="captions" />
          </video>
        ) : (
          <img
            className={s.mediaPreview}
            src={content}
            alt={text(item.model) || assetId}
            decoding="async"
          />
        )}
      </div>
      <Fields
        data={{
          id: assetId,
          kind: item.kind,
          mime: item.mime,
          size: bytes(number(item.size) ?? 0),
          sha256: typeof item.sha256 === "string" ? item.sha256.slice(0, 12) : item.sha256,
          model: item.model,
          created_at: number(item.created_at),
          expires_at: number(item.expires_at),
        }}
      />
      <div className={s.actions}>
        {configured && link && (
          <button
            type="button"
            onClick={() => {
              void navigator.clipboard
                .writeText(link)
                .then(() =>
                  notify({ key: `media-copy-${assetId}`, tone: "good", title: t("media.copied") }),
                )
                .catch(() => setError(t("media.copyFailed")));
            }}
          >
            {t("media.copyLink")}
          </button>
        )}
        <a className={s.pill} href={`${content}?download=1`} download>
          {t("media.download")}
        </a>
        {confirming ? (
          <button
            className={s.danger}
            disabled={busy}
            onClick={() => {
              setBusy(true);
              setError(null);
              void api
                .delete(`/media/assets/${encodeURIComponent(assetId)}`)
                .then(() => {
                  onDeleted();
                  onClose();
                })
                .catch((err: unknown) => setError(errorMessage(err)))
                .finally(() => setBusy(false));
            }}
          >
            {t("media.deleteConfirm")}
          </button>
        ) : (
          <button className={s.danger} onClick={() => setConfirming(true)}>
            {t("media.delete")}
          </button>
        )}
      </div>
      {!configured && <p className={s.note}>{t("media.noLinkNote")}</p>}
      {confirming && <p className={s.note}>{t("media.deleteHint")}</p>}
    </Drawer>
  );
}

function MediaSettingsDrawer({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const t = useT();
  const [form, setForm] = useState<{
    revision: number;
    base: string;
    limit: number;
    ttl: number;
    locked: Set<string>;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    void api
      .get<unknown>("/settings")
      .then((response) => {
        if (cancelled) return;
        const data = object(response.data, "设置");
        const items = list(data.items);
        const find = (key: string) => items.find((item) => item.key === key);
        setForm({
          revision: number(data.revision) ?? 0,
          base: text(find("media_public_base_url")?.value ?? ""),
          limit: number(find("media_max_total_mb")?.value) ?? 1024,
          ttl: number(find("media_ttl_hours")?.value) ?? 72,
          locked: new Set(
            items
              .filter((item) => item.locked === true && typeof item.key === "string")
              .map((item) => text(item.key)),
          ),
        });
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const invalid = form ? validateMediaBaseUrl(form.base) : null;
  const presets =
    form && !TTL_PRESETS.includes(form.ttl)
      ? [...TTL_PRESETS, form.ttl].sort((a, b) => a - b)
      : TTL_PRESETS;
  const save = () => {
    if (!form || invalid) return;
    const values: Record<string, unknown> = {};
    if (!form.locked.has("media_public_base_url"))
      values.media_public_base_url = form.base.trim().replace(/\/+$/, "");
    if (!form.locked.has("media_max_total_mb")) values.media_max_total_mb = form.limit;
    if (!form.locked.has("media_ttl_hours")) values.media_ttl_hours = form.ttl;
    setBusy(true);
    setError(null);
    void api
      .patch("/settings", { revision: form.revision, values })
      .then(() => {
        notify({ key: "media-settings", tone: "good", title: t("media.settings.saved") });
        onSaved();
        onClose();
      })
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setBusy(false));
  };
  const lockedNote = (key: string) =>
    form?.locked.has(key) ? <p className={s.note}>{t("media.settings.locked")}</p> : null;
  return (
    <Drawer title={t("media.settings.title")} onClose={onClose} dismissDisabled={busy}>
      <ErrorNotice message={error} />
      {form && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            save();
          }}
        >
          <label className={s.field}>
            {t("media.settings.baseUrl")}
            <input
              value={form.base}
              placeholder={t("media.settings.baseUrlPlaceholder")}
              aria-describedby={invalid ? "media-base-error" : undefined}
              disabled={busy || form.locked.has("media_public_base_url")}
              onChange={(event) => setForm({ ...form, base: event.target.value })}
            />
          </label>
          {invalid && (
            <p className={s.errorText} id="media-base-error" role="alert">
              {t(invalid)}
            </p>
          )}
          <p className={s.note}>{t("media.settings.baseUrlHelp")}</p>
          {!invalid && form.base.trim() && (
            <p className={`${s.note} ${s.mono}`}>
              {t("media.settings.baseUrlExample", {
                url: `${form.base.trim().replace(/\/+$/, "")}/v1/media/images/img_…`,
              })}
            </p>
          )}
          {lockedNote("media_public_base_url")}
          <label className={s.field}>
            {t("media.settings.limit")}
            <input
              type="number"
              min={64}
              max={1048576}
              value={form.limit}
              disabled={busy || form.locked.has("media_max_total_mb")}
              onChange={(event) => setForm({ ...form, limit: Number(event.target.value) })}
            />
          </label>
          {lockedNote("media_max_total_mb")}
          <label className={s.field}>
            {t("media.settings.ttl")}
            <Select
              value={form.ttl}
              disabled={busy || form.locked.has("media_ttl_hours")}
              onChange={(value) => setForm({ ...form, ttl: Number(value) })}
            >
              {presets.map((hours) => (
                <option key={hours} value={hours}>
                  {t("media.settings.ttlHours", { hours })}
                </option>
              ))}
            </Select>
          </label>
          {lockedNote("media_ttl_hours")}
          <p className={s.note}>{t("media.settings.note")}</p>
          <button className={s.primary} disabled={busy || invalid !== null}>
            {t("media.settings.save")}
          </button>
        </form>
      )}
    </Drawer>
  );
}

export function Media() {
  const t = useT();
  const [tab, setTab] = useState<"library" | "tasks" | "models">("library");
  const [kind, setKind] = useState("");
  const [taskStatus, setTaskStatus] = useState("");
  const [assetCursors, setAssetCursors] = useState<string[]>([]);
  const [taskCursors, setTaskCursors] = useState<string[]>([]);
  const [preview, setPreview] = useState<RecordValue | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const summary = useResource("/media", normalizeMediaSummary);
  const assetParams = new URLSearchParams({ limit: "24" });
  if (kind) assetParams.set("kind", kind);
  if (assetCursors.length) assetParams.set("cursor", assetCursors.at(-1)!);
  const assets = useResource(`/media/assets?${assetParams.toString()}`, pageNormalize);
  const taskParams = new URLSearchParams({ limit: "50" });
  if (taskStatus) taskParams.set("status", taskStatus);
  if (taskCursors.length) taskParams.set("cursor", taskCursors.at(-1)!);
  const tasks = useResource(`/media/tasks?${taskParams.toString()}`, pageNormalize);
  const data = summary.data;
  const configured = data?.configured === true;
  const reloadAll = () => {
    summary.reload();
    if (tab === "library") assets.reload();
    if (tab === "tasks") tasks.reload();
  };

  const openAsset = (item: RecordValue) => setPreview(item);
  const openAssetById = (id: string) => {
    void api
      .get<unknown>(`/media/assets/${encodeURIComponent(id)}`)
      .then((response) => setPreview(object(response.data, "媒体资源")))
      .catch((err: unknown) =>
        notify({ key: `media-open-${id}`, tone: "bad", title: errorMessage(err) }),
      );
  };

  return (
    <>
      <PageTitle
        title={t("media.title")}
        description={t("media.desc")}
        actions={
          <>
            <Badge tone={configured ? "good" : "warn"}>
              {configured ? t("media.baseConfigured") : t("media.baseMissing")}
            </Badge>
            <button onClick={reloadAll}>
              <RefreshIcon spinning={summary.refreshing} />
              {t("media.refresh")}
            </button>
            <button className={s.pill} onClick={() => setSettingsOpen(true)}>
              {t("media.settings")}
            </button>
          </>
        }
      />
      {data && !configured && (
        <div className={s.warning} role="alert">
          <Icon name="alert" />
          <p>{data.locked ? t("media.bannerLocked") : t("media.banner")}</p>
          {!data.locked && (
            <button onClick={() => setSettingsOpen(true)}>{t("media.bannerAction")}</button>
          )}
        </div>
      )}
      {data && (
        <div className={s.kpiGrid}>
          <div className={s.kpiCard}>
            <small>{t("media.kpi.usage")}</small>
            <strong>
              {bytes(data.usage.total_bytes)} / {bytes(data.limit_bytes)}
            </strong>
            <div
              className={`${s.usageMeter} ${data.usage.total_bytes >= data.limit_bytes ? s.full : data.usage.total_bytes >= data.limit_bytes * 0.85 ? s.warn : ""}`}
            >
              <span
                style={{
                  width: `${Math.min(100, data.limit_bytes ? (data.usage.total_bytes / data.limit_bytes) * 100 : 0)}%`,
                }}
              />
            </div>
          </div>
          <div className={s.kpiCard}>
            <small>{t("media.kpi.images")}</small>
            <strong>
              {data.usage.images.count} · {bytes(data.usage.images.bytes)}
            </strong>
            <div>
              <span className={s.pill}>{t("media.retention", { hours: data.ttl_hours })}</span>
            </div>
          </div>
          <div className={s.kpiCard}>
            <small>{t("media.kpi.videos")}</small>
            <strong>
              {data.usage.videos.count} · {bytes(data.usage.videos.bytes)}
            </strong>
          </div>
          <div className={s.kpiCard}>
            <small>{t("media.kpi.tasks")}</small>
            <strong>
              {t("media.kpi.tasksValue", { active: data.tasks.active, failed: data.tasks.failed })}
            </strong>
          </div>
        </div>
      )}
      <div className={s.tabs} role="tablist" aria-label={t("media.title")}>
        <SegmentThumb />
        {(
          [
            ["library", t("media.tab.library")],
            ["tasks", t("media.tab.tasks")],
            ["models", t("media.tab.models")],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            role="tab"
            aria-selected={tab === value}
            className={tab === value ? s.selectedTab : ""}
            onClick={() => {
              setTab(value);
              setAssetCursors([]);
              setTaskCursors([]);
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "library" && (
        <>
          <div className={s.filters}>
            <Select
              aria-label={t("media.models.kind")}
              value={kind}
              onChange={(value) => {
                setKind(value);
                setAssetCursors([]);
              }}
            >
              <option value="">{t("media.filter.all")}</option>
              <option value="image">{t("media.filter.images")}</option>
              <option value="video">{t("media.filter.videos")}</option>
            </Select>
          </div>
          <ResourceState {...assets} />
          {assets.data &&
            (assets.data.items.length ? (
              <div className={s.mediaGrid}>
                {assets.data.items.map((item) => {
                  const remaining = remainingLabel(item.expires_at, t);
                  const isVideo = item.kind === "video";
                  return (
                    <button
                      key={text(item.id)}
                      className={s.mediaCard}
                      onClick={() => openAsset(item)}
                      title={text(item.model)}
                    >
                      <span className={`${s.mediaThumb} ${isVideo ? s.isVideo : ""}`}>
                        {isVideo ? (
                          <Icon name="play" />
                        ) : (
                          <img
                            src={`/admin/media/assets/${encodeURIComponent(text(item.id))}/content`}
                            alt={`${t(isVideo ? "media.kind.video" : "media.kind.image")} ${text(item.model)} ${remaining.text}`}
                            loading="lazy"
                            decoding="async"
                          />
                        )}
                      </span>
                      <span className={s.mediaMeta}>
                        <strong title={text(item.model)}>{text(item.model)}</strong>
                        <small>
                          <Badge tone={isVideo ? "neutral" : "neutral"}>
                            {t(isVideo ? "media.kind.video" : "media.kind.image")}
                          </Badge>{" "}
                          {bytes(number(item.size) ?? 0)}
                        </small>
                        <small className={remaining.warn ? s.warnText : ""}>
                          {t("media.remaining", { time: remaining.text })}
                        </small>
                      </span>
                    </button>
                  );
                })}
              </div>
            ) : (
              <Empty title={t("media.empty.library")}>{t("media.empty.libraryHint")}</Empty>
            ))}
          <div className={s.pagination}>
            <span>第 {assetCursors.length + 1} 页</span>
            <div className={s.actions}>
              <button
                disabled={!assetCursors.length || assets.loading}
                onClick={() => setAssetCursors((old) => old.slice(0, -1))}
              >
                {t("logs.prevPage")}
              </button>
              <button
                disabled={!assets.data?.has_more || assets.loading}
                onClick={() => {
                  if (assets.data?.next_cursor)
                    setAssetCursors((old) => [...old, assets.data!.next_cursor!]);
                }}
              >
                {t("logs.nextPage")}
              </button>
            </div>
          </div>
        </>
      )}

      {tab === "tasks" && (
        <>
          <div className={s.filters}>
            <Select
              aria-label={t("media.tab.tasks")}
              value={taskStatus}
              onChange={(value) => {
                setTaskStatus(value);
                setTaskCursors([]);
              }}
            >
              <option value="">{t("media.filter.all")}</option>
              <option value="active">{t("media.filter.active")}</option>
              <option value="completed">{t("media.filter.completed")}</option>
              <option value="failed">{t("media.filter.failed")}</option>
            </Select>
          </div>
          <ResourceState {...tasks} />
          {tasks.data &&
            (tasks.data.items.length ? (
              <div className={s.tableWrap}>
                <table>
                  <thead>
                    <tr>
                      <th>{t("logs.col.time")}</th>
                      <th>{t("media.tasks.taskId")}</th>
                      <th>{t("media.tasks.model")}</th>
                      <th>{t("media.tasks.account")}</th>
                      <th>{t("logs.col.status")}</th>
                      <th>{t("media.tasks.credit")}</th>
                      <th>{t("media.tasks.asset")}</th>
                      <th>{t("logs.col.actions")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tasks.data.items.map((item) => {
                      const status = text(item.status);
                      const active = status === "queued" || status === "in_progress";
                      return (
                        <tr key={text(item.task_id)}>
                          <td>
                            {typeof item.created === "number"
                              ? new Date(item.created * 1000).toLocaleString("zh-CN")
                              : text(item.created)}
                          </td>
                          <td className={s.mono} title={text(item.task_id)}>
                            {text(item.task_id).length > 18
                              ? `${text(item.task_id).slice(0, 18)}…`
                              : text(item.task_id)}
                          </td>
                          <td>{text(item.model)}</td>
                          <td title={text(item.account_key)}>
                            {text(item.profile)}
                            <small className={s.mono}>{text(item.account_key).slice(0, 12)}</small>
                          </td>
                          <td>
                            <Badge tone={taskStatusTone(status)}>
                              {t(
                                status === "queued"
                                  ? "media.status.queued"
                                  : status === "in_progress"
                                    ? "media.status.in_progress"
                                    : status === "completed"
                                      ? "media.status.completed"
                                      : "media.status.failed",
                              )}
                            </Badge>
                            {active && item.error ? (
                              <>
                                {" "}
                                <Badge tone="warn">{t("media.tasks.retrying")}</Badge>
                              </>
                            ) : null}
                            {item.error ? (
                              <small title={text(item.error)}>
                                {text(item.error).length > 40
                                  ? `${text(item.error).slice(0, 40)}…`
                                  : text(item.error)}
                              </small>
                            ) : null}
                          </td>
                          <td>{number(item.credit) === null ? "—" : number(item.credit)}</td>
                          <td>
                            {item.media_id ? (
                              <button onClick={() => openAssetById(text(item.media_id))}>
                                {t("media.tasks.view")}
                              </button>
                            ) : (
                              <span className={s.muted}>—</span>
                            )}
                          </td>
                          <td>
                            {active && (
                              <button
                                onClick={() => {
                                  void api
                                    .post(
                                      `/media/tasks/${encodeURIComponent(text(item.task_id))}/refresh`,
                                    )
                                    .then(() => {
                                      tasks.reload();
                                      summary.reload();
                                    })
                                    .catch((err: unknown) =>
                                      notify({
                                        key: `media-task-${text(item.task_id)}`,
                                        tone: "bad",
                                        title: errorMessage(err),
                                      }),
                                    );
                                }}
                              >
                                {t("media.tasks.refresh")}
                              </button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty title={t("media.empty.tasks")} />
            ))}
          <div className={s.pagination}>
            <span>第 {taskCursors.length + 1} 页</span>
            <div className={s.actions}>
              <button
                disabled={!taskCursors.length || tasks.loading}
                onClick={() => setTaskCursors((old) => old.slice(0, -1))}
              >
                {t("logs.prevPage")}
              </button>
              <button
                disabled={!tasks.data?.has_more || tasks.loading}
                onClick={() => {
                  if (tasks.data?.next_cursor)
                    setTaskCursors((old) => [...old, tasks.data!.next_cursor!]);
                }}
              >
                {t("logs.nextPage")}
              </button>
            </div>
          </div>
        </>
      )}

      {tab === "models" && data && (
        <>
          <div className={s.tableWrap}>
            <table>
              <thead>
                <tr>
                  <th>{t("media.models.model")}</th>
                  <th>{t("media.models.kind")}</th>
                  <th>{t("media.models.source")}</th>
                  <th>{t("media.models.accounts")}</th>
                </tr>
              </thead>
              <tbody>
                {data.models.map((model) => (
                  <tr key={model.id}>
                    <td className={s.mono}>
                      {model.id}
                      {model.defaults.length > 0 && (
                        <Badge tone="neutral">
                          {t("media.models.default")} ·{" "}
                          {model.defaults
                            .map((kindName) =>
                              t(
                                kindName === "image"
                                  ? "media.models.kindImage"
                                  : kindName === "edit"
                                    ? "media.models.kindEdit"
                                    : "media.models.kindVideo",
                              ),
                            )
                            .join("/")}
                        </Badge>
                      )}
                    </td>
                    <td>
                      {model.kinds
                        .map((kindName) =>
                          t(
                            kindName === "image"
                              ? "media.models.kindImage"
                              : kindName === "edit"
                                ? "media.models.kindEdit"
                                : "media.models.kindVideo",
                          ),
                        )
                        .join(" / ")}
                    </td>
                    <td>
                      {model.sources
                        .map((source) =>
                          t(
                            source === "catalog"
                              ? "media.models.sourceCatalog"
                              : "media.models.sourceStatic",
                          ),
                        )
                        .join(" + ")}
                    </td>
                    <td>
                      {model.accounts > 0 ? (
                        model.accounts
                      ) : (
                        <Badge tone="warn">{t("media.models.noAccounts")}</Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className={s.note}>{t("media.models.note")}</p>
        </>
      )}

      {preview && (
        <AssetPreview
          item={preview}
          configured={configured}
          onClose={() => setPreview(null)}
          onDeleted={() => {
            assets.reload();
            summary.reload();
          }}
        />
      )}
      {settingsOpen && (
        <MediaSettingsDrawer
          onClose={() => setSettingsOpen(false)}
          onSaved={() => {
            summary.reload();
            assets.reload();
          }}
        />
      )}
    </>
  );
}
