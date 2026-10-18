import type { ExecutionState, PlanState } from "../../shared/types/dispatch";
import { parseBasis } from "./revision";
import type { M3ExecutionView } from "./types";
import { projectPortableTrajectory } from "./portableTrajectory";

function epochUs(time: string): bigint {
  const match = /^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,6}))?\+07:00$/.exec(time);
  if (!match || !Number.isFinite(Date.parse(time))) throw new Error("Invalid execution time");
  return BigInt(Date.parse(`${match[1]}+07:00`)) * 1000n + BigInt((match[2] ?? "").padEnd(6, "0"));
}

/** Projects only accepted public EDGE geometry. Stops and observation timestamps are never invented. */
export function adaptAcceptedExecution(view: M3ExecutionView): { planState: PlanState; executionState: ExecutionState } {
  const basis = { ...parseBasis(view.basis) };
  const planState: PlanState = { acceptedExecution: null, acceptedPlans: [], activeAcceptedPlanId: null, proposedAlternatives: [], selectedAlternativeId: null, operationalPlanAssessment: null };
  const executionState: ExecutionState = { activePlanId: null, progressByPlanId: {} };
  if (view.schema_version !== "task02-m2-execution-view/2" || view.execution_mode !== "SIMULATED_REPLAY" || view.real_world_observation !== false) throw new Error("Invalid execution contract");
  if (view.accepted_trajectory === null) {
    if (view.active_job_id !== null) throw new Error("Active job missing accepted trajectory");
    return { planState, executionState };
  }
  const now = epochUs(view.current_time);
  for (const vehicle of view.vehicles) {
    if (vehicle.position_timestamp !== undefined && epochUs(vehicle.position_timestamp) > now) throw new Error("Future observation time");
  }
  const { segments, profile } = projectPortableTrajectory(view.accepted_trajectory, { jobId: view.active_job_id!, vehicleIds: view.vehicles.map(v => v.vehicle_id), requiredServed: view.planned_served_suffix, allowedServed: [...view.delivered_prefix, ...view.planned_served_suffix] });
  const jobId = view.active_job_id!;
  const routes = view.accepted_trajectory.vehicle_routes as Array<{ vehicle_id: string; order_sequence: string[] }>;
  const vehicleOrderIds = Object.fromEntries(routes.map(route => [route.vehicle_id, [...new Set([...route.order_sequence, ...view.vehicles.find(v => v.vehicle_id === route.vehicle_id)!.onboard_order_ids])]]));
  planState.acceptedExecution = { source: "MEMBER3_HTTP", jobId, profile, basis, segments, vehicleOrderIds,
    unserved: view.unserved.map(item => ({ orderId: item.order_id, reason: item.reason })) };
  executionState.activePlanId = jobId;
  return { planState, executionState };
}
