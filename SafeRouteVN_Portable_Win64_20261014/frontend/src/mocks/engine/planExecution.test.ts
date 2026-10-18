import { arrive } from "../../test/arrive";
import { describe, expect, it } from "vitest";
import { MockStateEngine } from "./MockStateEngine";

function acceptBalancedPlan(engine: MockStateEngine) {
  const optimized = engine.optimize();
  const balanced = optimized.planState.proposedAlternatives.find((proposal) => proposal.content!.profile === "BALANCED");
  if (!balanced) throw new Error("Missing BALANCED proposal in test setup.");
  engine.selectAlternative(balanced.id);
  return engine.acceptSelectedPlan();
}

describe("accepted plan and execution lifecycle", () => {
  it("leaves the snapshot unchanged when Accept fails validation", () => {
    const engine = new MockStateEngine();
    const before = engine.getSnapshot();

    expect(() => engine.acceptSelectedPlan()).toThrow("Hãy chọn một phương án");

    expect(engine.getSnapshot()).toEqual(before);
  });

  it("rejects a selected plan as stale when Accept reaches the rain expiry", () => {
    const engine = new MockStateEngine();
    engine.loadScenario("S4");
    engine.triggerFixtureEvent("S4-E1");
    const optimized = engine.optimize();
    engine.selectAlternative(optimized.planState.proposedAlternatives[0].id);
    engine.advanceDemoClock(58);

    expect(() => engine.acceptSelectedPlan()).toThrow("trạng thái cũ");
  });

  it("clears proposals after Accept and keeps the plan in immutable history", () => {
    const engine = new MockStateEngine();
    const accepted = acceptBalancedPlan(engine);
    const firstRecord = accepted.planState.acceptedPlans[0];

    expect(accepted.planState.proposedAlternatives).toEqual([]);
    expect(accepted.planState.selectedAlternativeId).toBeNull();
    expect(accepted.planState.activeAcceptedPlanId).toBe(firstRecord.id);

    engine.triggerFixtureEvent("S2-E1");
    const replacement = acceptBalancedPlan(engine);

    expect(replacement.planState.acceptedPlans).toHaveLength(2);
    expect(replacement.planState.acceptedPlans[0]).toMatchObject({ id: firstRecord.id, plan: firstRecord.plan });
    expect(replacement.planState.acceptedPlans[0].supersededAt).toEqual(expect.any(String));
    expect(replacement.planState.acceptedPlans[1].id).not.toBe(firstRecord.id);
  });

  it("allows a depot pickup only for an order in the vehicle current stop", () => {
    const engine = new MockStateEngine();
    const accepted = acceptBalancedPlan(engine);
    const vehiclePlan = accepted.planState.acceptedPlans[0].plan.vehiclePlans.find((plan) => plan.orderedStops[0]?.orderIds.includes("O001"));
    if (!vehiclePlan) throw new Error("Missing O001 pickup plan in test setup.");

    const updated = engine.pickupOrder({ vehicleId: vehiclePlan.vehicleId, orderId: "O001" });

    expect(updated.decisionState.orders.find((order) => order.id === "O001")).toMatchObject({ status: "ONBOARD", assignedVehicleId: vehiclePlan.vehicleId });
    expect(updated.decisionState.vehicles.find((vehicle) => vehicle.id === vehiclePlan.vehicleId)?.onboardOrderIds).toContain("O001");
  });

  it("marks an accepted plan stale after a world event while retaining its route", () => {
    const engine = new MockStateEngine();
    const accepted = acceptBalancedPlan(engine);

    const stale = engine.triggerFixtureEvent("S4-E1");

    expect(stale.planState.activeAcceptedPlanId).toBe(accepted.planState.activeAcceptedPlanId);
    expect(stale.planState.operationalPlanAssessment).toMatchObject({
      planId: accepted.planState.activeAcceptedPlanId,
      status: "NEEDS_REOPTIMIZATION"
    });
  });

  it("moves through grouped pickups before delivering the matching current stop", () => {
    const engine = new MockStateEngine();
    const accepted = acceptBalancedPlan(engine);
    const plan = accepted.planState.acceptedPlans[0].plan.vehiclePlans.find((vehiclePlan) => vehiclePlan.orderedStops[0]?.orderIds.includes("O001"));
    if (!plan) throw new Error("Missing O001 plan in test setup.");
    const pickupOrderIds = plan.orderedStops[0].orderIds;

    pickupOrderIds.forEach((orderId) => engine.pickupOrder({ vehicleId: plan.vehicleId, orderId }));
    const currentDelivery = plan.orderedStops[1];
    arrive(engine);
    const delivered = engine.deliverOrder({ vehicleId: plan.vehicleId, orderId: currentDelivery.orderIds[0] });

    expect(delivered.decisionState.orders.find((order) => order.id === currentDelivery.orderIds[0])).toMatchObject({ status: "DELIVERED", assignedVehicleId: plan.vehicleId });
    expect(delivered.executionState.progressByPlanId[accepted.planState.activeAcceptedPlanId!][plan.vehicleId].completedStopIds).toContain(currentDelivery.id);
  });
});
