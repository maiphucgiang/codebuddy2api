import { useEffect, useState } from "react";
import { api, errorMessage, object, useSession } from "../api";
import { Badge, ErrorNotice, Panel, ResourceState } from "../components";
import s from "../ui.module.scss";
import { t as translate, useT } from "../i18n";
import { notify } from "../toast";

type Account = {
  username: string;
  password_source: string;
  bootstrap: boolean;
  changed_at: number;
};

function normalize(value: unknown): Account {
  const data = object(value, translate("account.title"));
  if (
    typeof data.username !== "string" ||
    typeof data.password_source !== "string" ||
    typeof data.bootstrap !== "boolean" ||
    typeof data.changed_at !== "number"
  )
    throw new Error(translate("account.invalidResponse"));
  return {
    username: data.username,
    password_source: data.password_source,
    bootstrap: data.bootstrap,
    changed_at: data.changed_at,
  };
}

export function AccountPanel() {
  const t = useT();
  const [account, setAccount] = useState<Account | null>(null);
  const [username, setUsername] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    void api
      .get("/account")
      .then((response) => {
        const data = normalize(response.data);
        setAccount(data);
        setUsername(data.username);
      })
      .catch((reason: unknown) => setError(errorMessage(reason)))
      .finally(() => setLoading(false));
  };
  useEffect(load, []);

  const changed = account !== null && (username !== account.username || newPassword.length > 0);
  const save = (event: React.FormEvent) => {
    event.preventDefault();
    if (!account) return;
    if (newPassword !== confirmation) {
      setError(t("account.mismatch"));
      return;
    }
    const body: Record<string, string> = { current_password: currentPassword };
    if (username !== account.username) body.username = username;
    if (newPassword) body.new_password = newPassword;
    setBusy(true);
    setError(null);
    void api
      .put("/account", body)
      .then((response) => {
        const data = object(response.data, t("account.title"));
        if (typeof data.username !== "string" || typeof data.csrf_token !== "string")
          throw new Error(t("account.missingCsrf"));
        useSession.setState({ csrf: data.csrf_token, bootstrap: data.bootstrap === true });
        setAccount({ ...account, username: data.username, bootstrap: data.bootstrap === true });
        setUsername(data.username);
        setCurrentPassword("");
        setNewPassword("");
        setConfirmation("");
        notify({ key: "account-saved", tone: "good", title: t("account.saved") });
      })
      .catch((reason: unknown) => setError(errorMessage(reason)))
      .finally(() => setBusy(false));
  };

  const revokeAll = () => {
    if (!window.confirm(t("account.revokeConfirm"))) return;
    setBusy(true);
    setError(null);
    void api
      .delete("/sessions")
      .then(() =>
        useSession.setState({
          status: "anonymous",
          csrf: null,
          bootstrap: false,
          error: null,
        }),
      )
      .catch((reason: unknown) => setError(errorMessage(reason)))
      .finally(() => setBusy(false));
  };

  return (
    <>
      <ResourceState loading={loading} error={error && !account ? error : null} reload={load} />
      {account && (
        <div className={s.stack}>
          <Panel
            title={t("account.title")}
            action={
              <Badge tone={account.bootstrap ? "warn" : "good"}>
                {account.bootstrap ? t("account.bootstrapInitial") : t("account.bootstrapChanged")}
              </Badge>
            }
          >
            <div className={s.panelBody}>
              <form className={`${s.drawerForm} ${s.accountForm}`} onSubmit={save}>
                <label className={s.field}>
                  {t("login.username")}
                  <input
                    name="username"
                    autoComplete="username"
                    value={username}
                    onChange={(event) => setUsername(event.target.value)}
                    pattern="[A-Za-z0-9_.\-]{1,32}"
                    required
                  />
                </label>
                <label className={s.field}>
                  {t("common.currentPassword")}
                  <input
                    name="current-password"
                    type="password"
                    autoComplete="current-password"
                    value={currentPassword}
                    onChange={(event) => setCurrentPassword(event.target.value)}
                    required
                  />
                </label>
                <label className={s.field}>
                  {t("account.newPassword")}
                  <input
                    name="new-password"
                    type="password"
                    autoComplete="new-password"
                    value={newPassword}
                    onChange={(event) => setNewPassword(event.target.value)}
                    minLength={8}
                    maxLength={128}
                    placeholder={t("account.passwordHint")}
                  />
                </label>
                <label className={s.field}>
                  {t("account.confirmPassword")}
                  <input
                    name="confirm-new-password"
                    type="password"
                    autoComplete="new-password"
                    value={confirmation}
                    onChange={(event) => setConfirmation(event.target.value)}
                    minLength={newPassword ? 8 : undefined}
                    maxLength={128}
                    placeholder={t("account.passwordHint")}
                  />
                </label>
                <ErrorNotice message={error} />
                <div className={s.actions}>
                  <button
                    className={s.primary}
                    disabled={busy || !changed || !currentPassword}
                    aria-busy={busy}
                  >
                    {busy ? t("account.saving") : t("account.save")}
                  </button>
                </div>
              </form>
            </div>
          </Panel>
          <Panel title={t("account.sessionsTitle")} hint={t("account.sessionsHint")}>
            <div className={s.panelBody}>
              <div className={s.actions}>
                <button className={s.danger} type="button" disabled={busy} onClick={revokeAll}>
                  {t("account.revokeAll")}
                </button>
              </div>
            </div>
          </Panel>
        </div>
      )}
    </>
  );
}
