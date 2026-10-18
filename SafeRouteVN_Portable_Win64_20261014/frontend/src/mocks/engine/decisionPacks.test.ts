import { describe, expect, it } from "vitest";
import { MockStateEngine } from "./MockStateEngine";
import { buildPreparedAlternatives } from "./decisionPacks";

describe("prepared decision packs", () => {
  it("uses explicitly schematic lightweight plans for the initial S0 world", () => {
    const engine = new MockStateEngine();
    const alternatives = engine.optimize().planState.proposedAlternatives;
    expect(alternatives).toHaveLength(3);
    expect(alternatives.every((p) => p.content!.provenance.source !== "Member 2 offline runtime")).toBe(true);
    expect(alternatives.every((p) => p.content!.vehiclePlans.flatMap((v) => v.routeSegments).every((s) => s.geometrySource === "SCHEMATIC_DEMO"))).toBe(true);
    expect(alternatives[0].content!.vehiclePlans.flatMap((v) => v.routeSegments).length).toBeGreaterThan(0);
  });

  it.each(["S2", "S3", "S4"] as const)("uses lightweight initial %s plans without applying its event", (scenario) => {
    const engine = new MockStateEngine();
    engine.loadScenario(scenario);
    const snapshot = engine.optimize();
    expect(snapshot.demo.availableEvents[0].status).toBe("READY_TO_TRIGGER");
    expect(snapshot.decisionState.orders).toHaveLength(8);
    expect(snapshot.planState.proposedAlternatives.every((p) => p.content!.provenance.source !== "Member 2 offline runtime")).toBe(true);
  });
  it("creates three deterministic proposals without changing a stable world version", () => {
    const engine = new MockStateEngine();
    const before = engine.getSnapshot();

    const first = engine.optimize();
    const second = engine.optimize();

    expect(first.decisionState.version).toBe(before.decisionState.version);
    expect(first.planState.proposedAlternatives.map((plan) => plan.content!.profile)).toEqual(["FASTEST", "BALANCED", "SAFER"]);
    expect(second.planState.proposedAlternatives).toEqual(first.planState.proposedAlternatives);
    expect(second.planState.selectedAlternativeId).toBeNull();
  });

  it("binds each proposal to the world snapshot that produced it", () => {
    const engine = new MockStateEngine();
    const snapshot = engine.optimize();

    expect(snapshot.planState.proposedAlternatives[0]).toMatchObject({
      generatedForSessionId: snapshot.decisionState.sessionId,
      generatedForStateVersion: snapshot.decisionState.version
    });
  });

  it("clears a selection when Dispatcher optimizes the same state again", () => {
    const engine = new MockStateEngine();
    const first = engine.optimize();
    engine.selectAlternative(first.planState.proposedAlternatives[1].id);

    const replaced = engine.optimize();

    expect(replaced.planState.selectedAlternativeId).toBeNull();
    expect(replaced.planState.proposedAlternatives).toHaveLength(3);
  });

  it("provides precomputed LineString geometry and meaningful profile differences", () => {
    const engine = new MockStateEngine();
    const [fastest, balanced, safer] = buildPreparedAlternatives(engine.getSnapshot().decisionState);

    expect(fastest.content!.vehiclePlans[0].routeSegments[0].geometry.type).toBe("LineString");
    expect(new Set([fastest.content!.metrics.durationMinutes, balanced.content!.metrics.durationMinutes, safer.content!.metrics.durationMinutes]).size).toBeGreaterThan(1);
    expect(fastest.content!.vehiclePlans).not.toEqual(safer.content!.vehiclePlans);
  });

  it("includes estimated fuel cost in each prepared proposal", () => {
    const proposals = buildPreparedAlternatives(new MockStateEngine().getSnapshot().decisionState);

    for (const proposal of proposals) expect(proposal.content!.metrics.fuelCostVnd).toBeCloseTo(proposal.content!.metrics.distanceKm * 2500);
  });

  it("keeps S3 onboard work with V1 and reassigns waiting work to V2", () => {
    const engine = new MockStateEngine();
    engine.loadScenario("S3");
    engine.triggerFixtureEvent("S3-E1");

    const fastest = buildPreparedAlternatives(engine.getSnapshot().decisionState).find((proposal) => proposal.content!.profile === "FASTEST");

    expect(fastest?.content!.vehiclePlans.find((plan) => plan.vehicleId === "V1")?.orderedStops.some((stop) => stop.orderIds.includes("O001"))).toBe(true);
    expect(fastest?.content!.unserved.some((item) => item.orderId === "O001")).toBe(true);
    expect(fastest?.content!.vehiclePlans.find((plan) => plan.vehicleId === "V2")?.orderedStops.some((stop) => stop.orderIds.includes("O003"))).toBe(true);
  });

  it("preserves S3 custody for every profile after V1 becomes unavailable", () => {
    const engine = new MockStateEngine();
    engine.loadScenario("S3");
    engine.triggerFixtureEvent("S3-E1");
    const proposals = buildPreparedAlternatives(engine.getSnapshot().decisionState);
    for (const proposal of proposals) {
      const v1 = proposal.content!.vehiclePlans.find((plan) => plan.vehicleId === "V1");
      const v2 = proposal.content!.vehiclePlans.find((plan) => plan.vehicleId === "V2");
      expect(v1?.orderedStops.some((stop) => stop.orderIds.includes("O001"))).toBe(true);
      expect(v2?.orderedStops.some((stop) => stop.orderIds.includes("O001"))).toBe(false);
      expect(proposal.content!.unserved.some((entry) => entry.orderId === "O001")).toBe(true);
    }
  });

  it("preserves custody in an S0 to S3 event round after V1 picked up O002", () => {
    const engine = new MockStateEngine();
    const optimized = engine.optimize();
    engine.selectAlternative(optimized.planState.proposedAlternatives[1].id);
    engine.acceptSelectedPlan();
    engine.pickupOrder({ vehicleId: "V1", orderId: "O002" });
    engine.triggerFixtureEvent("S3-E1");
    for (const proposal of engine.optimize().planState.proposedAlternatives) {
      const v1Orders = proposal.content!.vehiclePlans.find((plan) => plan.vehicleId === "V1")?.orderedStops.flatMap((stop) => stop.orderIds) ?? [];
      const v2Orders = proposal.content!.vehiclePlans.find((plan) => plan.vehicleId === "V2")?.orderedStops.flatMap((stop) => stop.orderIds) ?? [];
      expect(v1Orders).toContain("O002");
      expect(v2Orders).not.toContain("O002");
      expect(proposal.content!.unserved.some((item) => item.orderId === "O002")).toBe(true);
    }
  });
});
