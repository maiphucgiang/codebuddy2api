import { useState } from "react";
import {
  api,
  credentialResponse,
  errorMessage,
  modelResponse,
  object,
  text,
  useResource,
  type Credential,
  type ModelRule,
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
  Panel,
  ResourceState,
} from "../components";
import s from "../ui.module.scss";
export const profiles = ["cn-cli", "cn-work", "intl-cli", "intl-work"];
export function validateRule(rule: ModelRule, models: ModelRule[]): string | null {
  if (!rule.public_id.trim()) return "对外 ID 不能为空";
  if (rule.public_id !== rule.public_id.trim() || /\s/.test(rule.public_id))
    return "对外 ID 不能包含空白字符";
  if (
    models.some(
      (m) => m.id !== rule.id && (m.id === rule.public_id || m.public_id === rule.public_id),
    )
  )
    return "对外 ID 与已有模型或别名冲突";
  if (rule.region && !["cn", "intl"].includes(rule.region)) return "区域无效";
  if (rule.profile && !profiles.includes(rule.profile)) return "产品无效";
  if (rule.region && rule.profile && !rule.profile.startsWith(`${rule.region}-`))
    return "区域与产品不匹配";
  return null;
}
export function ModelEditor({
  model,
  models,
  credentials,
  revision,
  onClose,
  onSaved,
}: {
  model: ModelRule;
  models: ModelRule[];
  credentials: Credential[] | null;
  revision: number;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [rule, setRule] = useState({ ...model });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<RecordValue | null>(null);
  const update = <K extends keyof ModelRule>(key: K, value: ModelRule[K]) => {
    setRule((old) => ({ ...old, [key]: value }));
    setPreview(null);
  };
  const payload = () => ({
    revision,
    public_id: rule.public_id,
    enabled: rule.enabled,
    keep_original: rule.keep_original,
    region: rule.region || null,
    profile: rule.profile || null,
    credential_ids: rule.credential_ids,
  });
  const run = (save: boolean) => {
    const invalid = validateRule(rule, models);
    if (invalid) {
      setError(invalid);
      return;
    }
    setError(null);
    setBusy(true);
    const path = `/models/${encodeURIComponent(model.id)}`;
    void (save ? api.put(path, payload()) : api.post<unknown>(`${path}/preview`, payload()))
      .then((res) => {
        if (save) {
          onSaved();
          onClose();
        } else setPreview(object(res.data));
      })
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setBusy(false));
  };
  return (
    <Drawer title="编辑模型规则" onClose={onClose}>
      <p className={s.mono}>{model.id}</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          run(true);
        }}
      >
        <label className={s.check}>
          <input
            type="checkbox"
            checked={rule.enabled}
            onChange={(e) => update("enabled", e.target.checked)}
          />
          启用模型
        </label>
        <label className={s.field}>
          对外 ID
          <input
            value={rule.public_id}
            onChange={(e) => update("public_id", e.target.value)}
            required
          />
        </label>
        <label className={s.check}>
          <input
            type="checkbox"
            checked={rule.keep_original}
            onChange={(e) => update("keep_original", e.target.checked)}
          />
          同时保留原模型 ID
        </label>
        <div className={s.formGrid}>
          <label className={s.field}>
            区域
            <select value={rule.region} onChange={(e) => update("region", e.target.value)}>
              <option value="">不限制</option>
              <option value="cn">中国大陆 · CN</option>
              <option value="intl">国际 · INTL</option>
            </select>
          </label>
          <label className={s.field}>
            产品
            <select value={rule.profile} onChange={(e) => update("profile", e.target.value)}>
              <option value="">不限制</option>
              {profiles.map((p) => (
                <option key={p}>{p}</option>
              ))}
            </select>
          </label>
        </div>
        <fieldset className={s.fieldset}>
          <legend>指定凭证</legend>
          <p>留空则自动选择；指定凭证不可用时不会切换到其他账号。</p>
          {credentials === null ? (
            <p>凭证列表未加载，现有绑定保持不变。</p>
          ) : (
            credentials.map((c) => (
              <label className={s.check} key={c.id}>
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
                {c.name ?? c.id}
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
        <ErrorNotice message={error} />
        <div className={s.actions}>
          <button type="button" disabled={busy} onClick={() => run(false)}>
            预览候选路由
          </button>
          <button className={s.primary} disabled={busy}>
            {busy ? "处理中…" : "保存规则"}
          </button>
        </div>
      </form>
      {preview && (
        <Panel title="路由预览">
          <Fields data={preview} />
        </Panel>
      )}
    </Drawer>
  );
}
export function Models() {
  const resource = useResource("/models", modelResponse);
  const credentials = useResource("/credentials", credentialResponse);
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<ModelRule | null>(null);
  const rows = resource.data?.models.filter((m) =>
    `${m.id} ${m.public_id}`.toLowerCase().includes(query.toLowerCase()),
  );
  return (
    <>
      <PageTitle
        title="模型路由"
        actions={
          <button onClick={resource.reload}>
            <Icon name="refresh" />
            刷新目录
          </button>
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
            placeholder="搜索模型 ID 或对外别名…"
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
                    <th>模型 / 对外 ID</th>
                    <th>状态</th>
                    <th>倍率 / 产品倍率</th>
                    <th>区域 / 产品</th>
                    <th>凭证范围</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((m) => (
                    <tr key={m.id}>
                      <td>
                        <strong className={s.mono}>{m.public_id}</strong>
                        <small>
                          {m.id}
                          {m.keep_original && " · 保留原 ID"}
                        </small>
                      </td>
                      <td>
                        <Badge tone={m.enabled ? "good" : "neutral"}>
                          {m.enabled ? "已启用" : "已停用"}
                        </Badge>
                      </td>
                      <td>
                        {text(m.credits)}
                        <small>
                          {m.credits_by_profile ? text(m.credits_by_profile) : "产品倍率未知"}
                        </small>
                      </td>
                      <td>
                        {m.region || "全部区域"}
                        <small>{m.profile || "全部产品"}</small>
                      </td>
                      <td>
                        {m.credential_ids.length
                          ? `${m.credential_ids.length} 个指定账号`
                          : "不限定账号"}
                      </td>
                      <td>
                        <button onClick={() => setEditing(m)}>编辑规则</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty title={query ? "没有匹配的模型" : "模型目录为空"} />
          ))}
      </Panel>
      {editing && resource.data && (
        <ModelEditor
          model={editing}
          models={resource.data.models}
          credentials={credentials.data}
          revision={resource.data.revision}
          onClose={() => setEditing(null)}
          onSaved={resource.reload}
        />
      )}
    </>
  );
}
