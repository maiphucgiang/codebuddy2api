import { number } from "./api";

export type ServerToolSummary = {
  total: number;
  search: number;
  fetch: number;
  failed: number;
  durationMs: number;
  // The backend caps attempts, so a truncation marker means the counts are a lower bound.
  truncated: boolean;
};

// Aggregates the gateway-executed web tool calls recorded in a request's attempts.
// Returns null when there are none, so callers can render nothing.
export function serverToolSummary(attempts: unknown): ServerToolSummary | null {
  if (!Array.isArray(attempts)) return null;
  const summary: ServerToolSummary = {
    total: 0,
    search: 0,
    fetch: 0,
    failed: 0,
    durationMs: 0,
    truncated: false,
  };
  for (const attempt of attempts) {
    if (typeof attempt !== "object" || attempt === null) continue;
    const entry = attempt as Record<string, unknown>;
    if (entry.stage === "attempts_truncated") summary.truncated = true;
    if (entry.stage !== "server_tool") continue;
    summary.total += 1;
    if (entry.model === "web-search") summary.search += 1;
    if (entry.model === "web-fetch") summary.fetch += 1;
    if (entry.outcome === "error") summary.failed += 1;
    summary.durationMs += number(entry.duration_ms) ?? 0;
  }
  return summary.total ? summary : null;
}
