import type { M3Catalog } from "./types";
import type { ScenarioId } from "../../shared/types/scenario";
import { Member3Error } from "./errors";
export interface ScenarioOption { id: ScenarioId; orderCount: number; vehicleCount: number; initialTime: string; fixtureSha256: string }
export function adaptScenarioCatalog(catalog: M3Catalog): ScenarioOption[] {
  const ids = new Set<string>();
  return catalog.scenarios.flatMap(s => {
    if (ids.has(s.scenario_id) || !Number.isInteger(s.order_count) || s.order_count < 0 || !Number.isInteger(s.vehicle_count) || s.vehicle_count < 0 ||
        typeof s.initial_time !== "string" || !Number.isFinite(Date.parse(s.initial_time)) || !/^[a-f0-9]{64}$/.test(s.fixture_sha256)) throw new Member3Error("INVALID_RESPONSE", "Invalid M3 scenario catalog metadata.");
    ids.add(s.scenario_id);
    return /^S[0-4]$/.test(s.scenario_id) ? [{ id: s.scenario_id as ScenarioId, orderCount: s.order_count, vehicleCount: s.vehicle_count, initialTime: s.initial_time, fixtureSha256: s.fixture_sha256 }] : [];
  });
}
