import { describe, expect, it } from "vitest";
import { parseDecisionResult } from "./decisionResultAdapter";

const alternative = (profile: string) => ({
  profile,
  vehicle_assignments: { V1: ["O001"], V2: ["O002"] },
  stop_sequence: { V1: ["O001"], V2: ["O002"] },
  route_geometry: {
    V1: [[106.71, 10.8], [106.72, 10.81]],
    V2: [[106.72, 10.8], [106.73, 10.81]]
  },
  metrics: { example_metric: 12 },
  explanation_facts: []
});

const result = () => ({
  schema_version: "1.0.0",
  scenario_id: "S0",
  context_version: "c1",
  state_version: "s1",
  alternatives: [alternative("FASTEST"), alternative("BALANCED"), alternative("SAFER")],
  diagnostics: [],
  provenance: { source: "test" }
});

describe("Member 2 DecisionResult adapter", () => {
  it("preserves the three public profile alternatives and opaque metrics", () => {
    const parsed = parseDecisionResult({ ...result(), distance_unit: "m", coordinate_order: "longitude_latitude" });

    expect(parsed.alternatives.map((item) => item.profile)).toEqual(["FASTEST", "BALANCED", "SAFER"]);
    expect(parsed.alternatives[0].vehicleAssignments.V1).toEqual(["O001"]);
    expect(parsed.alternatives[0].routeGeometry.V1.coordinates[0]).toEqual([106.71, 10.8]);
    expect(parsed.alternatives[0].metrics).toEqual({ example_metric: 12 });
    expect(parsed.unitMetadata).toEqual({ distance_unit: "m", coordinate_order: "longitude_latitude" });
  });

  it("rejects missing or reordered profiles", () => {
    const input = result();
    input.alternatives = [alternative("BALANCED"), alternative("FASTEST"), alternative("SAFER")];

    expect(() => parseDecisionResult(input)).toThrow(/FASTEST.*BALANCED.*SAFER/);
  });

  it("rejects invalid WGS84 coordinates", () => {
    const input = result();
    input.alternatives[0].route_geometry.V1[0] = [10.8, 200];

    expect(() => parseDecisionResult(input)).toThrow(/geometry/i);
  });
});
