import { useEffect, useState } from "react";
import { NavLink, useParams } from "react-router";
import { api, errorMessage, list, number, object, text, useResource } from "../api";
import { useT } from "../i18n";
import {
  Badge,
  DrawerPresence,
  Empty,
  ErrorNotice,
  Icon,
  PageTitle,
  Panel,
  RefreshIcon,
  ResourceState,
} from "../components";
import { Markdown } from "../markdown";
import { MirrorCard, MirrorDrawer } from "../MirrorDrawer";
import { UpdateProgress, type UpdateJob } from "../UpdateProgress";
import { notify } from "../toast";
import s from "../ui.module.scss";

type Release = {
  tag: string;
  version: string;
  name: string;
  notes: string;
  publishedAt: string | null;
  url: string;
  installable: boolean;
};
type UpdateState = {
  current: string;
  repository: string;
  deployment: string;
  selfUpdate: boolean;
  blockReason: string | null;
  checkedAt: number | null;
  error: string | null;
  latest: string | null;
  updateAvailable: boolean;
  releases: Release[];
  job: UpdateJob;
  mirror: string;
  mirrors: string[];
  mirrorCustom: string;
  mirrorLocked: boolean;
  githubToken: boolean;
  build: Build | null;
};
// Where the running code came from: a git checkout against its tag, or an image's channel.
type Build = {
  commit: string | null;
  tagCommit: string | null;
  dirty: boolean;
  exact: boolean | null;
  channel: string | null;
};

// Fast enough for the download bar to move smoothly; the job lives on the server.
const JOB_POLL = 800;

function optional(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}
// Only https links leave the dashboard; anything else renders as plain text.
function safeUrl(value: unknown): string | null {
  return typeof value === "string" && value.startsWith("https://") ? value : null;
}

function normalize(value: unknown): UpdateState {
  const d = object(value, "更新状态");
  const self = object(d.self_update ?? {}, "更新状态");
  const job = object(d.job ?? {}, "更新任务");
  return {
    current: text(d.current),
    repository: safeUrl(d.repository) ?? "",
    deployment: text(d.deployment),
    selfUpdate: self.available === true,
    blockReason: optional(self.reason),
    checkedAt: number(d.checked_at),
    error: optional(d.error),
    latest: optional(d.latest),
    updateAvailable: d.update_available === true,
    releases: list(d.releases, "版本列表").map((item) => ({
      tag: text(item.tag),
      version: text(item.version),
      name: text(item.name),
      notes: typeof item.notes === "string" ? item.notes : "",
      publishedAt: optional(item.published_at),
      url: safeUrl(item.url) ?? "",
      installable: item.installable === true,
    })),
    job: {
      status: text(job.status),
      step: optional(job.step),
      target: optional(job.target),
      message: optional(job.message),
      backup: optional(job.backup),
      received: number(job.received),
      total: number(job.total),
    },
    mirror: typeof d.mirror === "string" ? d.mirror : "",
    mirrors: Array.isArray(d.mirrors)
      ? d.mirrors.filter((item): item is string => typeof item === "string")
      : [],
    mirrorCustom: typeof d.mirror_custom === "string" ? d.mirror_custom : "",
    mirrorLocked: d.mirror_locked === true,
    githubToken: d.github_token === true,
    build: d.build && typeof d.build === "object" ? build(object(d.build)) : null,
  };
}

function build(value: Record<string, unknown>): Build {
  return {
    commit: optional(value.commit),
    tagCommit: optional(value.tag_commit),
    dirty: value.dirty === true,
    exact: typeof value.exact === "boolean" ? value.exact : null,
    channel: optional(value.channel),
  };
}

/** Why the running code may not be the release its version names, or null when it is. */
function useBuildWarning(d: UpdateState | null | undefined): string | null {
  const t = useT();
  const b = d?.build;
  if (!d || !b) return null;
  const version = `v${d.current}`;
  const commit = b.commit ?? "—";
  if (d.deployment === "git" && b.exact === false) {
    if (!b.tagCommit) return t("updates.drift.noTag", { version, commit });
    if (b.commit !== b.tagCommit)
      return t(b.dirty ? "updates.drift.aheadDirty" : "updates.drift.ahead", {
        version,
        commit,
        tagCommit: b.tagCommit,
      });
    return t("updates.drift.dirty", { version });
  }
  if (d.deployment === "docker" && b.channel === "edge")
    return t("updates.drift.edge", { version, commit });
  return null;
}

function newer(version: string, current: string): boolean {
  const a = version.split(".").map(Number);
  const b = current.split(".").map(Number);
  for (let i = 0; i < 3; i++) if ((a[i] ?? 0) !== (b[i] ?? 0)) return (a[i] ?? 0) > (b[i] ?? 0);
  return false;
}

function date(value: string | number | null): string {
  if (value === null) return "—";
  const parsed = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(parsed.valueOf()) ? "—" : parsed.toLocaleString("zh-CN");
}

