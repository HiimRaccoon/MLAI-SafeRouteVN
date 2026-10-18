import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parseExecutionView } from "./executionViewAdapter";
import { parseDecisionResult } from "./decisionResultAdapter";
import { decisionAlternativeSegments, executionForecastSegments } from "./geometryAdapter";

const proposedCoordinates = [[106.71, 10.8], [106.712, 10.801], [106.714, 10.803], [106.72, 10.81]];

function decision() {
  return {
    schema_version: "1.0.0", scenario_id: "S3", context_version: "test-context", state_version: "test-state",
    diagnostics: [], provenance: { source: "contract test" },
    alternatives: ["FASTEST", "BALANCED", "SAFER"].map((profile) => ({
      profile, vehicle_assignments: { V1: ["O001"] }, stop_sequence: { V1: ["O001"] },
      route_geometry: { V1: proposedCoordinates }, metrics: {}, explanation_facts: []
    }))
  };
}

describe("Member 2 geometry projection", () => {
  it.each(["S2", "S3", "S4"])("preserves every point and EDGE action order from %s", (scenario) => {
    const raw = JSON.parse(readFileSync(resolve(process.cwd(), `src/integrations/member2/fixtures/${scenario}_execution_view.json`), "utf8"));
    const view = parseExecutionView(raw);
    const segments = executionForecastSegments(view);
    const sourceEdges = raw.accepted_trajectory.vehicle_routes.flatMap((route: { vehicle_id: string; actions: Array<{ kind: string; geometry?: number[][] }> }) =>
      route.actions.filter((action) => action.kind === "EDGE").map((action) => ({ vehicleId: route.vehicle_id, coordinates: action.geometry })));
    expect(segments.map((segment) => ({ vehicleId: segment.vehicleId, coordinates: segment.coordinates }))).toEqual(sourceEdges);
    expect(segments.every((segment) => !segment.completed && segment.geometrySource === "MEMBER2_SUPPLIED")).toBe(true);
    expect(segments.some((segment) => segment.coordinates.length > 2)).toBe(true);
  });

  it("keeps a proposed full polyline independent of accepted EDGE geometry", () => {
    const raw = JSON.parse(readFileSync(resolve(process.cwd(), "src/integrations/member2/fixtures/S3_execution_view.json"), "utf8"));
    const accepted = executionForecastSegments(parseExecutionView(raw));
    const proposed = decisionAlternativeSegments(parseDecisionResult(decision()).alternatives[0]);
    expect(proposed).toHaveLength(1);
    expect(proposed[0].coordinates).toEqual([[106.71, 10.8], [106.712, 10.801], [106.714, 10.803], [106.72, 10.81]]);
    expect(proposed[0].geometrySource).toBe("MEMBER2_SUPPLIED");
    expect(accepted[0].coordinates).toEqual(raw.accepted_trajectory.vehicle_routes[0].actions[0].geometry);
    expect(proposed[0].coordinates).not.toEqual(accepted[0].coordinates);
  });

  it("rejects absent route geometry instead of generating connections from stops", () => {
    const raw = decision();
    Reflect.deleteProperty(raw.alternatives[0].route_geometry, "V1");
    expect(() => parseDecisionResult(raw)).toThrow(/geometry/i);
  });
});
