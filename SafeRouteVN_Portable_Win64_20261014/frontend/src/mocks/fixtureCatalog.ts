import s0Raw from "../../../scenarios/fixtures/thu-duc-binh-thanh-v1/S0.json";
import s1Raw from "../../../scenarios/fixtures/thu-duc-binh-thanh-v1/S1.json";
import s2Raw from "../../../scenarios/fixtures/thu-duc-binh-thanh-v1/S2.json";
import s3Raw from "../../../scenarios/fixtures/thu-duc-binh-thanh-v1/S3.json";
import s4Raw from "../../../scenarios/fixtures/thu-duc-binh-thanh-v1/S4.json";
import type { ScenarioFixture, ScenarioId } from "../shared/types/scenario";

export interface FixtureScenarioSummary {
  id: ScenarioId;
  label: string;
  fixture: ScenarioFixture;
}

const s0Fixture: ScenarioFixture = {
  ...(s0Raw as unknown as ScenarioFixture),
  initialState: {
    ...(s0Raw as unknown as ScenarioFixture).initialState,
    vehicles: (s0Raw as unknown as ScenarioFixture).initialState.vehicles.filter(
      (v) => v.id === "V1" || v.id === "V2"
    )
  }
};

const catalog: readonly FixtureScenarioSummary[] = [
  { id: "S0", label: "S0 — Kế hoạch ban đầu", fixture: s0Fixture },
  { id: "S1", label: "S1 — Normal Delivery Day", fixture: s1Raw as ScenarioFixture },
  { id: "S2", label: "S2 — Đơn hàng khẩn", fixture: s2Raw as ScenarioFixture },
  { id: "S3", label: "S3 — Xe không khả dụng", fixture: s3Raw as ScenarioFixture },
  { id: "S4", label: "S4 — Mưa cục bộ", fixture: s4Raw as ScenarioFixture }
];

export function listFixtureScenarios(): readonly FixtureScenarioSummary[] {
  return catalog;
}

export function getFixtureScenario(id: ScenarioId): ScenarioFixture {
  const scenario = catalog.find((item) => item.id === id);

  if (!scenario) {
    throw new Error(`Không tìm thấy fixture ${id}.`);
  }

  return scenario.fixture;
}
