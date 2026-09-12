import { useState } from "react";
import {
  api,
  errorMessage,
  list,
  metric,
  object,
  text,
  useResource,
  type RecordValue,
} from "../api";
import {
  Badge,
  ClearLogs,
  Drawer,
  Empty,
  ErrorNotice,
  Fields,
  Icon,
  PageTitle,
  Panel,
  ResourceState,
} from "../components";
import { profiles } from "./Models";
import s from "../ui.module.scss";
function normalize(value: unknown) {
  const data = object(value);
  if (typeof data.has_more !== "boolean" || (data.has_more && typeof data.next_cursor !== "string"))
    throw new Error("日志分页响应不完整");
  return {
    items: list(data.items),
    next_cursor: typeof data.next_cursor === "string" ? data.next_cursor : null,
    has_more: data.has_more,
    degraded: data.degraded === true,
  };
}
const emptyFilters = { model: "", credential: "", profile: "", status: "", search: "" };
export function Logs() {
  const [kind, setKind] = useState("request");
  const [draft, setDraft] = useState(emptyFilters);
  const [filters, setFilters] = useState(emptyFilters);
  const [cursors, setCursors] = useState<string[]>([]);
  const [detail, setDetail] = useState<RecordValue | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [clear, setClear] = useState(false);
  const params = new URLSearchParams({ kind, limit: "50" });
  Object.entries(filters).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  if (cursors.length) params.set("cursor", cursors.at(-1)!);
  const resource = useResource(`/logs?${params.toString()}`, normalize);
  const openDetail = (item: RecordValue) => {
    setDetail(item);
    setDetailError(null);
    if (kind !== "request") return;
    if (typeof item.id !== "string") {
      setDetailError("请求记录缺少 ID，无法加载详情");
      return;
    }
    setDetailLoading(true);
    void api
      .get<unknown>(`/logs/${encodeURIComponent(item.id)}`)
      .then((res) => setDetail(object(res.data)))
      .catch((err: unknown) => setDetailError(errorMessage(err)))
      .finally(() => setDetailLoading(false));
  };
  return (
    <>
      <PageTitle
        title="日志审计"
        actions={
          <>
            <button onClick={resource.reload}>
              <Icon name="refresh" />
              刷新
            </button>
            <button onClick={() => setClear(true)}>清理日志</button>
          </>
        }
      />
      <div className={s.tabs} role="tablist" aria-label="日志类型">
        {[
          ["request", "请求审计"],
          ["runtime", "运行事件"],
          ["admin", "管理操作"],
        ].map(([value, label]) => (
          <button
            key={value}
            role="tab"
            aria-selected={kind === value}
            className={kind === value ? s.selectedTab : ""}
            onClick={() => {
              setKind(value);
              setCursors([]);
              setFilters(emptyFilters);
              setDraft(emptyFilters);
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <Panel
        title={kind === "request" ? "请求记录" : kind === "runtime" ? "运行事件" : "管理操作记录"}
        hint="每页 50 条"
      >
        <form
          className={s.filters}
          onSubmit={(e) => {
            e.preventDefault();
            setFilters(draft);
            setCursors([]);
          }}
        >
          {kind === "request" && (
            <>
              <input
                aria-label="筛选模型"
                placeholder="模型 ID"
                value={draft.model}
                onChange={(e) => setDraft({ ...draft, model: e.target.value })}
              />
              <input
                aria-label="筛选凭证"
                placeholder="凭证 account_key"
                value={draft.credential}
                onChange={(e) => setDraft({ ...draft, credential: e.target.value })}
              />
              <select
                aria-label="筛选产品"
                value={draft.profile}
                onChange={(e) => setDraft({ ...draft, profile: e.target.value })}
              >
                <option value="">全部产品</option>
                {profiles.map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </select>
              <select
                aria-label="筛选状态"
                value={draft.status}
                onChange={(e) => setDraft({ ...draft, status: e.target.value })}
              >
                <option value="">全部状态</option>
                <option value="success">成功</option>
                <option value="error">失败</option>
                <option value="cancelled">取消</option>
                <option value="429">HTTP 429</option>
              </select>
            </>
          )}
          <input
            aria-label="搜索日志"
            placeholder="搜索标识 / 操作…"
            value={draft.search}
            onChange={(e) => setDraft({ ...draft, search: e.target.value })}
          />
          <button type="submit">应用筛选</button>
          <button
            type="button"
            onClick={() => {
              setFilters(emptyFilters);
              setDraft(emptyFilters);
              setCursors([]);
            }}
          >
            重置
          </button>
        </form>
        <ResourceState {...resource} />
        {resource.data?.degraded && (
          <div className={s.warning} role="alert">
            日志读取已降级，列表可能不完整，请检查审计存储状态。
          </div>
        )}
        {resource.data &&
          (resource.data.items.length ? (
            <div className={s.tableWrap}>
              <table>
                <thead>
                  <tr>
                    <th>时间 / ID</th>
                    <th>{kind === "request" ? "模型 / 产品" : "操作 / 类型"}</th>
                    <th>状态</th>
                    {kind === "request" && (
                      <>
                        <th>耗时</th>
                        <th>Token / Credit</th>
                      </>
                    )}
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {resource.data.items.map((item, i) => (
                    <tr key={text(item.id) + i}>
                      <td>
                        {typeof item.started_at === "number"
                          ? new Date(item.started_at * 1000).toLocaleString("zh-CN")
                          : text(item.started_at)}
                        <small className={s.mono}>{text(item.id)}</small>
                      </td>
                      <td>
                        <strong>{text(kind === "request" ? item.model : item.action)}</strong>
                        <small>{text(kind === "request" ? item.profile : item.kind)}</small>
                      </td>
                      <td>
                        <Badge
                          tone={
                            item.outcome === "success"
                              ? "good"
                              : item.outcome === "error"
                                ? "bad"
                                : "neutral"
                          }
                        >
                          {text(item.outcome ?? item.level ?? item.status)}
                        </Badge>
                        {item.status_code !== undefined && (
                          <small>HTTP {text(item.status_code)}</small>
                        )}
                      </td>
                      {kind === "request" && (
                        <>
                          <td>{metric(item.duration_ms)} ms</td>
                          <td>
                            {metric(item.total_tokens)}
                            <small>{metric(item.credit)} Credit</small>
                          </td>
                        </>
                      )}
                      <td>
                        <button disabled={detailLoading} onClick={() => openDetail(item)}>
                          查看详情
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty title="没有匹配的日志">请尝试调整筛选。</Empty>
          ))}
        <div className={s.pagination}>
          <span>第 {cursors.length + 1} 页</span>
          <div className={s.actions}>
            <button
              disabled={!cursors.length || resource.loading}
              onClick={() => setCursors((old) => old.slice(0, -1))}
            >
              上一页
            </button>
            <button
              disabled={!resource.data?.has_more || resource.loading}
              onClick={() => {
                if (resource.data?.next_cursor)
                  setCursors((old) => [...old, resource.data!.next_cursor!]);
              }}
            >
              下一页
            </button>
          </div>
        </div>
      </Panel>
      {detail && (
        <Drawer
          title="日志详情与实际尝试"
          onClose={() => {
            setDetail(null);
            setDetailError(null);
          }}
        >
          <ErrorNotice message={detailError} />
          {detailLoading ? (
            <p role="status">正在加载详情…</p>
          ) : (
            <>
              <Fields
                data={Object.fromEntries(
                  Object.entries(detail).filter(([key]) => key !== "attempts"),
                )}
              />
              {Array.isArray(detail.attempts) && (
                <Panel title="实际尝试">
                  {detail.attempts.length ? (
                    list(detail.attempts).map((attempt, i) => (
                      <div key={i}>
                        <h3>尝试 {i + 1}</h3>
                        <Fields data={attempt} />
                      </div>
                    ))
                  ) : (
                    <p>暂无尝试记录。</p>
                  )}
                </Panel>
              )}
            </>
          )}
        </Drawer>
      )}
      {clear && (
        <ClearLogs
          onClose={() => setClear(false)}
          onDone={() => {
            setCursors([]);
            resource.reload();
          }}
        />
      )}
    </>
  );
}
