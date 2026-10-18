import { expect, it } from "vitest";
import { adaptJobForecast, parseJobForecast } from "./forecastViewAdapter";
import { forecastView } from "./forecastTestFixture";
import { basis } from "./testFixtures";
import { createMapScene } from "../../shared/components/mapScene";
import { createAdminMapPresentation } from "../../admin/adminMapPresentation";
import type { DispatchSnapshot } from "../../shared/types/dispatch";

function proposal() { return adaptJobForecast(parseJobForecast(forecastView()), { basis, jobId: "job-return", profile: "SAFER", comparisonId: "comparison-test", vehicleIds: ["V1"], deliveredPrefix: [] })!; }
function snapshot(): DispatchSnapshot {
  const p = proposal();
  return { backend: { basis }, decisionState: { sessionId: basis.session_id, vehicles: [], orders: [], locations: [], context: { rain: null } },
    planState: { acceptedExecution: null, acceptedPlans: [], activeAcceptedPlanId: null, selectedAlternativeId: p.id, proposedAlternatives: [p] },
    executionState: { activePlanId: null, progressByPlanId: {} } } as unknown as DispatchSnapshot;
}
it("preserves certified partial EDGE source and rational wire values without accepted state", () => {
  const raw = forecastView(); const original = structuredClone(raw);
  const p = proposal();
  expect(p.content).toBeUndefined();
  expect(p.nativeForecast!.segments[0]).toMatchObject({ coordinates: [[106.7, 10.8], [106.701, 10.802], [106.702, 10.803]], fractionStart: 0.25, fractionStartExact: "1/4", fractionEnd: 1, returnToDepot: true, completed: false });
  expect(p.nativeForecast!.trajectory).toEqual(raw.trajectory);
  expect(raw).toEqual(original);
});
it("clips preview drawing but preserves every original point", () => {
  const raw = forecastView(); const route = (raw.trajectory.vehicle_routes as Array<{ actions: Record<string, unknown>[] }>)[0];
  Object.assign(route.actions[0], { geometry: [[0, 0], [1, 0], [2, 0]], fraction_end: 0.75 });
  const p = adaptJobForecast(parseJobForecast(raw), { basis, jobId: raw.job_id, profile: "SAFER", comparisonId: "c", vehicleIds: ["V1"], deliveredPrefix: [] })!;
  expect(p.nativeForecast!.segments[0].drawableCoordinates).toEqual([[0.5, 0], [1, 0], [1.5, 0]]);
  expect(p.nativeForecast!.segments[0].coordinates).toEqual([[0, 0], [1, 0], [2, 0]]);
});
it("retains int64 offsets and rational numerators above the JS safe integer limit", () => {
  const raw = forecastView(); const route = (raw.trajectory.vehicle_routes as Array<Record<string, unknown>>)[0];
  const action = (route.actions as Array<Record<string, unknown>>)[0];
  route.start_us = action.start_us = "9007199254740993"; route.return_us = action.end_us = "9007199254740995";
  action.fraction_start = 0.5; action.fraction_start_exact = "9007199254740993/18014398509481986";
  const p = adaptJobForecast(parseJobForecast(raw), { basis, jobId: "job-return", profile: "SAFER", comparisonId: "c", vehicleIds: ["V1"], deliveredPrefix: [] })!;
  expect(p.nativeForecast!.segments[0].fractionStartExact).toBe("9007199254740993/18014398509481986");
  const projectedRoute = (p.nativeForecast!.trajectory.vehicle_routes as Array<Record<string, unknown>>)[0];
  expect(projectedRoute.start_us).toBe("9007199254740993"); expect(projectedRoute.return_us).toBe("9007199254740995");
});
it("rejects all nine stale basis fields even when counters match", () => {
  for (const key of Object.keys(basis) as Array<keyof typeof basis>) {
    const current = { ...basis, [key]: key.endsWith("sha256") ? "a".repeat(64) : key === "head_version" ? "2" : key === "generation" ? "1" : "different" };
    if (key === "root_sha256") current[key] = "f".repeat(64);
    if (key === "session_id") {
      expect(() => adaptJobForecast(parseJobForecast(forecastView()), { basis: current, jobId: "job-return", profile: "SAFER", comparisonId: "c", vehicleIds: ["V1"], deliveredPrefix: [] })).toThrow();
      continue;
    }
    expect(adaptJobForecast(parseJobForecast(forecastView()), { basis: current, jobId: "job-return", profile: "SAFER", comparisonId: "c", vehicleIds: ["V1"], deliveredPrefix: [] })).toBeNull();
  }
});
it("rejects wrong profile/job/session/build, uncertified lifecycle and malformed portable actions", () => {
  for (const patch of [{ profile: "FASTEST" }, { job_id: "other" }, { session_id: "other" }, { build_sha256: "f".repeat(64) }, { units: {} }, { metrics: { total_distance_m: -1 } }]) {
    expect(() => parseJobForecast({ ...forecastView(), ...patch })).toThrow();
  }
  const malformed = forecastView(); (malformed.trajectory.vehicle_routes as Array<{ actions: Record<string, unknown>[] }>)[0].actions[0].from_node = -1;
  expect(() => proposalFrom(malformed)).toThrow();
  const pending = forecastView(); Object.assign(pending.job_view, { job_status: "RUNNING" });
  expect(() => parseJobForecast(pending)).toThrow();
});
function proposalFrom(raw: unknown) { return adaptJobForecast(parseJobForecast(raw), { basis, jobId: "job-return", profile: "SAFER", comparisonId: "c", vehicleIds: ["V1"], deliveredPrefix: [] }); }
it("keeps typed no-witness forecasts unavailable", () => {
  const raw = forecastView();
  Object.assign(raw, { trajectory: null, metrics: null, job_view: { ...raw.job_view, job_status: "QUEUED", internal_status: null, business_status: null, validation: { status: "NOT_RUN", valid: null }, served_orders: [], unserved_orders: [], plan_available: false, coverage_evaluated: false } });
  expect(proposalFrom(raw)).toBeNull();
});
it("allows the delivered prefix to be absent from a certified reoptimized suffix", () => {
  const raw = forecastView(); raw.job_view.served_orders = ["O0"];
  const p = adaptJobForecast(parseJobForecast(raw), { basis, jobId: "job-return", profile: "SAFER", comparisonId: "c", vehicleIds: ["V1"], deliveredPrefix: ["O0"] });
  expect(p!.nativeForecast!.segments).toHaveLength(1);
  expect(p!.nativeForecast!.jobView.served_orders).toEqual(["O0"]);
  expect(() => adaptJobForecast(parseJobForecast(raw), { basis, jobId: "job-return", profile: "SAFER", comparisonId: "c", vehicleIds: ["V1"], deliveredPrefix: [] })).toThrow();
});
it("validates portable geometry even when the forecast is historical", () => {
  const raw = forecastView(); const route = (raw.trajectory.vehicle_routes as Array<{ actions: Record<string, unknown>[] }>)[0];
  route.actions[0].geometry = [[200, 10]];
  expect(() => parseJobForecast(raw)).toThrow();
});
it("derives native service leg palettes before visibility and return filters", () => {
  const raw = forecastView(); const route = (raw.trajectory.vehicle_routes as Array<{ order_sequence: string[]; actions: Record<string, unknown>[] }>)[0];
  raw.job_view.internal_status = "PARTIAL"; raw.job_view.business_status = "PARTIAL"; raw.job_view.served_orders = ["O2", "O3"];
  route.order_sequence = ["O2", "O3"];
  route.actions[0].end_us = "1000000";
  const edge = structuredClone(route.actions[0]);
  route.actions.push({ kind: "SERVICE", start_us: "1000000", end_us: "1000000", node_id: 2, order_id: "O2", load_after_kg: 0 },
    { ...edge, start_us: "1000000", end_us: "2000000" },
    { kind: "SERVICE", start_us: "2000000", end_us: "2000000", node_id: 2, order_id: "O3", load_after_kg: 0 });
  const p = adaptJobForecast(parseJobForecast(raw), { basis, jobId: "job-return", profile: "SAFER", comparisonId: "c", vehicleIds: ["V1"], deliveredPrefix: [] })!;
  const s = snapshot(); s.planState.proposedAlternatives = [p]; s.planState.selectedAlternativeId = p.id;
  const first = createAdminMapPresentation(s, p, ["V1"]);
  expect(first.scene.proposed.map(segment => segment.color)).toEqual(["#2563eb", "#06b6d4"]);
  expect(createAdminMapPresentation(s, p, []).scene.proposed).toEqual([]);
  expect(createAdminMapPresentation(s, p, ["V1"]).scene.proposed.map(segment => segment.color)).toEqual(["#2563eb", "#06b6d4"]);
  expect(p.nativeForecast!.segments.map(segment => segment.coordinates)).toEqual(first.scene.proposed.map(segment => segment.coordinates));
});
it("keeps Driver accepted-only while Admin previews the complete route including return", () => {
  const s = snapshot(), p = s.planState.proposedAlternatives[0];
  expect(createMapScene(s, p).proposed).toHaveLength(1);
  expect(createMapScene(s, p, "V1").proposed).toEqual([]);
  expect(createMapScene(s, p, "V1").accepted).toEqual([]);
  expect(s.planState.acceptedExecution).toBeNull();
  const admin = createAdminMapPresentation(s, p, ["V1"]);
  expect(admin.source).toBe("PROPOSED"); expect(admin.scene.proposed).toHaveLength(1);
  expect(admin.scene.proposed[0].returnToDepot).toBe(true);
  expect(p.nativeForecast!.segments).toHaveLength(1);
  s.backend!.stale = true;
  expect(createMapScene(s, p).proposed).toEqual([]);
  expect(createAdminMapPresentation(s, p, ["V1"]).source).toBeNull();
});
it("preserves the accepted route while a different proposal is highlighted", () => {
  const s = snapshot(), p = s.planState.proposedAlternatives[0];
  s.planState.acceptedExecution = { source: "MEMBER3_HTTP", jobId: "accepted-job", profile: "FASTEST", basis,
    segments: [{ id: "accepted-edge", vehicleId: "V1", coordinates: [[0, 0], [1, 1]], completed: false, geometrySource: "MEMBER2_SUPPLIED", returnToDepot: false }], vehicleOrderIds: { V1: [] }, unserved: [] };
  const accepted = structuredClone(s.planState.acceptedExecution);
  expect(createMapScene(s, p).accepted.map(segment => segment.id)).toEqual(["accepted-edge"]);
  expect(createMapScene(s, p, "V1").accepted.map(segment => segment.coordinates)).toEqual([[[0, 0], [1, 1]]]);
  expect(createMapScene(s, p, "V1").proposed).toEqual([]);
  expect(createAdminMapPresentation(s, p, ["V1"]).source).toBe("PROPOSED");
  expect(s.planState.acceptedExecution).toEqual(accepted);
});