function InstallAction({
  release,
  current,
  deployment,
  warning,
  onStarted,
}: {
  release: Release;
  current: string;
  deployment: string;
  warning: string | null;
  onStarted: () => void;
}) {
  const t = useT();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const upgrade = newer(release.version, current);
  const label = t(upgrade ? "updates.install" : "updates.rollback", { tag: release.tag });
  if (!confirming)
    return (
      <div>
        <button className={upgrade ? s.primary : ""} onClick={() => setConfirming(true)}>
          <Icon name={upgrade ? "up" : "down"} />
          {label}
        </button>
      </div>
    );
  return (
    <div className={s.cleanupBox}>
      <div>
        <strong>{label}</strong>
        <p>{t(upgrade ? "updates.confirm" : "updates.confirmRollback", { tag: release.tag })}</p>
        {(deployment === "git" || deployment === "docker") && (
          <p>{t(`updates.note.${deployment}`)}</p>
        )}
        {warning && <p className={s.textWarning}>{warning}</p>}
        <ErrorNotice message={error} />
      </div>
      <span className={s.confirmActions}>
        <button disabled={busy} onClick={() => setConfirming(false)}>
          {t("updates.cancel")}
        </button>
        <button
          className={s.primary}
          disabled={busy}
          onClick={() => {
            setBusy(true);
            setError(null);
            void api
              .post("/update/install", { tag: release.tag })
              .then(onStarted)
              .catch((err: unknown) => {
                setError(errorMessage(err));
                setBusy(false);
              });
          }}
        >
          {t("updates.confirmInstall")}
        </button>
      </span>
    </div>
  );
}

function Guidance({ deployment }: { deployment: string }) {
  const t = useT();
  if (deployment === "docker")
    return (
      <pre className={s.codeBlock}>
        {`${t("updates.guide.docker")}
docker compose pull
docker compose up -d --no-build

${t("updates.guide.dockerAuto")}
0 4 * * * cd /path/to/codebuddy2api && docker compose pull -q && docker compose up -d --no-build`}
      </pre>
    );
  if (deployment === "git")
    return (
      <pre className={s.codeBlock}>
        {`${t("updates.guide.git")}
git pull --ff-only
uv sync --locked --no-build
(cd web && vp install --frozen-lockfile && vp build)

${t("updates.guide.restart")}`}
      </pre>
    );
  return null;
}

