import { expect, it } from "vitest";
import { adaptAcceptedExecution } from "./executionViewAdapter";
import { basis } from "./testFixtures";
import { acceptedView } from "./acceptedTestFixture";
import type { M3ExecutionView } from "./types";
import { createMapScene } from "../../shared/components/mapScene";
import { createAdminMapPresentation } from "../../admin/adminMapPresentation";
import type { DispatchSnapshot } from "../../shared/types/dispatch";

it("preserves_all_edge_points_direction_and_fraction", () => {
  const view = acceptedView(); const original = structuredClone(view);
  const result = adaptAcceptedExecution(view);
  const route = result.planState.acceptedExecution!.segments[0];
  expect(route.coordinates).toEqual([[106.7, 10.8], [106.701, 10.802], [106.702, 10.803]]);
  expect(route.edgeId).toBe("edge-return"); expect(route.actionIndex).toBe(0);
  expect(route.fractionStart).toBe(0.25); expect(route.fractionStartExact).toBe("1/4");
  expect(route.returnToDepot).toBe(true); expect(route.completed).toBe(false);
  expect(view).toEqual(original);
});
it("rejects invalid EDGE identity, custody action and ordering", () => {
  for (const patch of [{ from_node: -1 }, { incoming_edge: 42 }, { end_us: "-1" }, { fraction_start: 0.9, fraction_end: 0.2 }]) {
    const view = acceptedView(); const routes = view.accepted_trajectory!.vehicle_routes as Array<{actions: Array<Record<string, unknown>>}>;
    Object.assign(routes[0].actions[0], patch);
    expect(() => adaptAcceptedExecution(view)).toThrow();
  }
});
it("does not mark actions after the last observation as completed", () => {
  const view = acceptedView(); view.vehicles[0].position_timestamp = "1970-01-01T07:00:01+07:00";
  expect(adaptAcceptedExecution(view).planState.acceptedExecution!.segments[0].completed).toBe(false);
});
it("keeps_proposal_and_accepted_sources_separate", () => {
  const view = acceptedView(); view.accepted_trajectory = null; view.active_job_id = null;
  const result = adaptAcceptedExecution(view);
  expect(result.planState.acceptedExecution).toBeNull();
  expect(result.planState.proposedAlternatives).toEqual([]);
  expect(view.vehicles[0].position_timestamp).toBeUndefined();
  expect(view.vehicles[0].activity).toBeUndefined();
});
it("rejects binding mismatch and malformed source geometry", () => {
  const view = acceptedView(); view.active_job_id = "another-job";
  expect(() => adaptAcceptedExecution(view)).toThrow();
  view.active_job_id = "job-return";
  const route = (view.accepted_trajectory!.vehicle_routes as Array<{actions: Array<Record<string, unknown>>}>)[0];
  route.actions[0].geometry = [[200, 10]];
  expect(() => adaptAcceptedExecution(view)).toThrow();
});
it("retains return-only continuation for Admin and Driver without assuming a decision epoch", () => {
  const view = acceptedView();
  // Public route offsets have no public decision_epoch. An ISO timestamp cannot establish completion.
  view.vehicles[0].position_timestamp = view.current_time;
  const mapped = adaptAcceptedExecution(view);
  expect(mapped.planState.acceptedExecution!.segments[0].completed).toBe(false);
  expect(mapped.planState.acceptedExecution!.segments[0].completionAvailable).toBe(false);
  const snapshot = { ...mapped, backend: { basis }, decisionState: { vehicles: [], orders: [], locations: [], context: { rain: null } } } as unknown as DispatchSnapshot;
  const driver = createMapScene(snapshot, undefined, "V1");
  expect(driver.accepted).toHaveLength(1); expect(driver.proposed).toEqual([]);
  expect(createMapScene(snapshot, undefined, "V2").accepted).toEqual([]);
  const admin = createAdminMapPresentation(snapshot, undefined, ["V1"]);
  expect(admin.source).toBe("ACCEPTED"); expect(admin.scene.accepted).toEqual([]);
  expect(mapped.planState.acceptedExecution!.segments[0].coordinates).toEqual(driver.accepted[0].coordinates);
  expect(createAdminMapPresentation(snapshot, undefined, []).scene.accepted).toEqual([]);
});
it("allows prior delivered orders to be absent from a reoptimized return trajectory", () => {
  const view = acceptedView(); view.order_ids.push("O0"); view.delivered_prefix.push("O0");
  expect(adaptAcceptedExecution(view).planState.acceptedExecution!.unserved).toEqual([{ orderId: "O1", reason: "CUSTODY_BLOCKED" }]);
});
it("keeps full source geometry but draws only the supplied directed fraction", () => {
  const view = acceptedView();
  const action = (view.accepted_trajectory!.vehicle_routes as Array<{actions: Array<Record<string, unknown>>}>)[0].actions[0];
  action.geometry = [[0, 0], [1, 0], [2, 0]]; action.fraction_start = 0.25; action.fraction_end = 0.75;
  const segment = adaptAcceptedExecution(view).planState.acceptedExecution!.segments[0];
  expect(segment.coordinates).toEqual([[0, 0], [1, 0], [2, 0]]);
  expect(segment.drawableCoordinates).toEqual([[0.5, 0], [1, 0], [1.5, 0]]);
});
it("validates optional temporal, WAIT and route fields before rendering", () => {
  for (const patch of [{ overlay_sha256: "invalid" }, { temporal_policy: "" }, { temporal_segments: [{ start_us: "20", end_us: "10", edge_id: "wrong-edge", layer: "UNKNOWN" }] }, { temporal_segments: [{ start_us: "0", end_us: "2000000", edge_id: "edge-return", layer: ["BASELINE"] }] }]) {
    const view = acceptedView(); const route = (view.accepted_trajectory!.vehicle_routes as Array<{actions: Array<Record<string, unknown>>}>)[0];
    Object.assign(route.actions[0], patch); expect(() => adaptAcceptedExecution(view)).toThrow();
  }
  for (const patch of [{ total_distance_m: -1 }, { total_cost_vnd: "100" }, { return_load_kg: NaN }]) {
    const view = acceptedView(); Object.assign((view.accepted_trajectory!.vehicle_routes as unknown[])[0] as object, patch);
    expect(() => adaptAcceptedExecution(view)).toThrow();
  }
  const view = acceptedView(); const route = (view.accepted_trajectory!.vehicle_routes as Array<{actions: Array<Record<string, unknown>>}>)[0];
  route.actions.push({ kind: "WAIT", start_us: "2000000", end_us: "2000000", node_id: -1, reason: "" });
  expect(() => adaptAcceptedExecution(view)).toThrow();
});
it("rejects invalid identifier/profile types and exact drawable fraction reversal", () => {
  for (const change of ["PROFILE_ARRAY", "EDGE_ID", "EXACT_FRACTION", "ACTION_KIND_ARRAY"] as const) {
    const view = acceptedView();
    const action = (view.accepted_trajectory!.vehicle_routes as Array<{actions: Array<Record<string, unknown>>}>)[0].actions[0];
    if (change === "PROFILE_ARRAY") view.accepted_trajectory!.profile = ["SAFER"];
    if (change === "EDGE_ID") action.edge_id = "invalid edge id";
    if (change === "EXACT_FRACTION") { action.fraction_start_exact = "3/4"; action.fraction_end = 0.5; }
    if (change === "ACTION_KIND_ARRAY") action.kind = ["EDGE"];
    expect(() => adaptAcceptedExecution(view)).toThrow();
  }
});
it("includes native vehicle order markers without legacy plan stops", () => {
  const view = acceptedView(); const mapped = adaptAcceptedExecution(view);
  const snapshot = { ...mapped, backend: { basis }, decisionState: { vehicles: [], orders: [{ id: "O1", longitude: 106.7, latitude: 10.8, status: "ONBOARD", priority: 1 }], locations: [], context: { rain: null } } } as unknown as DispatchSnapshot;
  expect(createMapScene(snapshot, undefined, "V1").markers.map(m => m.id)).toContain("O1");
  expect(createMapScene(snapshot, undefined, "V2").markers.map(m => m.id)).not.toContain("O1");
});
