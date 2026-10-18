import type { Member2DecisionAlternative, Member2DecisionResult, Member2Profile } from "./types";
import { line, list, record, string, stringList } from "./validation";

const PROFILES: Member2Profile[] = ["FASTEST", "BALANCED", "SAFER"];

function stringMap(value: unknown, path: string): Record<string, string[]> {
  return Object.fromEntries(Object.entries(record(value, path)).map(([vehicleId, orders]) => [
    string(vehicleId, `${path}.vehicle_id`),
    stringList(orders, `${path}.${vehicleId}`)
  ]));
}

function alternative(value: unknown, index: number): Member2DecisionAlternative {
  const raw = record(value, `alternatives[${index}]`);
  if (raw.profile !== PROFILES[index]) throw new Error("Alternatives must be FASTEST, BALANCED, SAFER in order");
  const vehicleAssignments = stringMap(raw.vehicle_assignments, "vehicle_assignments");
  const stopSequence = stringMap(raw.stop_sequence, "stop_sequence");
  const geometry = record(raw.route_geometry, "route_geometry");
  const routeGeometry = Object.fromEntries(Object.entries(geometry).map(([vehicleId, coordinates]) => [
    vehicleId,
    { type: "LineString" as const, coordinates: line(coordinates, `route_geometry.${vehicleId}`) }
  ]));
  for (const vehicleId of Object.keys(vehicleAssignments)) {
    if (!stopSequence[vehicleId] || !routeGeometry[vehicleId]) throw new Error(`Missing stop or route geometry for ${vehicleId}`);
  }
  const assigned = Object.values(vehicleAssignments).flat();
  if (new Set(assigned).size !== assigned.length) throw new Error("An order cannot be assigned twice");
  return {
    profile: raw.profile as Member2Profile,
    vehicleAssignments,
    stopSequence,
    routeGeometry,
    metrics: { ...record(raw.metrics, "metrics") },
    explanationFacts: [...list(raw.explanation_facts, "explanation_facts")]
  };
}

export function parseDecisionResult(value: unknown): Member2DecisionResult {
  const raw = record(value, "DecisionResult");
  if (raw.schema_version !== "1.0.0") throw new Error("Unsupported DecisionResult schema_version");
  const alternatives = list(raw.alternatives, "alternatives");
  if (alternatives.length !== 3) throw new Error("Alternatives must be FASTEST, BALANCED, SAFER in order");
  const knownKeys = new Set(["schema_version", "scenario_id", "context_version", "state_version", "alternatives", "diagnostics", "provenance"]);
  return {
    schemaVersion: "1.0.0",
    scenarioId: string(raw.scenario_id, "scenario_id"),
    contextVersion: string(raw.context_version, "context_version"),
    stateVersion: string(raw.state_version, "state_version"),
    alternatives: [alternative(alternatives[0], 0), alternative(alternatives[1], 1), alternative(alternatives[2], 2)],
    diagnostics: [...list(raw.diagnostics, "diagnostics")],
    provenance: { ...record(raw.provenance, "provenance") },
    unitMetadata: Object.fromEntries(Object.entries(raw).filter(([key]) => !knownKeys.has(key)))
  };
}
