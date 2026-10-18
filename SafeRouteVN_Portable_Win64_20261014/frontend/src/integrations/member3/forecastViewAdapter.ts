import type { PlanProfile } from "../../shared/types/dispatch";
import type { ProposedAlternative } from "../../shared/types/dispatch";
import { list, record, string } from "../member2/validation";
import { Member3Error } from "./errors";
import { parseJobView } from "./jobViewAdapter";
import { parseBasis, sameBasis } from "./revision";
import { projectPortableTrajectory } from "./portableTrajectory";
import { adaptMetrics, forecastUnits } from "./metricsAdapter";
import type { M3JobForecast, M3Basis } from "./types";

const keys = ["schema_version", "session_id", "job_id", "profile", "input_basis", "build_sha256", "job_view", "trajectory", "metrics", "units", "metric_scope", "execution_mode", "real_world_observation"];
const units = { distance: "m", duration: "s", action_time: "us", cost: "VND", mass: "kg", geometry_crs: "WGS84", geometry_order: "longitude_latitude", exposure: "PROXY" };
function check(value: unknown): asserts value { if (!value) throw new Member3Error("INVALID_RESPONSE", "Invalid public forecast contract or binding."); }
export function parseJobForecast(raw: unknown): M3JobForecast {
  const value = record(raw, "forecast") as unknown as M3JobForecast;
  check(Object.keys(value).length === keys.length && keys.every(k => k in value));
  check(value.schema_version === "task02-m2-job-forecast/1" && typeof value.profile === "string" && ["FASTEST", "BALANCED", "SAFER"].includes(value.profile));
  const basis = parseBasis(value.input_basis), job = parseJobView(value.job_view);
  check(Object.keys(value.input_basis).length === 9 && Object.keys(job.input_basis).length === 9 && value.session_id === basis.session_id && value.job_id === job.job_id && sameBasis(basis, job.input_basis) && value.build_sha256 === basis.build_sha256);
  check(value.execution_mode === "SIMULATED_REPLAY" && value.real_world_observation === false && value.metric_scope === "FORECAST_ONLY");
  const suppliedUnits = record(value.units, "forecast.units");
  check(Object.keys(suppliedUnits).length === Object.keys(units).length && Object.entries(units).every(([k, v]) => suppliedUnits[k] === v));
  if (!job.plan_available) check(value.trajectory === null && value.metrics === null);
  else {
    const trajectory = record(value.trajectory, "forecast.trajectory");
    check(trajectory.job_id === value.job_id && trajectory.profile === value.profile);
    const metrics = record(value.metrics, "forecast.metrics");
    check(Object.values(metrics).every(v => typeof v === "number" && Number.isFinite(v) && v >= 0));
    projectPortableTrajectory(trajectory, { jobId: value.job_id, profile: value.profile,
      vehicleIds: list(trajectory.vehicle_routes, "vehicle_routes").map(route => string(record(route, "route").vehicle_id, "vehicle_id")),
      requiredServed: [], allowedServed: job.served_orders });
  }
  return value;
}

/** Historical data is readable; only a fully current certified witness becomes a proposal. */
export function adaptJobForecast(view: M3JobForecast, binding: { basis: M3Basis; jobId: string; profile: PlanProfile; comparisonId: string; vehicleIds: readonly string[]; deliveredPrefix: readonly string[] }): ProposedAlternative | null {
  parseJobForecast(view);
  check(view.job_id === binding.jobId && view.profile === binding.profile && view.session_id === binding.basis.session_id);
  if (!sameBasis(view.input_basis, binding.basis) || view.trajectory === null) return null;
  const job = view.job_view;
  const { segments } = projectPortableTrajectory(view.trajectory, { jobId: view.job_id, profile: view.profile, vehicleIds: binding.vehicleIds,
    requiredServed: job.served_orders.filter(id => !binding.deliveredPrefix.includes(id)), allowedServed: job.served_orders });
  return { id: view.job_id, generatedForSessionId: view.session_id, generatedForStateVersion: 0,
    origin: { kind: "MEMBER3", sessionId: view.session_id, comparisonId: binding.comparisonId, jobId: view.job_id, profile: view.profile, inputBasis: { ...view.input_basis } },
    nativeForecast: { source: "MEMBER3_HTTP", trajectory: view.trajectory, segments, metrics: adaptMetrics(view.metrics, "FORECAST_ONLY", forecastUnits), jobView: job } };
}
