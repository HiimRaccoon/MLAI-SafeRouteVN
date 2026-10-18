import { expect, it } from "vitest";
import { createSessionReference } from "./sessionReference";
const session = { session_id: "owned-one", scenario_id: "S1" as const, build_sha256: "b".repeat(64), catalog_sha256: "c".repeat(64), fixture_sha256: "f".repeat(64) };
it("persists pointer metadata only, strips physical/token fields, and isolates backend origins", () => {
  const values = new Map<string, string>(), storage = { getItem: (k: string) => values.get(k) ?? null, setItem: (k: string, v: string) => { values.set(k, v); } };
  values.set("saferoute.phase1.dispatch.v1", "mock-world");
  const ref = createSessionReference({ baseUrl: "http://localhost:8004", storage });
  ref.write({ schemaVersion: 1, session, world: { orders: ["invented"] }, token: "secret" } as Parameters<typeof ref.write>[0]);
  expect(JSON.parse(values.get(ref.key)!)).toEqual({ schemaVersion: 1, session });
  expect(createSessionReference({ baseUrl: "http://localhost:8005", storage }).read()).toBeNull();
  expect(values.get("saferoute.phase1.dispatch.v1")).toBe("mock-world");
});
it("receives reference notifications without writing back or observing other origins", () => {
  const values = new Map<string, string>(); let writes = 0, notify: ((key: string | null) => void) | undefined;
  const storage = { getItem: (k: string) => values.get(k) ?? null, setItem: (k: string, v: string) => { writes++; values.set(k, v); } };
  const ref = createSessionReference({ baseUrl: "http://localhost:8004", storage, subscribeChanges: callback => { notify = callback; return () => { notify = undefined; }; } });
  const seen: unknown[] = [], stop = ref.subscribe(p => seen.push(p));
  ref.write({ schemaVersion: 1, session }); notify!("other-origin"); expect(seen).toEqual([]);
  notify!(ref.key); expect(seen).toEqual([{ schemaVersion: 1, session }]); expect(writes).toBe(1);
  stop(); expect(notify).toBeUndefined();
});
