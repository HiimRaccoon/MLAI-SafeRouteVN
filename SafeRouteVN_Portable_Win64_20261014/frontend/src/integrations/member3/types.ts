export type M3ScenarioId = "S0" | "S1" | "S2" | "S3" | "S4" | "S5" | "S6" | "S7" | "S8";
export interface M3Diagnostic { severity: "ERROR" | "WARNING"; code: string; path: string; message: string }
export interface M3Envelope<T> {
  schema_version: "saferoute-m3-http-response/1";
  request_id: string;
  status: "OK" | "ERROR";
  data: T | null;
  diagnostics: M3Diagnostic[];
}
export interface M3Basis {
  session_id: string;
  head_version: string;
  generation: string;
  build_sha256: string;
  root_sha256: string;
  head_sha256: string;
  source_sha256: string;
  context_version: string;
  overlay_sha256: string | null;
}
export interface M3WorldView {
  schema_version: string;
  basis: M3Basis;
  current_time: string;
  execution_mode: "SIMULATED_REPLAY";
  real_world_observation: false;
}
export type M3EventType = "URGENT_ORDER" | "VEHICLE_UNAVAILABLE" | "LOCAL_RAIN_WHAT_IF";
export interface M3PendingEvent { event_id: string; event_type: M3EventType; timestamp: string; apply_allowed: boolean }
export interface M3PendingEventsView extends M3WorldView { schema_version: "saferoute-m3-pending-events/1"; events: M3PendingEvent[] }
export interface M3ReplayReceipt {
  schema_version: "saferoute-m3-replay-receipt/1"; mutation_id: string; session_id: string;
  operation: "advance" | "apply_event" | "pause" | "reset" | "deliver"; status: "ADVANCED" | "NOOP" | "APPLIED" | "PAUSED" | "RESET" | "DISPATCHED";
  vehicle_id?: string; order_id?: string;
  source: "M2_PUBLIC_SDK" | "M3_MANUAL_CONTROL" | "M3_NEW_SESSION";
  input_basis: M3Basis; basis: M3Basis; recorded_at: string; links: { state: string; history: string };
  target_time?: string; event_id?: string; event_type?: M3EventType; event_sha256?: string;
  mode?: "STEP"; paused?: true; new_session_id?: string; new_session?: M3Session;
}
export interface M3ReplayAudit { schema_version: "saferoute-m3-replay-history/1"; session_id: string; history: M3ReplayReceipt[] }
export interface M3ReplayMutationView { schema_version: "saferoute-m3-replay-view/1"; receipt: M3ReplayReceipt; execution_view: M3ExecutionView }
export interface ApplyEventRequest { request_id: string; expected_revision: { head_version: string; generation: string } }
export interface ReplayStepRequest extends ApplyEventRequest { target_time?: string | null }
export type PlaybackSpeed = import('../../shared/replaySpeeds').ReplaySpeed;
export interface StartPlaybackRequest extends ApplyEventRequest { speed: PlaybackSpeed }
export interface PlaybackSpeedRequest { request_id: string; speed: PlaybackSpeed }
export interface M3PlaybackState {
  auto_deliver?: boolean;
  mode: "STEP" | "AUTOMATIC"; paused: boolean; fully_paused: boolean; in_flight: boolean;
  reason: string; speed: PlaybackSpeed; controller_revision: string; next_tick_at: string | null; end_time: string | null;
  step_seconds: number; cadence: "DISCRETE_BEST_EFFORT"; base_tick_interval_seconds: 1;
  catch_up: false; auto_apply_event: false; auto_accept_plan: false; execution_mode: "SIMULATED_REPLAY"; real_world_observation: false;
}
export interface M3PlaybackView extends M3PlaybackState { schema_version: "saferoute-m3-playback-controller/1"; execution_view: M3ExecutionView }
export interface M3PlaybackControl {
  schema_version: "saferoute-m3-playback-control/1"; controller: M3PlaybackState;
  receipt: { schema_version: "saferoute-m3-playback-receipt/1"; receipt_id: string; session_id: string; operation: "start" | "speed" | "pause" | "auto_deliver"; source: "M3_PLAYBACK_CONTROL"; recorded_at: string; request_id: string; actor_id: string; controller: Omit<M3PlaybackState, "step_seconds" | "cadence" | "base_tick_interval_seconds" | "catch_up" | "auto_apply_event" | "auto_accept_plan" | "execution_mode" | "real_world_observation"> };
}
export interface M3ReplayReset { schema_version: "saferoute-m3-replay-reset/1"; source_session_id: string; session: M3Session; receipt: M3ReplayReceipt; execution_view: M3ExecutionView }
export interface M3Vehicle {
  awaiting_delivery?: string | null;
  vehicle_id: string;
  availability: "AVAILABLE" | "UNAVAILABLE";
  capacity_kg: number;
  current_load_kg: number;
  onboard_order_ids: string[];
  remaining_range_m: number;
  position: { kind: string; coordinates: [number, number]; node_id?: number | string; position_source?: "SIMULATED" };
  position_timestamp?: string;
  planned_suffix?: string[];
  activity?: string;
}
export interface M3ExecutionView extends M3WorldView {
  delivery_day?: DeliveryDay;
  delivery_barrier?: string | null;
  schema_version: "task02-m2-execution-view/2";
  order_ids: string[];
  delivered_prefix: string[];
  planned_served_suffix: string[];
  vehicles: M3Vehicle[];
  unserved: Array<{ order_id: string; reason: string; owner_vehicle_id?: string | null }>;
  pending_event_ids: string[];
  accepted_trajectory: Record<string, unknown> | null;
  active_job_id: string | null;
  observed_metrics: Record<string, number | string> | null;
  planned_suffix_metrics: Record<string, number> | null;
  projected_whole_metrics: Record<string, number> | null;
  metric_scope: "OBSERVED_PREFIX_ONLY";
}

