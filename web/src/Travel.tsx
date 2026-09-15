import { DataValue } from "./values";

export function TravelSummary({ trip }: { trip: Record<string, unknown> | null }) {
  if (!trip) return null;
  const remaining = trip.remaining_seconds;
  const minutes =
    trip.state === "traveling" &&
    trip.stale !== true &&
    typeof remaining === "number" &&
    Number.isFinite(remaining) &&
    remaining >= 0
      ? Math.ceil(remaining / 60)
      : null;
  const knownInteger = (value: unknown): value is number =>
    typeof value === "number" && Number.isSafeInteger(value);
  return (
    <>
      {typeof trip.location_name === "string" && trip.location_name && (
        <small>旅行地点：{trip.location_name}</small>
      )}
      {minutes !== null && (
        <small>
          查询时剩余：
          {minutes === 0
            ? "预计已到达，请查询核验"
            : minutes >= 60
              ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`
              : `${minutes} 分钟`}
        </small>
      )}
      {trip.claimed === true && trip.claimed_credit == null && (
        <small>领取已确认，积分数额未返回</small>
      )}
      {typeof trip.phase === "string" && (
        <small>
          阶段：
          <DataValue name="phase" value={trip.phase} />
          {typeof trip.error_kind === "string" && (
            <>
              {" · "}
              <DataValue name="error_kind" value={trip.error_kind} />
            </>
          )}
          {knownInteger(trip.http_status) && ` · HTTP ${trip.http_status}`}
          {knownInteger(trip.code) && ` · 业务码 ${trip.code}`}
        </small>
      )}
    </>
  );
}
