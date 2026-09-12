import { useState } from "react";
import { list, metric, number, object, text, useResource, type RecordValue } from "../api";
import { Badge, Empty, Fields, Icon, PageTitle, Panel, ResourceState } from "../components";
import s from "../ui.module.scss";
function normalize(value: unknown): RecordValue & {
  summary: RecordValue;
  series: RecordValue[];
  models: RecordValue[];
  profiles: RecordValue[];
  health: RecordValue;
  storage: RecordValue;
} {
  const d = object(value);
  for (const field of ["series", "models", "profiles"]) {
    if (list(d[field], field).some((row) => number(row.requests) === null))
      throw new Error(`${field} 中存在未知请求量，不能绘制为 0`);
  }
  return {
    ...d,
    summary: object(d.summary, "统计"),
    series: list(d.series, "趋势"),
    models: list(d.models, "模型排行"),
    profiles: list(d.profiles, "产品分布"),
    health: object(d.health, "健康状态"),
    storage: object(d.storage, "存储状态"),
  };
}
function Health({ data }: { data: RecordValue }) {
  if (!Array.isArray(data.credentials)) return <Fields data={data} />;
  const credentials = list(data.credentials);
  if (!credentials.length) return <Empty title="尚未添加凭证" />;
  return (
    <div className={s.healthList}>
      {credentials.map((c, i) => (
        <div key={i}>
          <div>
            <strong>{text(c.name ?? c.filename)}</strong>
            <small>{text(c.profile)}</small>
          </div>
          <div>
            <Badge tone={c.enabled === false ? "neutral" : c.health === "ready" ? "good" : "warn"}>
              {c.enabled === false ? "人工停用" : c.health === "ready" ? "就绪" : text(c.health)}
            </Badge>
            {Array.isArray(c.cooldowns) && c.cooldowns.length > 0 && (
              <small>{c.cooldowns.length} 个模型冷却</small>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
function OfficialCredits({ value }: { value: unknown }) {
  const rows = Object.entries(object(value));
  if (!rows.length) return <Empty title="暂无官方余额快照" />;
  return (
    <div className={s.tableWrap}>
      <table>
        <thead>
          <tr>
            <th>凭证</th>
            <th>官方余额</th>
            <th>同步状态</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([key, value]) => {
            const row = object(value);
            const credits = row.credits ? object(row.credits) : null;
            return (
              <tr key={key}>
                <td>{typeof row.name === "string" ? row.name : key.split(/[\\/]/).at(-1)}</td>
                <td>{metric(credits?.credits ?? credits?.remaining)}</td>
                <td>
                  {row.error ? (
                    <Badge tone="warn">同步异常</Badge>
                  ) : (
                    <span>
                      {typeof row.fetched_at === "number"
                        ? new Date(row.fetched_at * 1000).toLocaleString("zh-CN")
                        : "同步时间未知"}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
function bytes(value: unknown) {
  const n = number(value);
  if (n === null) return "未知";
  if (n < 1024) return `${metric(n)} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / 1048576).toFixed(1)} MiB`;
}
export function Trend({ rows }: { rows: RecordValue[] }) {
  if (!rows.length) return <Empty title="当前时间范围内暂无请求" />;
  const max = Math.max(1, ...rows.map((r) => number(r.requests) ?? 0));
  const xAt = (index: number) => (rows.length === 1 ? 390 : 30 + (index * 720) / (rows.length - 1));
  const points = rows
    .map((r, i) => `${xAt(i)},${180 - ((number(r.requests) ?? 0) / max) * 145}`)
    .join(" ");
  return (
    <>
      <svg viewBox="0 0 780 220" className={s.chart} role="img" aria-label="按日请求量趋势">
        <defs>
          <linearGradient id="chartFill" x1="0" y1="0" x2="0" y2="1">
            <stop stopColor="#78AD48" stopOpacity=".22" />
            <stop offset="1" stopColor="#78AD48" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[35, 83, 131, 180].map((y) => (
          <line key={y} x1="30" y1={y} x2="750" y2={y} stroke="#e4eadd" strokeDasharray="4 5" />
        ))}
        {rows.length > 1 && (
          <>
            <polygon points={`30,180 ${points} 750,180`} fill="url(#chartFill)" />
            <polyline points={points} stroke="#78AD48" strokeWidth="3" fill="none" />
          </>
        )}
        {rows.map((r, i) => (
          <circle
            key={i}
            cx={xAt(i)}
            cy={180 - ((number(r.requests) ?? 0) / max) * 145}
            r="3.5"
            fill="#3F7028"
          >
            <title>
              {text(r.date)} · {metric(r.requests)} 请求
            </title>
          </circle>
        ))}
        <text x={xAt(0)} y="209" textAnchor={rows.length === 1 ? "middle" : "start"}>
          {text(rows[0].date)}
        </text>
        {rows.length > 1 && (
          <text x="750" y="209" textAnchor="end">
            {text(rows.at(-1)?.date)}
          </text>
        )}
      </svg>
      <details className={s.chartData}>
        <summary>查看趋势数据表</summary>
        <table>
          <thead>
            <tr>
              <th>日期</th>
              <th>请求</th>
              <th>成功</th>
              <th>失败</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td>{text(r.date)}</td>
                <td>{metric(r.requests)}</td>
                <td>{metric(r.success)}</td>
                <td>{metric(r.error)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </>
  );
}
export function Dashboard() {
  const [days, setDays] = useState("7");
  const resource = useResource(`/dashboard?days=${days}`, normalize);
  const d = resource.data;
  const rate = number(d?.summary.success_rate);
  return (
    <>
      <PageTitle
        title="运行概览"
        actions={
          <>
            <select
              aria-label="统计时间范围"
              value={days}
              onChange={(e) => setDays(e.target.value)}
            >
              {["1", "7", "30", "90"].map((n) => (
                <option value={n} key={n}>
                  最近 {n} 天
                </option>
              ))}
            </select>
            <button onClick={resource.reload}>
              <Icon name="refresh" />
              刷新
            </button>
          </>
        }
      />
      <ResourceState {...resource} />
      {d && (
        <>
          {(d.degraded === true || d.storage.degraded === true) && (
            <div className={s.warning} role="alert">
              统计可能不完整，请检查日志存储状态。
            </div>
          )}
          <div className={s.stats}>
            {[
              ["请求总量", metric(d.summary.requests), "arrow"],
              ["成功率", rate === null ? "未知" : `${(rate * 100).toFixed(1)}%`, "shield"],
              ["Token 用量", metric(d.summary.total_tokens), "model"],
              ["网关已知 Credit", metric(d.summary.credit), "leaf"],
            ].map(([label, value, icon]) => (
              <section className={s.stat} key={label}>
                <div>
                  {label}
                  <span>
                    <Icon name={icon} />
                  </span>
                </div>
                <strong>{value}</strong>
              </section>
            ))}
          </div>
          <div className={s.dashboardGrid}>
            <Panel title="请求趋势" hint={`最近 ${days} 天 · UTC`} className={s.trend}>
              <Trend rows={d.series} />
            </Panel>
            <Panel title="产品分布" hint="按请求量">
              {d.profiles.length ? (
                <div className={s.distribution}>
                  {d.profiles.map((p, i) => {
                    const total = d.profiles.reduce((sum, r) => sum + (number(r.requests) ?? 0), 0);
                    const count = number(p.requests);
                    return (
                      <div key={i}>
                        <div>
                          <strong>{text(p.profile) || "未选路"}</strong>
                          <span>{metric(p.requests)}</span>
                        </div>
                        <progress
                          max={total || 1}
                          value={count ?? 0}
                          aria-label={`${text(p.profile) || "未选路"} 请求占比`}
                        />
                      </div>
                    );
                  })}
                </div>
              ) : (
                <Empty title="暂无产品用量" />
              )}
            </Panel>
            <Panel title="模型使用排行">
              {d.models.length ? (
                <div className={s.tableWrap}>
                  <table>
                    <thead>
                      <tr>
                        <th>模型</th>
                        <th>请求</th>
                        <th>Token</th>
                        <th>Credit</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...d.models]
                        .sort((a, b) => (number(b.requests) ?? -1) - (number(a.requests) ?? -1))
                        .slice(0, 10)
                        .map((m, i) => (
                          <tr key={i}>
                            <td>
                              <span className={s.rank}>{String(i + 1).padStart(2, "0")}</span>
                              <strong>{text(m.model) || "未识别模型"}</strong>
                            </td>
                            <td>{metric(m.requests)}</td>
                            <td>{metric(m.total_tokens)}</td>
                            <td>{metric(m.credit)}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty title="尚无模型请求记录" />
              )}
            </Panel>
            <Panel title="凭证健康">
              <Health data={d.health} />
            </Panel>
            <Panel title="日志存储">
              <div className={s.storageOverview}>
                <Badge
                  tone={
                    d.storage.degraded === true
                      ? "bad"
                      : d.storage.degraded === false
                        ? "good"
                        : "neutral"
                  }
                >
                  {d.storage.degraded === true
                    ? "采集降级"
                    : d.storage.degraded === false
                      ? "正常"
                      : "状态未知"}
                </Badge>
                <span>
                  明细 {bytes(d.storage.logical_bytes)} / {bytes(d.storage.max_bytes)}
                </span>
              </div>
              <Fields
                data={{
                  数据库文件: bytes(d.storage.db_bytes),
                  "WAL 文件": bytes(d.storage.wal_bytes),
                  "SHM 文件": bytes(d.storage.shm_bytes),
                  保留天数: metric(d.storage.retention_days),
                  清理状态:
                    d.storage.pending_cleanup === true
                      ? "分批清理中"
                      : d.storage.pending_cleanup === false
                        ? "已完成"
                        : "未知",
                }}
              />
              <details className={s.chartData}>
                <summary>查看存储诊断</summary>
                <Fields data={d.storage} />
              </details>
            </Panel>
            <Panel title="官方账号余额">
              <p className={s.note}>账号余额，并非本网关消费统计。</p>
              {d.official_credits !== undefined ? (
                <OfficialCredits value={d.official_credits} />
              ) : (
                <p className={s.muted}>暂无余额数据，可在凭证管理查看。</p>
              )}
            </Panel>
          </div>
          <p className={s.updated}>
            更新于{" "}
            {typeof d.generated_at === "number"
              ? new Date(d.generated_at * 1000).toLocaleString("zh-CN")
              : text(d.generated_at)}{" "}
          </p>
        </>
      )}
    </>
  );
}
