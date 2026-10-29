import { useEffect, useState } from "react";
import { NavLink, Navigate, Outlet, Route, Routes, useLocation } from "react-router";
import { api, establishSession, errorMessage, list, object, useSession } from "./api";
import { ErrorNotice, Icon } from "./components";
import { Dashboard } from "./pages/Dashboard";
import { Models } from "./pages/Models";
import { Credentials } from "./pages/Credentials";
import { Keys } from "./pages/Keys";
import { Logs } from "./pages/Logs";
import { Media } from "./pages/Media";
import { Settings } from "./pages/Settings";
import { Storage } from "./pages/Storage";
import { Updates } from "./pages/Updates";
import { Appearance, LanguageToggle } from "./appearance";
import s from "./ui.module.scss";
import { LanguageProvider, chooseLanguage, serverPreference, useT } from "./i18n";

const navigation = [
  ["", "nav.overview", "grid"],
  ["/models", "nav.models", "model"],
  ["/credentials", "nav.credentials", "account"],
  ["/keys", "nav.keys", "key"],
  ["/logs", "nav.logs", "logs"],
  ["/media", "nav.media", "image"],
  ["/storage", "nav.storage", "database"],
  ["/settings", "nav.settings", "settings"],
  ["/updates", "nav.updates", "up"],
];
export function Guard() {
  const t = useT();
  const { status, error } = useSession();
  if (status === "checking")
    return (
      <div className={s.center} role="status">
        {t("app.verifying")}
      </div>
    );
  if (status === "error")
    return (
      <div className={s.center}>
        <ErrorNotice
          message={error}
          retry={() => {
            void establishSession();
          }}
        />
        <NavLink to="/dashboard/login">{t("app.backToSignIn")}</NavLink>
      </div>
    );
  return status === "authenticated" ? <Outlet /> : <Navigate to="/dashboard/login" replace />;
}
function Shell() {
  const t = useT();
  const location = useLocation();
  const bootstrap = useSession((state) => state.bootstrap);
  const [bootstrapDismissed, setBootstrapDismissed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [updateAvailable, setUpdateAvailable] = useState(false);
  useEffect(() => {
    // Cached server-side; the gateway checks releases in the background.
    void api
      .get<unknown>("/update")
      .then((response) => setUpdateAvailable(object(response.data).update_available === true))
      .catch(() => undefined);
  }, []);
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem("codebuddy.sidebar.collapsed") === "true";
    } catch {
      return false;
    }
  });
  const toggleSidebar = () => {
    const next = !collapsed;
    setCollapsed(next);
    try {
      localStorage.setItem("codebuddy.sidebar.collapsed", String(next));
    } catch {
      /* Session-only preference when storage is unavailable. */
    }
  };
  return (
    <div className={`${s.app} ${collapsed ? s.collapsed : ""}`}>
      <a href="#main" className={s.skip}>
        {t("app.skip")}
      </a>
      <aside className={s.sidebar}>
        <div className={s.sidebarHead}>
          <NavLink className={s.brand} to="/dashboard" aria-label={t("app.brandAria")}>
            <span className={s.brandIcon}>
              <Icon name="leaf" />
            </span>
            <span className={s.brandText}>
              CodeBuddy<small>{t("app.brandSubtitle")}</small>
            </span>
          </NavLink>
        </div>
        <p className={s.navLabel}>{t("app.workspace")}</p>
        <nav id="workspace-navigation" aria-label={t("app.primaryNav")}>
          {navigation.map(([path, labelKey, icon]) => {
            const flagged = path === "/updates" && updateAvailable;
            const label = flagged ? `${t(labelKey)} · ${t("updates.available")}` : t(labelKey);
            return (
              <NavLink
                key={path}
                to={`/dashboard${path}`}
                end={path === ""}
                aria-label={label}
                title={collapsed ? label : undefined}
                data-label={label}
                className={({ isActive }) => `${s.navItem} ${isActive ? s.active : ""}`}
              >
                <Icon name={icon} />
                <span className={s.navText}>{t(labelKey)}</span>
                {flagged && <span className={s.navDot} />}
                <span className={s.navArrow}>
                  <Icon name="arrow" />
                </span>
              </NavLink>
            );
          })}
        </nav>
        <button
          className={s.sidebarBar}
          onClick={toggleSidebar}
          aria-label={collapsed ? t("app.expandSidebar") : t("app.collapseSidebar")}
          aria-expanded={!collapsed}
          aria-controls="workspace-navigation"
          title={collapsed ? t("app.expandSidebar") : t("app.collapseSidebar")}
        >
          <span className={s.sidebarBarText}>
            {collapsed ? t("app.expandSidebar") : t("app.collapseSidebar")}
          </span>
          <Icon name={collapsed ? "expand" : "collapse"} />
        </button>
      </aside>
      <div className={s.workspace}>
        <header className={s.header}>
          <span>
            {t("app.workspace")} <span className={s.separator}>/</span>{" "}
            {t(
              navigation.find(
                ([path]) =>
                  path &&
                  (location.pathname === `/dashboard${path}` ||
                    location.pathname.startsWith(`/dashboard${path}/`)),
              )?.[1] ?? "nav.overview",
            )}
          </span>
          <div className={s.actions}>
            <LanguageToggle />
            <Appearance />
            <span className={s.session}>
              <span />
              {t("app.sessionActive")}
            </span>
            <button
              disabled={busy}
              onClick={() => {
                setBusy(true);
                void api
                  .delete("/session")
                  .then(() => useSession.setState({ status: "anonymous", csrf: null, error: null }))
                  .catch((err: unknown) => setError(errorMessage(err)))
                  .finally(() => setBusy(false));
              }}
            >
              {t("app.signOut")}
            </button>
          </div>
        </header>
        <main id="main" className={s.main}>
          <ErrorNotice message={error} />
          {bootstrap && !bootstrapDismissed && (
            <div className={s.notice} role="status">
              <span>
                {t("app.bootstrapBanner")}{" "}
                <NavLink to="/dashboard/settings?tab=account">{t("app.bootstrapAction")}</NavLink>
              </span>
              <button
                type="button"
                aria-label={t("common.dismissNotice")}
                onClick={() => setBootstrapDismissed(true)}
              >
                <Icon name="close" />
              </button>
            </div>
          )}
          <Outlet />
        </main>
        <footer className={s.footer}>CodeBuddy2API</footer>
      </div>
    </div>
  );
}
function Login() {
  const t = useT();
  const status = useSession((state) => state.status);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [visible, setVisible] = useState(false);
  const [caps, setCaps] = useState(false);
  const [busy, setBusy] = useState(false);
  const [shake, setShake] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (status === "authenticated") return <Navigate to="/dashboard" replace />;
  return (
    <div className={s.login}>
      <div className={s.loginBackdrop} aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <div className={s.loginAppearance}>
        <LanguageToggle />
        <Appearance />
      </div>
      <section className={s.loginIntro}>
        <div className={s.brand}>
          <span className={s.brandIcon}>
            <Icon name="leaf" />
          </span>
          CodeBuddy
        </div>
        <h1>{t("login.subtitle")}</h1>
        <div className={s.loginOrbit} aria-hidden="true">
          <Icon name="leaf" />
        </div>
      </section>
      <section className={`${s.loginCard} ${shake ? s.loginShake : ""}`}>
        <Icon name="shield" />
        <h2>{t("login.title")}</h2>
        <form
          method="post"
          action="/admin/session"
          onSubmit={(e) => {
            e.preventDefault();
            const passwordInput = e.currentTarget.elements.namedItem(
              "password",
            ) as HTMLInputElement;
            setBusy(true);
            setShake(false);
            setError(null);
            void establishSession({ username, password, remember })
              .then(() => {
                const Credential = (
                  window as unknown as {
                    PasswordCredential?: new (options: {
                      id: string;
                      password: string;
                    }) => Credential;
                  }
                ).PasswordCredential;
                if (Credential && navigator.credentials) {
                  try {
                    const credential = new Credential({ id: username, password });
                    void navigator.credentials.store(credential).catch(() => undefined);
                  } catch {
                    // Native autocomplete remains available when credential storage is rejected.
                  }
                }
              })
              .catch((err: unknown) => {
                setPassword("");
                setError(errorMessage(err));
                setShake(true);
                passwordInput.focus();
              })
              .finally(() => setBusy(false));
          }}
        >
          <label className={s.field}>
            {t("login.username")}
            <span className={s.inputGroup}>
              <Icon name="user" />
              <input
                name="username"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                required
                autoFocus
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="admin"
              />
            </span>
          </label>
          <label className={s.field}>
            {t("login.password")}
            <span className={s.inputGroup}>
              <Icon name="lock" />
              <input
                name="password"
                type={visible ? "text" : "password"}
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={t("login.passwordPlaceholder")}
                aria-describedby={caps ? "login-caps" : undefined}
                aria-invalid={error ? true : undefined}
                onKeyDown={(e) => setCaps(e.getModifierState("CapsLock"))}
                onKeyUp={(e) => setCaps(e.getModifierState("CapsLock"))}
              />
              <button
                type="button"
                className={s.inputAction}
                onMouseDown={(e) => e.preventDefault()}
                aria-label={visible ? t("login.hidePassword") : t("login.showPassword")}
                aria-pressed={visible}
                onClick={() => setVisible((value) => !value)}
              >
                <Icon name={visible ? "eyeOff" : "eye"} />
              </button>
            </span>
          </label>
          {caps && (
            <span id="login-caps" className={s.capsHint} role="status">
              <Icon name="alert" />
              {t("login.capsLock")}
            </span>
          )}
          <label className={s.check}>
            <input
              type="checkbox"
              name="remember"
              checked={remember}
              onChange={(e) => setRemember(e.target.checked)}
            />
            {t("login.remember")}
          </label>
          <ErrorNotice message={error} />
          <button
            className={s.primary}
            disabled={busy || !username.trim() || !password}
            aria-busy={busy}
          >
            {busy ? t("login.verifying") : t("login.enter")}
            <Icon name={busy ? "refresh" : "arrow"} />
          </button>
        </form>
      </section>
    </div>
  );
}
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/dashboard/login" element={<Login />} />
      <Route element={<Guard />}>
        <Route path="/dashboard" element={<Shell />}>
          <Route index element={<Dashboard />} />
          <Route path="models" element={<Models />} />
          <Route path="credentials" element={<Credentials />} />
          <Route path="keys" element={<Keys />} />
          <Route path="logs" element={<Logs />} />
          <Route path="media" element={<Media />} />
          <Route path="settings" element={<Settings />} />
          <Route path="storage" element={<Storage />} />
          <Route path="updates" element={<Updates />} />
          <Route path="updates/:tag" element={<Updates />} />
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  );
}
export default function App() {
  useEffect(() => {
    void establishSession();
  }, []);
  const status = useSession((state) => state.status);
  useEffect(() => {
    if (status !== "authenticated") return;
    // The stored language wins over the browser cache once the session is known.
    void api
      .get<unknown>("/settings")
      .then((response) => {
        const data = object(response.data);
        const stored = list(data.items).find((item) => item.key === "language");
        if (stored) chooseLanguage(serverPreference(stored.value));
      })
      .catch(() => undefined);
  }, [status]);
  return (
    <LanguageProvider>
      <AppRoutes />
    </LanguageProvider>
  );
}
