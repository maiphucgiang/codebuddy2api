import { metric, type RecordValue } from "./api";
import s from "./ui.module.scss";

const labels: Record<string, string> = {
  id: "标识",
  name: "名称",
  filename: "文件名",
  uid: "账号 ID",
  nickname: "昵称",
  account_key: "账号指纹",
  public_id: "对外 ID",
  upstream_id: "上游 ID",
  model: "模型",
  public_model: "对外模型",
  upstream_model: "上游模型",
  profile: "产品",
  site: "区域",
  region: "区域",
  credential: "凭证",
  credential_ids: "绑定账号",
  bindings: "绑定模型",
  enabled: "启用状态",
  auto_checkin: "自动签到",
  auto_travel: "自动旅行",
  travel_supported: "支持旅行",
  travel: "旅行状态",
  checkin: "签到状态",
  last_success: "上次成功状态",
  state: "阶段",
  at: "记录时间",
  claimed: "本次已领取",
  departed: "本次已派出",
  claimed_credit: "本次领取积分",
  reward_credit: "旅行奖励积分",
  location_id: "地点编号",
  location_name: "旅行地点",
  arrive_at: "预计到达",
  server_now: "上游时间",
  daily_limit_reached: "今日派遣已达上限",
  available: "可用状态",
  available_credentials: "可用账号数",
  health: "健康状态",
  status: "状态",
  status_code: "HTTP 状态",
  outcome: "结果",
  error: "错误",
  error_code: "错误代码",
  last_error_code: "最近错误",
  last_error: "最近异常",
  reason: "原因",
  code: "代码",
  message: "说明",
  kind: "类型",
  action: "操作",
  level: "级别",
  started_at: "开始时间",
  finished_at: "结束时间",
  fetched_at: "同步时间",
  updated_at: "更新时间",
  created_at: "创建时间",
  generated_at: "生成时间",
  last_failure_at: "最近失败",
  token_expires_at: "令牌到期",
  expires_at: "到期时间",
  expiresAt: "到期时间",
  soonest_expiry: "最近到期",
  lastRefreshTime: "最近刷新",
  fail_until: "熔断截止",
  cooldown_until: "冷却截止",
  until: "截止时间",
  token_expired: "令牌已过期",
  cooldowns: "模型冷却",
  cooldown_remaining: "剩余冷却秒数",
  remaining_seconds: "剩余秒数",
  sync_pending: "等待同步",
  catalog_ready: "目录已就绪",
  partial: "数据不完整",
  stale_accounts: "待更新账号",
  candidates: "可用账号",
  excluded: "不可用账号",
  requests: "请求数",
  success: "成功",
  cancelled: "已取消",
  duration_ms: "耗时",
  protocol: "协议",
  request_id: "请求 ID",
  upstream_request_id: "上游请求 ID",
  attempt: "尝试序号",
  max_attempts: "尝试预算",
  retry_after: "重试等待秒数",
  input_tokens: "输入 Token",
  output_tokens: "输出 Token",
  total_tokens: "总 Token",
  reasoning_tokens: "思考 Token",
  cache_read_tokens: "缓存读取 Token",
  cache_creation_tokens: "缓存写入 Token",
  credit: "已知消耗 Credit",
  credits: "官方额度",
  remaining: "剩余额度",
  used: "已用额度",
  segments: "额度分段",
  total: "总额",
  count: "数量",
  credits_by_profile: "产品倍率",
  detail: "详情",
  diagnostics: "诊断",
  request_preview: "请求预览",
  response_preview: "响应预览",
  logical_bytes: "明细占用",
  db_bytes: "数据库大小",
  wal_bytes: "WAL 大小",
  shm_bytes: "SHM 大小",
  max_bytes: "明细容量上限",
  retention_days: "明细保留天数",
  request_count: "请求明细数",
  event_count: "事件明细数",
  ingest_count: "防重记录数",
  pending_cleanup: "等待清理",
  preview_limit: "诊断预览上限",
  schema_version: "存储版本",
  epoch: "统计代次",
  degraded: "存储降级",
  failure_count: "失败次数",
  dropped_records: "未写入记录数",
  closed: "存储已关闭",
  budget_scope: "容量统计范围",
  fault_counter_scope: "故障计数范围",
  automatic_vacuum: "自动压缩",
  lock_timeout_ms: "锁等待上限",
  sql_deadline_ms: "SQL 执行上限",
};
const profiles: Record<string, string> = {
  "cn-cli": "大陆 · CodeBuddy",
  "cn-work": "大陆 · WorkBuddy",
  "intl-cli": "国际 · CodeBuddy",
  "intl-work": "国际 · WorkBuddy",
  cn: "中国大陆",
  intl: "国际",
};
const statuses: Record<string, string> = {
  ready: "就绪",
  disabled: "已停用",
  error: "失败",
  success: "成功",
  cancelled: "已取消",
  circuit_open: "认证熔断",
  expired: "已过期",
  completed: "已完成",
  pending: "等待中",
  details_logical_bytes: "请求与事件明细",
  process_lifetime: "本次运行期间",
  request: "推理请求",
  runtime: "运行事件",
  admin: "管理操作",
};
const times = new Set([
  "started_at",
  "finished_at",
  "fetched_at",
  "updated_at",
  "created_at",
  "generated_at",
  "last_failure_at",
  "token_expires_at",
  "expires_at",
  "expiresAt",
  "soonest_expiry",
  "lastRefreshTime",
  "fail_until",
  "cooldown_until",
  "until",
]);
const ownLabel = (map: Record<string, string>, key: string) =>
  Object.hasOwn(map, key) ? map[key] : undefined;
