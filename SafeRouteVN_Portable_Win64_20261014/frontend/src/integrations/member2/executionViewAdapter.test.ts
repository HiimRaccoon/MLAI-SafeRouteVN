import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parseExecutionView } from "./executionViewAdapter";

function sample(id: "S2" | "S3" | "S4"): Record<string, unknown> {
  return JSON.parse(readFileSync(resolve(process.cwd(), `src/integrations/member2/fixtures/${id}_execution_view.json`), "utf8"));
}

describe("Member 2 public execution-view/2 adapter", () => {
  it.each(["S2", "S3", "S4"] as const)("reads %s as simulated replay with distinct metric scopes", (id) => {
    const view = parseExecutionView(sample(id));
    expect(view.schemaVersion).toBe("task02-m2-execution-view/2");
    expect(view.simulatedReplay).toBe(true);
    expect(view.currentTime).toMatch(/\+07:00$/);
    expect(view.observedMetrics).not.toBe(view.plannedSuffixMetrics);
    expect(view.projectedWholeMetrics).not.toBe(view.observedMetrics);
    expect(view.orderIds.sort()).toEqual([...new Set([...view.deliveredPrefix, ...view.plannedServedSuffix, ...view.unserved.map((u) => u.orderId)])].sort());
    expect(view.vehicles.every((v) => v.position.source === "SIMULATED")).toBe(true);
  });

  it("keeps S3 onboard custody with immobilized V1 and unserved recovery", () => {
    const view = parseExecutionView(sample("S3"));
    const v1 = view.vehicles.find((vehicle) => vehicle.vehicleId === "V1");
    expect(v1?.availability).toBe("UNAVAILABLE");
    expect(v1?.onboardOrderIds).toContain("O001");
    expect(view.unserved).toContainEqual({ orderId: "O001", ownerVehicleId: "V1", reason: "CUSTODY_BLOCKED" });
    expect(view.vehicles.find((vehicle) => vehicle.vehicleId === "V2")?.onboardOrderIds).not.toContain("O001");
  });

  it("renders geometry only for EDGE actions, preserving exact WGS84 points", () => {
    const raw = sample("S3");
    const view = parseExecutionView(raw);
    const routes = (raw.accepted_trajectory as { vehicle_routes: Array<{ vehicle_id: string; actions: Array<{ kind: string; geometry?: number[][] }> }> }).vehicle_routes;
    const edges = routes.flatMap((route) => route.actions.filter((action) => action.kind === "EDGE"));
    expect(view.forecastEdges.features).toHaveLength(edges.length);
    expect(view.forecastEdges.features[0].geometry.coordinates).toEqual(edges[0].geometry);
    expect(view.forecastEdges.features[0].properties.simulation).toBe(true);
  });

  it("rejects false live claims, malformed geometry, and unsupported versions", () => {
    const live = sample("S2");
    live.real_world_observation = true;
    expect(() => parseExecutionView(live)).toThrow(/observation/i);
    const badVersion = sample("S2");
    badVersion.schema_version = "task02-m2-execution-view/3";
    expect(() => parseExecutionView(badVersion)).toThrow(/schema/i);
    const badGeometry = sample("S2");
    const routes = (badGeometry.accepted_trajectory as { vehicle_routes: Array<{ actions: Array<{ kind: string; geometry?: number[][] }> }> }).vehicle_routes;
    const edge = routes[0].actions.find((action) => action.kind === "EDGE");
    if (!edge) throw new Error("Fixture missing EDGE action");
    edge.geometry = [[999, 10], [106, 10]];
    expect(() => parseExecutionView(badGeometry)).toThrow(/geometry/i);
  });

  it("keeps absent metric scopes distinct from an explicit zero and rejects unsafe integer transport", () => {
    const raw = sample("S2");
    raw.observed_metrics = null;
    raw.planned_suffix_metrics = { total_distance_m: 0 };
    const parsed = parseExecutionView(raw);
    expect(parsed.observedMetrics).toBeNull();
    expect(parsed.plannedSuffixMetrics?.total_distance_m).toBe(0);
    const unsafe = sample("S2");
    (unsafe.basis as Record<string, unknown>).head_version = Number.MAX_SAFE_INTEGER + 1;
    expect(() => parseExecutionView(unsafe)).toThrow(/unsafe integer/i);
  });
});
