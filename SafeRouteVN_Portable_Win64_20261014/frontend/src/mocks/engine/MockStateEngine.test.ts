import { arrive } from "../../test/arrive";
import { describe, expect, it } from "vitest";
import { DispatchError, MockStateEngine } from "./MockStateEngine";

describe("MockStateEngine scenario lifecycle", () => {
  it("loads direct event scenarios from initialState and keeps their event ready", () => {
    const engine = new MockStateEngine();

    const snapshot = engine.loadScenario("S3");

    expect(snapshot.decisionState.orders).toHaveLength(8);
    expect(snapshot.decisionState.events[0]).toMatchObject({ id: "S3-E1", status: "READY_TO_TRIGGER" });
    expect(snapshot.decisionState.vehicles.find((vehicle) => vehicle.id === "V1")?.availability).toBe("AVAILABLE");
  });

  it("offers one event source per direct scenario and three choices from S0", () => {
    const engine = new MockStateEngine();

    expect(engine.getSnapshot().demo.availableEvents.map((event) => event.id)).toEqual(["S2-E1", "S3-E1", "S4-E1"]);
    expect(engine.loadScenario("S4").demo.availableEvents.map((event) => event.id)).toEqual(["S4-E1"]);
  });

  it("reports triggered event status through the UI event summary", () => {
    const engine = new MockStateEngine();

    const snapshot = engine.triggerFixtureEvent("S2-E1");

    expect(snapshot.demo.availableEvents.find((event) => event.id === "S2-E1")?.status).toBe("TRIGGERED");
  });

  it("applies an urgent order only after Dispatcher triggers the fixture event", () => {
    const engine = new MockStateEngine();
    const before = engine.getSnapshot();

    const after = engine.triggerFixtureEvent("S2-E1");

    expect(after.decisionState.version).toBe(before.decisionState.version + 1);
    expect(after.decisionState.orders.find((order) => order.id === "O009")).toMatchObject({ priority: 3, status: "WAITING" });
    expect(after.decisionState.events[0].status).toBe("TRIGGERED");
  });

  it("does not accumulate a second event in the same demo round", () => {
    const engine = new MockStateEngine();
    engine.triggerFixtureEvent("S2-E1");

    let thrown: unknown;
    try {
      engine.triggerFixtureEvent("S3-E1");
    } catch (error) {
      thrown = error;
    }

    expect(thrown).toBeInstanceOf(DispatchError);
    expect(thrown).toMatchObject({ code: "EVENT_ALREADY_TRIGGERED" } satisfies Partial<DispatchError>);
  });

  it("keeps the onboard owner when S3 makes V1 unavailable", () => {
    const engine = new MockStateEngine();
    engine.loadScenario("S3");

    const snapshot = engine.triggerFixtureEvent("S3-E1");

    expect(snapshot.decisionState.orders.find((order) => order.id === "O001")?.assignedVehicleId).toBe("V1");
    expect(snapshot.decisionState.vehicles.find((vehicle) => vehicle.id === "V1")?.availability).toBe("UNAVAILABLE");
  });

  it("expires a triggered rain event through the deterministic demo clock", () => {
    const engine = new MockStateEngine();
    engine.loadScenario("S4");
    engine.triggerFixtureEvent("S4-E1");

    const expired = engine.advanceDemoClock(60);

    expect(expired.decisionState.context.rain).toBeNull();
    expect(expired.decisionState.events[0].status).toBe("EXPIRED");
  });

  it("keeps the +07 demo timezone across lifecycle actions", () => {
    const engine = new MockStateEngine();
    const optimized = engine.optimize();
    expect(optimized.demoClock.now).toMatch(/\+07:00$/);
    engine.selectAlternative(optimized.planState.proposedAlternatives[0].id);
    const accepted = engine.acceptSelectedPlan();
    expect(accepted.demoClock.now).toMatch(/\+07:00$/);
    const pickup = accepted.planState.acceptedPlans[0].plan.vehiclePlans.find((plan) => plan.vehicleId === "V1")?.orderedStops[0];
    if (!pickup) throw new Error("Expected a V1 depot pickup stop");
    for (const orderId of pickup.orderIds) {
      expect(engine.pickupOrder({ vehicleId: "V1", orderId }).demoClock.now).toMatch(/\+07:00$/);
    }
    const firstDelivery = accepted.planState.acceptedPlans[0].plan.vehiclePlans.find((plan) => plan.vehicleId === "V1")?.orderedStops.find((stop) => stop.kind === "DELIVERY");
    if (!firstDelivery) throw new Error("Expected a V1 delivery stop");
    arrive(engine, "V1");
    expect(engine.deliverOrder({ vehicleId: "V1", orderId: firstDelivery.orderIds[0] }).demoClock.now).toMatch(/\+07:00$/);
    const afterEvent = engine.triggerFixtureEvent("S2-E1");
    expect(afterEvent.demoClock.now).toMatch(/\+07:00$/);
  });

  it("does not unlock another event after a rain event has expired", () => {
    const engine = new MockStateEngine();
    engine.triggerFixtureEvent("S4-E1");
    engine.advanceDemoClock(60);

    expect(() => engine.triggerFixtureEvent("S2-E1")).toThrow("Mỗi lượt demo");
  });
});
