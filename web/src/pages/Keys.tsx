import { useState } from "react";
import { api, errorMessage, list, metric, object, useResource, type RecordValue } from "../api";
import {
  Badge,
  Drawer,
  DrawerPresence,
  Empty,
  ErrorNotice,
  Icon,
  PageTitle,
  Panel,
  ResourceState,
} from "../components";
import s from "../ui.module.scss";
import { t } from "../i18n";
import { notify } from "../toast";

type KeyRow = {
  id: string;
  name: string;
  type: "admin" | "standard";
  prefix: string;
  masked: string | null;
  enabled: boolean;
  expires_at: number | null;
  ip_allow: string[] | null;
  rpm: number | null;
  models: string[] | null;
  created_at: number | null;
  last_used_at: number | null;
  version: number;
  usage: RecordValue | null;
  overridden: boolean;
};
type KeysData = { external: { origin: string } | null; items: KeyRow[] };
type ModelOption = { id: string; label: string };
type Editor = { item: KeyRow | null };
type Secret = { value: string; oneTime: boolean };

function originLabel(origin: string) {
  if (origin === "cli") return t("keys.origin.cli");
  if (origin === "environment") return t("keys.origin.environment");
  if (origin === "dotenv") return t("keys.origin.dotenv");
  return t("keys.origin.external");
}

