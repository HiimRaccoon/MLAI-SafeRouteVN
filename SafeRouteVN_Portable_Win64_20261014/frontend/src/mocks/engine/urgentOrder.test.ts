import { arrive } from "../../test/arrive";
import { describe, expect, it } from "vitest";
import { MockStateEngine } from "./MockStateEngine";

function accept(engine: MockStateEngine) {
  const proposals = engine.optimize().planState.proposedAlternatives;
  engine.selectAlternative(proposals[1].id);
  return engine.acceptSelectedPlan();
}

describe("Urgent Order ON/OFF", () => {
  it("starts a new S0 round without an urgent order and leaves OFF unchanged", () => {
    const engine = new MockStateEngine();
    const initial = engine.getSnapshot();
    expect(initial.decisionState.orders).toHaveLength(3);
    expect(initial.decisionState.orders.some((order) => order.id === "O009")).toBe(false);
    expect(initial.demo.availableEvents[0].status).toBe("READY_TO_TRIGGER");
    expect(engine.setUrgentOrderEnabled(false)).toEqual(initial);
  });

  it("cancels only the waiting urgent order, keeps physical progress/history, invalidates proposals and advances the demo clock", () => {
    const engine = new MockStateEngine();
    accept(engine);
    engine.pickupOrder({ vehicleId: "V1", orderId: "O002" });
    engine.setUrgentOrderEnabled(true);
    engine.optimize();
    const before = engine.getSnapshot();
    const after = engine.setUrgentOrderEnabled(false);
    expect(after.decisionState.orders).toEqual(before.decisionState.orders.filter((order) => order.id !== "O009"));
    expect(after.decisionState.vehicles).toEqual(before.decisionState.vehicles);
    expect(after.executionState).toEqual(before.executionState);
    expect(after.planState.acceptedPlans).toEqual(before.planState.acceptedPlans);
    expect(after.planState.activeAcceptedPlanId).toBe(before.planState.activeAcceptedPlanId);
    expect(after.planState.operationalPlanAssessment).toMatchObject({ status: "NEEDS_REOPTIMIZATION", reasons: ["URGENT_ORDER_CANCELLED"] });
    expect(after.planState.proposedAlternatives).toEqual([]);
    expect(after.planState.selectedAlternativeId).toBeNull();
    expect(after.decisionState.version).toBe(before.decisionState.version + 1);
    expect(new Date(after.demoClock.now).getTime() - new Date(before.demoClock.now).getTime()).toBe(60_000);
    expect(after.demo.availableEvents[0].status).toBe("READY_TO_TRIGGER");
  });

  it("allows ON/OFF/ON without duplicate orders or accumulating a different event, including after hydrate", () => {
    const engine = new MockStateEngine();
    const on = engine.setUrgentOrderEnabled(true);
    expect(engine.setUrgentOrderEnabled(true)).toEqual(on);
    const off = engine.setUrgentOrderEnabled(false);
    const reloaded = new MockStateEngine(off);
    expect(() => reloaded.triggerFixtureEvent("S3-E1")).toThrow();
    expect(reloaded.getSnapshot()).toEqual(off);
    expect(reloaded.setUrgentOrderEnabled(true).decisionState.orders.filter((order) => order.id === "O009")).toHaveLength(1);
    expect(reloaded.resetDemoSession().decisionState.orders).toHaveLength(3);
    expect(reloaded.getSnapshot().demo.roundEventId).toBeNull();
    expect(reloaded.triggerFixtureEvent("S3-E1").demo.availableEvents[1].status).toBe("TRIGGERED");
  });

  it("restores deterministic lightweight geometry after OFF and ON", () => {
    const engine = new MockStateEngine();
    const initial = engine.optimize().planState.proposedAlternatives.map((proposal) => proposal.content!.vehiclePlans);
    engine.setUrgentOrderEnabled(true);
    const urgent = engine.optimize().planState.proposedAlternatives;
    expect(urgent.every((proposal) => proposal.content!.provenance.integrationMode === undefined)).toBe(true);
    engine.setUrgentOrderEnabled(false);
    const restored = engine.optimize().planState.proposedAlternatives;
    expect(restored.every((proposal) => proposal.content!.provenance.source !== "Member 2 offline runtime")).toBe(true);
    expect(restored.map((proposal) => proposal.content!.vehiclePlans)).toEqual(initial);
    engine.setUrgentOrderEnabled(true);
    expect(engine.optimize().planState.proposedAlternatives.map((proposal) => proposal.content!.vehiclePlans)).toEqual(urgent.map((proposal) => proposal.content!.vehiclePlans));
  });

  it.each(["ONBOARD", "DELIVERED"])("rejects cancellation of %s urgent cargo without changing the world", (status) => {
    const engine = new MockStateEngine();
    engine.setUrgentOrderEnabled(true);
    const accepted = accept(engine);
    const plan = accepted.planState.acceptedPlans[0].plan.vehiclePlans.find((v) => v.orderedStops.some((s) => s.orderIds.includes("O009")))!;
    for (const stop of plan.orderedStops) {
      if (stop.kind === "DEPOT_PICKUP") {
        for (const orderId of stop.orderIds) engine.pickupOrder({ vehicleId: plan.vehicleId, orderId });
        if (status === "ONBOARD" && stop.orderIds.includes("O009")) break;
      } else {
        arrive(engine);
        for (const orderId of stop.orderIds) engine.deliverOrder({ vehicleId: plan.vehicleId, orderId });
        if (stop.orderIds.includes("O009")) break;
      }
    }
    const before = engine.getSnapshot();
    expect(before.decisionState.orders.find((o) => o.id === "O009")?.status).toBe(status);
    expect(() => engine.setUrgentOrderEnabled(false)).toThrow(expect.objectContaining({ code: "URGENT_ORDER_NOT_CANCELLABLE" }));
    expect(engine.getSnapshot()).toEqual(before);
  });

  it("restores the event-round lock for legacy persisted triggered snapshots", () => {
    const engine = new MockStateEngine();
    const legacy = engine.triggerFixtureEvent("S2-E1");
    delete legacy.demo.roundEventId;
    const restored = new MockStateEngine(legacy);
    restored.setUrgentOrderEnabled(false);
    expect(() => restored.triggerFixtureEvent("S4-E1")).toThrow();
  });
});
