import type { PlanProfile } from "../../shared/types/dispatch";
import { record } from "../member2/validation";
import { isAcceptableJob, sameBasis } from "./revision";
import type { M3Basis, M3ComparisonView, M3ExecutionView } from "./types";

export type MetricScope = "FORECAST_ONLY" | "OBSERVED_PREFIX_ONLY" | "PLANNED_SUFFIX" | "PROJECTED_WHOLE";
export interface ScopedMetrics {
  scope: MetricScope;
  values: Record<string, number | null>;
  units: Record<string, string>;
  /** Canonical microseconds are retained without rounding their wire identity. */
  exactValues?: Record<string, string>;
}
// Units declared by M2 runtime trajectory/execution contracts, not inferred from labels.
export const forecastUnits = { total_distance_m: "m", total_travel_time_s: "s", total_exposure: "proxy", total_cost_vnd: "VND", total_soft_lateness_s: "s" };
export const observedUnits = { distance_m: "m", relative_exposure_proxy: "proxy", cost_vnd: "VND", travel_time_us: "us", waiting_time_us: "us", service_time_us: "us" };

export function adaptMetrics(raw: unknown, scope: MetricScope, units: Record<string, string>): ScopedMetrics {
  const input = raw === null ? {} : record(raw, "metrics");
  const values: ScopedMetrics["values"] = {};
  for (const key of new Set([...Object.keys(input), ...Object.keys(units)])) {
    const value = input[key];
    if (value != null && (typeof value !== "number" || !Number.isFinite(value) || value < 0)) throw new Error(`Invalid metric ${key}`);
    values[key] = value == null ? null : value as number;
  }
  return { scope, values, units: { ...units } };
}
export function comparisonMetrics(comparison: M3ComparisonView | undefined, profile: PlanProfile, current: M3Basis): ScopedMetrics | null {
  if (!comparison || !sameBasis(comparison.input_basis, current)) return null;
  const child = comparison.jobs.find(row => row.profile === profile);
  const result = comparison.outcome?.comparison?.jobs.find(row => row.profile === profile && row.job_id === child?.job_id);
  if (!child?.view || !isAcceptableJob(child.view, current) || !result || !sameBasis(result.basis, current)) return null;
  return adaptMetrics(result.metrics, "FORECAST_ONLY", forecastUnits);
}
export function executionMetrics(view: M3ExecutionView) {
  const raw = view.observed_metrics;
  const normalized: Record<string, unknown> = { ...(raw ?? {}) };
  const exactValues: Record<string, string> = {};
  for (const [key, value] of Object.entries(normalized)) {
    if (key.endsWith("_us") && typeof value === "string" && /^(0|[1-9]\d*)$/.test(value) && BigInt(value) < (1n << 63n)) {
      exactValues[key] = value;
      normalized[key] = Number(value); // display only; exact source remains above
    }
  }
  return {
    observed: { ...adaptMetrics(normalized, "OBSERVED_PREFIX_ONLY", observedUnits), exactValues },
    planned: adaptMetrics(view.planned_suffix_metrics, "PLANNED_SUFFIX", forecastUnits),
    projected: adaptMetrics(view.projected_whole_metrics, "PROJECTED_WHOLE", forecastUnits)
  };
}
export function formatMetric(metrics: ScopedMetrics, key: string, targetUnit = metrics.units[key]): string {
  const value = metrics.values[key];
  if (value == null || !metrics.units[key]) return "—";
  const source = metrics.units[key];
  const factor = source === targetUnit ? 1 : source === "m" && targetUnit === "km" ? 1 / 1000 :
    source === "s" && targetUnit === "min" ? 1 / 60 : source === "us" && targetUnit === "min" ? 1 / 60000000 : null;
  if (factor === null) throw new Error(`Unsupported metric conversion ${source} to ${targetUnit}`);
  return `${(value * factor).toLocaleString("en-US", { maximumFractionDigits: 3 })} ${targetUnit}`;
}
