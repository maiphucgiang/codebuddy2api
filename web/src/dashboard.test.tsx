import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { api } from "./api";
import { Dashboard, Trend } from "./pages/Dashboard";
afterEach(() => vi.restoreAllMocks());

describe("truthful sparse request trends", () => {
  it("renders a single day as one centered point without an invented decline", () => {
    render(<Trend rows={[{ date: "2026-09-12", requests: 2, success: 1, error: 1 }]} />);
    const chart = screen.getByRole("img", { name: "按日请求量趋势" });
    expect(chart.querySelectorAll("circle")).toHaveLength(1);
    expect(chart.querySelector("circle")?.getAttribute("cx")).toBe("390");
    expect(chart.querySelector("polygon")).toBeNull();
    expect(chart.querySelector("polyline")).toBeNull();
    expect(screen.getByText("2026-09-12", { selector: "span" })).toBeTruthy();
  });

  it("draws a line and area only between multiple measured dates", () => {
    render(
      <Trend
        rows={[
          { date: "2026-09-11", requests: 1 },
          { date: "2026-09-12", requests: 2 },
        ]}
      />,
    );
    const chart = screen.getByRole("img", { name: "按日请求量趋势" });
    expect(chart.querySelectorAll("circle")).toHaveLength(2);
    expect(chart.querySelectorAll("polyline")).toHaveLength(1);
    expect(chart.querySelectorAll("polygon")).toHaveLength(1);
  });
});

describe("dashboard granularity", () => {
  it("requests hourly buckets for one day and allows an explicit daily view", async () => {
    const get = vi.spyOn(api, "get").mockImplementation(async (path) => {
      const query = new URL(path, "https://isolated.invalid").searchParams;
      const grain =
        query.get("granularity") === "hour" ||
        (query.get("granularity") === "auto" && query.get("days") === "1")
          ? "hour"
          : "day";
      return {
        data: {
          summary: { requests: 2, success_rate: 1, total_tokens: null, credit: null },
          series: Array.from({ length: grain === "hour" ? 24 : 1 }, (_, i) => ({
            bucket: 1789344000 + i * 3600,
            date: grain === "hour" ? `2026-09-14 ${i}:00` : "2026-09-14",
            requests: grain === "day" ? 2 : [2, 9].includes(i) ? 1 : 0,
            success: 0,
            error: 0,
          })),
          models: [],
          profiles: [],
          health: { credentials: [] },
          storage: {},
          range: { granularity: grain },
        },
      };
    });
    render(<Dashboard />);
    await screen.findByRole("img", { name: "按日请求量趋势" });
    fireEvent.change(screen.getByLabelText("统计时间范围"), { target: { value: "1" } });
    const hourly = await screen.findByRole("img", { name: "按小时请求量趋势" });
    expect(hourly.querySelectorAll("circle")).toHaveLength(24);
    expect(get).toHaveBeenCalledWith("/dashboard?days=1&granularity=auto", expect.anything());
    fireEvent.change(screen.getByLabelText("统计粒度"), { target: { value: "day" } });
    await waitFor(() =>
      expect(
        screen.getByRole("img", { name: "按日请求量趋势" }).querySelectorAll("circle"),
      ).toHaveLength(1),
    );
    expect(get).toHaveBeenCalledWith("/dashboard?days=1&granularity=day", expect.anything());
  });
  it("does not interpolate missing hourly history", () => {
    render(
      <Trend
        granularity="hour"
        partial
        rows={[
          { bucket: 0, date: "00:00", requests: 2 },
          { bucket: 36000, date: "10:00", requests: 1 },
        ]}
      />,
    );
    const chart = screen.getByRole("img", { name: "按小时请求量趋势" });
    expect(chart.querySelectorAll("circle")).toHaveLength(2);
    expect(chart.querySelector("polyline")).toBeNull();
  });
});