export function profileLabel(value: string) {
  return ownLabel(profiles, value) ?? value;
}
export function bytes(value: number) {
  if (value < 1024) return `${metric(value)} B`;
  if (value < 1048576) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1073741824) return `${(value / 1048576).toFixed(1)} MiB`;
  return `${(value / 1073741824).toFixed(2)} GiB`;
}
export function DataValue({
  value,
  name = "",
  depth = 0,
}: {
  value: unknown;
  name?: string;
  depth?: number;
}) {
  if (value === null || value === undefined) return <span className={s.valueMuted}>未知</span>;
  if (typeof value === "boolean") {
    const warning = ["partial", "degraded", "token_expired", "closed"].includes(name);
    return (
      <span className={`${s.badge} ${value ? (warning ? s.warn : s.good) : s.neutral}`}>
        {name === "enabled" ? (value ? "已启用" : "已停用") : value ? "是" : "否"}
      </span>
    );
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return <span className={s.valueMuted}>未知</span>;
    if (times.has(name)) {
      if (value === 0) return <>未设置</>;
      const date = new Date(value > 1e11 ? value : value * 1000);
      return <>{Number.isNaN(date.valueOf()) ? "时间无效" : date.toLocaleString("zh-CN")}</>;
    }
    if (name.endsWith("_bytes")) return <>{bytes(value)}</>;
    if (name.endsWith("_ms")) return <>{metric(value)} ms</>;
    return <>{new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 8 }).format(value)}</>;
  }
  if (typeof value === "string") {
    if (["request_preview", "response_preview", "diagnostic_preview"].includes(name)) {
      let formatted = value;
      if (value.length <= 8192 && ["{", "["].includes(value.trimStart()[0] ?? "")) {
        try {
          formatted = JSON.stringify(JSON.parse(value), null, 2);
        } catch {
          /* Truncated previews remain readable text. */
        }
      }
      return (
        <details className={s.rawData}>
          <summary>查看原始诊断</summary>
          <pre>{formatted}</pre>
        </details>
      );
    }
    const label = ["profile", "site", "region"].includes(name)
      ? ownLabel(profiles, value)
      : ["health", "status", "outcome", "kind", "budget_scope", "fault_counter_scope"].includes(
            name,
          )
        ? ownLabel(statuses, value)
        : undefined;
    return <span className={s.valueText}>{label ?? (value || "—")}</span>;
  }
  if (typeof value !== "object") return <span className={s.valueMuted}>未知</span>;
  if (depth >= 4)
    return (
      <details className={s.rawData}>
        <summary>查看深层诊断</summary>
        <pre>{JSON.stringify(value, null, 2)}</pre>
      </details>
    );
  if (Array.isArray(value)) {
    if (!value.length) return <span className={s.valueMuted}>无</span>;
    const simple = value.every((item) => item === null || typeof item !== "object");
    return (
      <div className={simple ? s.valueChips : s.valueList}>
        {value.map((item, i) => (
          <div className={simple ? s.valueChip : s.valueCard} key={i}>
            <DataValue value={item} depth={depth + 1} />
          </div>
        ))}
      </div>
    );
  }
  return <Fields data={value as RecordValue} depth={depth + 1} />;
}
export function Fields({ data, depth = 0 }: { data: RecordValue; depth?: number }) {
  return (
    <dl className={`${s.details} ${depth ? s.nestedDetails : ""}`}>
      {Object.entries(data).map(([key, value]) => (
        <div key={key}>
          <dt title={key}>{ownLabel(labels, key) ?? key.replaceAll("_", " ")}</dt>
          <dd>
            <DataValue value={value} name={key} depth={depth} />
          </dd>
        </div>
      ))}
    </dl>
  );
}
