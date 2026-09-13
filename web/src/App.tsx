import { useEffect, useState } from "react";
import { NavLink, Navigate, Outlet, Route, Routes, useLocation } from "react-router";
import { api, establishSession, errorMessage, useSession } from "./api";
import { ErrorNotice, Icon } from "./components";
import { Dashboard } from "./pages/Dashboard";
import { Models } from "./pages/Models";
import { Credentials } from "./pages/Credentials";
import { Logs } from "./pages/Logs";
import { Settings } from "./pages/Settings";
import s from "./ui.module.scss";

const navigation = [
  ["", "概览", "grid"],
  ["/models", "模型路由", "model"],
  ["/credentials", "凭证管理", "key"],
  ["/logs", "日志审计", "logs"],
  ["/settings", "系统设置", "settings"],
];
export function Guard() {
  const { status, error } = useSession();
  if (status === "checking")
    return (
      <div className={s.center} role="status">
        正在验证登录状态…
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
        <NavLink to="/dashboard/login">返回登录</NavLink>
      </div>
    );
  return status === "authenticated" ? <Outlet /> : <Navigate to="/dashboard/login" replace />;
}
function Shell() {
  const location = useLocation();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <div className={s.app}>
      <a href="#main" className={s.skip}>
        跳转到内容
      </a>
      <aside className={s.sidebar}>
        <NavLink className={s.brand} to="/dashboard">
          <span className={s.brandIcon}>
            <Icon name="leaf" />
          </span>
          <span>
            CodeBuddy<small>个人管理工作台</small>
          </span>
        </NavLink>
        <p className={s.navLabel}>工作空间</p>
        <nav aria-label="主导航">
          {navigation.map(([path, label, icon]) => (
            <NavLink
              key={path}
              to={`/dashboard${path}`}
              end={path === ""}
              className={({ isActive }) => `${s.navItem} ${isActive ? s.active : ""}`}
            >
              <Icon name={icon} />
              {label}
              <span className={s.navArrow}>↗</span>
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className={s.workspace}>
        <header className={s.header}>
          <span>
            工作空间 <span className={s.separator}>/</span>{" "}
            {navigation.find(([path]) => `/dashboard${path}` === location.pathname)?.[1] ?? "概览"}
          </span>
          <div className={s.actions}>
            <span className={s.session}>
              <span />
              会话已认证
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
              退出登录
            </button>
          </div>
        </header>
        <main id="main" className={s.main}>
          <ErrorNotice message={error} />
          <Outlet />
        </main>
        <footer className={s.footer}>CodeBuddy2API</footer>
      </div>
    </div>
  );
}
function Login() {
  const status = useSession((state) => state.status);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (status === "authenticated") return <Navigate to="/dashboard" replace />;
  return (
    <div className={s.login}>
      <section className={s.loginIntro}>
        <div className={s.brand}>
          <span className={s.brandIcon}>
            <Icon name="leaf" />
          </span>
          CodeBuddy
        </div>
        <h1>网关管理</h1>
        <div className={s.loginOrbit}>
          <Icon name="leaf" />
        </div>
      </section>
      <section className={s.loginCard}>
        <Icon name="shield" />
        <h2>登录管理控制台</h2>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setBusy(true);
            setError(null);
            const value = key;
            setKey("");
            void establishSession(value)
              .catch((err: unknown) => setError(errorMessage(err)))
              .finally(() => setBusy(false));
          }}
        >
          <label className={s.field}>
            API key
            <input
              type="password"
              name="api-key"
              autoComplete="off"
              required
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="输入当前生效的 API key"
            />
          </label>
          <ErrorNotice message={error} />
          <button className={s.primary} disabled={busy || !key.trim()}>
            {busy ? "正在验证…" : "进入工作台"}
            <Icon name="arrow" />
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
          <Route path="logs" element={<Logs />} />
          <Route path="settings" element={<Settings />} />
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
  return <AppRoutes />;
}