export interface DeliveryDay {
  date: string; cutoff: string; complete: boolean; recovery_minutes: number;
  catalog: Record<string, {pickup_location_id:string; graph_node_id:number; coordinates:[number,number]}>;
  customers: Record<string,{name:string;phone:string;arrived_at:string}>;
  depots: Array<{location_id:string;coordinates:[number,number]}>;
  queue: Record<string,{date:string;reason:string;depot_id:string}>;
  recoveries: Array<{order_id:string;vehicle_id:string;depot_id:string;ready_at:string;status:string}>;
  before_event: Record<string,number> | null;
  confirmations?: Record<string,string>;
  pending_events: Array<{event_id:string;timestamp:string}>;
}
export interface M3Projection extends M3WorldView {
  units: { distance: "m"; duration: "s"; mass: "kg"; money: "VND" };
}
export interface M3OrdersView extends M3Projection {
  schema_version: "saferoute-m3-orders-view/1";
  orders: Array<{ display?: Pick<import('../../shared/types/scenario').Order,'customerName'|'customerPhone'|'address'|'notes'>; order_id: string; status: "WAITING" | "ONBOARD" | "DELIVERED"; owner_vehicle_id: string | null;
    planned_in_accepted_suffix: boolean; unserved_reason: string | null; demand_kg: number; priority: number;
    pickup_location_id: string; delivery_region_id: string; graph_node_id: string; coordinates: [number, number];
    service_time_s: number; earliest: string; preferred_due: string; hard_deadline: string }>;
}
export interface M3VehiclesView extends M3Projection {
  schema_version: "saferoute-m3-vehicles-view/1";
  vehicles: M3Vehicle[];
  vehicle_metadata: Array<{ display?: Pick<import('../../shared/types/scenario').Vehicle,'driverName'|'driverPhone'|'licensePlate'|'vehicleModel'>; vehicle_id: string; vehicle_type: string; cost_per_km_vnd: number; range_m: number; working_start: string; working_end: string }>;
}
export interface M3LocationsView extends M3Projection {
  schema_version: "saferoute-m3-locations-view/1";
  rain?: { polygon: import('../../shared/types/scenario').GeoJsonPolygon; ends_at: string } | null;
  locations: Array<{ location_id: string; kind: "DEPOT" | "DELIVERY"; order_id?: string; graph_node_id: string;
    coordinates: [number, number]; opening_time?: string; closing_time?: string }>;
}
export interface M3Session { session_id: string; scenario_id: M3ScenarioId; build_sha256: string; catalog_sha256: string; fixture_sha256: string; run_id?: string }
export interface M3Run {
  source_kind?: 'WEATHER_RUN' | 'IMPORTED_SCENARIO'; name?: string;
  run_id: string; scenario_id: M3ScenarioId; status: 'BUILDING'|'READY'|'FAILED'|'INTERRUPTED'; message: string;
  epoch: string; manifest_sha256: string|null; fixture_sha256?: string; online_weather_enabled: boolean;
  weather?: { fetched_at: string; valid_at: string; expires_at: string; provider: string; region_count: number; fallback_used: boolean };
  traffic_source?: string; temporal_model?: string;
}

