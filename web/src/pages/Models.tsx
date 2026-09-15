import { useEffect, useId, useRef, useState } from "react";
import {
  api,
  credentialResponse,
  errorMessage,
  list,
  modelResponse,
  object,
  useResource,
  type Credential,
  type ModelRule,
  type RecordValue,
} from "../api";
import {
  Badge,
  DataValue,
  Drawer,
  DrawerPresence,
  Empty,
  ErrorNotice,
  Icon,
  PageTitle,
  Panel,
  ResourceState,
  profileLabel,
} from "../components";
import s from "../ui.module.scss";
export const profiles = ["cn-cli", "cn-work", "intl-cli", "intl-work"];
const validId = (id: string) => /^[A-Za-z0-9_.:/@-]{1,160}$/.test(id) && ![".", ".."].includes(id);
export function validateRule(rule: ModelRule, models: ModelRule[]): string | null {
  if (!rule.public_id.trim()) return "对外 ID 不能为空";
  if (rule.public_id !== rule.public_id.trim() || /\s/.test(rule.public_id))
    return "对外 ID 不能包含空白字符";
  if (!validId(rule.public_id)) return "对外 ID 只允许 1–160 位字母、数字及 _ . : / @ -";
  if (!validId(rule.upstream_id ?? rule.id)) return "上游 ID 不能为空，且须为有效模型标识";
  if (
    models.some(
      (m) => m.id !== rule.id && (m.id === rule.public_id || m.public_id === rule.public_id),
    )
  )
    return "对外 ID 与已有模型或别名冲突";
  if (rule.credential_ids.length && (rule.region || rule.profile))
    return "指定账号与区域/产品范围只能选择一种";
  if (rule.region && !["cn", "intl"].includes(rule.region)) return "区域无效";
  if (rule.profile && !profiles.includes(rule.profile)) return "产品无效";
  if (rule.region && rule.profile && !rule.profile.startsWith(`${rule.region}-`))
    return "区域与产品不匹配";
  return null;
}
type Mode = "auto" | "accounts" | "region" | "legacy";
function bindingMode(rule: ModelRule): Mode {
  if (rule.credential_ids.length) return rule.region || rule.profile ? "legacy" : "accounts";
  return rule.region || rule.profile ? "region" : "auto";
}
type Preview = { candidates: RecordValue[]; excluded: RecordValue[] };
function RoutePreview({ preview }: { preview: Preview }) {
  return (
    <Panel
      title="路由预览"
      hint={`${preview.candidates.length} 个候选 · ${preview.excluded.length} 个不可用`}
    >
      <div className={s.routePreview}>
        {!preview.candidates.length && (
          <p className={s.note}>当前没有可用候选。保存不会授权范围外的账号，也不会绕过账号目录。</p>
        )}
        {[
          ...preview.candidates.map((row) => ({ row, allowed: true })),
          ...preview.excluded.map((row) => ({ row, allowed: false })),
        ].map(({ row, allowed }, i) => (
          <div className={s.routeCandidate} key={i}>
            <div>
              <strong>{typeof row.name === "string" ? row.name : "未命名账号"}</strong>
              <small>
                {typeof row.profile === "string" ? profileLabel(row.profile) : "产品未知"}
              </small>
            </div>
            <Badge tone={allowed ? "good" : "warn"}>
              {allowed ? "候选" : typeof row.reason === "string" ? row.reason : "不可用"}
            </Badge>
          </div>
        ))}
      </div>
    </Panel>
  );
}
export function ModelEditor({
  model,
  models,
  credentials,
  revision,
  onClose,
  onSaved,
  creating = false,
}: {
  model: ModelRule;
  models: ModelRule[];
  credentials: Credential[] | null;
  revision: number;
  onClose: () => void;
  onSaved: () => void;
  creating?: boolean;
}) {
  const [rule, setRule] = useState({
    ...model,
    upstream_id: model.upstream_id ?? model.id,
    region:
      model.region ||
      (!model.credential_ids.length && profiles.includes(model.profile)
        ? model.profile.split("-")[0]
        : ""),
  });
  const [mode, setMode] = useState<Mode>(() => bindingMode(model));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const request = useRef<AbortController | null>(null);
  const suggestions = useId();
  useEffect(() => () => request.current?.abort(), []);
  const update = <K extends keyof ModelRule>(key: K, value: ModelRule[K]) => {
    setRule((old) => ({ ...old, [key]: value }));
    setPreview(null);
  };
  const chooseMode = (next: Mode) => {
    setMode(next);
    setPreview(null);
    setError(null);
    setRule((old) => ({
      ...old,
      credential_ids: next === "accounts" ? old.credential_ids : [],
      region:
        next === "region" ? old.region || (old.profile.startsWith("intl-") ? "intl" : "cn") : "",
      profile: next === "region" ? old.profile : "",
    }));
  };
  const run = (save: boolean) => {
    const invalid =
      validateRule(rule, models) ??
      (mode === "accounts" && !rule.credential_ids.length ? "请选择至少一个账号" : null);
    if (invalid) {
      setError(invalid);
      return;
    }
    setError(null);
    setBusy(true);
    const controller = new AbortController();
    request.current = controller;
    const payload = {
      revision,
      public_id: rule.public_id,
      upstream_id: rule.upstream_id,
      enabled: rule.enabled,
      keep_original: rule.keep_original,
      region: rule.region || null,
      profile: rule.profile || null,
      credential_ids: rule.credential_ids,
    };
    const path = creating ? "/models" : `/models/${encodeURIComponent(model.id)}`;
    const config = { signal: controller.signal };
    void (
      save
        ? creating
          ? api.post(path, payload, config)
          : api.put(path, payload, config)
        : api.post(`${path}/preview`, payload, config)
    )
      .then((response) => {
        if (controller.signal.aborted) return;
        if (save) {
          const result = object(response.data);
          if (!Number.isInteger(result.revision) || Number(result.revision) <= revision)
            throw new Error("后端未确认模型保存，请刷新后核验");
          onSaved();
          onClose();
        } else {
          const data = object(response.data);
          setPreview({ candidates: list(data.candidates), excluded: list(data.excluded) });
        }
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
  };
  return (
    <Drawer title={creating ? "新增模型" : "编辑模型规则"} onClose={onClose} dismissDisabled={busy}>
      <p className={s.note}>
        对外 ID 供客户端使用，上游 ID
        是发送给官方后端的模型名称。两者独立，账号能力与额度校验仍然生效。
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          run(true);
        }}
      >
        <fieldset className={s.formFields} disabled={busy}>
          <div className={s.formGrid}>
            <label className={s.field}>
              对外 ID
              <input
                value={rule.public_id}
                required
                maxLength={160}
                placeholder="例如 my-coding-model"
                onChange={(e) => update("public_id", e.target.value)}
              />
            </label>
            <label className={s.field}>
              上游 ID
              <input
                value={rule.upstream_id}
                required
                maxLength={160}
                list={suggestions}
                placeholder="实际调用的模型 ID"
                onChange={(e) => update("upstream_id", e.target.value)}
              />
            </label>
            <datalist id={suggestions}>
              {[...new Set(models.map((m) => m.upstream_id ?? m.id))].map((id) => (
                <option key={id} value={id} />
              ))}
            </datalist>
          </div>
          <div className={s.actions}>
            <label className={s.check}>
              <input
                type="checkbox"
                checked={rule.enabled}
                onChange={(e) => update("enabled", e.target.checked)}
              />
              启用模型
            </label>
            {!creating && !model.custom && (
              <label className={s.check}>
                <input
                  type="checkbox"
                  checked={rule.keep_original}
                  onChange={(e) => update("keep_original", e.target.checked)}
                />
                同时保留原模型 ID
              </label>
            )}
          </div>
          <fieldset className={s.fieldset}>
            <legend>选择路由方式</legend>
            <div className={s.routeModes}>
              {(
                [
                  ["auto", "自动选择", "按现有账号能力选路"],
                  ["accounts", "指定账号", "只使用选中的账号"],
                  ["region", "指定区域", "仅从该区域中选择"],
                ] as const
              ).map(([value, label, hint]) => (
                <label
                  className={`${s.modeOption} ${mode === value ? s.modeSelected : ""}`}
                  key={value}
                >
                  <input
                    type="radio"
                    name={`route-${suggestions}`}
                    value={value}
                    checked={mode === value}
                    onChange={() => chooseMode(value)}
                  />
                  <span>
                    <strong>{label}</strong>
                    <small>{hint}</small>
                  </span>
                </label>
              ))}
            </div>
            <p>账号与区域二选一，切换方式会清除另一种绑定；不可用时不越界回退。</p>
          </fieldset>
          {mode === "legacy" && (
            <div className={s.warning}>
              旧规则同时设置了账号和区域。当前仍按原交集执行，请明确选择一种路由方式后再保存。
            </div>
          )}
          {mode === "region" && (
            <div className={s.formGrid}>
              <label className={s.field}>
                区域
                <select
                  value={rule.region || (rule.profile.startsWith("intl-") ? "intl" : "cn")}
                  onChange={(e) => {
                    const region = e.target.value;
                    setRule((old) => ({
                      ...old,
                      region,
                      profile: old.profile.startsWith(`${region}-`) ? old.profile : "",
                    }));
                    setPreview(null);
                  }}
                >
                  <option value="cn">中国大陆 · CN</option>
                  <option value="intl">国际 · INTL</option>
                </select>
              </label>
              <label className={s.field}>
                产品
                <select value={rule.profile} onChange={(e) => update("profile", e.target.value)}>
                  <option value="">不限产品</option>
                  {profiles
                    .filter((p) => !rule.region || p.startsWith(`${rule.region}-`))
                    .map((p) => (
                      <option key={p} value={p}>
                        {profileLabel(p)}
                      </option>
                    ))}
                </select>
              </label>
            </div>
          )}
          {mode === "accounts" && (
            <fieldset className={s.fieldset}>
              <legend>指定凭证</legend>
              {credentials === null ? (
                <p>凭证列表未加载，现有绑定保持不变。</p>
              ) : !credentials.length ? (
                <p>暂无账号，请先在凭证管理添加。</p>
              ) : (
                credentials.map((c) => (
                  <label className={s.accountChoice} key={c.id}>
                    <input
                      type="checkbox"
                      checked={rule.credential_ids.includes(c.id)}
                      onChange={(e) =>
                        update(
                          "credential_ids",
                          e.target.checked
                            ? [...rule.credential_ids, c.id]
                            : rule.credential_ids.filter((id) => id !== c.id),
                        )
                      }
                    />
                    <span>
                      <strong>{c.name ?? c.id}</strong>
                      <small>
                        {typeof c.profile === "string" ? profileLabel(c.profile) : "产品未知"}
                      </small>
                    </span>
                    {c.enabled === false && <Badge tone="warn">已停用</Badge>}
                  </label>
                ))
              )}
              {rule.credential_ids
                .filter((id) => !credentials?.some((c) => c.id === id))
                .map((id) => (
                  <label className={s.check} key={id}>
                    <input
                      type="checkbox"
                      checked
                      onChange={() =>
                        update(
                          "credential_ids",
                          rule.credential_ids.filter((v) => v !== id),
                        )
                      }
                    />
                    {id} · 未在当前列表中
                  </label>
                ))}
            </fieldset>
          )}
        </fieldset>
        <ErrorNotice message={error} />
        <div className={s.actions}>
          <button type="button" disabled={busy} onClick={() => run(false)}>
            预览候选路由
          </button>
          <button className={s.primary} disabled={busy}>
            {busy ? "处理中…" : creating ? "创建模型" : "保存规则"}
          </button>
        </div>
      </form>
      {preview && <RoutePreview preview={preview} />}
    </Drawer>
  );
}
const emptyRule: ModelRule = {
  id: "",
  public_id: "",
  upstream_id: "",
  enabled: true,
  keep_original: false,
  region: "",
  profile: "",
  credential_ids: [],
};
export function Models() {
  const resource = useResource("/models", modelResponse);
  const credentials = useResource("/credentials", credentialResponse);
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<ModelRule | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<ModelRule | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rows = resource.data?.models.filter((m) =>
    `${m.upstream_id ?? m.id} ${m.public_id}`.toLowerCase().includes(query.toLowerCase()),
  );
  return (
    <>
      <PageTitle
        title="模型路由"
        description="独立映射模型名称，并明确每条路由的账号或区域边界。"
        actions={
          <>
            <button
              onClick={() => {
                resource.reload();
                credentials.reload();
              }}
            >
              <Icon name="refresh" />
              刷新目录
            </button>
            <button
              className={s.primary}
              disabled={!resource.data}
              onClick={() => setCreating(true)}
            >
              <Icon name="plus" />
              新增模型
            </button>
          </>
        }
      />
      <ResourceState {...resource} />
      <ErrorNotice message={credentials.error} retry={credentials.reload} />
      <Panel
        title="模型目录"
        hint={resource.data ? `${resource.data.models.length} 个模型` : undefined}
      >
        <div className={s.toolbar}>
          <input
            aria-label="搜索模型"
            placeholder="搜索对外 ID 或上游模型…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        {rows &&
          (rows.length ? (
            <div className={s.tableWrap}>
              <table>
                <thead>
                  <tr>
                    <th>对外模型 / 上游 ID</th>
                    <th>状态</th>
                    <th>目录倍率</th>
                    <th>路由边界</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((m) => (
                    <tr key={m.id}>
                      <td>
                        <strong className={s.modelName}>{m.public_id}</strong>
                        <small className={s.mono}>{m.upstream_id ?? m.id}</small>
                        {m.keep_original && <small>同时保留 {m.id}</small>}
                      </td>
                      <td>
                        <div className={s.badgeStack}>
                          <Badge tone={m.enabled ? "good" : "neutral"}>
                            {m.enabled ? "已启用" : "已停用"}
                          </Badge>
                          {m.custom && <Badge>自建</Badge>}
                          {m.available === false && m.enabled && (
                            <Badge tone="warn">暂无候选</Badge>
                          )}
                        </div>
                      </td>
                      <td>
                        <DataValue value={m.credits} />
                        {m.credits_by_profile &&
                        typeof m.credits_by_profile === "object" &&
                        !Array.isArray(m.credits_by_profile) ? (
                          <div className={s.priceList}>
                            {Object.entries(m.credits_by_profile).map(([profile, price]) => (
                              <small key={profile}>
                                {profileLabel(profile)} <DataValue value={price} />
                              </small>
                            ))}
                          </div>
                        ) : (
                          <small>产品倍率未知</small>
                        )}
                      </td>
                      <td>
                        {m.credential_ids.length
                          ? `${m.credential_ids.length} 个指定账号`
                          : m.region
                            ? profileLabel(m.region)
                            : "自动选择"}
                        <small>
                          {m.profile
                            ? profileLabel(m.profile)
                            : m.credential_ids.length
                              ? "严格绑定，不越界回退"
                              : "按账号能力选择"}
                        </small>
                        {bindingMode(m) === "legacy" && <Badge tone="warn">旧联合范围</Badge>}
                      </td>
                      <td>
                        <div className={s.rowActions}>
                          <button onClick={() => setEditing(m)}>编辑规则</button>
                          {m.custom && (
                            <button
                              className={s.textDanger}
                              onClick={() => {
                                setDeleting(m);
                                setError(null);
                              }}
                            >
                              删除
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty title={query ? "没有匹配的模型" : "模型目录为空"}>
              可新增模型映射，或添加凭证以同步目录。
            </Empty>
          ))}
      </Panel>
      <DrawerPresence>
        {(editing || creating) && resource.data && (
          <ModelEditor
            model={editing ?? emptyRule}
            creating={creating}
            models={resource.data.models}
            credentials={credentials.data}
            revision={resource.data.revision}
            onClose={() => {
              setEditing(null);
              setCreating(false);
            }}
            onSaved={resource.reload}
          />
        )}
      </DrawerPresence>
      <DrawerPresence>
        {deleting && resource.data && (
          <Drawer title="删除自建模型" onClose={() => setDeleting(null)} dismissDisabled={busy}>
            <p className={s.warning}>
              删除 {deleting.public_id} 的本地路由。不会删除上游模型、账号或历史统计。
            </p>
            <ErrorNotice message={error} />
            <button
              className={s.danger}
              disabled={busy}
              onClick={() => {
                setBusy(true);
                setError(null);
                void api
                  .delete(`/models/${encodeURIComponent(deleting.id)}`, {
                    data: { revision: resource.data!.revision },
                  })
                  .then((response) => {
                    if (object(response.data).ok !== true)
                      throw new Error("后端未确认删除，请刷新后核验");
                    setDeleting(null);
                    resource.reload();
                  })
                  .catch((err: unknown) => setError(errorMessage(err)))
                  .finally(() => setBusy(false));
              }}
            >
              {busy ? "正在删除…" : "确认删除模型"}
            </button>
          </Drawer>
        )}
      </DrawerPresence>
    </>
  );
}
