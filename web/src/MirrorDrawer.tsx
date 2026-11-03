import { useState, type ReactNode } from "react";
import { api, errorMessage, list, number, object, text } from "./api";
import { Badge, Drawer, ErrorNotice, Icon, RefreshIcon } from "./components";
import { useT } from "./i18n";
import s from "./ui.module.scss";

type Probe = {
  mirror: string;
  ok: boolean;
  latency: number | null;
  speed: number | null;
  error: string | null;
};

function host(mirror: string): string {
  try {
    const url = new URL(mirror);
    return url.host + (url.pathname === "/" ? "" : url.pathname);
  } catch {
    return mirror;
  }
}

function rate(speed: number): string {
  return speed >= 1024 ? `${(speed / 1024).toFixed(1)} MB/s` : `${Math.round(speed)} KB/s`;
}

// The server stores prefixes with a trailing slash; match that so a typed prefix finds its result.
function prefix(value: string): string {
  const trimmed = value.trim();
  return trimmed && !trimmed.endsWith("/") ? `${trimmed}/` : trimmed;
}

/** The fourth overview card: the source in use, opening the drawer to switch and test. */
export function MirrorCard({ mirror, onOpen }: { mirror: string; onOpen: () => void }) {
  const t = useT();
  return (
    <button type="button" className={`${s.kpiCard} ${s.kpiAction}`} onClick={onOpen}>
      <div>
        <span>{t("updates.mirror")}</span>
        <span>
          <Icon name="down" />
        </span>
      </div>
      <strong className={s.kpiText}>{mirror ? host(mirror) : t("updates.mirrorDirect")}</strong>
      <small>{t("updates.mirrorCardHint")}</small>
    </button>
  );
}

export function MirrorDrawer({
  mirror,
  mirrors,
  custom,
  locked,
  githubToken,
  onSaved,
  onClose,
}: {
  mirror: string;
  mirrors: string[];
  custom: string;
  locked: boolean;
  githubToken: boolean;
  onSaved: () => void;
  onClose: () => void;
}) {
  const t = useT();
  const [draft, setDraft] = useState(custom);
  const [results, setResults] = useState<Record<string, Probe>>({});
  const [tokenDraft, setTokenDraft] = useState("");
  const [tokenSaving, setTokenSaving] = useState(false);
  // Sources under test; "*" while every source is being tested at once.
  const [testing, setTesting] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const test = (key: string, target?: string) => {
    setTesting((current) => [...current, key]);
    setError(null);
    void api
      .post<unknown>(
        "/update/mirrors/test",
        target === undefined ? undefined : { mirror: target },
        { timeout: 60000 },
      )
      .then((response) => {
        const probes = list(object(response.data).results, t("updates.mirror")).map(
          (item): Probe => ({
            mirror: typeof item.mirror === "string" ? item.mirror : "",
            ok: item.ok === true,
            latency: number(item.latency_ms),
            speed: number(item.speed),
            error: item.error ? text(item.error) : null,
          }),
        );
        // A single test keeps only its own row, even if an older server answers for every source.
        const kept = target === undefined ? probes : probes.filter((probe) => probe.mirror === key);
        setResults((current) => ({
          ...current,
          ...Object.fromEntries(kept.map((probe) => [probe.mirror, probe])),
        }));
      })
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setTesting((current) => current.filter((item) => item !== key)));
  };
  const use = (target: string, remember: boolean) => {
    setSaving(true);
    setError(null);
    void api
      .post("/update/mirror", remember ? { mirror: target, custom: target } : { mirror: target })
      .then(onSaved)
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setSaving(false));
  };
  const saveToken = () => {
    setTokenSaving(true);
    setError(null);
    void api
      .post("/update/mirror", { github_token: tokenDraft.trim() })
      .then(() => {
        setTokenDraft("");
        onSaved();
      })
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setTokenSaving(false));
  };
  const everything = testing.includes("*");
  const fastest = Object.values(results)
    .filter((probe) => probe.ok && probe.speed !== null)
    .sort((a, b) => (b.speed ?? 0) - (a.speed ?? 0))[0]?.mirror;
  const row = (key: string, label: string, name: ReactNode, remember = false) => {
    const probe = results[key];
    const empty = remember && !key;
    const inUse = !empty && key === mirror;
    const running = everything || testing.includes(key);
    return (
      <li
        key={remember ? "custom" : key || "direct"}
        className={`${inUse ? s.mirrorCurrent : ""} ${remember ? s.mirrorCustom : ""}`}
      >
        <span className={s.mirrorName}>
          {name}
          {!empty && key === fastest && <Badge tone="good">{t("updates.mirrorFastest")}</Badge>}
          {inUse && <Badge>{t("updates.mirrorInUse")}</Badge>}
        </span>
        <span className={s.mirrorStats}>
          {!probe ? (
            <small className={s.storageSubtle}>{t("updates.mirrorUntested")}</small>
          ) : probe.ok && probe.speed !== null ? (
            `${probe.latency ?? "—"} ms · ${rate(probe.speed)}`
          ) : (
            <Badge tone="bad">{probe.error ?? "—"}</Badge>
          )}
        </span>
        <span className={s.mirrorActions}>
          <button
            type="button"
            aria-label={`${t("updates.mirrorTestOne")} ${label}`}
            disabled={running || empty}
            onClick={() => test(key, remember ? draft.trim() : key)}
          >
            <RefreshIcon spinning={running} />
            {t("updates.mirrorTestOne")}
          </button>
          <button
            type="button"
            aria-label={`${t("updates.mirrorUse")} ${label}`}
            disabled={locked || saving || inUse || empty || probe?.ok === false}
            onClick={() => use(remember ? draft.trim() : key, remember)}
          >
            {inUse ? t("updates.mirrorInUse") : t("updates.mirrorUse")}
          </button>
        </span>
      </li>
    );
  };
  return (
    <Drawer title={t("updates.mirror")} onClose={onClose}>
      <div className={s.panelBody}>
        <p className={s.storageLead}>{t("updates.mirrorDesc")}</p>
        {locked && <small className={s.storageSubtle}>{t("updates.mirrorLocked")}</small>}
        <div className={s.confirmActions}>
          <button type="button" disabled={everything} onClick={() => test("*")}>
            <RefreshIcon spinning={everything} />
            {everything ? t("updates.mirrorTesting") : t("updates.mirrorTestAll")}
          </button>
        </div>
        <ErrorNotice message={error} />
        <ul className={s.mirrorList} aria-label={t("updates.mirrorResults")}>
          {row("", t("updates.mirrorDirect"), t("updates.mirrorDirect"))}
          {mirrors.map((candidate) => row(candidate, host(candidate), host(candidate)))}
          {row(
            prefix(draft),
            t("updates.mirrorCustomSlot"),
            <input
              aria-label={t("updates.mirrorCustomSlot")}
              placeholder={t("updates.mirrorCustomPlaceholder")}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
            />,
            true,
          )}
        </ul>
        <div className={s.confirmActions}>
          <input
            aria-label={t("updates.mirrorToken")}
            autoComplete="off"
            placeholder={
              githubToken ? t("updates.mirrorTokenSet") : t("updates.mirrorTokenPlaceholder")
            }
            type="password"
            value={tokenDraft}
            onChange={(e) => setTokenDraft(e.target.value)}
          />
          <button type="button" disabled={tokenSaving || !tokenDraft.trim()} onClick={saveToken}>
            {t("updates.mirrorTokenSave")}
          </button>
        </div>
        <small className={s.storageSubtle}>{t("updates.mirrorTokenHint")}</small>
      </div>
    </Drawer>
  );
}
