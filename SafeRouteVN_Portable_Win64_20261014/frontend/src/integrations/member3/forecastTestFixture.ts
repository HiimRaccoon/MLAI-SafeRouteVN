// Test-only public forecast, including exact offsets and a directed partial EDGE.
import { acceptedView } from "./acceptedTestFixture";
import { basis, job } from "./testFixtures";

export function forecastView() {
  const trajectory = acceptedView().accepted_trajectory!;
  return {
    schema_version: "task02-m2-job-forecast/1", session_id: basis.session_id, job_id: "job-return", profile: "SAFER",
    input_basis: { ...basis }, build_sha256: basis.build_sha256,
    job_view: { ...job(), job_id: "job-return", internal_status: "RETURN_ONLY", business_status: "UNSUPPORTED", served_orders: [] as string[], unserved_orders: [{ order_id: "O1", reason: "CUSTODY_BLOCKED" }] },
    trajectory, metrics: { total_distance_m: 50, total_travel_time_s: 2, total_exposure: 2, total_cost_vnd: 100 },
    units: { distance: "m", duration: "s", action_time: "us", cost: "VND", mass: "kg", geometry_crs: "WGS84", geometry_order: "longitude_latitude", exposure: "PROXY" },
    metric_scope: "FORECAST_ONLY", execution_mode: "SIMULATED_REPLAY", real_world_observation: false
  };
}
