import { expect, it } from "vitest";
import { createPendingCommandStore } from "./requestId";
it("retains immutable command ID/body across retry and refresh", () => {
  const values = new Map<string, string>();
  const storage = { getItem: (k: string) => values.get(k) ?? null, setItem: (k: string, v: string) => { values.set(k, v); } };
  const store = createPendingCommandStore(storage, "origin");
  const intent = { sessionId: "session-test", operation: "compare", body: { expected_revision: { head_version: "9007199254740993", generation: "0" } } };
  const first = store.begin(intent);
  expect(store.begin(intent)).toEqual(first);
  const refreshed = createPendingCommandStore(storage, "origin");
  expect(refreshed.read()).toEqual(first);
  expect(() => refreshed.begin({ ...intent, body: {} })).toThrow();
  refreshed.complete("different-id"); expect(refreshed.read()).toEqual(first);
  refreshed.complete(first.requestId); expect(refreshed.read()).toBeNull();
  expect(refreshed.begin(intent).requestId).not.toBe(first.requestId);
});
