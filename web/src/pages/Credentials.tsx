import { useEffect, useRef, useState } from "react";
import {
  api,
  credentialResponse,
  errorMessage,
  list,
  metric,
  number,
  object,
  text,
  useResource,
  type Credential,
} from "../api";
import {
  Badge,
  Drawer,
  Empty,
  ErrorNotice,
  Fields,
  Icon,
  PageTitle,
  Panel,
  ResourceState,
} from "../components";
import { prepareImports, type ImportResult } from "../imports";
import s from "../ui.module.scss";
function expiry(value: unknown, milliseconds = false) {
  return typeof value === "number"
    ? new Date(milliseconds ? value : value * 1000).toLocaleString("zh-CN")
    : text(value);
}
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
function OAuth({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [site, setSite] = useState("cn");
  const [login, setLogin] = useState<{ id: string; url: string; until: number } | null>(null);
  const [message, setMessage] = useState("选择站点后发起授权。");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    if (!login) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (Date.now() > login.until) {
        setLogin(null);
        setMessage("授权已过期，请重新发起。");
        return;
      }
      if (document.visibilityState === "hidden") {
        timer = setTimeout(() => {
          void poll();
        }, 4000);
        return;
      }
      try {
        const response = await api.get<unknown>("/oauth/poll", {
          params: { login_id: login.id },
          signal: abort.signal,
        });
        const data = object(response.data);
        if (typeof data.done !== "boolean") throw new Error("OAuth 轮询响应缺少 done 状态");
        if (abort.signal.aborted) return;
        if (data.done) {
          setLogin(null);
          if (data.error) setError(text(data.error));
          else {
            setMessage("授权完成，凭证已添加。");
            onDone();
          }
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
  }, [login, onDone]);
  return (
    <Drawer title="OAuth 添加凭证" onClose={onClose}>
      <p className={s.note}>请仅在官方站点完成授权。</p>
      <label className={s.field}>
        登录站点
        <select value={site} disabled={!!login || busy} onChange={(e) => setSite(e.target.value)}>
          <option value="cn">中国大陆 · CN</option>
          <option value="intl">国际 · INTL</option>
        </select>
      </label>
      <ErrorNotice message={error} />
      <p role="status">{message}</p>
      {login ? (
        <>
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
        </>
      ) : (
        <button
          className={s.primary}
          disabled={busy}
          onClick={() => {
            setError(null);
            setBusy(true);
            controller.current = new AbortController();
            void api
              .post<unknown>("/oauth/start", null, {
                params: { site },
                signal: controller.current.signal,
              })
              .then((res) => {
                const d = object(res.data);
                if (typeof d.login_id !== "string") throw new Error("OAuth 响应缺少 login_id");
                const seconds = number(d.expires_in);
                setLogin({
                  id: d.login_id,
                  url: safeOAuthUrl(d.verification_uri),
                  until: Date.now() + Math.min(seconds ?? 300, 600) * 1000,
                });
                setMessage("等待官方授权完成…");
              })
              .catch((err: unknown) => {
                if (!controller.current?.signal.aborted) setError(errorMessage(err));
              })
              .finally(() => setBusy(false));
          }}
        >
          {busy ? "正在发起…" : "发起 OAuth 授权"}
        </button>
      )}
    </Drawer>
  );
}
function ImportDrawer({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [results, setResults] = useState<ImportResult[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <Drawer title="导入凭证" onClose={onClose}>
      <p className={s.note}>
        支持 UTF-8 .info 或 ZIP，ZIP 仅接受根目录 .info。单项 ≤ 1 MiB，每批 ≤ 100 项 / 32
        MiB，不覆盖现有文件。
      </p>
      <label className={s.upload}>
        选择 .info 或 ZIP
        <input
          type="file"
          accept=".info,.zip"
          multiple
          disabled={busy}
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            e.target.value = "";
            if (!files.length) return;
            setBusy(true);
            setError(null);
            setResults([]);
            void prepareImports(files)
              .then(async (batch) => {
                setResults(batch.results);
                if (!batch.files.length) return;
                const response = await api.post<unknown>("/credentials/upload", {
                  files: batch.files,
                  replace: false,
                });
                const server = list(object(response.data).results).map((item) => {
                  if (typeof item.name !== "string" || typeof item.ok !== "boolean")
                    throw new Error("导入结果缺少 name / ok");
                  return {
                    name: item.name,
                    ok: item.ok,
                    ...(item.error ? { error: text(item.error) } : {}),
                  };
                });
                setResults([...batch.results, ...server]);
                if (server.some((item) => item.ok)) onDone();
              })
              .catch((err: unknown) => setError(errorMessage(err)))
              .finally(() => setBusy(false));
          }}
        />
      </label>
      {busy && <p role="status">正在检查并上传，请勿关闭…</p>}
      <ErrorNotice message={error} />
      {results.map((r, i) => (
        <div className={s.importResult} key={i}>
          <Badge tone={r.ok ? "good" : "bad"}>{r.ok ? "已导入" : "未导入"}</Badge>
          <strong>{r.name}</strong>
          {r.error && <p>{r.error}</p>}
        </div>
      ))}
    </Drawer>
  );
}
export function Credentials() {
  const resource = useResource("/credentials", credentialResponse);
  const [selected, setSelected] = useState<string[]>([]);
  const [drawer, setDrawer] = useState<"oauth" | "import" | "export" | null>(null);
  const [detail, setDetail] = useState<Credential | null>(null);
  const [deleting, setDeleting] = useState<Credential | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const run = (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    void action()
      .then(() => {
        resource.reload();
        setDeleting(null);
      })
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setBusy(false));
  };
  const liveSelected = selected.filter((id) => resource.data?.some((c) => c.id === id));
  return (
    <>
      <PageTitle
        title="凭证管理"
        actions={
          <>
            <button onClick={() => setDrawer("import")}>导入文件</button>
            <button className={s.primary} onClick={() => setDrawer("oauth")}>
              <Icon name="key" />
              添加凭证
            </button>
          </>
        }
      />
      <ResourceState {...resource} />
      <ErrorNotice message={error} />
      <Panel
        title="账号凭证"
        hint={resource.data ? `${resource.data.length} 个账号` : "等待凭证列表"}
      >
        <div className={s.toolbar}>
          <div className={s.actions}>
            <button onClick={resource.reload}>
              <Icon name="refresh" />
              刷新
            </button>
            <button disabled={busy} onClick={() => run(() => api.post("/checkin"))}>
              签到 / 同步余额
            </button>
          </div>
          <button disabled={!liveSelected.length} onClick={() => setDrawer("export")}>
            导出已选 ({liveSelected.length})
          </button>
        </div>
        {resource.data &&
          (resource.data.length ? (
            <div className={s.tableWrap}>
              <table>
                <thead>
                  <tr>
                    <th>
                      <input
                        aria-label="选择全部凭证"
                        type="checkbox"
                        checked={
                          resource.data.length > 0 && liveSelected.length === resource.data.length
                        }
                        onChange={(e) =>
                          setSelected(e.target.checked ? resource.data!.map((c) => c.id) : [])
                        }
                      />
                    </th>
                    <th>凭证 / 产品</th>
                    <th>人工状态</th>
                    <th>认证健康</th>
                    <th>模型 429 冷却</th>
                    <th>官方余额 / 到期</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {resource.data.map((c) => {
                    const until = number(c.fail_until);
                    const remaining =
                      until === null ? null : Math.max(0, Math.ceil(until - now / 1000));
                    const cooldowns = Array.isArray(c.cooldowns) ? list(c.cooldowns) : null;
                    const balance =
                      c.credits && typeof c.credits === "object" ? object(c.credits) : null;
                    return (
                      <tr key={c.id}>
                        <td>
                          <input
                            aria-label={`选择 ${c.name ?? c.id}`}
                            type="checkbox"
                            checked={liveSelected.includes(c.id)}
                            onChange={(e) =>
                              setSelected(
                                e.target.checked
                                  ? [...selected, c.id]
                                  : selected.filter((id) => id !== c.id),
                              )
                            }
                          />
                        </td>
                        <td>
                          <strong>{c.name ?? "安全文件名不可用"}</strong>
                          <small>
                            {text(c.profile)} · {text(c.nickname ?? c.uid)}
                          </small>
                          <small>
                            {c.sync_pending === true
                              ? "目录同步中"
                              : c.catalog_ready === true
                                ? "目录已就绪"
                                : "目录状态待确认"}
                          </small>
                        </td>
                        <td>
                          <Badge tone={c.enabled === true ? "good" : "neutral"}>
                            {c.enabled === true
                              ? "已启用"
                              : c.enabled === false
                                ? "人工停用"
                                : "未知"}
                          </Badge>
                        </td>
                        <td>
                          <Badge
                            tone={
                              remaining !== null && remaining > 0
                                ? "bad"
                                : c.health === "ready"
                                  ? "good"
                                  : "warn"
                            }
                          >
                            {remaining !== null && remaining > 0
                              ? `认证熔断 ${remaining}s`
                              : c.health === "ready"
                                ? "认证正常"
                                : text(c.health)}
                          </Badge>
                          <small>{c.token_expired === true ? "Token 已过期" : ""}</small>
                        </td>
                        <td>
                          {cooldowns ? (
                            cooldowns.length ? (
                              cooldowns.map((cooldown, i) => (
                                <small key={i}>
                                  {text(cooldown.model)} ·{" "}
                                  {number(cooldown.until) !== null
                                    ? `${Math.max(0, Math.ceil(Number(cooldown.until) - now / 1000))}s`
                                    : text(cooldown.remaining_seconds)}
                                </small>
                              ))
                            ) : (
                              <Badge>无模型冷却</Badge>
                            )
                          ) : c.model_cooldowns ? (
                            text(c.model_cooldowns)
                          ) : (
                            "未知"
                          )}
                        </td>
                        <td>
                          {balance ? metric(balance.credits ?? balance.remaining) : "未知"}
                          <small>
                            {c.token_expires_at !== undefined
                              ? expiry(c.token_expires_at, true)
                              : c.expiresAt !== undefined
                                ? expiry(c.expiresAt, true)
                                : expiry(c.expires_at ?? balance?.soonest_expiry)}
                          </small>
                        </td>
                        <td>
                          <div className={s.rowActions}>
                            <button onClick={() => setDetail(c)}>详情</button>
                            <button
                              disabled={busy || typeof c.enabled !== "boolean"}
                              onClick={() =>
                                run(() =>
                                  api.patch(`/credentials/${encodeURIComponent(c.id)}`, {
                                    enabled: !c.enabled,
                                  }),
                                )
                              }
                            >
                              {c.enabled === false ? "启用" : "停用"}
                            </button>
                            <button
                              className={s.textDanger}
                              disabled={!c.name || busy}
                              onClick={() => setDeleting(c)}
                            >
                              删除
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty title="还没有凭证">添加账号或导入 .info 文件。</Empty>
          ))}
      </Panel>
      {drawer === "oauth" && <OAuth onClose={() => setDrawer(null)} onDone={resource.reload} />}{" "}
      {drawer === "import" && (
        <ImportDrawer onClose={() => setDrawer(null)} onDone={resource.reload} />
      )}{" "}
      {detail && (
        <Drawer title="凭证详情" onClose={() => setDetail(null)}>
          <Fields data={detail} />
        </Drawer>
      )}
      {deleting && (
        <Drawer title="删除凭证" onClose={() => setDeleting(null)}>
          <div className={s.warning}>
            将永久删除 {deleting.name}，不可撤销。如已绑定模型规则，请先解除绑定。
          </div>
          <ErrorNotice message={error} />
          <button
            className={s.danger}
            disabled={busy}
            onClick={() =>
              run(() => api.delete(`/credentials/${encodeURIComponent(deleting.name!)}`))
            }
          >
            确认删除凭证
          </button>
        </Drawer>
      )}
      {drawer === "export" && (
        <Drawer title="导出明文凭证" onClose={() => setDrawer(null)}>
          <div className={s.warning}>
            <Icon name="alert" />
            <div>
              <strong>文件包含明文认证信息</strong>
              <p>
                将导出 {liveSelected.length}{" "}
                个凭证，他人可能借此使用账号。请仅保存在可信设备，勿分享或公开上传。
              </p>
            </div>
          </div>
          <ErrorNotice message={error} />
          <button
            className={s.danger}
            disabled={busy || !liveSelected.length}
            onClick={() => {
              setBusy(true);
              setError(null);
              void api
                .post<Blob>(
                  "/credentials/export",
                  { ids: liveSelected, confirm: true },
                  { responseType: "blob" },
                )
                .then((response) => {
                  if (!response.data.size) throw new Error("导出响应为空");
                  const type = String(response.headers["content-type"] ?? "");
                  if (type.includes("json") || type.includes("html"))
                    throw new Error("导出响应不是凭证附件");
                  const url = URL.createObjectURL(response.data);
                  const link = document.createElement("a");
                  link.href = url;
                  const header = String(response.headers["content-disposition"] ?? "");
                  const filename = /filename="?([^";]+)"?/.exec(header)?.[1];
                  link.download =
                    filename && !/[\\/]/.test(filename)
                      ? filename
                      : liveSelected.length === 1
                        ? "credential.info"
                        : "credentials.zip";
                  link.click();
                  setTimeout(() => URL.revokeObjectURL(url), 1000);
                  setDrawer(null);
                })
                .catch((err: unknown) => setError(errorMessage(err)))
                .finally(() => setBusy(false));
            }}
          >
            我理解明文风险，下载已选凭证
          </button>
        </Drawer>
      )}
    </>
  );
}
