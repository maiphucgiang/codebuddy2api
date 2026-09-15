import { useCallback, useEffect, useState } from "react";
import { OAuth } from "../OAuth";
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
  DataValue,
  Drawer,
  DrawerPresence,
  Empty,
  ErrorNotice,
  Fields,
  Icon,
  PageTitle,
  Panel,
  ResourceState,
  profileLabel,
} from "../components";
import { prepareImports, type ImportResult } from "../imports";
import { downloadFilename } from "../downloads";
import s from "../ui.module.scss";
function expiry(value: unknown, milliseconds = false) {
  return typeof value === "number"
    ? new Date(milliseconds ? value : value * 1000).toLocaleString("zh-CN")
    : text(value);
}
export { safeOAuthUrl } from "../OAuth";
function ImportDrawer({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [results, setResults] = useState<ImportResult[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <Drawer title="导入凭证" onClose={onClose} dismissDisabled={busy}>
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
  const [notice, setNotice] = useState<string | null>(null);
  const [maintenance, setMaintenance] = useState<Record<string, unknown>[]>([]);
  const maintain = (
    action: "refresh" | "checkin" | "sync" | "travel" | "travel-status",
    credential?: Credential,
  ) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setMaintenance([]);
    const path = credential
      ? `/credentials/${encodeURIComponent(credential.id)}/${action}`
      : `/${action}`;
    void api
      .post(path, undefined, { timeout: 300000 })
      .then(({ data }) => {
        const results = list(object(data).results, "凭证操作结果");
        if (
          !results.length ||
          results.some((r) => typeof r.ok !== "boolean" || typeof r.message !== "string")
        )
          throw new Error("未收到完整操作结果，请刷新列表核验，勿直接重复执行");
        for (const r of results) if (r.travel !== undefined) object(r.travel, "旅行操作结果");
        setMaintenance(results);
      })
      .catch((err: unknown) =>
        setError(`${errorMessage(err)}；请求失败不代表后台已停止，请刷新列表核验。`),
      )
      .finally(() => {
        setBusy(false);
        resource.reload();
      });
  };
  const oauthDone = useCallback(() => {
    setDrawer(null);
    setNotice("授权完成，凭证已添加。");
    resource.reload();
  }, [resource.reload]);
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
  const preference = (
    credential: Credential,
    field: "auto_checkin" | "auto_travel",
    enabled: boolean,
  ) => {
    run(async () => {
      const response = await api.patch(`/credentials/${encodeURIComponent(credential.id)}`, {
        [field]: enabled,
      });
      const saved = object(response.data);
      if (
        saved.id !== credential.id ||
        saved[field] !== enabled ||
        !Number.isInteger(saved.revision)
      )
        throw new Error("设置保存结果未确认，请刷新列表核验");
      setNotice(
        `${field === "auto_checkin" ? "自动签到" : "自动旅行"}已${enabled ? "开启" : "关闭"}；保存不会立即领取，后续维护按新设置执行。`,
      );
    });
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
      {notice && (
        <p className={s.successNotice} role="status">
          {notice}
        </p>
      )}
      <ResourceState {...resource} />
      <ErrorNotice message={error} />
      {maintenance.length > 0 && (
        <Panel title="凭证操作结果">
          <ul className={s.operationResults} aria-live="polite">
            {maintenance.map((r, i) => (
              <li key={i}>
                <strong>{text(r.name)}</strong>
                <Badge tone={r.ok ? "good" : "warn"}>
                  {r.ok
                    ? "已完成"
                    : r.checkin_ok === true ||
                        r.claimed === true ||
                        r.departed === true ||
                        (r.travel &&
                          typeof r.travel === "object" &&
                          (object(r.travel).claimed === true || object(r.travel).departed === true))
                      ? "部分完成"
                      : r.skipped
                        ? "已跳过"
                        : "未完成"}
                </Badge>
                <span>{text(r.message)}</span>
              </li>
            ))}
          </ul>
        </Panel>
      )}
      <Panel
        title="账号凭证"
        hint={resource.data ? `${resource.data.length} 个账号` : "等待凭证列表"}
      >
        <div className={s.toolbar}>
          <div className={s.actions}>
            <button onClick={resource.reload}>
              <Icon name="refresh" />
              刷新列表
            </button>
            <button disabled={busy} onClick={() => maintain("checkin")}>
              批量签到
            </button>
            <button disabled={busy} onClick={() => maintain("sync")}>
              同步全部余额
            </button>
            {busy && <span role="status">操作执行中，请勿重复提交…</span>}
          </div>
          <button disabled={!liveSelected.length} onClick={() => setDrawer("export")}>
            导出已选 ({liveSelected.length})
          </button>
        </div>
        <p className={s.note}>
          自动任务按账号保存：国内默认签到后旅行，国际默认关闭。开关分别生效；余额同步不触发领取，关闭开关不撤回已发送的请求。
        </p>
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
                    <th>自动任务 / 上次结果</th>
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
                    const checkin =
                      c.checkin && typeof c.checkin === "object" ? object(c.checkin) : null;
                    const trip = c.travel && typeof c.travel === "object" ? object(c.travel) : null;
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
                            {profileLabel(text(c.profile))} · {text(c.nickname ?? c.uid)}
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
                        <td className={s.automation}>
                          <label className={s.check}>
                            <input
                              type="checkbox"
                              role="switch"
                              aria-label={`自动签到 ${c.name ?? c.id}`}
                              checked={c.auto_checkin === true}
                              disabled={busy || typeof c.auto_checkin !== "boolean"}
                              onChange={(e) => preference(c, "auto_checkin", e.target.checked)}
                            />
                            自动签到
                          </label>
                          <small>
                            上次签到{checkin?.date ? `（${text(checkin.date)}）` : ""}：
                            {checkin ? text(checkin.message) : "尚未查询"}
                          </small>
                          <label className={s.check}>
                            <input
                              type="checkbox"
                              role="switch"
                              aria-label={`自动旅行 ${c.name ?? c.id}`}
                              checked={c.auto_travel === true}
                              disabled={
                                busy ||
                                c.travel_supported !== true ||
                                typeof c.auto_travel !== "boolean"
                              }
                              onChange={(e) => preference(c, "auto_travel", e.target.checked)}
                            />
                            自动旅行
                          </label>
                          <small>
                            {c.travel_supported === true
                              ? `上次旅行：${trip ? text(trip.message) : "尚未查询"}`
                              : "旅行仅适用于国内账号"}
                          </small>
                          {trip?.stale === true && <small>状态可能已变化，请先查询核验</small>}
                          {c.enabled === false && <small>账号停用期间不执行自动任务</small>}
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
                            <DataValue value={c.model_cooldowns} />
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
                              disabled={busy || c.enabled !== true}
                              aria-label={`刷新凭证 ${c.name ?? c.id}`}
                              onClick={() => maintain("refresh", c)}
                            >
                              刷新 Token
                            </button>
                            <button
                              disabled={busy || c.enabled !== true}
                              aria-label={`签到 ${c.name ?? c.id}`}
                              onClick={() => maintain("checkin", c)}
                            >
                              {c.auto_travel === true ? "签到并旅行" : "签到"}
                            </button>
                            <button
                              disabled={busy || c.enabled !== true}
                              aria-label={`同步余额 ${c.name ?? c.id}`}
                              onClick={() => maintain("sync", c)}
                            >
                              同步余额
                            </button>
                            {c.travel_supported === true && (
                              <>
                                <button
                                  disabled={busy || c.enabled !== true}
                                  aria-label={`旅行状态 ${c.name ?? c.id}`}
                                  onClick={() => maintain("travel-status", c)}
                                >
                                  旅行状态
                                </button>
                                <button
                                  disabled={busy || c.enabled !== true}
                                  aria-label={`旅行领派 ${c.name ?? c.id}`}
                                  onClick={() => maintain("travel", c)}
                                >
                                  旅行领派
                                </button>
                              </>
                            )}
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
      <DrawerPresence>
        {drawer === "oauth" && <OAuth onClose={() => setDrawer(null)} onDone={oauthDone} />}
      </DrawerPresence>
      <DrawerPresence>
        {drawer === "import" && (
          <ImportDrawer onClose={() => setDrawer(null)} onDone={resource.reload} />
        )}
      </DrawerPresence>
      <DrawerPresence>
        {detail && (
          <Drawer title="凭证详情" onClose={() => setDetail(null)}>
            <Fields data={detail} />
          </Drawer>
        )}
      </DrawerPresence>
      <DrawerPresence>
        {deleting && (
          <Drawer title="删除凭证" onClose={() => setDeleting(null)} dismissDisabled={busy}>
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
      </DrawerPresence>
      <DrawerPresence>
        {drawer === "export" && (
          <Drawer title="导出明文凭证" onClose={() => setDrawer(null)} dismissDisabled={busy}>
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
                    link.download = downloadFilename(
                      header,
                      liveSelected.length === 1 ? "credential.info" : "credentials.zip",
                    );
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
      </DrawerPresence>
    </>
  );
}
