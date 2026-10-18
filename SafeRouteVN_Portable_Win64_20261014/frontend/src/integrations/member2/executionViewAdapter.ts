import type { GeoJsonFeatureCollection, Member2ExecutionVehicle, Member2ExecutionView } from "./types";
import { finite, line, list, point, record, string, stringList } from "./validation";

function assertSafeNumericTransport(value: unknown, path: string, seen = new WeakSet<object>()): void {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error(`Non-finite number in ${path}`);
    if (Number.isInteger(value) && !Number.isSafeInteger(value)) throw new Error(`Unsafe integer in ${path}; transport as a decimal string`);
    return;
  }
  if (value === null || typeof value !== "object") return;
  if (seen.has(value)) return;
  seen.add(value);
  for (const [key, child] of Object.entries(value)) assertSafeNumericTransport(child, `${path}.${key}`, seen);
}

function nullableMetrics(value: unknown, path: string): Record<string, number> | null {
  if (value === null) return null;
  return Object.fromEntries(Object.entries(record(value, path)).map(([key, metric]) => [key, finite(metric, `${path}.${key}`)]));
}

function vehicle(value: unknown, index: number): Member2ExecutionVehicle {
  const raw = record(value, `vehicles[${index}]`);
  const position = record(raw.position, `vehicles[${index}].position`);
  if (position.position_source !== "SIMULATED") throw new Error("Execution view position must be SIMULATED");
  return {
    vehicleId: string(raw.vehicle_id, "vehicle_id"),
    availability: string(raw.availability, "availability"),
    capacityKg: finite(raw.capacity_kg, "capacity_kg"),
    currentLoadKg: finite(raw.current_load_kg, "current_load_kg"),
    onboardOrderIds: stringList(raw.onboard_order_ids, "onboard_order_ids"),
    remainingRangeM: finite(raw.remaining_range_m, "remaining_range_m"),
    position: {
      coordinates: point(position.coordinates, "position.coordinates"),
      kind: string(position.kind, "position.kind"),
      source: "SIMULATED"
    },
    plannedSuffix: stringList(raw.planned_suffix, "planned_suffix"),
    activity: string(raw.activity, "activity")
  };
}

function forecastEdges(trajectory: Record<string, unknown> | null): GeoJsonFeatureCollection {
  const features: GeoJsonFeatureCollection["features"] = [];
  if (!trajectory) return { type: "FeatureCollection", features };
  for (const [routeIndex, routeValue] of list(trajectory.vehicle_routes, "accepted_trajectory.vehicle_routes").entries()) {
    const route = record(routeValue, `vehicle_routes[${routeIndex}]`);
    const vehicleId = string(route.vehicle_id, "vehicle_id");
    for (const [actionIndex, actionValue] of list(route.actions, `vehicle_routes[${routeIndex}].actions`).entries()) {
      const action = record(actionValue, `actions[${actionIndex}]`);
      if (action.kind !== "EDGE") continue;
      features.push({
        type: "Feature",
        geometry: { type: "LineString", coordinates: line(action.geometry, `actions[${actionIndex}].geometry`) },
        properties: {
          vehicleId,
          edgeId: string(action.edge_id, "edge_id"),
          scope: "FORECAST_SOURCE_EDGE_GEOMETRY",
          simulation: true,
          fractionStart: finite(action.fraction_start, "fraction_start")
        }
      });
    }
  }
  return { type: "FeatureCollection", features };
}

export function parseExecutionView(value: unknown): Member2ExecutionView {
  const raw = record(value, "execution view");
  assertSafeNumericTransport(raw, "execution view");
  if (raw.schema_version !== "task02-m2-execution-view/2") throw new Error("Unsupported execution view schema_version");
  if (raw.execution_mode !== "SIMULATED_REPLAY" || raw.real_world_observation !== false) {
    throw new Error("Execution view must be simulated replay, not real-world observation");
  }
  if (raw.metric_scope !== "OBSERVED_PREFIX_ONLY") throw new Error("Unsupported metric scope");
  const currentTime = string(raw.current_time, "current_time");
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?\+07:00$/.test(currentTime) || Number.isNaN(Date.parse(currentTime))) {
    throw new Error("Invalid current_time; expected +07:00 demo time");
  }
  const trajectory = raw.accepted_trajectory === null ? null : record(raw.accepted_trajectory, "accepted_trajectory");
  const vehicles = list(raw.vehicles, "vehicles").map(vehicle);
  const unserved = list(raw.unserved, "unserved").map((value, index) => {
    const item = record(value, `unserved[${index}]`);
    return {
      orderId: string(item.order_id, "order_id"),
      reason: string(item.reason, "reason"),
      ownerVehicleId: item.owner_vehicle_id == null ? null : string(item.owner_vehicle_id, "owner_vehicle_id")
    };
  });
  const orderIds = stringList(raw.order_ids, "order_ids");
  const deliveredPrefix = stringList(raw.delivered_prefix, "delivered_prefix");
  const plannedServedSuffix = stringList(raw.planned_served_suffix, "planned_served_suffix");
  const accounted = [...deliveredPrefix, ...plannedServedSuffix, ...unserved.map((item) => item.orderId)];
  if (new Set(accounted).size !== accounted.length || accounted.length !== orderIds.length ||
      accounted.some((id) => !orderIds.includes(id))) throw new Error("Execution order partition is inconsistent");
  for (const item of unserved) {
    if (item.reason === "CUSTODY_BLOCKED" && item.ownerVehicleId &&
        !vehicles.find((candidate) => candidate.vehicleId === item.ownerVehicleId)?.onboardOrderIds.includes(item.orderId)) {
      throw new Error(`Custody owner missing onboard order ${item.orderId}`);
    }
  }
  return {
    schemaVersion: "task02-m2-execution-view/2",
    simulatedReplay: true,
    currentTime,
    basis: { ...record(raw.basis, "basis") },
    orderIds,
    deliveredPrefix,
    plannedServedSuffix,
    unserved,
    observedMetrics: nullableMetrics(raw.observed_metrics, "observed_metrics"),
    plannedSuffixMetrics: nullableMetrics(raw.planned_suffix_metrics, "planned_suffix_metrics"),
    projectedWholeMetrics: nullableMetrics(raw.projected_whole_metrics, "projected_whole_metrics"),
    vehicles,
    activeJobId: raw.active_job_id == null ? null : string(raw.active_job_id, "active_job_id"),
    pendingEventIds: stringList(raw.pending_event_ids, "pending_event_ids"),
    forecastEdges: forecastEdges(trajectory),
    acceptedTrajectory: trajectory
  };
}
