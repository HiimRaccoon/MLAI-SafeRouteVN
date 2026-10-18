import { describe, expect, it } from "vitest";
import s1Fixture from "../../../../scenarios/fixtures/thu-duc-binh-thanh-v1/S1.json";
import { MOCK_STORAGE_KEY, MockDispatchApi, type StorageLike } from "./MockDispatchApi";

class TestStorage implements StorageLike {
  private readonly values = new Map<string, string>();
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) { this.values.set(key, value); }
  removeItem(key: string) { this.values.delete(key); }
}

describe("S1 initial offline roads and dispatch lifecycle", () => {
  it("replaces an executed session with the clean, event-free S1 source state", async () => {
    const api = new MockDispatchApi({ storage: new TestStorage() });
    try {
      const old = await api.optimize();
      await api.selectAlternative(old.planState.proposedAlternatives[0].id);
      const accepted = await api.acceptSelectedPlan();
      const pickup = accepted.planState.acceptedPlans[0].plan.vehiclePlans.flatMap((vehicle) =>
        vehicle.orderedStops.filter((stop) => stop.kind === "DEPOT_PICKUP").map((stop) => ({ vehicleId: vehicle.vehicleId, orderId: stop.orderIds[0] })))[0];
      await api.pickupOrder(pickup);
      const loaded = await api.loadScenario("S1");
      expect(loaded.decisionState.sessionId).not.toBe(old.decisionState.sessionId);
      expect(loaded.decisionState).toMatchObject({
        scenarioId: "S1", version: 1, orders: s1Fixture.initialState.orders,
        vehicles: s1Fixture.initialState.vehicles, locations: s1Fixture.initialState.locations,
        events: [], context: { rain: null }
      });
      expect(loaded.decisionState.orders).toHaveLength(8);
      expect(loaded.decisionState.vehicles).toHaveLength(2);
      expect(loaded.planState).toEqual({ acceptedPlans: [], activeAcceptedPlanId: null,
        proposedAlternatives: [], selectedAlternativeId: null, operationalPlanAssessment: null });
      expect(loaded.executionState).toEqual({ activePlanId: null, progressByPlanId: {} });
      expect(loaded.demo.availableEvents).toEqual([]);
      expect(loaded.demo.roundEventId).toBeNull();
      expect(loaded.demoClock.now).toBe("2026-09-27T21:00:00+07:00");
    } finally { api.dispose(); }
  });

  it("keeps explicit offline S1 plans lightweight and labels schematic geometry", async () => {
    const api = new MockDispatchApi({ storage: new TestStorage() });
    try {
      await api.loadScenario("S1");
      const snapshot = await api.optimize();
      expect(snapshot.planState.proposedAlternatives.map(p => p.content!.profile)).toEqual(["FASTEST", "BALANCED", "SAFER"]);
      expect(snapshot.planState.activeAcceptedPlanId).toBeNull();
      for (const proposal of snapshot.planState.proposedAlternatives) {
        const plan = proposal.content!;
        expect(plan.provenance.source).not.toBe("Member 2 offline runtime");
        expect(plan.provenance.buildSha256).toBeUndefined();
        const served = plan.vehiclePlans.flatMap(v => v.orderedStops.filter(s => s.kind === "DELIVERY").flatMap(s => s.orderIds));
        expect([...served, ...plan.unserved.map(o => o.orderId)].sort()).toEqual(snapshot.decisionState.orders.map(o => o.id).sort());
        const segments = plan.vehiclePlans.flatMap(v => v.routeSegments);
        expect(segments.length).toBeGreaterThan(0);
        expect(segments.every(s => s.geometrySource === "SCHEMATIC_DEMO")).toBe(true);
      }
    } finally { api.dispose(); }
  });

  it("keeps S1 proposals out of Driver execution until Accept and restores the exact accepted plan", async () => {
    const storage = new TestStorage();
    const admin = new MockDispatchApi({ storage });
    const driver = new MockDispatchApi({ storage });
    const syncDriver = () => window.dispatchEvent(new StorageEvent("storage", { key: MOCK_STORAGE_KEY, newValue: storage.getItem(MOCK_STORAGE_KEY) }));
    try {
      await admin.loadScenario("S1");
      const optimized = await admin.optimize();
      const selected = optimized.planState.proposedAlternatives[0];
      await admin.selectAlternative(selected.id);
      syncDriver();
      expect((await driver.getSnapshot()).executionState.activePlanId).toBeNull();
      expect((await driver.getSnapshot()).planState.acceptedPlans).toEqual([]);
      const accepted = await admin.acceptSelectedPlan();
      syncDriver();
      const received = await driver.getSnapshot();
      expect(received.executionState.activePlanId).toBe(accepted.planState.activeAcceptedPlanId);
      expect(received.planState.acceptedPlans).toHaveLength(1);
      expect(received.planState.acceptedPlans[0].plan).toEqual(selected.content);
      const hydrated = new MockDispatchApi({ storage });
      try { expect((await hydrated.getSnapshot()).planState.acceptedPlans).toEqual(accepted.planState.acceptedPlans); }
      finally { hydrated.dispose(); }
    } finally { admin.dispose(); driver.dispose(); }
  });
});