export function Updates() {
  const t = useT();
  const { tag } = useParams();
  const resource = useResource("/update", normalize);
  const { reload } = resource;
  const [checking, setChecking] = useState(false);
  const [mirrorOpen, setMirrorOpen] = useState(false);
  const d = resource.data;
  const running = d?.job.status === "running";
  useEffect(() => {
    if (!running) return;
    const timer = setTimeout(reload, JOB_POLL);
    return () => clearTimeout(timer);
  }, [running, d, reload]);
  const busy = running || d?.job.status === "restarting";
  const latest = d?.releases[0];
  const selected = d && (tag ? d.releases.find((item) => item.tag === tag) : latest);
  const canInstall = (release: Release | undefined): release is Release =>
    Boolean(d?.selfUpdate && !busy && release?.installable && release.version !== d.current);
  // Every check ends in a toast, so an unchanged page never reads as no response.
  const check = () => {
    setChecking(true);
    void api
      .post<unknown>("/update/check")
      .then((response) => {
        const result = normalize(response.data);
        notify(
          result.error
            ? {
                key: "update-check",
                tone: "bad",
                title: t("updates.checkFailed"),
                body: <span>{result.error}</span>,
              }
            : result.updateAvailable && result.latest
              ? {
                  key: "update-check",
                  tone: "good",
                  title: t("updates.found", { version: `v${result.latest}` }),
                  body: <span>{t("updates.foundBody")}</span>,
                }
              : {
                  key: "update-check",
                  tone: "good",
                  title: t("updates.upToDate"),
                  body: <span>{t("updates.upToDateBody", { version: `v${result.current}` })}</span>,
                },
        );
        reload();
      })
      .catch((err: unknown) =>
        notify({
          key: "update-check",
          tone: "bad",
          title: t("updates.checkFailed"),
          body: <span>{errorMessage(err)}</span>,
        }),
      )
      .finally(() => setChecking(false));
  };
  const buildWarning = useBuildWarning(d);
  const install = (release: Release) =>
    d && (
      <InstallAction
        release={release}
        current={d.current}
        deployment={d.deployment}
        warning={buildWarning}
        onStarted={reload}
      />
    );
  return (
    <>
      <PageTitle title={t("updates.title")} description={t("updates.desc")} />
      <ResourceState {...resource} />
      {buildWarning && (
        <div className={s.warning} role="status">
          <Icon name="alert" />
          <span>{buildWarning}</span>
        </div>
      )}
      <ErrorNotice message={d?.error ?? null} />
      {d && (
        <>
          <div className={s.kpiGrid}>
            <article className={s.kpiCard}>
              <div>
                <span>{t("updates.current")}</span>
                <button
                  type="button"
                  className={s.pill}
                  aria-label={t("updates.check")}
                  disabled={checking}
                  onClick={check}
                >
                  <RefreshIcon spinning={checking} />
                  <span className={s.pillLabel}>
                    {checking ? t("updates.checking") : t("updates.check")}
                  </span>
                </button>
              </div>
              <strong>v{d.current}</strong>
              <small>{t(`updates.deploy.${d.deployment}`)}</small>
            </article>
            <article className={s.kpiCard}>
              <div>
                <span>{t("updates.latest")}</span>
                <span>
                  <Icon name="up" />
                </span>
              </div>
              <strong>{d.latest ? `v${d.latest}` : "—"}</strong>
              <small>
                {d.updateAvailable
                  ? t("updates.available")
                  : d.latest
                    ? t("updates.upToDate")
                    : t("updates.unknown")}
              </small>
            </article>
            <article className={s.kpiCard}>
              <div>
                <span>{t("updates.checkedAt")}</span>
                <span>
                  <Icon name="refresh" />
                </span>
              </div>
              <strong className={s.kpiText}>
                {d.checkedAt ? date(d.checkedAt) : t("updates.never")}
              </strong>
              <small>{t("updates.autoCheck")}</small>
            </article>
            <MirrorCard mirror={d.mirror} onOpen={() => setMirrorOpen(true)} />
          </div>
          <DrawerPresence>
            {mirrorOpen && (
              <MirrorDrawer
                mirror={d.mirror}
                mirrors={d.mirrors}
                custom={d.mirrorCustom}
                locked={d.mirrorLocked}
                githubToken={d.githubToken}
                onSaved={reload}
                onClose={() => setMirrorOpen(false)}
              />
            )}
          </DrawerPresence>
          <div className={s.stack}>
            <Panel
              title={t("updates.panel")}
              action={
                d.repository && (
                  <a
                    className={`${s.linkButton} ${s.roundButton}`}
                    href={d.repository}
                    target="_blank"
                    rel="noreferrer"
                    aria-label={t("updates.repository")}
                    title={d.repository}
                  >
                    <Icon name="github" />
                  </a>
                )
              }
            >
              <div className={s.panelBody}>
                <UpdateProgress job={d.job} />
                {d.selfUpdate ? (
                  d.updateAvailable && canInstall(latest) ? (
                    install(latest)
                  ) : (
                    !busy && (
                      <p className={s.storageLead}>
                        {d.latest ? t("updates.upToDate") : t("updates.unknown")}
                      </p>
                    )
                  )
                ) : (
                  <>
                    <p className={s.storageLead}>{d.blockReason}</p>
                    <Guidance deployment={d.deployment} />
                  </>
                )}
              </div>
            </Panel>
            <Panel title={t("updates.releases")} hint={t("updates.releasesHint")}>
              {d.releases.length ? (
                <div className={s.releaseLayout}>
                  <nav className={s.releaseList} aria-label={t("updates.releases")}>
                    {d.releases.map((release) => (
                      <NavLink
                        key={release.tag}
                        to={`/dashboard/updates/${release.tag}`}
                        className={({ isActive }) =>
                          `${s.releaseItem} ${isActive || (!tag && release === latest) ? s.active : ""}`
                        }
                      >
                        <strong>{release.tag}</strong>
                        <span>{date(release.publishedAt)}</span>
                        {release === latest && (
                          <Badge tone="good">{t("updates.badge.latest")}</Badge>
                        )}
                        {release.version === d.current && (
                          <Badge>{t("updates.badge.current")}</Badge>
                        )}
                      </NavLink>
                    ))}
                  </nav>
                  <article className={s.releaseNotes}>
                    {selected ? (
                      <>
                        <div className={s.releaseHead}>
                          <div>
                            <h2>{selected.name}</h2>
                            <small>{date(selected.publishedAt)}</small>
                          </div>
                          {selected.url && (
                            <a
                              className={`${s.linkButton} ${s.pill}`}
                              href={selected.url}
                              target="_blank"
                              rel="noreferrer"
                            >
                              <Icon name="github" />
                              {t("updates.openGithub")}
                            </a>
                          )}
                        </div>
                        {/* The latest upgrade already has its button in the update panel. */}
                        {canInstall(selected) &&
                          !(d.updateAvailable && selected === latest) &&
                          install(selected)}
                        {selected.notes.trim() ? (
                          <Markdown source={selected.notes} />
                        ) : (
                          <Empty title={t("updates.noNotes")} />
                        )}
                      </>
                    ) : (
                      <Empty title={t("updates.notFound")} />
                    )}
                  </article>
                </div>
              ) : (
                <div className={s.panelBody}>
                  <Empty title={t("updates.emptyReleases")} />
                </div>
              )}
            </Panel>
          </div>
        </>
      )}
    </>
  );
}
