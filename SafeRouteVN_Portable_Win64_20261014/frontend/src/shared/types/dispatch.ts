import type { FixtureEvent, GeoJsonPolygon, Location, Order, ScenarioId, Vehicle } from "./scenario";

export type EventStatus = "READY_TO_TRIGGER" | "TRIGGERED" | "EXPIRED";

export interface DecisionEvent {
  id: string;
  sourceScenarioId: ScenarioId;
  type: FixtureEvent["type"];
  status: EventStatus;
  fixtureEvent: FixtureEvent;
}

export interface DecisionState {
  sessionId: string;
  /** Legacy mock counter. Backend leaves this at 0; its server revision is backend.basis. */
  version: number;
  scenarioId: ScenarioId;
  orders: Order[];
  vehicles: Vehicle[];
  locations: Location[];
  context: { rain: { polygon: GeoJsonPolygon; endsAt: string } | null };
  events: DecisionEvent[];
}

export type PlanProfile = "FASTEST" | "BALANCED" | "SAFER";

export interface PlanStop {
  id: string;
  kind: "DEPOT_PICKUP" | "DELIVERY";
  orderIds: string[];
  label: string;
  location: { latitude: number; longitude: number };
}

export interface RouteSegment {
  id: string;
  fromStopId: string;
  toStopId: string;
  geometry: { type: "LineString"; coordinates: [number, number][] };
  distanceKm: number;
  durationMinutes: number;
  relativeExposure?: number;
  geometrySource?: "SCHEMATIC_DEMO" | "MEMBER2_SUPPLIED";
}

export interface SuppliedRouteAction {
  kind: string;
  order_id?: string;
  node_id?: number;
  edge_id?: string;
  geometry?: [number, number][];
  distance_m?: number;
  exposure?: number;
  load_after_kg?: number;
  start_us?: number | string;
  end_us?: number | string;
}

export interface VehiclePlan {
  vehicleId: string;
  orderedStops: PlanStop[];
  routeSegments: RouteSegment[];
  suppliedActions?: SuppliedRouteAction[];
}

export interface PlanMetrics {
  distanceKm: number;
  durationMinutes: number;
  exposureScore: number | null;
  onTimeRate: number | null;
  fuelCostVnd: number;
}

export interface ImmutablePlanContent {
  profile: PlanProfile;
  vehiclePlans: VehiclePlan[];
  metrics: PlanMetrics;
  unserved: { orderId: string; reason: string }[];
  provenance: { source: "Dữ liệu demo" | "Member 2 offline runtime"; scenarioId: ScenarioId; stateVersion: number; computedAt?: string; buildSha256?: string; integrationMode?: "LOCAL_MANUAL_ANCHOR" };
}

export interface ProposedAlternative {
  origin?: ProposalOrigin;
  id: string;
  content?: ImmutablePlanContent;
  /** Certified public forecast metadata; never substitutes a legacy mock plan or accepted world. */
  nativeForecast?: {
    source: "MEMBER3_HTTP";
    trajectory: Record<string, unknown>;
    segments: import("../components/mapScene").MapSegment[];
    metrics: ReturnType<typeof import("../../integrations/member3/metricsAdapter").adaptMetrics>;
    jobView: import("../../integrations/member3/types").M3JobView;
  };
  generatedForSessionId: string;
  generatedForStateVersion: number;
}

export type ProposalOrigin =
  | { kind: "MOCK"; sessionId: string; stateVersion: number }
  | { kind: "MEMBER3"; sessionId: string; comparisonId: string; jobId: string; profile: PlanProfile; inputBasis: import("../../integrations/member3/types").M3Basis };

export interface AcceptedPlanRecord {
  id: string;
  plan: ImmutablePlanContent;
  acceptedAt: string;
  supersededAt?: string;
}

export interface OperationalPlanAssessment {
  planId: string;
  status: "ACTIVE" | "NEEDS_REOPTIMIZATION";
  reasons: string[];
  assessedAgainstSessionId: string;
  assessedAgainstStateVersion: number;
}

export interface PlanState {
  /** Public M3 accepted trajectory; never populated by an unaccepted forecast. */
  acceptedExecution?: {
    source: "MEMBER3_HTTP";
    jobId: string;
    profile: PlanProfile;
    basis: import("../../integrations/member3/types").M3Basis;
    segments: import("../components/mapScene").MapSegment[];
    vehicleOrderIds: Record<string, string[]>;
    unserved: { orderId: string; reason: string }[];
  } | null;
  acceptedPlans: AcceptedPlanRecord[];
  activeAcceptedPlanId: string | null;
  proposedAlternatives: ProposedAlternative[];
  selectedAlternativeId: string | null;
  operationalPlanAssessment: OperationalPlanAssessment | null;
}

export interface ExecutionState {
  activePlanId: string | null;
  progressByPlanId: Record<string, Record<string, {
    currentStopId: string | null;
    completedStopIds: string[];
    completedSegmentIds: string[];
    travel?: { segmentId: string; elapsedMinutes: number };
  }>>;
}

export interface DemoEventSummary {
  id: string;
  sourceScenarioId: ScenarioId;
  type: FixtureEvent["type"];
  label: string;
  status: EventStatus;
}

export interface DispatchSnapshot {
  /** M3 transport metadata; mock snapshots keep their existing shape. */
  backend?: {
    run?: import('../../integrations/member3/types').M3Run;
    source: "MEMBER3_HTTP";
    baseUrl: string;
    executionMode: "SIMULATED_REPLAY";
    realWorldObservation: false;
    /** Server identity/revision. Bind backend proposals/jobs to every field, including string head_version and generation. */
    basis: import("../../integrations/member3/types").M3Basis;
    executionView: import("../../integrations/member3/types").M3ExecutionView;
    metrics?: ReturnType<typeof import("../../integrations/member3/metricsAdapter").executionMetrics>;
    scenarios?: import("../../integrations/member3/scenarioAdapter").ScenarioOption[];
    comparison?: import("../../integrations/member3/types").M3ComparisonView;
    jobs?: Record<string, import("../../integrations/member3/types").M3JobView>;
    capabilities?: import("../../integrations/member3/capabilities").DispatchCapabilities;
    stale?: boolean;
    mutationPending?: boolean;
    /** Confirmed historical metadata; active geometry comes from executionView. */
    acceptances?: import("../../integrations/member3/types").M3AcceptanceReceipt[];
    pendingEvents?: import("../../integrations/member3/types").M3PendingEventsView;
    replayHistory?: import("../../integrations/member3/types").M3ReplayAudit;
    needsReoptimization?: boolean;
    playback?: import("../../integrations/member3/types").M3PlaybackView;
    /** Confirmed controller command; physical world still awaits a coherent server read. */
    playbackConverging?: boolean;
    error?: { code: string; message: string };
  };
  decisionState: DecisionState;
  planState: PlanState;
  executionState: ExecutionState;
  demoClock: { now: string; label: "Thời gian demo" };
  demo: {
    playback?: { playing: boolean; speed: import('../replaySpeeds').ReplaySpeed; lastWallTime: number };
    importedFixture?: import('./scenario').ScenarioFixture;
    availableEvents: DemoEventSummary[];
    /** Demo bookkeeping: retain the one-event lock even while Urgent is OFF. Optional for legacy hydration. */
    roundEventId?: string | null;
  };
}
