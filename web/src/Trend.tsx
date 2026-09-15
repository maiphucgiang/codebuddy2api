import { useId, useState, type PointerEvent } from "react";
import { metric, number, text, type RecordValue } from "./api";
import { Empty } from "./components";
import s from "./ui.module.scss";

export function Trend({
  rows,
  granularity = "day",
  partial = false,
}: {
  rows: RecordValue[];
  granularity?: string;
  partial?: boolean;
}) {
  const id = useId();
  const [selection, setSelection] = useState<{ rows: RecordValue[]; index: number } | null>(null);
  const active = selection?.rows === rows ? selection.index : null;
  if (!rows.length)
    return <Empty title={partial ? "该范围缺少小时记录" : "当前时间范围内暂无请求"} />;
  const max = Math.max(1, ...rows.map((r) => number(r.requests) ?? 0));
  const first = number(rows[0].bucket),
    last = number(rows.at(-1)?.bucket);
  const xAt = (index: number) =>
    rows.length === 1
      ? 390
      : first !== null && last !== null && last > first && number(rows[index].bucket) !== null
        ? 30 + ((Number(rows[index].bucket) - first) * 720) / (last - first)
        : 30 + (index * 720) / (rows.length - 1);
  const yAt = (index: number) => 180 - ((number(rows[index].requests) ?? 0) / max) * 145;
  const points = rows.map((_, i) => `${xAt(i)},${yAt(i)}`).join(" ");
  const choose = (index: number) =>
    setSelection({ rows, index: Math.max(0, Math.min(rows.length - 1, index)) });
  const pointer = (event: PointerEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width) return;
    const x = ((event.clientX - rect.left) * 780) / rect.width;
    let closest = 0;
    for (let i = 1; i < rows.length; i++)
      if (Math.abs(x - xAt(i)) < Math.abs(x - xAt(closest))) closest = i;
    const step = granularity === "hour" ? 3600 : 86400;
    const tolerance =
      first !== null && last !== null && last > first ? (720 * step) / (last - first) / 2 : 30;
    if (partial && Math.abs(x - xAt(closest)) > tolerance) setSelection(null);
    else choose(closest);
  };
  const selected = active === null ? null : rows[active];
  return (
    <>
      <div className={s.chartInteractive}>
        <svg
          viewBox="0 0 780 200"
          preserveAspectRatio="none"
          className={s.chart}
          role="img"
          tabIndex={0}
          aria-label={granularity === "hour" ? "按小时请求量趋势" : "按日请求量趋势"}
          aria-describedby={`${id}-hint${selected ? ` ${id}-tooltip` : ""}`}
          onPointerMove={pointer}
          onPointerDown={pointer}
          onPointerLeave={(e) => {
            if (e.pointerType !== "touch") setSelection(null);
          }}
          onFocus={() => choose(0)}
          onBlur={() => setSelection(null)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              setSelection(null);
              return;
            }
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
            e.preventDefault();
            choose(
              e.key === "Home"
                ? 0
                : e.key === "End"
                  ? rows.length - 1
                  : (active ?? 0) + (e.key === "ArrowRight" ? 1 : -1),
            );
          }}
        >
          <defs>
            <linearGradient id={`${id}-fill`} x1="0" y1="0" x2="0" y2="1">
              <stop stopColor="var(--accent)" stopOpacity=".22" />
              <stop offset="1" stopColor="var(--accent)" stopOpacity="0" />
            </linearGradient>
          </defs>
          {[35, 83, 131, 180].map((y) => (
            <line
              key={y}
              x1="30"
              y1={y}
              x2="750"
              y2={y}
              stroke="var(--line)"
              strokeDasharray="4 5"
            />
          ))}
          {rows.length > 1 && !partial && (
            <>
              <polygon points={`30,180 ${points} 750,180`} fill={`url(#${id}-fill)`} />
              <polyline points={points} stroke="var(--accent)" strokeWidth="3" fill="none" />
            </>
          )}
          {rows.map(
            (r, i) =>
              (rows.length <= 168 || Number(r.requests) > 0) && (
                <circle key={i} cx={xAt(i)} cy={yAt(i)} r="3.5" fill="var(--strong)" />
              ),
          )}
          {active !== null && (
            <g aria-hidden="true">
              <line
                x1={xAt(active)}
                x2={xAt(active)}
                y1="24"
                y2="180"
                stroke="var(--accent)"
                strokeDasharray="3 4"
              />
              <circle
                cx={xAt(active)}
                cy={yAt(active)}
                r="6"
                fill="var(--surface)"
                stroke="var(--accent)"
                strokeWidth="3"
              />
            </g>
          )}
        </svg>
        {selected && active !== null && (
          <div
            className={s.chartTooltip}
            id={`${id}-tooltip`}
            role="tooltip"
            style={{ left: `${Math.min(96, Math.max(4, (xAt(active) / 780) * 100))}%` }}
            data-edge={xAt(active) < 220 ? "left" : xAt(active) > 560 ? "right" : "center"}
          >
            <strong>{text(selected.date)} · UTC</strong>
            <dl>
              <div>
                <dt>请求</dt>
                <dd>{metric(selected.requests)}</dd>
              </div>
              <div>
                <dt>成功</dt>
                <dd>{metric(selected.success)}</dd>
              </div>
              <div>
                <dt>失败</dt>
                <dd>{metric(selected.error)}</dd>
              </div>
            </dl>
          </div>
        )}
      </div>
      <p className={s.srOnly} id={`${id}-hint`}>
        指向或点按查看时段；键盘左右方向键逐点查看，Home 和 End 跳到首尾，Esc 关闭提示。
      </p>
      <div className={s.chartAxis}>
        <span>{text(rows[0].date)}</span>
        {rows.length > 1 && <span>{text(rows.at(-1)?.date)}</span>}
      </div>
      <details className={s.chartData}>
        <summary>查看趋势数据表</summary>
        <table>
          <thead>
            <tr>
              <th>{granularity === "hour" ? "时段（UTC）" : "日期（UTC）"}</th>
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
