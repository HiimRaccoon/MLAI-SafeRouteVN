import { arrive } from "../../test/arrive";
import { describe, expect, it } from "vitest";
import { MockStateEngine } from "../../mocks/engine/MockStateEngine";
import { createMapScene } from "./mapScene";

describe("map scene from supplied geometry", () => {
  it("carries supplied source metadata from a proposal into the immutable accepted map", () => {
    const snapshot = new MockStateEngine().optimize();
    const proposal = snapshot.planState.proposedAlternatives[0];
    const segment = proposal.content!.vehiclePlans[0].routeSegments[0];
    segment.geometrySource = "MEMBER2_SUPPLIED";
    segment.geometry.coordinates = [[106.7, 10.8], [106.701, 10.801], [106.704, 10.803], [106.71, 10.81]];
    const engine = new MockStateEngine(snapshot);
    engine.selectAlternative(proposal.id);
    const accepted = engine.acceptSelectedPlan();
    const scene = createMapScene(accepted, proposal);
    expect(scene.accepted[0].geometrySource).toBe("MEMBER2_SUPPLIED");
    expect(scene.proposed[0].geometrySource).toBe("MEMBER2_SUPPLIED");
    expect(scene.accepted[0].coordinates).toEqual([[106.7, 10.8], [106.701, 10.801], [106.704, 10.803], [106.71, 10.81]]);
  });
  it("keeps accepted and proposal LineStrings separate without calculating routes", () => {
    const engine = new MockStateEngine();
    const optimized = engine.optimize();
    const proposal = optimized.planState.proposedAlternatives[1];
    engine.selectAlternative(proposal.id);
    const accepted = engine.acceptSelectedPlan();
    const scene = createMapScene(accepted, proposal);
    expect(scene.accepted[0].coordinates).toEqual(accepted.planState.acceptedPlans[0].plan.vehiclePlans[0].routeSegments[0].geometry.coordinates);
    expect(scene.proposed[0].coordinates).toEqual(proposal.content!.vehiclePlans[0].routeSegments[0].geometry.coordinates);
  });

  it("shows only the selected driver's accepted route and dims completed segments", () => {
    const engine = new MockStateEngine();
    const optimized = engine.optimize();
    engine.selectAlternative(optimized.planState.proposedAlternatives[0].id);
    const accepted = engine.acceptSelectedPlan();
    const depot = accepted.planState.acceptedPlans[0].plan.vehiclePlans[0].orderedStops[0];
    for (const orderId of depot.orderIds) engine.pickupOrder({ vehicleId: "V1", orderId });
    arrive(engine);
    engine.deliverOrder({ vehicleId: "V1", orderId: "O001" });
    const scene = createMapScene(engine.getSnapshot(), undefined, "V1");
    expect(scene.accepted.every((segment) => segment.vehicleId === "V1")).toBe(true);
    expect(scene.accepted.some((segment) => segment.completed)).toBe(true);
    expect(scene.proposed).toHaveLength(0);
  });

  it("uses the fixture rain polygon and all relevant marker coordinates", () => {
    const engine = new MockStateEngine();
    engine.loadScenario("S4");
    const rain = engine.triggerFixtureEvent("S4-E1");
    const scene = createMapScene(rain);
    expect(scene.rain?.coordinates).toEqual(rain.decisionState.context.rain?.polygon.coordinates);
    expect(scene.markers.some((marker) => marker.kind === "depot")).toBe(true);
    expect(scene.markers.some((marker) => marker.kind === "order")).toBe(true);
  });
});
