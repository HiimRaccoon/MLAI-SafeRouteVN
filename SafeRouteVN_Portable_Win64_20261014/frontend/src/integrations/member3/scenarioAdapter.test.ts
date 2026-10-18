import { expect, it } from "vitest";
import { adaptScenarioCatalog } from "./scenarioAdapter";
import type { M3Catalog } from "./types";
it("scenario_selector_uses_server_catalog", () => {
  const catalog: M3Catalog = { schema_version: "saferoute-m3-scenario-catalog/1", catalog_sha256: "a".repeat(64), execution_mode: "SIMULATED_REPLAY", real_world_observation: false,
    scenarios: ["S1", "S5"].map(id => ({ scenario_id: id as "S1" | "S5", order_count: 17, vehicle_count: 3, initial_time: "2026-09-27T21:00:00+07:00", fixture_sha256: "b".repeat(64) })) };
  expect(adaptScenarioCatalog(catalog)).toEqual([{ id: "S1", orderCount: 17, vehicleCount: 3, initialTime: "2026-09-27T21:00:00+07:00", fixtureSha256: "b".repeat(64) }]);
  expect(() => adaptScenarioCatalog({ ...catalog, scenarios: [{ ...catalog.scenarios[0], order_count: -1 }] })).toThrow();
});
