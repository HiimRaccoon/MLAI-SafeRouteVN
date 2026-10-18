import { describe, expect, it } from "vitest";
import s1Raw from "../../../scenarios/fixtures/thu-duc-binh-thanh-v1/S1.json";
import { getFixtureScenario, listFixtureScenarios } from "./fixtureCatalog";

describe("Member 1 fixture catalog", () => {
  it("keeps the pinned S0 contract of three orders and two vehicles", () => {
    const scenario = getFixtureScenario("S0");

    expect(scenario.initialState.orders).toHaveLength(3);
    expect(scenario.initialState.vehicles).toHaveLength(2); // V1, V2
  });

  it("exposes the real fixture event payloads without applying them", () => {
    const urgent = getFixtureScenario("S2").events[0];
    const unavailable = getFixtureScenario("S3").initialState.orders.find((order) => order.id === "O001");
    const rain = getFixtureScenario("S4").events[0];

    expect(urgent).toMatchObject({ type: "URGENT_ORDER", orderPayload: { priority: 3 } });
    expect(unavailable).toMatchObject({ status: "ONBOARD", assignedVehicleId: "V1" });
    expect(rain).toMatchObject({ type: "LOCAL_RAIN_WHAT_IF", polygon: { type: "Polygon" } });
  });

  it("loads the exact Member 1 normal delivery fixture without frontend events", () => {
    const scenario = getFixtureScenario("S1");
    expect(scenario).toEqual(s1Raw);
    expect(scenario.scenarioId).toBe("S1");
    expect(scenario.description).toBe("Normal synthetic delivery day");
    expect(scenario.initialState.orders.map((order) => order.id)).toEqual([
      "O001", "O002", "O003", "O004", "O005", "O006", "O007", "O008"
    ]);
    expect(scenario.initialState.vehicles.map((vehicle) => [vehicle.id, vehicle.capacityKg])).toEqual([["V1", 15], ["V2", 15]]);
    expect(scenario.initialState.currentTime).toBe("2026-09-27T21:00:00+07:00");
    expect(scenario.events).toEqual([]);
  });

  it("lists the five supported scenarios in source order", () => {
    expect(listFixtureScenarios().map((scenario) => scenario.id)).toEqual(["S0", "S1", "S2", "S3", "S4"]);
  });
});
