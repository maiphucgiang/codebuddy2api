import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  api,
  catalogScanResponse,
  credentialResponse,
  errorMessage,
  list,
  modelResponse,
  number,
  object,
  text,
  useResource,
  type CatalogScan,
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
  RefreshIcon,
  PageTitle,
  Panel,
  ResourceState,
  profileLabel,
} from "../components";
import {
  CapabilityChips,
  EffortChips,
  ModelMetadata,
  SpecValue,
  effortSummary,
  formatRate,
  modelDisplayName,
  modelSearchText,
  rateOf,
  specRange,
} from "../modelMetadata";
import {
  OutputProbeNote,
  RateOverrideFields,
  describeProbe,
  formatEstimate,
  measuredNote,
  rateDetails,
  rateSource,
  useOutputProbes,
  useRateProbeJob,
  type RateProbeControl,
  type RateSource,
} from "../modelRates";
import { JobBanner, ProgressBanner, type Job } from "../jobs";
import s from "../ui.module.scss";
import { SearchInput, Select } from "../fields";
import { clearToasts, notify } from "../toast";
export const profiles = ["cn-cli", "cn-work", "intl-cli", "intl-work"];
const mediaKindLabels: Record<string, string> = { image: "文生图", edit: "图生图", video: "视频" };
const validId = (id: string) => /^[A-Za-z0-9_.:/@-]{1,160}$/.test(id) && ![".", ".."].includes(id);
export function validateRule(rule: ModelRule, models: ModelRule[]): string | null {
  if (!rule.public_id.trim()) return "对外 ID 不能为空";
  if (rule.public_id !== rule.public_id.trim() || /\s/.test(rule.public_id))
    return "对外 ID 不能包含空白字符";
  if (!validId(rule.public_id)) return "对外 ID 只允许 1–160 位字母、数字及 _ . : / @ -";
  if (!validId(rule.upstream_id ?? rule.id)) return "上游 ID 不能为空，且须为有效模型标识";
  if (rule.routes) {
    if (!rule.routes.length) return "路由列表不能为空，删除全部候选请停用模型";
    const routeIds = new Set<string>();
    let hasWeight = false;
    let hasPositive = false;
    for (const route of rule.routes) {
      if (!validId(route.upstream_id) || routeIds.has(route.upstream_id))
        return "路由上游 ID 必须非空且不重复";
      routeIds.add(route.upstream_id);
      if (route.weight !== null) {
        hasWeight = true;
        if (!Number.isFinite(route.weight) || route.weight < 0 || route.weight > 1000)
          return "路由权重必须是 0–1000 的有限数字";
        if (route.weight > 0) hasPositive = true;
      }
    }
    if (hasWeight && !hasPositive) return "加权路由至少需要一条正权重路线";
  }
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
type Preview = { candidates: RecordValue[]; excluded: RecordValue[] } & Pick<
  ModelRule,
  "capabilities" | "limits" | "metadata_by_profile"
>;
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

function RateCell({ model }: { model: ModelRule }) {
  const rate = rateOf(model.credits);
  const details = rateDetails(model);
  const perProfile =
    model.credits_by_profile &&
    typeof model.credits_by_profile === "object" &&
    !Array.isArray(model.credits_by_profile)
      ? Object.entries(model.credits_by_profile)
      : [];
  // The headline rate comes from the cheapest product; its provenance decides the label.
  const headline = perProfile.find(([, price]) => rateOf(price) === rate)?.[0];
  const source = headline ? rateSource(details[headline]) : "declared";
  const unpriced = Object.entries(details).find(
    ([, detail]) => rateSource(detail) === "measured" && detail.measured?.rate == null,
  );
  if (rate === null) {
    if (unpriced)
      return (
        <div className={s.rateStack} title={measuredNote(unpriced[1])}>
          <span className={`${s.rateChip} ${s.rateMeasured}`}>实测计费 · 倍率待估</span>
        </div>
      );
    return (
      <>
        <DataValue value={model.credits} />
        <small className={s.specNote}>产品倍率未知</small>
      </>
    );
  }
  const rows = perProfile.filter(
    ([profile, price]) =>
      perProfile.length > 1 || rateOf(price) !== rate || rateSource(details[profile]) !== source,
  );
  const label = (value: number, kind: RateSource) =>
    kind === "measured"
      ? `≈${formatEstimate(value)} · 实测计费`
      : kind === "manual"
        ? `${formatEstimate(value)} · 手填`
        : value === 0
          ? "限时免费 0x"
          : formatRate(value);
  return (
    <div className={s.rateStack}>
      <span
        className={
          source === "measured"
            ? `${s.rateChip} ${s.rateMeasured}`
            : rate === 0
              ? `${s.rateChip} ${s.rateFree}`
              : s.rateChip
        }
        title={
          source === "measured" && headline
            ? measuredNote(details[headline])
            : source === "manual"
              ? "管理员手填倍率，路由与 /v1/models 以此为准"
              : undefined
        }
      >
        {label(rate, source)}
      </span>
      {rows.map(([profile, price]) => {
        const value = rateOf(price);
        const kind = rateSource(details[profile]);
        return (
          <small
            className={s.rateProfile}
            key={profile}
            title={kind === "measured" ? measuredNote(details[profile]) : undefined}
          >
            {profileLabel(profile)} {value === null ? "未知" : label(value, kind)}
          </small>
        );
      })}
      {unpriced && headline !== unpriced[0] && (
        <small className={s.rateProfile} title={measuredNote(unpriced[1])}>
          {profileLabel(unpriced[0])} 实测计费 · 倍率待估
        </small>
      )}
    </div>
  );
}
export function ModelEditor({
  model,
  models,
  credentials,
  revision,
  onClose,
  onSaved,
  rateProbe,
  creating = false,
}: {
  model: ModelRule;
  models: ModelRule[];
  credentials: Credential[] | null;
  revision: number;
  onClose: () => void;
  onSaved: () => void;
  rateProbe?: RateProbeControl;
  creating?: boolean;
}) {
  const [rule, setRule] = useState({
    ...model,
    upstream_id: model.upstream_id ?? model.id,
    routes: model.routes?.map((route) => ({ ...route })) ?? [],
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
      ...(rule.routes?.length ? { routes: rule.routes } : {}),
      enabled: rule.enabled,
      keep_original: rule.keep_original,
      region: rule.region || null,
      profile: rule.profile || null,
      credential_ids: rule.credential_ids,
      credits_override: rule.credits_override ?? {},
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
          setPreview({
            candidates: list(data.candidates),
            excluded: list(data.excluded),
            capabilities: data.capabilities,
            limits: data.limits,
            metadata_by_profile: data.metadata_by_profile,
          });
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
          <fieldset className={s.fieldset}>
            <legend>多模型路由</legend>
            <p>
              可按顺序或权重选择多个上游模型。留空权重时按列表从上到下尝试；关闭路线不会参与派发。
            </p>
            {rule.routes.length > 0 && (
              <div className={s.routePreview}>
                {rule.routes.map((route, index) => (
                  <div className={s.routeCandidate} key={`${route.upstream_id}-${index}`}>
                    <input
                      value={route.upstream_id}
                      list={suggestions}
                      placeholder="上游模型 ID"
                      onChange={(e) =>
                        update(
                          "routes",
                          rule.routes.map((item, i) =>
                            i === index ? { ...item, upstream_id: e.target.value } : item,
                          ),
                        )
                      }
                    />
                    <input
                      type="number"
                      min="0"
                      max="1000"
                      step="any"
                      value={route.weight ?? ""}
                      placeholder="权重"
                      onChange={(e) =>
                        update(
                          "routes",
                          rule.routes.map((item, i) =>
                            i === index
                              ? {
                                  ...item,
                                  weight: e.target.value === "" ? null : Number(e.target.value),
                                }
                              : item,
                          ),
                        )
                      }
                    />
                    <label className={s.check}>
                      <input
                        type="checkbox"
                        checked={route.enabled}
                        onChange={(e) =>
                          update(
                            "routes",
                            rule.routes.map((item, i) =>
                              i === index ? { ...item, enabled: e.target.checked } : item,
                            ),
                          )
                        }
                      />
                      启用
                    </label>
                    <button
                      type="button"
                      onClick={() =>
                        update(
                          "routes",
                          rule.routes.filter((_, i) => i !== index),
                        )
                      }
                    >
                      删除
                    </button>
                  </div>
                ))}
              </div>
            )}
            <button
              type="button"
              onClick={() =>
                update(
                  "routes",
                  rule.routes.length
                    ? [
                        ...rule.routes,
                        {
                          upstream_id: rule.upstream_id,
                          weight: null,
                          enabled: true,
                          source: "manual",
                        },
                      ]
                    : [
                        {
                          upstream_id: rule.upstream_id,
                          weight: null,
                          enabled: true,
                          source: "manual",
                        },
                      ],
                )
              }
            >
              添加路由候选
            </button>
          </fieldset>
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
                <Select
                  value={rule.region || (rule.profile.startsWith("intl-") ? "intl" : "cn")}
                  onChange={(value) => {
                    const region = value;
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
                </Select>
              </label>
              <label className={s.field}>
                产品
                <Select value={rule.profile} onChange={(value) => update("profile", value)}>
                  <option value="">不限产品</option>
                  {profiles
                    .filter((p) => !rule.region || p.startsWith(`${rule.region}-`))
                    .map((p) => (
                      <option key={p} value={p}>
                        {profileLabel(p)}
                      </option>
                    ))}
                </Select>
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
          {!creating && !model.custom && (
            <RateOverrideFields
              model={model}
              value={rule.credits_override ?? {}}
              onChange={(next) => update("credits_override", next)}
              probe={rateProbe}
              disabled={busy}
            />
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
      {preview && (
        <>
          <RoutePreview preview={preview} />
          <ModelMetadata model={preview} />
        </>
      )}
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
type SortKey = "catalog" | "rate" | "context" | "output" | "name";
type CatalogFilter = { region: string; capability: string; effort: string; price: string };
const emptyFilter: CatalogFilter = { region: "", capability: "", effort: "", price: "" };
const EFFORT_FILTERS = ["low", "medium", "high", "xhigh", "max"];
function rateValue(model: ModelRule): number {
  return rateOf(model.credits) ?? Number.POSITIVE_INFINITY;
}
function sortModels(list: ModelRule[], key: SortKey): ModelRule[] {
  if (key === "catalog") return list;
  const out = list.slice();
  const widest = (model: ModelRule, kind: "input" | "output") =>
    specRange(model, kind === "input" ? "maxInputTokens" : "maxOutputTokens").at(-1) ?? 0;
  if (key === "rate") out.sort((a, b) => rateValue(a) - rateValue(b));
  else if (key === "context") out.sort((a, b) => widest(b, "input") - widest(a, "input"));
  else if (key === "output") out.sort((a, b) => widest(b, "output") - widest(a, "output"));
  else out.sort((a, b) => a.public_id.localeCompare(b.public_id));
  return out;
}
function matchesCatalog(model: ModelRule, query: string, filter: CatalogFilter): boolean {
  if (query) {
    const text = modelSearchText(model);
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    if (!terms.every((term) => text.includes(term))) return false;
  }
  if (filter.region) {
    if (filter.region === "auto") {
      if (bindingMode(model) !== "auto") return false;
    } else if (model.region !== filter.region) return false;
  }
  if (filter.capability === "media_image") {
    if (!model.media_kinds?.some((kind) => kind === "image" || kind === "edit")) return false;
  } else if (filter.capability === "media_video") {
    if (!model.media_kinds?.includes("video")) return false;
  } else if (filter.capability) {
    const declared =
      model.capabilities && typeof model.capabilities === "object"
        ? (model.capabilities as RecordValue)
        : {};
    if (declared[filter.capability] !== "supported") return false;
  }
  if (filter.effort) {
    const efforts = effortSummary(model);
    if (filter.effort === "off" ? !efforts.closable : !efforts.options.includes(filter.effort))
      return false;
  }
  if (filter.price === "free" && rateOf(model.credits) !== 0) return false;
  if (filter.price === "paid" && (rateOf(model.credits) ?? 0) <= 0) return false;
  return true;
}
function rateProbeTitle(job: Job) {
  return job.meta.scope === "all" ? "一键检测倍率" : `倍率探测 · ${text(job.meta.scope)}`;
}
function notifyRateProbe(job: Job) {
  notify({
    key: "rate-probe",
    tone: (number(job.meta.failed) ?? 0) > 0 ? "warn" : "good",
    title: `${rateProbeTitle(job)}已完成`,
    body: (
      <span>
        {job.total
          ? `实测计费 ${text(job.meta.billed)} · 确认免费 ${text(job.meta.free)} · 失败 ${text(job.meta.failed)}`
          : "没有目录声明 0x 的模型"}
      </span>
    ),
  });
}
const scanSummary = (scan: CatalogScan) =>
  `成功 ${scan.success} · 失败 ${scan.failed} · 目录差异 ${scan.differences}`;
function notifyScan(scan: CatalogScan) {
  const versions = Object.entries(scan.profiles)
    .filter(([, row]) => row.distinct > 1)
    .map(([name, row]) => `${name}：${row.distinct} 组目录版本`)
    .join("；");
  const failed = scan.status === "failed";
  notify({
    key: "scan",
    tone: failed ? "bad" : scan.failed > 0 ? "warn" : "good",
    title: failed ? "全量扫描失败" : "全量扫描已完成",
    body: (
      <>
        <span>{scanSummary(scan)}</span>
        {failed && scan.error && <span>{scan.error}</span>}
        {versions && <span>{versions}</span>}
        {(failed || scan.failed > 0) && <span>失败账号保留上次成功的目录。</span>}
      </>
    ),
  });
}
export function Models() {
  const resource = useResource("/models", modelResponse, { cache: true });
  const credentials = useResource("/credentials", credentialResponse, { cache: true });
  const [query, setQuery] = useState("");
  const [detailsId, setDetailsId] = useState<string | null>(null);
  const detailed = resource.data?.models.find((model) => model.id === detailsId);
  const [editing, setEditing] = useState<ModelRule | null>(null);
  const [creating, setCreating] = useState(false);
  const [editorRevision, setEditorRevision] = useState<number | null>(null);
  const [deleting, setDeleting] = useState<ModelRule | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [scan, setScan] = useState<CatalogScan | null>(null);
  const [scanBusy, setScanBusy] = useState(false);
  const outputProbes = useOutputProbes();
  const rateJob = useRateProbeJob((job) => {
    resource.reload();
    notifyRateProbe(job);
  });
  // Only a scan this page watched finish raises a toast; a reload does not repeat an old one.
  const scanRunning = useRef(false);
  const loadScan = useCallback(() => {
    void api
      .get("/models/scan")
      .then((response) => {
        const next = catalogScanResponse(response.data);
        setScan(next);
        // A scan that just finished may have refreshed the catalog list.
        if (next.status === "completed" || next.status === "failed") resource.reload();
        if (next.status !== "running" && scanRunning.current) notifyScan(next);
        scanRunning.current = next.status === "running";
      })
      .catch(() => {
        // 被动状态轮询失败不升级为页面错误；提交扫描的失败才提示。
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    loadScan(); // An in-progress scan survives page reloads.
  }, [loadScan]);
  useEffect(() => {
    if (scan?.status !== "running") return;
    const timer = setInterval(loadScan, 2000);
    return () => clearInterval(timer);
  }, [scan?.status, loadScan]);
  const startScan = () => {
    if (scanBusy) return;
    setScanBusy(true);
    setError(null);
    void api
      .post("/models/scan")
      .then((response) => {
        const next = catalogScanResponse(response.data);
        scanRunning.current = next.status === "running";
        setScan(next);
        resource.reload();
      })
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setScanBusy(false));
  };
  const toggleGuard = (key: "model_guard" | "model_capability_guard", next: boolean) => {
    if (!resource.data || busy) return;
    setBusy(true);
    setError(null);
    void api
      .patch("/settings", {
        revision: resource.data.revision,
        values: { [key]: next },
      })
      .then(() => {
        notify({
          key: "guard",
          tone: "good",
          title: `${key === "model_guard" ? "表外模型拦截" : "模型能力预检"}已${next ? "开启" : "关闭"}`,
          body:
            key === "model_capability_guard" && !next ? (
              <span>
                仅关闭新增能力预检；元数据展示、国际图片归并及其他安全限制不变，新请求生效。
              </span>
            ) : undefined,
        });
        resource.reload();
      })
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setBusy(false));
  };
  const [filter, setFilter] = useState<CatalogFilter>(emptyFilter);
  const [sort, setSort] = useState<SortKey>("catalog");
  const filtering = Boolean(
    query || filter.region || filter.capability || filter.effort || filter.price,
  );
  const matched = resource.data?.models.filter((m) => matchesCatalog(m, query, filter));
  const rows = matched && sortModels(matched, sort);
  return (
    <>
      <PageTitle
        title="模型路由"
        description="独立映射模型名称，并明确每条路由的账号或区域边界。"
        actions={
          <>
            <button
              onClick={() => {
                clearToasts();
                resource.reload();
                credentials.reload();
              }}
            >
              <RefreshIcon spinning={resource.refreshing || credentials.refreshing} />
              刷新目录
            </button>
            <button
              disabled={rateJob.job?.status === "running"}
              onClick={() => rateJob.start()}
              title="后台任务：对目录声明 0x 的模型逐产品发起一次 ≈8K token 对话；有扣费即为实测计费，未扣费才是真 0 倍率"
            >
              <RefreshIcon spinning={rateJob.job?.status === "running"} />
              {rateJob.job?.status === "running"
                ? `检测中 ${rateJob.job.completed}/${rateJob.job.total}`
                : "一键检测倍率"}
            </button>
            <button
              disabled={scanBusy || scan?.status === "running"}
              onClick={startScan}
              title="逐个账号刷新目录，仅由你手动触发；单账号失败时保留其旧目录"
            >
              <RefreshIcon spinning={scanBusy || scan?.status === "running"} />
              {scan?.status === "running"
                ? `扫描中 ${scan.completed}/${scan.total}`
                : "扫描全部账号模型"}
            </button>
            <button
              className={s.primary}
              disabled={!resource.data}
              onClick={() => {
                setEditorRevision(resource.data?.revision ?? null);
                setCreating(true);
              }}
            >
              <Icon name="plus" />
              新增模型
            </button>
          </>
        }
      />
      <ResourceState {...resource} />
      <ErrorNotice message={error} />
      <ErrorNotice message={rateJob.error} />
      {rateJob.job?.status === "running" && (
        <JobBanner title={rateProbeTitle(rateJob.job)} job={rateJob.job} describe={describeProbe} />
      )}
      <ErrorNotice message={credentials.error} retry={credentials.reload} />
      {scan?.status === "running" && (
        <ProgressBanner title="扫描全部账号模型" completed={scan.completed} total={scan.total}>
          <small>{scanSummary(scan)}</small>
        </ProgressBanner>
      )}
      <Panel
        title="模型目录"
        hint={
          resource.data
            ? filtering && rows
              ? `命中 ${rows.length} / ${resource.data.models.length} 个模型`
              : `${resource.data.models.length} 个模型`
            : undefined
        }
      >
        <div className={s.toolbar}>
          <SearchInput
            aria-label="搜索模型"
            placeholder="搜索 ID、名称、描述、厂商，支持空格分隔多关键词…"
            value={query}
            onChange={setQuery}
          />
          <div className={s.filterBar}>
            <Select
              aria-label="筛选区域"
              value={filter.region}
              onChange={(value) => setFilter({ ...filter, region: value })}
            >
              <option value="">全部区域</option>
              <option value="auto">自动选择</option>
              <option value="cn">中国大陆</option>
              <option value="intl">国际</option>
            </Select>
            <Select
              aria-label="筛选能力"
              value={filter.capability}
              onChange={(value) => setFilter({ ...filter, capability: value })}
            >
              <option value="">全部能力</option>
              <option value="media_image">图片生成 / 编辑</option>
              <option value="media_video">视频生成</option>
              <option value="images">支持视觉</option>
              <option value="tools">支持工具</option>
              <option value="reasoning">支持思考</option>
            </Select>
            <Select
              aria-label="筛选思考"
              value={filter.effort}
              onChange={(value) => setFilter({ ...filter, effort: value })}
            >
              <option value="">全部档位</option>
              <option value="off">可关闭思考</option>
              {EFFORT_FILTERS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </Select>
            <Select
              aria-label="筛选价格"
              value={filter.price}
              onChange={(value) => setFilter({ ...filter, price: value })}
            >
              <option value="">全部价格</option>
              <option value="free">限时免费</option>
              <option value="paid">收费</option>
            </Select>
            <Select
              aria-label="排序方式"
              value={sort}
              onChange={(value) => setSort(value as SortKey)}
            >
              <option value="catalog">目录顺序</option>
              <option value="rate">倍率 低 → 高</option>
              <option value="context">上下文 大 → 小</option>
              <option value="output">最大输出 大 → 小</option>
              <option value="name">模型 ID A → Z</option>
            </Select>
            {filtering && (
              <button
                onClick={() => {
                  setQuery("");
                  setFilter(emptyFilter);
                  setSort("catalog");
                }}
              >
                重置
              </button>
            )}
            <label className={s.enableSwitch}>
              <input
                type="checkbox"
                role="switch"
                aria-label="表外模型拦截"
                checked={resource.data?.model_guard !== false}
                disabled={busy || !resource.data || resource.data.model_guard_locked === true}
                onChange={(e) => toggleGuard("model_guard", e.target.checked)}
              />
              <span>表外拦截：{resource.data?.model_guard === false ? "关闭" : "开启"}</span>
            </label>
            <label className={s.enableSwitch}>
              <input
                type="checkbox"
                role="switch"
                aria-label="模型能力预检"
                checked={resource.data?.model_capability_guard !== false}
                disabled={
                  busy ||
                  !resource.data ||
                  resource.data.model_capability_guard === undefined ||
                  resource.data.model_capability_guard_locked === true
                }
                onChange={(e) => toggleGuard("model_capability_guard", e.target.checked)}
              />
              <span>
                能力预检：
                {resource.data?.model_capability_guard === undefined
                  ? "未知"
                  : resource.data.model_capability_guard
                    ? "开启"
                    : "关闭"}
              </span>
            </label>
          </div>
        </div>
        {rows &&
          (rows.length ? (
            <div className={s.tableWrap}>
              <table>
                <thead>
                  <tr>
                    <th>模型 ID</th>
                    <th>消费倍率 / 优惠</th>
                    <th>模型能力</th>
                    <th>上下文窗口</th>
                    <th>最大输出</th>
                    <th>思考档位</th>
                    <th>路由边界</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((m) => (
                    <tr key={m.id}>
                      <td>
                        <div className={s.modelCell}>
                          <div className={s.modelIdLine}>
                            <strong className={s.modelName}>{m.public_id}</strong>
                            {!m.enabled && <Badge tone="neutral">已停用</Badge>}
                            {m.custom && <Badge>自建</Badge>}
                            {m.media_kinds?.map((kind) => (
                              <Badge key={kind} tone="neutral">
                                {mediaKindLabels[kind] ?? kind}
                              </Badge>
                            ))}
                            {m.embedding && <Badge tone="neutral">向量</Badge>}
                            {m.rerank && <Badge tone="neutral">重排序</Badge>}
                            {m.available === false && m.enabled && (
                              <Badge tone="warn">暂无候选</Badge>
                            )}
                          </div>
                          <small className={s.mono}>{m.upstream_id ?? m.id}</small>
                          {modelDisplayName(m) && <small>{modelDisplayName(m)}</small>}
                          {m.routes && m.routes.length > 1 && (
                            <div className={s.routeSummary} aria-label="模型路由">
                              <span className={s.routeSummaryLabel}>路由</span>
                              {m.routes.map((route) => (
                                <span
                                  className={`${s.routePill} ${route.enabled ? "" : s.routeDisabled}`}
                                  key={`${route.source}-${route.upstream_id}`}
                                  title={`${route.source} · ${route.enabled ? "启用" : "已关闭"}`}
                                >
                                  {route.upstream_id}
                                  {route.weight !== null ? ` · ${route.weight}` : ""}
                                </span>
                              ))}
                            </div>
                          )}
                          {m.keep_original && <small>同时保留 {m.id}</small>}
                        </div>
                      </td>
                      <td>
                        <RateCell model={m} />
                      </td>
                      <td className={s.capCell}>
                        <CapabilityChips model={m} />
                      </td>
                      <td className={s.specCell}>
                        <SpecValue model={m} kind="input" />
                      </td>
                      <td className={s.specCell}>
                        <SpecValue model={m} kind="output" />
                        <OutputProbeNote
                          probe={outputProbes[m.public_id] ?? outputProbes[m.upstream_id ?? m.id]}
                        />
                      </td>
                      <td>
                        <EffortChips model={m} />
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
                          <button onClick={() => setDetailsId(m.id)}>模型详情</button>
                          <button
                            onClick={() => {
                              setEditorRevision(resource.data?.revision ?? null);
                              setEditing(m);
                            }}
                          >
                            编辑规则
                          </button>
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
        {detailed && (
          <Drawer title={`模型详情 · ${detailed.public_id}`} onClose={() => setDetailsId(null)}>
            <ModelMetadata model={detailed} />
          </Drawer>
        )}
      </DrawerPresence>
      <DrawerPresence>
        {(editing || creating) && resource.data && (
          <ModelEditor
            model={editing ?? emptyRule}
            creating={creating}
            models={resource.data.models}
            credentials={credentials.data}
            revision={editorRevision ?? resource.data.revision}
            onClose={() => {
              setEditing(null);
              setCreating(false);
              setEditorRevision(null);
            }}
            onSaved={resource.reload}
            rateProbe={rateJob}
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
