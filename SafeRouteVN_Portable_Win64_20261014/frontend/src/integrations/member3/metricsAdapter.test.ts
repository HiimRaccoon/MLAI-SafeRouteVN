import { expect, it } from "vitest";
import { adaptMetrics, comparisonMetrics, executionMetrics, formatMetric } from "./metricsAdapter";
import { comparison } from "./testFixtures";
import type { M3ComparisonView, M3ExecutionView } from "./types";
import { acceptedView } from "./acceptedTestFixture";

it("does_not_conflate_observed_and_forecast", () => {
  const view: M3ExecutionView = { ...acceptedView(), observed_metrics: null, planned_suffix_metrics: { total_distance_m: 2000 }, projected_whole_metrics: { total_distance_m: 3000 } };
  const scopes = executionMetrics(view);
  expect(scopes.observed.scope).toBe("OBSERVED_PREFIX_ONLY");
  expect(scopes.observed.values.distance_m).toBeNull();
  expect(scopes.planned.values.total_distance_m).toBe(2000);
  expect(scopes.projected.values.total_distance_m).toBe(3000);
  expect(formatMetric(scopes.planned, "total_distance_m", "km")).toBe("2 km");
});
it("retains exact execution microseconds and rejects strings in other units", () => {
  const view = acceptedView(); view.observed_metrics = { travel_time_us: "90000000" };
  const observed = executionMetrics(view).observed;
  expect(observed.exactValues?.travel_time_us).toBe("90000000");
  expect(formatMetric(observed, "travel_time_us", "min")).toBe("1.5 min");
  view.observed_metrics = { cost_vnd: "100" };
  expect(() => executionMetrics(view)).toThrow();
});
it("preserves_proxy_units_and_missing_values", () => {
  const metrics = adaptMetrics({ exposure: 234, fuel: null, duration: 90 }, "FORECAST_ONLY", { exposure: "proxy", fuel: "VND", duration: "s", onTime: "%" });
  expect(formatMetric(metrics, "exposure")).toBe("234 proxy");
  expect(formatMetric(metrics, "duration", "min")).toBe("1.5 min");
  expect(formatMetric(metrics, "fuel")).toBe("—");
  expect(formatMetric(metrics, "onTime")).toBe("—");
  expect(() => formatMetric(metrics, "exposure", "km")).toThrow();
  for (const exposure of ["234", Infinity, NaN, -1]) expect(() => adaptMetrics({ exposure }, "FORECAST_ONLY", { exposure: "proxy" })).toThrow();
});
it("binds forecast KPI to the full current comparison basis and certified child", () => {
  const c = comparison() as M3ComparisonView;
  c.outcome!.comparison!.jobs[0].metrics = { total_distance_m: 2500, total_travel_time_s: 90, total_exposure: 234, total_cost_vnd: 4000 };
  expect(comparisonMetrics(c, "FASTEST", c.input_basis)?.values.total_cost_vnd).toBe(4000);
  expect(comparisonMetrics(c, "FASTEST", { ...c.input_basis, generation: "1" })).toBeNull();
  c.jobs[0].view!.plan_available = false;
  expect(comparisonMetrics(c, "FASTEST", c.input_basis)).toBeNull();
});
