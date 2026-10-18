export type Member2Profile = "FASTEST" | "BALANCED" | "SAFER";
export type Wgs84Point = [longitude: number, latitude: number];

export interface GeoJsonLineString {
  type: "LineString";
  coordinates: Wgs84Point[];
}

export interface Member2DecisionAlternative {
  profile: Member2Profile;
  vehicleAssignments: Record<string, string[]>;
  stopSequence: Record<string, string[]>;
  routeGeometry: Record<string, GeoJsonLineString>;
  /** AlternativeMetrics is defined by Member 2 common.py, outside this handoff subset. */
  metrics: Record<string, unknown>;
  explanationFacts: unknown[];
}

export interface Member2DecisionResult {
  schemaVersion: "1.0.0";
  scenarioId: string;
  contextVersion: string;
  stateVersion: string;
  alternatives: [Member2DecisionAlternative, Member2DecisionAlternative, Member2DecisionAlternative];
  diagnostics: unknown[];
  provenance: Record<string, unknown>;
  /** UnitMetadata fields are preserved from the envelope until common.py is handed off. */
  unitMetadata: Record<string, unknown>;
}

export interface GeoJsonFeatureCollection {
  type: "FeatureCollection";
  features: Array<{
    type: "Feature";
    geometry: GeoJsonLineString;
    properties: {
      vehicleId: string;
      edgeId: string;
      scope: "FORECAST_SOURCE_EDGE_GEOMETRY";
      simulation: true;
      fractionStart: number;
    };
  }>;
}

export interface Member2ExecutionVehicle {
  vehicleId: string;
  availability: string;
  capacityKg: number;
  currentLoadKg: number;
  onboardOrderIds: string[];
  remainingRangeM: number;
  position: { coordinates: Wgs84Point; kind: string; source: "SIMULATED" };
  plannedSuffix: string[];
  activity: string;
}

export interface Member2ExecutionView {
  schemaVersion: "task02-m2-execution-view/2";
  simulatedReplay: true;
  currentTime: string;
  basis: Record<string, unknown>;
  orderIds: string[];
  deliveredPrefix: string[];
  plannedServedSuffix: string[];
  unserved: Array<{ orderId: string; reason: string; ownerVehicleId: string | null }>;
  observedMetrics: Record<string, number> | null;
  plannedSuffixMetrics: Record<string, number> | null;
  projectedWholeMetrics: Record<string, number> | null;
  vehicles: Member2ExecutionVehicle[];
  activeJobId: string | null;
  pendingEventIds: string[];
  forecastEdges: GeoJsonFeatureCollection;
  /** Public accepted trajectory is kept as supplied; only EDGE geometry enters forecastEdges. */
  acceptedTrajectory: Record<string, unknown> | null;
}
