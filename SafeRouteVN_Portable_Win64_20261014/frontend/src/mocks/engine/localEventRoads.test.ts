import { describe, expect, it } from "vitest";
import { MockStateEngine } from "./MockStateEngine";

describe("lightweight manual event forecasts", () => {
  it("covers four S0 urgent orders with demo plans without changing capacity or dispatching", () => {
    const engine = new MockStateEngine();
    const initial = engine.optimize();
    engine.selectAlternative(initial.planState.proposedAlternatives[1].id);
    const accepted = engine.acceptSelectedPlan();
    engine.triggerFixtureEvent("S2-E1");
    const before = engine.getSnapshot();
    const proposed = engine.optimize();
    expect(proposed.decisionState).toEqual(before.decisionState);
    expect(proposed.decisionState.vehicles.map((v) => v.capacityKg)).toEqual([15, 15]);
    expect(proposed.planState.activeAcceptedPlanId).toBe(accepted.planState.activeAcceptedPlanId);
    expect(proposed.planState.acceptedPlans).toEqual(accepted.planState.acceptedPlans);
    for (const proposal of proposed.planState.proposedAlternatives) {
      expect(proposal.content!.provenance.source).not.toBe("Member 2 offline runtime");
      expect(proposal.content!.provenance.integrationMode).toBeUndefined();
      expect(proposal.content!.unserved).toEqual([]);
      const deliveries = proposal.content!.vehiclePlans.flatMap((v) => v.orderedStops.filter((s) => s.kind === "DELIVERY").flatMap((s) => s.orderIds));
      expect(deliveries.sort()).toEqual(["O001", "O002", "O003", "O009"]);
      const roads = proposal.content!.vehiclePlans.flatMap((v) => v.routeSegments);
      expect(roads.length).toBeGreaterThan(0);
      expect(roads.every((s) => s.geometrySource === "SCHEMATIC_DEMO")).toBe(true);
      expect(roads.every((s) => s.geometry.coordinates.length >= 2)).toBe(true);
    }
    const again = engine.optimize();
    expect(again.planState.proposedAlternatives).toEqual(proposed.planState.proposedAlternatives);
    engine.selectAlternative(again.planState.proposedAlternatives[0].id);
    const updated = engine.acceptSelectedPlan();
    expect(updated.planState.activeAcceptedPlanId).not.toBe(accepted.planState.activeAcceptedPlanId);
    expect(updated.planState.acceptedPlans[0].plan).toEqual(accepted.planState.acceptedPlans[0].plan);
    expect(updated.planState.proposedAlternatives).toEqual([]);
  });

  it("keeps unavailable V1 custody out of V2 lightweight plans", () => {
    const engine = new MockStateEngine();
    engine.loadScenario("S3");
    engine.triggerFixtureEvent("S3-E1");
    const before = engine.getSnapshot().decisionState;
    for (const proposal of engine.optimize().planState.proposedAlternatives) {
      expect(proposal.content!.provenance.integrationMode).toBeUndefined();
      expect(proposal.content!.vehiclePlans.find((v) => v.vehicleId === "V1")!.routeSegments).toEqual([]);
      expect(proposal.content!.vehiclePlans.find((v) => v.vehicleId === "V2")!.orderedStops.flatMap((s) => s.orderIds)).not.toContain("O001");
      expect(proposal.content!.unserved.some(o => o.orderId === "O001")).toBe(true);
    }
    expect(engine.getSnapshot().decisionState).toEqual(before);
  });

  it("refuses to reuse an event forecast after physical pickup", () => {
    const engine = new MockStateEngine();
    const initial = engine.optimize();
    engine.selectAlternative(initial.planState.proposedAlternatives[1].id);
    engine.acceptSelectedPlan();
    engine.pickupOrder({ vehicleId: "V1", orderId: "O002" });
    engine.triggerFixtureEvent("S2-E1");
    expect(engine.optimize().planState.proposedAlternatives.every((p) => p.content!.provenance.source !== "Member 2 offline runtime")).toBe(true);
  });

  it.each(["S2", "S4"] as const)("keeps %s demo event forecasts separate from SDK forecasts, including after expiry", (scenario) => {
    const engine = new MockStateEngine();
    engine.loadScenario(scenario);
    engine.triggerFixtureEvent(`${scenario}-E1`);
    const before = engine.getSnapshot().decisionState;
    const next = engine.optimize();
    expect(next.decisionState).toEqual(before);
    expect(next.planState.proposedAlternatives.every((p) => p.content!.provenance.integrationMode === undefined)).toBe(true);
    if (scenario === "S4") {
      engine.advanceDemoClock(60);
      expect(engine.optimize().planState.proposedAlternatives.every((p) => p.content!.provenance.source !== "Member 2 offline runtime")).toBe(true);
    }
  });

  it("keeps S0 rain unsupported instead of substituting the S4 eight-order world", () => {
    const engine = new MockStateEngine();
    engine.triggerFixtureEvent("S4-E1");
    const proposed = engine.optimize();
    expect(proposed.decisionState.orders).toHaveLength(3);
    expect(proposed.planState.proposedAlternatives.every((p) => p.content!.provenance.source !== "Member 2 offline runtime")).toBe(true);
  });
});
