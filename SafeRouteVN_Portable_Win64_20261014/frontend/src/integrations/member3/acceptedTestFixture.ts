// Test-only public contract example.
import { basis } from "./testFixtures";
import type { M3ExecutionView } from "./types";
export function acceptedView(): M3ExecutionView {
  return { schema_version: "task02-m2-execution-view/2", basis, current_time: "2026-09-27T21:00:00+07:00", execution_mode: "SIMULATED_REPLAY", real_world_observation: false,
    metric_scope: "OBSERVED_PREFIX_ONLY", vehicles: [{ vehicle_id: "V1", availability: "AVAILABLE", capacity_kg: 20, current_load_kg: 3, onboard_order_ids: ["O1"], remaining_range_m: 1000, position: { kind: "AT_NODE", coordinates: [106.7, 10.8] } }], order_ids: ["O1"], delivered_prefix: [], planned_served_suffix: [], unserved: [{ order_id: "O1", reason: "CUSTODY_BLOCKED", owner_vehicle_id: "V1" }], pending_event_ids: [], active_job_id: "job-return",
    observed_metrics: null, planned_suffix_metrics: null, projected_whole_metrics: null,
    accepted_trajectory: { job_id: "job-return", profile: "SAFER", forecast: true, domain_sha256: "e".repeat(64), vehicle_routes: [{ vehicle_id: "V1", order_sequence: [], start_us: "0", return_us: "2000000", start_node: 1, end_node: 2,
      actions: [{ kind: "EDGE", start_us: "0", end_us: "2000000", edge_id: "edge-return", from_node: 1, to_node: 2, incoming_edge: null, fraction_start: 0.25, fraction_start_exact: "1/4", fraction_end: 1,
        geometry: [[106.7, 10.8], [106.701, 10.802], [106.702, 10.803]], feature_payload: {}, distance_m: 50, exposure: 2 }] }] }
  };
}
