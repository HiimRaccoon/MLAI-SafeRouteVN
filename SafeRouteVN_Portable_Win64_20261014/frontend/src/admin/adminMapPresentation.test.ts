import { arrive } from "../test/arrive";
import { tinySuppliedEngine } from "../test/tinySuppliedPlan";
import { describe, expect, it } from "vitest";
import { MockStateEngine } from "../mocks/engine/MockStateEngine";
import { createAdminMapPresentation } from "./adminMapPresentation";
import { acceptedView } from "../integrations/member3/acceptedTestFixture";
import { adaptAcceptedExecution } from "../integrations/member3/executionViewAdapter";
import { basis } from "../integrations/member3/testFixtures";
import type { MapSegment } from "../shared/components/mapScene";
import { createMapScene } from "../shared/components/mapScene";

function acceptedS0() {
  const engine = tinySuppliedEngine();
  const optimized = engine.getSnapshot();
  engine.selectAlternative(optimized.planState.proposedAlternatives[0].id);
  return { engine, snapshot: engine.acceptSelectedPlan(), proposal: optimized.planState.proposedAlternatives[0] };
}

describe("Admin map display preferences and operational legs", () => {
  it("keeps native leg numbers per vehicle stable when other routes are hidden", () => {
    const snapshot = acceptedS0().snapshot;
    const view = acceptedView();
    Object.assign(snapshot, adaptAcceptedExecution(view));
    snapshot.backend = { source: "MEMBER3_HTTP", baseUrl: "http://localhost:8000", executionMode: "SIMULATED_REPLAY", realWorldObservation: false, basis, executionView: view };
    const edge = snapshot.planState.acceptedExecution!.segments[0];
    snapshot.planState.acceptedExecution!.segments = [
      { ...edge, id: "V1-edge-1", legId: "V1:leg:1", returnToDepot: false },
      { ...edge, id: "V1-edge-2", legId: "V1:leg:2", returnToDepot: false },
      { ...edge, id: "V2-edge-3", vehicleId: "V2", legId: "V2:leg:3", returnToDepot: false }
    ] satisfies MapSegment[];
    const before = structuredClone(snapshot);
    const both = createAdminMapPresentation(snapshot, undefined, ["V1", "V2"]);
    const onlyV2 = createAdminMapPresentation(snapshot, undefined, ["V2"]);
    expect(onlyV2.legs).toEqual(both.legs.filter(l => l.vehicleId === "V2"));
    expect(onlyV2.legs[0].number).toBe(3);
    expect(both.legs.filter(l => l.vehicleId === "V1").map(l => l.number)).toEqual([1, 2]);
    expect(snapshot).toEqual(before);
  });
  it("filters native return only on Admin and preserves Driver/source geometry and metrics", () => {
    const snapshot = acceptedS0().snapshot;
    const view = acceptedView(); Object.assign(snapshot, adaptAcceptedExecution(view));
    snapshot.backend = { source: "MEMBER3_HTTP", baseUrl: "http://localhost:8000", executionMode: "SIMULATED_REPLAY", realWorldObservation: false, basis, executionView: view };
    const before = structuredClone(snapshot);
    const admin = createAdminMapPresentation(snapshot, undefined, ["V1"]);
    expect(admin.scene.accepted).toEqual([]); expect(admin.legs).toEqual([]);
    expect(admin.source).toBe("ACCEPTED");
    expect(createMapScene(snapshot, undefined, "V1").accepted).toHaveLength(1);
    expect(snapshot).toEqual(before);
  });
  it("uses one native color per vehicle without recoloring on visibility changes", () => {
    const snapshot = acceptedS0().snapshot;
    const view = acceptedView(); Object.assign(snapshot, adaptAcceptedExecution(view));
    snapshot.backend = { source: "MEMBER3_HTTP", baseUrl: "http://localhost:8000", executionMode: "SIMULATED_REPLAY", realWorldObservation: false, basis, executionView: view };
    const edge = snapshot.planState.acceptedExecution!.segments[0];
    snapshot.planState.acceptedExecution!.segments = [
      ...["V1", "V2"].flatMap(vehicleId => [1, 2].map(number => ({ ...edge, vehicleId, id: `${vehicleId}-edge-${number}`, legId: `${vehicleId}:leg:${number}`, returnToDepot: false }))),
      { ...edge, vehicleId: "V2", id: "V2-return", legId: "V2:leg:3", returnToDepot: true }
    ];
    const before = structuredClone(snapshot);
    const both = createAdminMapPresentation(snapshot, undefined, ["V1", "V2"]);
    expect(both.legs.filter(l => l.vehicleId === "V1").map(l => l.color)).toEqual(["#2563eb", "#2563eb"]);
    expect(both.legs.filter(l => l.vehicleId === "V2").map(l => l.color)).toEqual(["#16a34a", "#16a34a"]);
    const onlyV2 = createAdminMapPresentation(snapshot, undefined, ["V2"]);
    expect(onlyV2.legs).toEqual(both.legs.filter(l => l.vehicleId === "V2"));
    expect(onlyV2.scene.accepted.map(s => s.color)).toEqual(["#16a34a", "#16a34a"]);
    expect(onlyV2.scene.accepted.every(s => !s.returnToDepot)).toBe(true);
    expect(snapshot).toEqual(before);
  });
  it.each([{ ids: [] }, { ids: ["V1"] }, { ids: ["V2"] }, { ids: ["V1", "V2"] }])("shows only explicitly enabled vehicle routes: $ids", ({ ids }) => {
    const { snapshot } = acceptedS0();
    const before = structuredClone(snapshot);
    const result = createAdminMapPresentation(snapshot, undefined, ids);
    expect([...new Set(result.scene.accepted.map((s) => s.vehicleId))].sort()).toEqual([...ids].sort());
    expect(result.scene.proposed).toEqual([]);
    expect(result.scene.markers).toHaveLength(6);
    expect(result.scene.markers.filter((m) => m.kind === "vehicle").map((m) => m.id)).toEqual(["V1", "V2"]);
    expect(snapshot).toEqual(before);
  });

  it("defaults to hidden routes without removing markers or rain", () => {
    const engine = new MockStateEngine();
    engine.loadScenario("S4");
    const snapshot = engine.triggerFixtureEvent("S4-E1");
    const result = createAdminMapPresentation(snapshot);
    expect(result.scene.accepted).toEqual([]);
    expect(result.scene.proposed).toEqual([]);
    expect(result.scene.rain).toEqual(snapshot.decisionState.context.rain!.polygon);
    expect(result.scene.markers).toHaveLength(snapshot.decisionState.orders.length + snapshot.decisionState.vehicles.length + snapshot.decisionState.locations.length);
  });

  it("previews one valid selected proposal instead of overlaying the accepted route", () => {
    const { engine } = acceptedS0();
    const snapshot = engine.optimize();
    const proposal = snapshot.planState.proposedAlternatives[1];
    engine.selectAlternative(proposal.id);
    const result = createAdminMapPresentation(engine.getSnapshot(), proposal, ["V2"]);
    expect(result.source).toBe("PROPOSED");
    expect(result.scene.accepted).toEqual([]);
    expect(result.scene.proposed.map((s) => s.coordinates)).toEqual(proposal.content!.vehiclePlans.find((v) => v.vehicleId === "V2")!.routeSegments.filter((s) => !s.toStopId.endsWith("return-depot")).map((s) => s.geometry.coordinates));
  });

  it.each(["session", "version", "selection"])("rejects a proposal with invalid %s binding", (field) => {
    const { snapshot, proposal } = acceptedS0();
    const candidate = structuredClone(proposal);
    snapshot.planState.proposedAlternatives = [candidate];
    snapshot.planState.selectedAlternativeId = candidate.id;
    if (field === "session") candidate.generatedForSessionId = "foreign-session";
    if (field === "version") candidate.generatedForStateVersion += 1;
    if (field === "selection") snapshot.planState.selectedAlternativeId = null;
    const result = createAdminMapPresentation(snapshot, candidate, ["V1"]);
    expect(result.source).toBe("ACCEPTED");
    expect(result.scene.proposed).toEqual([]);
    expect(result.scene.accepted.length).toBeGreaterThan(0);
  });

  it("uses a single vehicle color across every operational leg, keeping all supplied points", () => {
    const { snapshot } = acceptedS0();
    const first = createAdminMapPresentation(snapshot, undefined, ["V1", "V2"]);
    const second = createAdminMapPresentation(snapshot, undefined, ["V1", "V2"]);
    const v2 = first.scene.accepted.filter((s) => s.vehicleId === "V2");
    const legs = first.legs.filter((leg) => leg.vehicleId === "V2");
    expect(legs.map((leg) => leg.targetLabel)).toEqual(["O003", "O001"]);
    expect(new Set(legs.map((leg) => leg.color)).size).toBe(1);
    for (const leg of legs) {
      const edges = v2.filter((edge) => edge.legId === leg.id);
      expect(edges.length).toBeGreaterThan(1);
      expect(new Set(edges.map((edge) => edge.color))).toEqual(new Set([leg.color]));
    }
    const source = snapshot.planState.acceptedPlans[0].plan.vehiclePlans.find((v) => v.vehicleId === "V2")!;
    expect(source.routeSegments.some((s) => s.toStopId.endsWith("return-depot"))).toBe(true);
    expect(v2.map((s) => s.coordinates)).toEqual(source.routeSegments.filter((s) => !s.toStopId.endsWith("return-depot")).map((s) => s.geometry.coordinates));
    expect(first).toEqual(second);
    expect(first.legs.find((leg) => leg.vehicleId === "V1")!.color).toBe("#2563eb");
    expect(legs[0].color).toBe("#16a34a");
    expect(legs[1].color).toBe("#16a34a");
  });

  it("hides completed travel while preserving the original next-leg color and geometry", () => {
    const { engine, snapshot } = acceptedS0();
    const before = createAdminMapPresentation(snapshot, undefined, ["V2"]);
    const nextLeg = before.legs.find((leg) => leg.vehicleId === "V2" && leg.targetLabel === "O001")!;
    for (const orderId of ["O003", "O001"]) engine.pickupOrder({ vehicleId: "V2", orderId });
    arrive(engine);
    engine.deliverOrder({ vehicleId: "V2", orderId: "O003" });
    const result = createAdminMapPresentation(engine.getSnapshot(), undefined, ["V2"]);
    expect(result.legs.map((leg) => leg.targetLabel)).toEqual(["O001"]);
    expect(result.legs[0]).toEqual(nextLeg);
    expect(result.scene.accepted[0].coordinates).toEqual(before.scene.accepted.find((s) => s.legId === nextLeg.id)!.coordinates);
    expect(result.scene.accepted.every((s) => !s.completed)).toBe(true);
  });
});