function nullableNumber(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function stringList(value: unknown) {
  return Array.isArray(value) && value.every((item) => typeof item === "string")
    ? (value as string[])
    : null;
}
function normalize(value: unknown): KeysData {
  const data = object(value, t("keys.title"));
  const external = data.external == null ? null : object(data.external, t("keys.externalTitle"));
  return {
    external: external && typeof external.origin === "string" ? { origin: external.origin } : null,
    items: list(data.items, t("keys.title")).map((item) => {
      if (
        typeof item.id !== "string" ||
        typeof item.name !== "string" ||
        (item.type !== "admin" && item.type !== "standard") ||
        typeof item.version !== "number"
      )
        throw new Error(t("keys.invalidRecord"));
      return {
        id: item.id,
        name: item.name,
        type: item.type,
        prefix: typeof item.prefix === "string" ? item.prefix : "—",
        masked: typeof item.masked === "string" ? item.masked : null,
        enabled: item.enabled !== false,
        expires_at: nullableNumber(item.expires_at),
        ip_allow: stringList(item.ip_allow),
        rpm: nullableNumber(item.rpm),
        models: stringList(item.models),
        created_at: nullableNumber(item.created_at),
        last_used_at: nullableNumber(item.last_used_at),
        version: item.version,
        usage:
          item.usage && typeof item.usage === "object" && !Array.isArray(item.usage)
            ? (item.usage as RecordValue)
            : null,
        overridden: item.overridden === true,
      };
    }),
  };
}
function normalizeModels(value: unknown): ModelOption[] {
  const data = object(value, t("keys.models"));
  const options: ModelOption[] = [{ id: "auto", label: t("keys.auto") }];
  const seen = new Set(options.map((item) => item.id));
  for (const item of list(data.models, "模型列表")) {
    if (typeof item.id !== "string" || seen.has(item.id)) continue;
    seen.add(item.id);
    options.push({
      id: item.id,
      label: typeof item.public_id === "string" ? item.public_id : item.id,
    });
  }
  return options;
}
function localDateTime(timestamp: number | null) {
  if (timestamp === null) return "";
  const date = new Date(timestamp * 1000);
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}
function formatTime(timestamp: number | null) {
  return timestamp === null ? "—" : new Date(timestamp * 1000).toLocaleString("zh-CN");
}
function copied() {
  notify({ key: "keys-copied", tone: "good", title: t("keys.copied") });
}

export function Keys() {
  const resource = useResource("/keys", normalize);
  const modelResource = useResource("/models", normalizeModels);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [name, setName] = useState("");
  const [type, setType] = useState<"standard" | "admin">("standard");
  const [enabled, setEnabled] = useState(true);
  const [expiry, setExpiry] = useState("never");
  const [customExpiry, setCustomExpiry] = useState("");
  const [ipAllow, setIpAllow] = useState("");
  const [rpm, setRpm] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [secret, setSecret] = useState<Secret | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const data = resource.data;
  const adminUnavailable =
    Boolean(data?.external) || Boolean(data?.items.some((item) => item.type === "admin"));

  const openEditor = (item: KeyRow | null) => {
    setEditor({ item });
    setName(item?.name ?? "");
    setType(item?.type ?? "standard");
    setEnabled(item?.enabled ?? true);
    setExpiry(item?.expires_at == null ? "never" : "custom");
    setCustomExpiry(localDateTime(item?.expires_at ?? null));
    setIpAllow(item?.ip_allow?.join("\n") ?? "");
    setRpm(item?.rpm == null ? "" : String(item.rpm));
    setModels(item?.models ?? []);
    setError(null);
  };
  const finish = (result: RecordValue, oneTime: boolean) => {
    if (typeof result.secret === "string") setSecret({ value: result.secret, oneTime });
    setEditor(null);
    resource.reload();
  };
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!editor) return;
    const kind = editor.item?.type ?? type;
    let expiresAt: number | null = null;
    if (expiry === "custom") {
      const parsed = new Date(customExpiry).getTime();
      if (!customExpiry || !Number.isFinite(parsed)) {
        setError(t("keys.invalidExpiry"));
        return;
      }
      expiresAt = parsed / 1000;
    } else if (expiry !== "never") {
      expiresAt = Date.now() / 1000 + Number(expiry) * 86400;
    }
    const limits =
      kind === "admin"
        ? {}
        : {
            expires_at: expiresAt,
            ip_allow: ipAllow.trim()
              ? ipAllow
                  .split(/[\n,]/)
                  .map((item) => item.trim())
                  .filter(Boolean)
              : null,
            rpm: rpm.trim() ? Number(rpm) : null,
            models: models.length ? models : null,
          };
    setBusy(true);
    setError(null);
    const request = editor.item
      ? api.patch(`/keys/${encodeURIComponent(editor.item.id)}`, {
          version: editor.item.version,
          name,
          enabled,
          ...limits,
        })
      : api.post("/keys", { name, type, ...limits });
    void request
      .then((response) => finish(object(response.data, t("keys.title")), kind === "admin"))
      .catch((reason: unknown) => setError(errorMessage(reason)))
      .finally(() => setBusy(false));
  };
  const reveal = (item: KeyRow, copy = false) => {
    setError(null);
    void api
      .post(`/keys/${encodeURIComponent(item.id)}/reveal`)
      .then((response) => {
        const result = object(response.data, t("keys.title"));
        if (typeof result.secret !== "string") throw new Error(t("keys.invalidSecret"));
        if (copy) {
          return navigator.clipboard.writeText(result.secret).then(copied);
        }
        setSecret({ value: result.secret, oneTime: false });
      })
      .catch((reason: unknown) => setError(errorMessage(reason)));
  };
  const rotate = (item: KeyRow) => {
    if (!window.confirm(t("keys.rotateConfirm", { name: item.name }))) return;
    setError(null);
    void api
      .post(`/keys/${encodeURIComponent(item.id)}/rotate`, { version: item.version })
      .then((response) => finish(object(response.data, t("keys.title")), item.type === "admin"))
      .catch((reason: unknown) => setError(errorMessage(reason)));
  };
  const remove = (item: KeyRow) => {
    if (!window.confirm(t("keys.deleteConfirm", { name: item.name }))) return;
    setError(null);
    void api
      .delete(`/keys/${encodeURIComponent(item.id)}`, { data: { version: item.version } })
      .then(() => resource.reload())
      .catch((reason: unknown) => setError(errorMessage(reason)));
  };
  const copySecret = () => {
    if (!secret) return;
    void navigator.clipboard
      .writeText(secret.value)
      .then(copied)
      .catch((reason: unknown) => setError(errorMessage(reason)));
  };
  const modelLabel = (id: string) =>
    modelResource.data?.find((item) => item.id === id)?.label ?? id;

  return (
    <>
      <PageTitle
        title={t("keys.title")}
        description={t("keys.description")}
        actions={
          <button className={s.primary} disabled={!data} onClick={() => openEditor(null)}>
            <Icon name="plus" />
            {t("keys.create")}
          </button>
        }
      />
      {!editor && <ErrorNotice message={error} />}
      <ResourceState loading={resource.loading} error={resource.error} reload={resource.reload} />
      {data && (
        <div className={s.stack}>
          {data.external && (
            <Panel title={t("keys.externalTitle")}>
              <p className={s.note}>
                {t("keys.externalOccupied", { origin: originLabel(data.external.origin) })}
              </p>
            </Panel>
          )}
          <Panel title={t("keys.title")}>
            {!data.items.length ? (
              <Empty title={t("keys.empty")} />
            ) : (
              <div className={s.tableWrap}>
                <table className={s.keysTable}>
                  <thead>
                    <tr>
                      <th>{t("keys.name")}</th>
                      <th>{t("keys.type")}</th>
                      <th>{t("keys.secret")}</th>
                      <th>{t("keys.status")}</th>
                      <th>{t("keys.expiry")}</th>
                      <th>IP</th>
                      <th>RPM</th>
                      <th>{t("keys.models")}</th>
                      <th>{t("keys.usage24h")}</th>
                      <th>{t("keys.lastUsed")}</th>
                      <th>{t("keys.actions")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map((item) => {
                      const expired =
                        item.expires_at !== null && item.expires_at <= Date.now() / 1000;
                      const status = item.overridden
                        ? "keys.overridden"
                        : !item.enabled
                          ? "keys.disabled"
                          : expired
                            ? "keys.expired"
                            : "keys.enabled";
                      return (
                        <tr key={item.id}>
                          <td className={s.wrapCell}>{item.name}</td>
                          <td>
                            <Badge>
                              {item.type === "admin" ? t("keys.admin") : t("keys.standard")}
                            </Badge>
                          </td>
                          <td className={s.mono}>{item.masked ?? item.prefix}</td>
                          <td>
                            <Badge tone={status === "keys.enabled" ? "good" : "warn"}>
                              {t(status)}
                            </Badge>
                          </td>
                          <td>
                            {item.expires_at === null
                              ? t("keys.never")
                              : formatTime(item.expires_at)}
                          </td>
                          <td className={s.wrapCell}>
                            {item.ip_allow?.join("、") || t("keys.unlimited")}
                          </td>
                          <td>{item.rpm ?? t("keys.unlimited")}</td>
                          <td className={s.wrapCell}>
                            {item.models === null
                              ? t("keys.all")
                              : item.models.map((id) => modelLabel(id)).join("、") || t("keys.all")}
                          </td>
                          <td>{metric(item.usage?.requests)}</td>
                          <td>{formatTime(item.last_used_at)}</td>
                          <td>
                            <div className={s.rowActions}>
                              {!item.overridden && (
                                <>
                                  <button onClick={() => openEditor(item)}>{t("keys.edit")}</button>
                                  <button onClick={() => rotate(item)}>{t("keys.rotate")}</button>
                                  {item.type === "standard" && (
                                    <>
                                      <button onClick={() => reveal(item)}>
                                        {t("keys.reveal")}
                                      </button>
                                      <button onClick={() => reveal(item, true)}>
                                        {t("keys.copy")}
                                      </button>
                                    </>
                                  )}
                                </>
                              )}
                              <button onClick={() => remove(item)}>{t("keys.delete")}</button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
      )}
      <DrawerPresence>
        {editor && (
          <Drawer
            title={editor.item ? t("keys.editTitle") : t("keys.create")}
            onClose={() => setEditor(null)}
            dismissDisabled={busy}
          >
            <form className={s.drawerForm} onSubmit={submit}>
              <label className={s.field}>
                {t("keys.name")}
                <input
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  maxLength={64}
                  required
                  autoFocus
                />
              </label>
              {!editor.item && (
                <label className={s.field}>
                  {t("keys.type")}
                  <select
                    value={type}
                    onChange={(event) => setType(event.target.value as "standard" | "admin")}
                  >
                    <option value="standard">{t("keys.standardFull")}</option>
                    <option value="admin" disabled={adminUnavailable}>
                      {t("keys.adminFull")}
                    </option>
                  </select>
                </label>
              )}
              {adminUnavailable && !editor.item && (
                <p className={s.note}>{t("keys.adminUnavailable")}</p>
              )}
              {editor.item && (
                <label className={s.check}>
                  <input
                    type="checkbox"
                    checked={enabled}
                    onChange={(event) => setEnabled(event.target.checked)}
                  />
                  {t("keys.enabled")}
                </label>
              )}
              {(editor.item?.type ?? type) === "standard" && (
                <>
                  <label className={s.field}>
                    {t("keys.expiry")}
                    <select value={expiry} onChange={(event) => setExpiry(event.target.value)}>
                      <option value="never">{t("keys.never")}</option>
                      <option value="1">{t("keys.expiryDays", { n: 1 })}</option>
                      <option value="7">{t("keys.expiryDays", { n: 7 })}</option>
                      <option value="30">{t("keys.expiryDays", { n: 30 })}</option>
                      <option value="90">{t("keys.expiryDays", { n: 90 })}</option>
                      <option value="custom">{t("keys.customExpiry")}</option>
                    </select>
                  </label>
                  {expiry === "custom" && (
                    <label className={s.field}>
                      {t("keys.customExpiryLabel")}
                      <input
                        type="datetime-local"
                        value={customExpiry}
                        onChange={(event) => setCustomExpiry(event.target.value)}
                        required
                      />
                    </label>
                  )}
                  <label className={s.field}>
                    {t("keys.ipAllow")}
                    <textarea
                      value={ipAllow}
                      onChange={(event) => setIpAllow(event.target.value)}
                      placeholder={t("keys.ipHint")}
                      rows={4}
                    />
                  </label>
                  <label className={s.field}>
                    RPM
                    <input
                      type="number"
                      min={1}
                      max={100000}
                      step={1}
                      value={rpm}
                      onChange={(event) => setRpm(event.target.value)}
                      placeholder={t("keys.rpmHint")}
                    />
                  </label>
                  <fieldset className={s.fieldset}>
                    <legend>{t("keys.models")}</legend>
                    <p>{t("keys.modelsHint")}</p>
                    {(modelResource.data ?? []).map((option) => (
                      <label className={s.check} key={option.id}>
                        <input
                          type="checkbox"
                          checked={models.includes(option.id)}
                          onChange={(event) =>
                            setModels((current) =>
                              event.target.checked
                                ? [...current, option.id]
                                : current.filter((id) => id !== option.id),
                            )
                          }
                        />
                        {option.label}
                      </label>
                    ))}
                  </fieldset>
                </>
              )}
              <ErrorNotice message={error} />
              <div className={s.actions}>
                <button type="button" disabled={busy} onClick={() => setEditor(null)}>
                  {t("common.cancel")}
                </button>
                <button className={s.primary} disabled={busy || !name.trim()} aria-busy={busy}>
                  {editor.item ? t("keys.saveChanges") : t("keys.createSubmit")}
                </button>
              </div>
            </form>
          </Drawer>
        )}
      </DrawerPresence>
      <DrawerPresence>
        {secret && (
          <Drawer
            title={secret.oneTime ? t("keys.secretOnce") : t("keys.secretFull")}
            onClose={() => setSecret(null)}
          >
            {secret.oneTime && <p className={s.warning}>{t("keys.secretWarning")}</p>}
            <code className={s.secretValue}>{secret.value}</code>
            <div className={s.actions}>
              <button onClick={copySecret}>{t("keys.copy")}</button>
              <button className={s.primary} onClick={() => setSecret(null)}>
                {t("keys.close")}
              </button>
            </div>
          </Drawer>
        )}
      </DrawerPresence>
    </>
  );
}
