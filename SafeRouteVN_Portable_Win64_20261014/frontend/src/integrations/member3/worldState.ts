import {orderDisplay,vehicleDisplay} from '../../shared/displayMetadata';
import { finite, list, point, record, string, stringList } from "../member2/validation";
import type { DispatchSnapshot } from "../../shared/types/dispatch";
import type { Location, Order, ScenarioId, Vehicle } from "../../shared/types/scenario";
import { Member3Error } from "./errors";
import { adaptAcceptedExecution } from "./executionViewAdapter";
import { executionMetrics } from "./metricsAdapter";
import type { M3Basis, M3ExecutionView, M3LocationsView, M3OrdersView, M3Session, M3VehiclesView } from "./types";

function timestamp(value: unknown): string {
  const result = string(value, "timestamp");
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?\+07:00$/.test(result) || Number.isNaN(Date.parse(result))) throw new Error("Invalid timestamp");
  return result;
}
function nodeId(value: unknown): string | number {
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return value;
  if (typeof value === "string" && /^(0|[1-9]\d*)$/.test(value)) return value;
  throw new Error("Invalid graph node ID");
}
function sameIds(actual: string[], expected: string[]) {
  if (new Set(actual).size !== actual.length || actual.length !== expected.length || actual.some((id) => !expected.includes(id))) throw new Error("ID coverage mismatch");
}
function comparableBasis(value: unknown): M3Basis {
  const basis = record(value, "basis");
  for (const field of ["session_id", "build_sha256", "root_sha256", "head_sha256", "source_sha256", "context_version"]) string(basis[field], field);
  for (const field of ["head_version", "generation"]) {
    if (typeof basis[field] !== "string" || !/^(0|[1-9]\d*)$/.test(basis[field] as string) || BigInt(basis[field] as string) >= (1n << 63n)) throw new Error("Invalid revision");
  }
  if (basis.overlay_sha256 !== null) string(basis.overlay_sha256, "overlay_sha256");
  return basis as unknown as M3Basis;
}

function execution(value: M3ExecutionView) {
  const raw = record(value, "execution view");
  if (raw.schema_version !== "task02-m2-execution-view/2" || raw.execution_mode !== "SIMULATED_REPLAY" ||
      raw.real_world_observation !== false || raw.metric_scope !== "OBSERVED_PREFIX_ONLY") throw new Error("Invalid execution view contract");
  const orderIds = stringList(raw.order_ids, "order_ids");
  const delivered = stringList(raw.delivered_prefix, "delivered_prefix");
  const planned = stringList(raw.planned_served_suffix, "planned_served_suffix");
  const unserved = list(raw.unserved, "unserved").map((value) => {
    const item = record(value, "unserved order");
    string(item.reason, "unserved.reason");
    return string(item.order_id, "unserved.order_id");
  });
  sameIds(orderIds, orderIds);
  sameIds([...delivered, ...planned, ...unserved], orderIds);
  stringList(raw.pending_event_ids, "pending_event_ids");
  if (raw.accepted_trajectory !== null) record(raw.accepted_trajectory, "accepted_trajectory");
  const custody: string[] = [];
  const vehicleIds = list(raw.vehicles, "vehicles").map((value) => {
    const vehicle = record(value, "vehicle");
    const position = record(vehicle.position, "vehicle.position");
    string(position.kind, "position.kind");
    point(position.coordinates, "vehicle.position.coordinates");
    if (position.position_source !== undefined && position.position_source !== "SIMULATED") throw new Error("Unsupported position source");
    if (finite(vehicle.capacity_kg, "capacity_kg") <= 0 || finite(vehicle.current_load_kg, "current_load_kg") < 0 || finite(vehicle.remaining_range_m, "remaining_range_m") < 0) throw new Error("Invalid vehicle units");
    const onboard = stringList(vehicle.onboard_order_ids, "onboard_order_ids");
    if (onboard.some((id) => !orderIds.includes(id) || delivered.includes(id))) throw new Error("Invalid onboard coverage");
    custody.push(...onboard);
    return string(vehicle.vehicle_id, "vehicle_id");
  });
  sameIds(vehicleIds, vehicleIds);
  sameIds(custody, custody);
  return { orderIds, vehicleIds, currentTime: timestamp(raw.current_time) };
}

