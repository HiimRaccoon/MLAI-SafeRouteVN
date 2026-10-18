import { describe, expect, it } from "vitest";
import { MOCK_STORAGE_KEY, MockDispatchApi, type StorageLike } from "./MockDispatchApi";

class TestStorage implements StorageLike {
  readonly values = new Map<string, string>();
  writes = 0;

  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) { this.writes += 1; this.values.set(key, value); }
  removeItem(key: string) { this.values.delete(key); }
}

async function acceptPlan(api: MockDispatchApi) {
  const optimized = await api.optimize();
  const selected = optimized.planState.proposedAlternatives[0];
  await api.selectAlternative(selected.id);
  return api.acceptSelectedPlan();
}

describe("MockDispatchApi persistence and subscriptions", () => {
  it("persists Urgent OFF, synchronizes tabs without write loops, and keeps the round lock on reload", async () => {
    const storage = new TestStorage();
    const admin = new MockDispatchApi({ storage });
    const driver = new MockDispatchApi({ storage, eventTarget: window });
    const received: number[] = [];
    const unsubscribe = driver.subscribe((snapshot) => received.push(snapshot.decisionState.orders.length));
    await acceptPlan(admin);
    await admin.setUrgentOrderEnabled(true);
    window.dispatchEvent(new StorageEvent("storage", { key: MOCK_STORAGE_KEY, newValue: storage.getItem(MOCK_STORAGE_KEY) }));
    const off = await admin.setUrgentOrderEnabled(false);
    const writes = storage.writes;
    window.dispatchEvent(new StorageEvent("storage", { key: MOCK_STORAGE_KEY, newValue: storage.getItem(MOCK_STORAGE_KEY) }));
    expect(storage.writes).toBe(writes);
    expect(received).toEqual([4, 3]);
    expect(await driver.getSnapshot()).toEqual(off);
    const reloaded = new MockDispatchApi({ storage });
    expect(await reloaded.getSnapshot()).toEqual(off);
    await expect(reloaded.triggerFixtureEvent("S3-E1")).rejects.toMatchObject({ code: "EVENT_ALREADY_TRIGGERED" });
    const on = await reloaded.setUrgentOrderEnabled(true);
    expect(on.decisionState.orders).toHaveLength(4);
    expect(on.planState.acceptedPlans).toEqual(off.planState.acceptedPlans);
    unsubscribe();
    driver.dispose();
  });

  it("hydrates decision state, accepted history, execution and demo clock after reload", async () => {
    const storage = new TestStorage();
    const first = new MockDispatchApi({ storage });
    const accepted = await acceptPlan(first);

    const reloaded = new MockDispatchApi({ storage });
    const snapshot = await reloaded.getSnapshot();

    expect(snapshot.decisionState).toEqual(accepted.decisionState);
    expect(snapshot.planState.activeAcceptedPlanId).toBe(accepted.planState.activeAcceptedPlanId);
    expect(snapshot.executionState).toEqual(accepted.executionState);
    expect(snapshot.demoClock.now).toBe(accepted.demoClock.now);
  });

  it("recovers to a clean S0 session when local storage is corrupt", async () => {
    const storage = new TestStorage();
    storage.setItem(MOCK_STORAGE_KEY, "not-json");

    const api = new MockDispatchApi({ storage });

    await expect(api.getSnapshot()).resolves.toMatchObject({ decisionState: { scenarioId: "S0" } });
  });

  it("rejects a structurally incomplete persisted snapshot before rendering", async () => {
    const storage = new TestStorage();
    const source = new MockDispatchApi({ storage });
    await source.optimize();
    const malformed = JSON.parse(storage.getItem(MOCK_STORAGE_KEY)!) as { snapshot: { decisionState: { events?: unknown } } };
    delete malformed.snapshot.decisionState.events;
    storage.setItem(MOCK_STORAGE_KEY, JSON.stringify(malformed));

    await expect(new MockDispatchApi({ storage }).getSnapshot()).resolves.toMatchObject({ decisionState: { scenarioId: "S0" } });
  });

  it("rejects persisted proposals from before the fuel metric contract", async () => {
    const storage = new TestStorage();
    const source = new MockDispatchApi({ storage });
    await source.optimize();
    const persisted = JSON.parse(storage.getItem(MOCK_STORAGE_KEY)!);
    delete persisted.snapshot.planState.proposedAlternatives[0].content.metrics.fuelCostVnd;
    storage.setItem(MOCK_STORAGE_KEY, JSON.stringify(persisted));

    const restored = await new MockDispatchApi({ storage }).getSnapshot();
    expect(restored.planState.proposedAlternatives).toHaveLength(0);
    expect(restored.decisionState.scenarioId).toBe("S0");
  });

  it("notifies same-tab subscribers after a mutation", async () => {
    const api = new MockDispatchApi({ storage: new TestStorage() });
    const received: string[] = [];
    const unsubscribe = api.subscribe((snapshot) => received.push(snapshot.decisionState.sessionId));

    await api.optimize();
    unsubscribe();

    expect(received).toHaveLength(1);
  });

  it("hydrates an external storage event without writing the snapshot back", async () => {
    const storage = new TestStorage();
    const receivingTab = new MockDispatchApi({ storage, eventTarget: window });
    const externalTab = new MockDispatchApi({ storage, eventTarget: { addEventListener() {}, removeEventListener() {} } });
    const acceptedOutside = await acceptPlan(externalTab);
    const writesBeforeEvent = storage.writes;

    window.dispatchEvent(new StorageEvent("storage", { key: MOCK_STORAGE_KEY, newValue: storage.getItem(MOCK_STORAGE_KEY) }));

    expect(storage.writes).toBe(writesBeforeEvent);
    await expect(receivingTab.getSnapshot()).resolves.toMatchObject({ planState: { activeAcceptedPlanId: acceptedOutside.planState.activeAcceptedPlanId } });
  });

  it("rejects a selected proposal when another tab has changed the world", async () => {
    const storage = new TestStorage();
    const dispatcherA = new MockDispatchApi({ storage });
    const proposal = (await dispatcherA.optimize()).planState.proposedAlternatives[0];
    await dispatcherA.selectAlternative(proposal.id);
    const dispatcherB = new MockDispatchApi({ storage });
    await dispatcherB.triggerFixtureEvent("S2-E1");

    await expect(dispatcherA.acceptSelectedPlan()).rejects.toMatchObject({ code: "STALE_PROPOSAL" });
  });

  it("does not accept another tab's different selection for the same world version", async () => {
    const storage = new TestStorage();
    const dispatcherA = new MockDispatchApi({ storage });
    const alternatives = (await dispatcherA.optimize()).planState.proposedAlternatives;
    await dispatcherA.selectAlternative(alternatives[0].id);
    const dispatcherB = new MockDispatchApi({ storage });
    await dispatcherB.selectAlternative(alternatives[1].id);

    await expect(dispatcherA.acceptSelectedPlan()).rejects.toMatchObject({ code: "STALE_PROPOSAL" });
  });
});