export interface M3Explanations {
  schema_version: 'saferoute-explain/1'; session_id: string; comparison_id: string; basis: M3Basis;
  model: string; status: 'WAITING'|'PENDING'|'READY'|'FALLBACK'; message: string; limitations: string;
  profiles: Partial<Record<import('../../shared/types/dispatch').PlanProfile, {sentences: string[]; fact_ids: string[]}>>;
}
export interface M3LoadedSession { schema_version: "saferoute-m3-loaded-session/1"; session: M3Session; execution_view: M3ExecutionView }
export interface M3Catalog {
  schema_version: "saferoute-m3-scenario-catalog/1";
  catalog_sha256: string;
  execution_mode: "SIMULATED_REPLAY";
  real_world_observation: false;
  scenarios: Array<{ scenario_id: M3ScenarioId; fixture_sha256: string; initial_time: string; order_count: number; vehicle_count: number }>;
}
export interface M3Capabilities { schema_version: "task02-m2-runtime-capabilities/1"; build_sha256: string; [key: string]: unknown }
export interface M3Ready { ready: boolean; checks: Record<string, string>; execution_mode: "SIMULATED_REPLAY" }

export interface AcceptPlanRequest { request_id: string; expected_revision: { head_version: string; generation: string } }
export interface M3AcceptanceReceipt {
  schema_version: "saferoute-m3-plan-acceptance/1";
  acceptance_id: string; session_id: string; job_id: string; status: "ACCEPTED";
  input_basis: M3Basis; basis: M3Basis; recorded_at: string; links: { state: string; job: string };
}
export interface M3AcceptanceView { schema_version: "saferoute-m3-acceptance-view/1"; receipt: M3AcceptanceReceipt; execution_view: M3ExecutionView }
export interface M3AcceptanceAudit { schema_version: "saferoute-m3-acceptance-audit/1"; session_id: string; acceptances: M3AcceptanceReceipt[] }
export type M3JobStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
export type ComparisonStatus = "QUEUED" | "RUNNING" | "CANCEL_REQUESTED" | "COMPLETED" | "FAILED" | "CANCELLED";
export interface M3JobView {
  schema_version: "task02-m2-runtime-job-view/1"; job_id: string; job_status: M3JobStatus; input_basis: M3Basis;
  business_status: string | null; internal_status: string | null; diagnostics: Array<{ code: string; path: string; message: string; severity?: string }>;
  validation: { status: string; valid: boolean | null; validator_version?: string };
  coverage_evaluated: boolean; served_orders: string[]; unserved_orders: Array<{ display?: Pick<import('../../shared/types/scenario').Order,'customerName'|'customerPhone'|'address'|'notes'>; order_id: string; reason: string }>;
  plan_available: boolean; execution_view_required: true; public_api_v1_dynamic_plan_available: false;
}
export interface M3JobForecast {
  schema_version: "task02-m2-job-forecast/1";
  session_id: string;
  job_id: string;
  profile: import("../../shared/types/dispatch").PlanProfile;
  input_basis: M3Basis;
  build_sha256: string;
  job_view: M3JobView;
  trajectory: Record<string, unknown> | null;
  metrics: Record<string, number> | null;
  units: { distance: "m"; duration: "s"; action_time: "us"; cost: "VND"; mass: "kg"; geometry_crs: "WGS84"; geometry_order: "longitude_latitude"; exposure: "PROXY" };
  metric_scope: "FORECAST_ONLY";
  execution_mode: "SIMULATED_REPLAY";
  real_world_observation: false;
}
export interface M3ComparisonReceipt {
  schema_version: "saferoute-m3-profile-submission/1"; session_id: string; comparison_id: string;
  mode: "NEW_BATCH" | "EXISTING_JOBS"; input_basis: M3Basis; profiles: import("../../shared/types/dispatch").PlanProfile[];
  links: { poll: string; cancel: string };
}
export interface M3ComparisonView {
  schema_version: "saferoute-m3-profile-comparison/1"; session_id: string; comparison_id: string;
  mode: "NEW_BATCH" | "EXISTING_JOBS"; status: ComparisonStatus; input_basis: M3Basis;
  jobs: Array<{ profile: import("../../shared/types/dispatch").PlanProfile; job_id: string | null; view: M3JobView | null }>;
  outcome: { reason: string | null; comparison: {
    schema_version: "task02-m2-runtime-comparison/1"; status: "COMPARABLE" | "NON_COMPARABLE"; reason: string | null;
    jobs: Array<{ job_id: string; profile: import("../../shared/types/dispatch").PlanProfile; basis: M3Basis; domain_sha256: string; metrics: Record<string, number> }>;
  } | null } | null;
  links: { poll: string; cancel: string }; execution_mode: "SIMULATED_REPLAY"; real_world_observation: false;
  metric_scope: "FORECAST_ONLY"; exposure_is_proxy: true;
}
export interface CompareProfilesRequest { request_id: string; expected_revision: { head_version: string; generation: string } }
export interface M3ComparisonCancellation { schema_version: "saferoute-m3-profile-cancellation/1"; session_id: string; comparison_id: string; status: "CANCEL_REQUESTED" | "COMPLETED_IMMUTABLE"; affects_existing_jobs: false }