/** Read-only world projection. All physical state and metadata come through M3 HTTP. */
export function mapM3World(session: M3Session, state: M3ExecutionView, ordersView: M3OrdersView,
  vehiclesView: M3VehiclesView, locationsView: M3LocationsView, baseUrl: string): DispatchSnapshot {
  try {
    const parsed = execution(state);
    const basis = comparableBasis(state.basis);
    if (basis.session_id !== session.session_id || basis.build_sha256 !== session.build_sha256) throw new Error("Session binding mismatch");
    for (const [kind, projection] of [["orders", ordersView], ["vehicles", vehiclesView], ["locations", locationsView]] as const) {
      const value = record(projection, kind);
      if (value.schema_version !== `saferoute-m3-${kind}-view/1`) throw new Error("Invalid projection schema");
      const other = comparableBasis(value.basis);
      if (Object.keys(basis).some((key) => other[key as keyof M3Basis] !== basis[key as keyof M3Basis]) ||
          value.current_time !== state.current_time || value.execution_mode !== state.execution_mode || value.real_world_observation !== false) {
        throw new Member3Error("STATE_CHANGED", "M3 state changed during the read. Reload the current world.");
      }
      const units = record(value.units, "units");
      if (units.distance !== "m" || units.duration !== "s" || units.mass !== "kg" || units.money !== "VND") throw new Error("Invalid projection units");
    }
    const orders: Order[] = list(ordersView.orders, "orders").map((value) => {
      const item = record(value, "order");
      const id = string(item.order_id, "order_id");
      const [longitude, latitude] = point(item.coordinates, "order.coordinates");
      const owner = item.owner_vehicle_id === null ? null : string(item.owner_vehicle_id, "owner_vehicle_id");
      const onboard = state.vehicles.find((vehicle) => vehicle.onboard_order_ids.includes(id));
      const status = state.delivered_prefix.includes(id) ? "DELIVERED" : onboard ? "ONBOARD" : "WAITING";
      if (item.status !== status || owner !== (onboard?.vehicle_id ?? null)) throw new Error("Order custody mismatch");
      const demandKg = finite(item.demand_kg, "demand_kg"), serviceTime = finite(item.service_time_s, "service_time_s");
      if (demandKg < 0 || serviceTime < 0) throw new Error("Invalid order units");
      return { ...orderDisplay(item.display), id, longitude, latitude, status, assignedVehicleId: owner, demandKg, priority: finite(item.priority, "priority"),
        graphNodeId: nodeId(item.graph_node_id), pickupLocationId: string(item.pickup_location_id, "pickup_location_id"),
        deliveryRegionId: string(item.delivery_region_id, "delivery_region_id"), serviceTimeHours: serviceTime / 3600,
        earliest: timestamp(item.earliest), preferredDue: timestamp(item.preferred_due), hardDeadline: timestamp(item.hard_deadline),
        pickedUpAt: null, deliveredAt: null };
    });
    sameIds(orders.map((order) => order.id), parsed.orderIds);
    const metadata = list(vehiclesView.vehicle_metadata, "vehicle_metadata").map((value) => record(value, "vehicle metadata"));
    sameIds(metadata.map((item) => string(item.vehicle_id, "vehicle_id")), parsed.vehicleIds);
    sameIds(vehiclesView.vehicles.map((vehicle) => vehicle.vehicle_id), state.vehicles.map((vehicle) => vehicle.vehicle_id));
    const vehicles: Vehicle[] = state.vehicles.map((value) => {
      const meta = metadata.find((item) => item.vehicle_id === value.vehicle_id)!;
      const projected = vehiclesView.vehicles.find((item) => item.vehicle_id === value.vehicle_id)!;
      if (JSON.stringify(projected) !== JSON.stringify(value)) throw new Member3Error("STATE_CHANGED", "M3 vehicle projection differs from the current world.");
      if (!["AVAILABLE", "UNAVAILABLE"].includes(value.availability)) throw new Error("Invalid availability");
      const [longitude, latitude] = point(value.position.coordinates, "vehicle.position");
      return { ...vehicleDisplay(meta.display), id: value.vehicle_id, type: string(meta.vehicle_type, "vehicle_type"), availability: value.availability,
        capacityKg: finite(value.capacity_kg, "capacity_kg"), currentLoadKg: finite(value.current_load_kg, "current_load_kg"),
        onboardOrderIds: [...value.onboard_order_ids], currentPosition: { longitude, latitude,
          graphNodeId: value.position.node_id == null ? null : nodeId(value.position.node_id) },
        positionTimestamp: value.position_timestamp == null ? null : timestamp(value.position_timestamp), workingStart: timestamp(meta.working_start), workingEnd: timestamp(meta.working_end),
        remainingRangeM: finite(value.remaining_range_m, "remaining_range_m") };
    });
    const locations: Location[] = list(locationsView.locations, "locations").map((value) => {
      const item = record(value, "location");
      const id = string(item.location_id, "location_id");
      const [longitude, latitude] = point(item.coordinates, "location.coordinates");
      if (item.kind !== "DEPOT" && item.kind !== "DELIVERY") throw new Error("Invalid location kind");
      const order = item.kind === "DELIVERY" ? orders.find((order) => order.id === string(item.order_id, "location.order_id")) : undefined;
      if (item.kind === "DELIVERY" && (!order || order.longitude !== longitude || order.latitude !== latitude || String(order.graphNodeId) !== String(item.graph_node_id))) throw new Error("Delivery location mismatch");
      return { id, kind: item.kind, orderId: order?.id, longitude, latitude, graphNodeId: nodeId(item.graph_node_id),
        openingTime: item.kind === "DEPOT" ? timestamp(item.opening_time) : order!.earliest,
        closingTime: item.kind === "DEPOT" ? timestamp(item.closing_time) : order!.hardDeadline };
    });
    sameIds(locations.map((item) => item.id), locations.map((item) => item.id));
    sameIds(locations.filter((item) => item.kind === "DELIVERY").map((item) => item.orderId!), orders.map((item) => item.id));
    if (orders.some((order) => !locations.some((location) => location.kind === "DEPOT" && location.id === order.pickupLocationId))) throw new Error("Missing depot");
    let rain: DispatchSnapshot['decisionState']['context']['rain'] = null;
    if (locationsView.rain) {
      const area = record(locationsView.rain, 'rain');
      const polygon = record(area.polygon, 'rain.polygon');
      if (polygon.type !== 'Polygon' || !basis.overlay_sha256) throw new Error('Invalid rain binding');
      const coordinates = list(polygon.coordinates, 'rain.coordinates').map(raw => {
        const ring = list(raw, 'rain.ring').map(value => point(value, 'rain.point'));
        if (ring.length < 4 || ring[0][0] !== ring[ring.length - 1][0] || ring[0][1] !== ring[ring.length - 1][1]) throw new Error('Invalid rain ring');
        return ring;
      });
      if (!coordinates.length) throw new Error('Missing rain boundary');
      const endsAt = timestamp(area.ends_at);
      if (Date.parse(state.current_time) < Date.parse(endsAt)) rain = { polygon: { type: 'Polygon', coordinates }, endsAt };
    }
    return {
      // The legacy numeric version belongs to mock mode only. Backend revision and
      // proposal/job binding must use the complete string-valued backend.basis.
      decisionState: { sessionId: session.session_id, scenarioId: session.scenario_id as ScenarioId, version: 0, orders, vehicles, locations, context: { rain }, events: [] },
      ...adaptAcceptedExecution(state), demoClock: { now: parsed.currentTime, label: "Thời gian demo" }, demo: { availableEvents: [] },
      backend: { source: "MEMBER3_HTTP", baseUrl, basis, executionMode: state.execution_mode, realWorldObservation: false, executionView: state, metrics: executionMetrics(state) }
    };
  } catch (error) {
    if (error instanceof Member3Error) throw error;
    throw new Member3Error("INVALID_RESPONSE", `M3 world state is invalid (${error instanceof Error ? error.message : "validation failed"}); no local world was substituted.`);
  }
}
