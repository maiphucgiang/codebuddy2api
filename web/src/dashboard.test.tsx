import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vite-plus/test";
import { Trend } from "./pages/Dashboard";

describe("truthful sparse request trends", () => {
  it("renders a single day as one centered point without an invented decline", () => {
    render(<Trend rows={[{ date: "2026-09-12", requests: 2, success: 1, error: 1 }]} />);
    const chart = screen.getByRole("img", { name: "按日请求量趋势" });
    expect(chart.querySelectorAll("circle")).toHaveLength(1);
    expect(chart.querySelector("circle")?.getAttribute("cx")).toBe("390");
    expect(chart.querySelector("polygon")).toBeNull();
    expect(chart.querySelector("polyline")).toBeNull();
    expect(chart.querySelectorAll("text")).toHaveLength(1);
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
