import { useEffect, useRef, useState } from "react";
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
  DrawerPresence,
  Empty,
  ErrorNotice,
  Fields,
  RefreshIcon,
  SegmentThumb,
  PageTitle,
  Panel,
  ResourceState,
} from "../components";
import { eventActionLabel, eventKindLabel, webToolLabel } from "../values";
import { serverToolSummary } from "../serverTools";
import { CreditFilters, CreditRow, useAccountNames } from "../CreditChanges";
import { profiles } from "./Models";
import s from "../ui.module.scss";
import { t, useT } from "../i18n";
import { SearchInput, Select } from "../fields";
function normalize(value: unknown) {
  const data = object(value);
  if (typeof data.has_more !== "boolean" || (data.has_more && typeof data.next_cursor !== "string"))
    throw new Error(t("logs.pageIncomplete"));
  return {
    items: list(data.items),
    next_cursor: typeof data.next_cursor === "string" ? data.next_cursor : null,
    has_more: data.has_more,
    degraded: data.degraded === true,
  };
}
const emptyFilters = {
  model: "",
  credential: "",
  profile: "",
  status: "",
  client_ip: "",
  api_key: "",
  type: "",
  search: "",
};
const outcomeLabels: Record<string, string> = {
  success: "logs.outcome.success",
  error: "logs.outcome.error",
  cancelled: "logs.outcome.cancelled",
  warning: "logs.outcome.warning",
};
function ServerToolSummary({ attempts }: { attempts: unknown }) {
  const t = useT();
  const summary = serverToolSummary(attempts);
  if (!summary) return null;
  return (
    <>
      <div className={s.toolSummary} role="group" aria-label={t("logs.tool.summary")}>
        {(
          [
            [t("logs.tool.searches"), metric(summary.search), false],
            [t("logs.tool.fetches"), metric(summary.fetch), false],
            [t("logs.tool.failed"), metric(summary.failed), summary.failed > 0],
            [t("logs.tool.time"), `${metric(summary.durationMs)} ms`, false],
          ] as const
        ).map(([label, value, failed]) => (
          <div className={`${s.kpiCard} ${failed ? s.toolFailed : ""}`} key={label}>
            <small>{label}</small>
            <strong>{value}</strong>
          </div>
        ))}
      </div>
      {summary.truncated && <p className={s.note}>{t("logs.tool.truncated")}</p>}
    </>
  );
}
export function Logs() {
  const t = useT();
  const [kind, setKind] = useState("request");
  const [draft, setDraft] = useState(emptyFilters);
  const [filters, setFilters] = useState(emptyFilters);
  const names = useAccountNames(kind === "credit");
  const [cursors, setCursors] = useState<string[]>([]);
  const [detail, setDetail] = useState<RecordValue | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const detailRequest = useRef<AbortController | null>(null);
  useEffect(() => () => detailRequest.current?.abort(), []);
  const [clear, setClear] = useState(false);
  const [keyNames, setKeyNames] = useState<Record<string, string>>({});
  useEffect(() => {
    let active = true;
    void api
      .get("/keys")
      .then((response) => {
        if (!active) return;
        const rows = list(object(response.data).items, "密钥列表");
        setKeyNames(
          Object.fromEntries(
            rows
              .filter((item) => typeof item.id === "string" && typeof item.name === "string")
              .map((item) => [item.id, item.name]),
          ),
        );
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);
  const params = new URLSearchParams({ kind, limit: "50" });
  Object.entries(filters).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  if (cursors.length) params.set("cursor", cursors.at(-1)!);
  const resource = useResource(`/logs?${params.toString()}`, normalize);
  const apiKeyLabel = (value: unknown) => {
    if (value === null || value === undefined || value === "") return t("logs.legacyApiKey");
    const id = text(value);
    if (id === "anonymous") return t("logs.anonymousApiKey");
    if (id === "admin-external") return t("logs.externalApiKey");
    return keyNames[id] ?? t("logs.deletedApiKey", { id });
  };
  const keyOptions = Array.from(
    new Set([
      ...Object.keys(keyNames),
      ...(resource.data?.items.flatMap((item) =>
        typeof item.api_key === "string" && item.api_key ? [item.api_key] : [],
      ) ?? []),
    ]),
  );
  const closeDetail = () => {
    detailRequest.current?.abort();
    detailRequest.current = null;
    setDetail(null);
    setDetailError(null);
    setDetailLoading(false);
  };
  const openDetail = (item: RecordValue) => {
    detailRequest.current?.abort();
    detailRequest.current = null;
    setDetail(item);
    setDetailError(null);
    setDetailLoading(false);
    if (kind !== "request") return;
    if (typeof item.id !== "string") {
      setDetailError(t("logs.detailNeedsId"));
      return;
    }
    const controller = new AbortController();
    detailRequest.current = controller;
    const active = () => detailRequest.current === controller && !controller.signal.aborted;
    setDetailLoading(true);
    void api
      .get<unknown>(`/logs/${encodeURIComponent(item.id)}`, { signal: controller.signal })
      .then((res) => {
        if (active()) setDetail(object(res.data));
      })
      .catch((err: unknown) => {
        if (active()) setDetailError(errorMessage(err));
      })
      .finally(() => {
        if (active()) {
          detailRequest.current = null;
          setDetailLoading(false);
        }
      });
  };
  return (
    <>
      <PageTitle
        title={t("logs.title")}
        actions={
          <>
            <button onClick={resource.reload}>
              <RefreshIcon spinning={resource.refreshing} />
              {t("logs.refresh")}
            </button>
            <button onClick={() => setClear(true)}>{t("logs.clear")}</button>
          </>
        }
      />
      <div className={s.tabs} role="tablist" aria-label={t("logs.logTypes")}>
        <SegmentThumb />
        {[
          ["request", t("logs.tab.request")],
          ["runtime", t("logs.tab.runtime")],
          ["admin", t("logs.kind.admin")],
          ["credit", t("logs.tab.credit")],
        ].map(([value, label]) => (
          <button
            key={value}
            role="tab"
            aria-selected={kind === value}
            className={kind === value ? s.selectedTab : ""}
            onClick={() => {
              closeDetail();
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
        title={
          kind === "request"
            ? t("logs.panel.request")
            : kind === "runtime"
              ? t("logs.tab.runtime")
              : kind === "credit"
                ? t("logs.panel.credit")
                : t("logs.tab.admin")
        }
        hint={t("logs.perPage")}
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
                aria-label={t("logs.filter.model")}
                placeholder={t("logs.placeholder.model")}
                value={draft.model}
                onChange={(e) => setDraft({ ...draft, model: e.target.value })}
              />
              <input
                aria-label={t("logs.filter.credential")}
                placeholder={t("logs.placeholder.credential")}
                value={draft.credential}
                onChange={(e) => setDraft({ ...draft, credential: e.target.value })}
              />
              <input
                aria-label={t("logs.filter.clientIp")}
                placeholder={t("logs.placeholder.clientIp")}
                value={draft.client_ip}
                onChange={(e) => setDraft({ ...draft, client_ip: e.target.value })}
              />
              <Select
                aria-label={t("logs.filter.apiKey")}
                value={draft.api_key}
                onChange={(value) => setDraft({ ...draft, api_key: value })}
              >
                <option value="">{t("logs.allApiKeys")}</option>
                {keyOptions.map((id) => (
                  <option key={id} value={id}>
                    {apiKeyLabel(id)}
                  </option>
                ))}
              </Select>
              <Select
                aria-label={t("logs.filter.profile")}
                value={draft.profile}
                onChange={(value) => setDraft({ ...draft, profile: value })}
              >
                <option value="">{t("logs.allProfiles")}</option>
                {profiles.map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </Select>
              <Select
                aria-label={t("logs.filter.status")}
                value={draft.status}
                onChange={(value) => setDraft({ ...draft, status: value })}
              >
                <option value="">{t("logs.allStatuses")}</option>
                <option value="success">{t("logs.outcome.success")}</option>
                <option value="error">{t("logs.outcome.error")}</option>
                <option value="cancelled">{t("logs.outcome.cancelled")}</option>
                <option value="429">{t("logs.http429")}</option>
              </Select>
            </>
          )}
          {kind === "credit" && (
            <CreditFilters
              credential={draft.credential}
              type={draft.type}
              names={names}
              onChange={(patch) => setDraft({ ...draft, ...patch })}
            />
          )}
          <SearchInput
            aria-label={t("logs.filter.search")}
            placeholder={t("logs.placeholder.search")}
            value={draft.search}
            onChange={(search) => setDraft({ ...draft, search })}
          />
          <button type="submit">{t("logs.apply")}</button>
          <button
            type="button"
            onClick={() => {
              setFilters(emptyFilters);
              setDraft(emptyFilters);
              setCursors([]);
            }}
          >
            {t("logs.reset")}
          </button>
        </form>
        <ResourceState {...resource} />
        {resource.data?.degraded && (
          <div className={s.warning} role="alert">
            {t("logs.degraded")}
          </div>
        )}
        {resource.data &&
          (resource.data.items.length ? (
            <div className={s.tableWrap}>
              <table>
                <thead>
                  <tr>
                    <th>{t("logs.col.time")}</th>
                    {kind === "credit" ? (
                      <>
                        <th>{t("logs.col.changeAccount")}</th>
                        <th>{t("logs.col.changeAmount")}</th>
                        <th>{t("logs.col.changeSource")}</th>
                      </>
                    ) : (
                      <>
                        <th>
                          {kind === "request" ? t("logs.col.modelProfile") : t("logs.col.action")}
                        </th>
                        <th>{t("logs.col.status")}</th>
                      </>
                    )}
                    {kind === "request" && (
                      <>
                        <th>{t("logs.col.apiKey")}</th>
                        <th>{t("logs.col.duration")}</th>
                        <th>Token / Credit</th>
                      </>
                    )}
                    <th>{t("logs.col.actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {resource.data.items.map((item, i) => {
                    const outcome =
                      kind === "request" ? item.outcome : object(item.details ?? {}).outcome;
                    const tools = kind === "request" ? serverToolSummary(item.attempts) : null;
                    const toolModel = kind === "request" ? webToolLabel(item.model) : undefined;
                    return (
                      <tr key={text(item.id) + i}>
                        <td>
                          {typeof item.started_at === "number"
                            ? new Date(item.started_at * 1000).toLocaleString("zh-CN")
                            : text(item.started_at)}
                          <small className={s.mono}>{text(item.id)}</small>
                        </td>
                        {kind === "credit" ? (
                          <CreditRow item={item} names={names} />
                        ) : (
                          <>
                            <td>
                              <strong
                                title={
                                  kind === "request"
                                    ? toolModel && text(item.model)
                                    : text(item.action)
                                }
                              >
                                {text(
                                  kind === "request"
                                    ? toolModel
                                      ? t(toolModel)
                                      : item.model
                                    : eventActionLabel(item.action)
                                      ? t(eventActionLabel(item.action)!)
                                      : item.action,
                                )}
                              </strong>
                              <small title={kind === "request" ? undefined : text(item.kind)}>
                                {text(
                                  kind === "request"
                                    ? item.profile
                                    : eventKindLabel(item.kind)
                                      ? t(eventKindLabel(item.kind)!)
                                      : item.kind,
                                )}
                                {kind === "request" &&
                                  (item.protocol === "images" || item.protocol === "videos") && (
                                    <span className={s.toolBadge}>
                                      <Badge tone="neutral">
                                        {t(
                                          item.protocol === "images"
                                            ? "val.protocol.images"
                                            : "val.protocol.videos",
                                        )}
                                      </Badge>
                                    </span>
                                  )}
                                {tools && (
                                  <span className={s.toolBadge}>
                                    <Badge tone={tools.failed ? "bad" : "neutral"}>
                                      {t("logs.tool.badge", {
                                        n: tools.truncated ? `${tools.total}+` : tools.total,
                                      })}
                                    </Badge>
                                  </span>
                                )}
                              </small>
                              {kind === "request" && item.client_ip ? (
                                <small className={s.mono}>{text(item.client_ip)}</small>
                              ) : null}
                            </td>
                            <td>
                              <Badge
                                tone={
                                  outcome === "success"
                                    ? "good"
                                    : outcome === "error"
                                      ? "bad"
                                      : outcome === "warning"
                                        ? "warn"
                                        : "neutral"
                                }
                              >
                                {(typeof outcome === "string"
                                  ? t(outcomeLabels[outcome])
                                  : undefined) ?? text(outcome ?? item.level ?? item.status)}
                              </Badge>
                              {item.status_code !== undefined && (
                                <small>HTTP {text(item.status_code)}</small>
                              )}
                            </td>
                          </>
                        )}
                        {kind === "request" && (
                          <>
                            <td>{apiKeyLabel(item.api_key)}</td>
                            <td>{metric(item.duration_ms)} ms</td>
                            <td>
                              {metric(item.total_tokens)}
                              <small>{metric(item.credit)} Credit</small>
                            </td>
                          </>
                        )}
                        <td>
                          <button disabled={detailLoading} onClick={() => openDetail(item)}>
                            {t("logs.viewDetail")}
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty title={t("logs.noMatch")}>{t("logs.adjustFilters")}</Empty>
          ))}
        <div className={s.pagination}>
          <span>第 {cursors.length + 1} 页</span>
          <div className={s.actions}>
            <button
              disabled={!cursors.length || resource.loading}
              onClick={() => setCursors((old) => old.slice(0, -1))}
            >
              {t("logs.prevPage")}
            </button>
            <button
              disabled={!resource.data?.has_more || resource.loading}
              onClick={() => {
                if (resource.data?.next_cursor)
                  setCursors((old) => [...old, resource.data!.next_cursor!]);
              }}
            >
              {t("logs.nextPage")}
            </button>
          </div>
        </div>
      </Panel>
      <DrawerPresence>
        {detail && (
          <Drawer title={t("logs.detailTitle")} onClose={closeDetail}>
            <ErrorNotice message={detailError} />
            {detailLoading ? (
              <p role="status">{t("logs.detailLoading")}</p>
            ) : (
              <>
                <Fields
                  data={Object.fromEntries(
                    Object.entries(detail).filter(([key]) => key !== "attempts"),
                  )}
                />
                {Array.isArray(detail.attempts) && (
                  <Panel title={t("logs.attempts")}>
                    <ServerToolSummary attempts={detail.attempts} />
                    {detail.attempts.length ? (
                      list(detail.attempts).map((attempt, i) => (
                        <div key={i}>
                          <h3>{t("logs.attemptN", { n: i + 1 })}</h3>
                          <Fields data={attempt} />
                        </div>
                      ))
                    ) : (
                      <p>{t("logs.noAttempts")}</p>
                    )}
                  </Panel>
                )}
              </>
            )}
          </Drawer>
        )}
      </DrawerPresence>
      <DrawerPresence>
        {clear && (
          <ClearLogs
            onClose={() => setClear(false)}
            onDone={() => {
              setCursors([]);
              resource.reload();
            }}
          />
        )}
      </DrawerPresence>
    </>
  );
}
