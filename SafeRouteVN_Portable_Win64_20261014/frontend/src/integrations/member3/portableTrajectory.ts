import type { PlanProfile } from "../../shared/types/dispatch";
import type { MapSegment } from "../../shared/components/mapScene";
import { vehicleRouteColor } from "../../shared/components/routeLegPresentation";
import { finite, line, list, record, string, stringList } from "../member2/validation";
import { drawableEdgeCoordinates } from "./edgeGeometry";

function exact(value: unknown): bigint {
  if (typeof value === "number" && Number.isSafeInteger(value)) return BigInt(value);
  if (typeof value === "string" && /^(0|-?[1-9]\d*)$/.test(value)) {
    const result = BigInt(value);
    if (result >= -(1n << 63n) && result < (1n << 63n)) return result;
  }
  throw new Error("Invalid exact action time");
}
function nonnegative(value: unknown, name: string): number {
  const result = finite(value, name);
  if (result < 0) throw new Error(`Negative ${name}`);
  return result;
}
function fields(value: Record<string, unknown>, required: string[], optional: string[] = []) {
  if (required.some(k => !(k in value)) || Object.keys(value).some(k => !required.includes(k) && !optional.includes(k))) throw new Error("Invalid trajectory fields");
}
function node(value: unknown) {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value <= 0) throw new Error("Invalid trajectory node");
}
function digest(value: unknown) {
  if (typeof value !== "string" || !/^[a-f0-9]{64}$/.test(value)) throw new Error("Invalid trajectory digest");
}
function identifier(value: unknown, path: string): string {
  const result = string(value, path);
  if (!/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$/.test(result)) throw new Error(`Invalid ${path} identity`);
  return result;
}
const actionFields: Record<string, string[]> = {
  EDGE: ["edge_id", "from_node", "to_node", "incoming_edge", "fraction_start", "fraction_start_exact", "fraction_end", "geometry", "feature_payload", "distance_m", "exposure", "overlay_sha256", "temporal_policy", "temporal_segments"],
  PICKUP: ["order_id", "node_id", "load_after_kg"], SERVICE: ["order_id", "node_id", "load_after_kg", "continuation"], WAIT: ["node_id", "order_id", "reason"]
};
/** Validates public portable forecast routes without creating physical execution state. */
export function projectPortableTrajectory(raw: unknown, binding: { jobId: string; profile?: PlanProfile; vehicleIds: readonly string[]; requiredServed: readonly string[]; allowedServed: readonly string[] }): { segments: MapSegment[]; profile: PlanProfile; served: string[] } {
  const trajectory = record(raw, "trajectory");
  fields(trajectory, ["job_id", "profile", "forecast", "domain_sha256", "vehicle_routes"]);
  const jobId = identifier(trajectory.job_id, "job_id");
  if (jobId !== binding.jobId || trajectory.forecast !== true || typeof trajectory.profile !== "string" || !["FASTEST", "BALANCED", "SAFER"].includes(trajectory.profile) || binding.profile !== undefined && trajectory.profile !== binding.profile ||
      typeof trajectory.domain_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(trajectory.domain_sha256)) throw new Error("Portable trajectory binding mismatch");
  const segments: MapSegment[] = [];
  const vehicleIds = new Set<string>(); const served: string[] = [];
  for (const rawRoute of list(trajectory.vehicle_routes, "vehicle_routes")) {
    const route = record(rawRoute, "route"), vehicleId = identifier(route.vehicle_id, "vehicle_id");
    fields(route, ["vehicle_id", "order_sequence", "actions", "start_us", "return_us", "start_node", "end_node"], ["route_column_id", "return_load_kg", "total_distance_m", "total_travel_time_s", "total_exposure", "total_cost_vnd", "total_soft_lateness_s"]);
    node(route.start_node); node(route.end_node);
    if (route.route_column_id !== undefined) identifier(route.route_column_id, "route_column_id");
    if (route.return_load_kg !== undefined && finite(route.return_load_kg, "return_load_kg") < -1e-9) throw new Error("Invalid return load");
    for (const key of ["total_distance_m", "total_travel_time_s", "total_exposure", "total_cost_vnd", "total_soft_lateness_s"]) {
      if (route[key] !== undefined) nonnegative(route[key], key);
    }
    if (!binding.vehicleIds.includes(vehicleId) || vehicleIds.has(vehicleId)) throw new Error("Portable route vehicle mismatch");
    vehicleIds.add(vehicleId);
    served.push(...stringList(route.order_sequence, "order_sequence").map(o => identifier(o, "order_sequence")));
    const actions = list(route.actions, "actions").map(a => record(a, "action"));
    let previous = exact(route.start_us); const end = exact(route.return_us);
    if (end < previous) throw new Error("Reversed route time");
    let leg = 1;
    for (const [index, action] of actions.entries()) {
      const start = exact(action.start_us), finish = exact(action.end_us);
      if (start < previous || finish < start || finish > end) throw new Error("Unordered portable action times");
      previous = finish;
      if (typeof action.kind !== "string" || !["EDGE", "PICKUP", "SERVICE", "WAIT"].includes(action.kind)) throw new Error("Unknown portable action");
      fields(action, ["kind", "start_us", "end_us"], actionFields[String(action.kind)]);
      if (action.kind === "PICKUP" || action.kind === "SERVICE") {
        identifier(action.order_id, "order_id"); node(action.node_id);
        if (finite(action.load_after_kg, "load_after_kg") < -1e-9 || action.kind === "PICKUP" && start !== finish || action.continuation !== undefined && typeof action.continuation !== "boolean") throw new Error("Invalid custody action");
      }
      if (action.kind === "SERVICE") leg++;
      if (action.kind === "WAIT") {
        if (action.node_id !== undefined) node(action.node_id);
        if (action.order_id !== undefined) identifier(action.order_id, "order_id");
        if (action.reason !== undefined) string(action.reason, "reason");
      }
      if (action.kind !== "EDGE") continue;
      const edgeId = identifier(action.edge_id, "edge_id");
      node(action.from_node); node(action.to_node);
      if (action.incoming_edge !== null) identifier(action.incoming_edge, "incoming_edge");
      const fractionStart = nonnegative(action.fraction_start, "fraction_start"), fractionEnd = nonnegative(action.fraction_end, "fraction_end");
      if (fractionStart > fractionEnd || fractionEnd > 1) throw new Error("Invalid EDGE fraction");
      const fractionStartExact = action.fraction_start_exact;
      if (fractionStartExact !== undefined) {
        if (typeof fractionStartExact !== "string" || !/^(0|[1-9]\d*)(\/[1-9]\d*)?$/.test(fractionStartExact)) throw new Error("Invalid exact EDGE fraction");
        const [n, d = 1n] = fractionStartExact.split("/").map(BigInt);
        if (n > d || d >= (1n << 63n)) throw new Error("Invalid exact EDGE progress");
      }
      record(action.feature_payload, "feature_payload");
      nonnegative(action.distance_m, "distance_m"); nonnegative(action.exposure, "exposure");
      if (action.overlay_sha256 !== undefined) digest(action.overlay_sha256);
      if (action.temporal_policy !== undefined) string(action.temporal_policy, "temporal_policy");
      if (action.temporal_segments !== undefined) {
        let prior = start;
        for (const raw of list(action.temporal_segments, "temporal_segments")) {
          const temporal = record(raw, "temporal segment"), begin = exact(temporal.start_us), end = exact(temporal.end_us);
          if (begin < prior || end < begin || end > finish || temporal.edge_id !== edgeId || typeof temporal.layer !== "string" || !["BASELINE", "WET", "BASELINE_AFTER_EXPIRY"].includes(temporal.layer)) throw new Error("Invalid temporal segment binding/time");
          prior = end;
        }
      }
      const nextService = actions.slice(index + 1).find(a => a.kind === "SERVICE");
      const returnToDepot = !nextService;
      const coordinates = line(action.geometry, "EDGE.geometry");
      const drawableStart = typeof fractionStartExact === "string" ? (() => { const [n, d = "1"] = fractionStartExact.split("/"); return Number(n) / Number(d); })() : fractionStart;
      segments.push({ id: `${jobId}:${vehicleId}:action:${index}`, vehicleId, edgeId, actionIndex: index,
        coordinates, drawableCoordinates: drawableEdgeCoordinates(coordinates, drawableStart, fractionEnd), fractionStart, fractionEnd,
        ...(typeof fractionStartExact === "string" ? { fractionStartExact } : {}), returnToDepot,
        completed: false, completionAvailable: false, geometrySource: "MEMBER2_SUPPLIED", legId: `${vehicleId}:leg:${leg}`, color: vehicleRouteColor(vehicleId) });
    }
    if (actions.length && previous !== end) throw new Error("Route return differs from final action");
  }
  const expected = binding.allowedServed;
  if (new Set(served).size !== served.length || binding.requiredServed.some(id => !served.includes(id)) || served.some(id => !expected.includes(id))) throw new Error("Portable route service coverage mismatch");
  return { segments, profile: trajectory.profile as PlanProfile, served };
}
