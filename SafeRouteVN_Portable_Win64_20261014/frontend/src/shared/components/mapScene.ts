import type { DispatchSnapshot, ProposedAlternative } from "../types/dispatch";
import type { GeoJsonPolygon } from "../types/scenario";
import { proposalCurrency } from "../../integrations/member3/revision";

type Point = [longitude: number, latitude: number];

export interface MapSegment {
  id: string;
  vehicleId: string;
  coordinates: Point[];
  completed: boolean;
  geometrySource: "SCHEMATIC_DEMO" | "MEMBER2_SUPPLIED";
  /** Shared presentation metadata; never changes route geometry. */
  legId?: string;
  color?: string;
  edgeId?: string;
  actionIndex?: number;
  fractionStart?: number;
  fractionEnd?: number;
  fractionStartExact?: string;
  returnToDepot?: boolean;
  /** Native full source coordinates stay above; this interval alone is drawable. */
  drawableCoordinates?: Point[];
  completionAvailable?: boolean;
}

export interface MapMarker {
  id: string;
  kind: "depot" | "order" | "vehicle";
  coordinates: Point;
  label: string;
  urgent?: boolean;
  unavailable?: boolean;
  destination?: boolean;
}

export interface MapScene {
  /** Full accepted geometry for motion, independent of route visibility/mode. */
  motion?: { key: string; clock: string; playing: boolean; stoppedVehicleIds: string[]; segments: MapSegment[] };
  viewKey?: string;
  accepted: MapSegment[];
  proposed: MapSegment[];
  markers: MapMarker[];
  rain: GeoJsonPolygon | null;
  vehicleColors?: Record<string, string>;
}

export function createMapScene(snapshot: DispatchSnapshot, proposed?: ProposedAlternative, vehicleId?: string): MapScene {
  const active = snapshot.planState.acceptedPlans.find((plan) => plan.id === snapshot.planState.activeAcceptedPlanId);
  const accepted = snapshot.backend ? snapshot.planState.acceptedExecution?.segments.filter(segment => !vehicleId || segment.vehicleId === vehicleId) ?? [] : active?.plan.vehiclePlans.filter((vehicle) => !vehicleId || vehicle.vehicleId === vehicleId).flatMap((vehicle) => {
    const completed = new Set(snapshot.executionState.progressByPlanId[active.id]?.[vehicle.vehicleId]?.completedSegmentIds ?? []);
    return vehicle.routeSegments.map((segment) => ({
      id: segment.id,
      vehicleId: vehicle.vehicleId,
      coordinates: segment.geometry.coordinates,
      completed: completed.has(segment.id),
      geometrySource: segment.geometrySource ?? "SCHEMATIC_DEMO"
    }));
  }) ?? [];
  const preview = vehicleId ? [] : snapshot.backend ?
    !snapshot.backend.stale && !snapshot.backend.error && proposed?.nativeForecast && proposalCurrency(proposed, snapshot) === "CURRENT" &&
    snapshot.planState.proposedAlternatives.some(p => p.id === proposed.id) ? proposed.nativeForecast.segments : [] : proposed?.content?.vehiclePlans.flatMap((vehicle) => vehicle.routeSegments.map((segment) => ({
    id: segment.id,
    vehicleId: vehicle.vehicleId,
    coordinates: segment.geometry.coordinates,
    completed: false,
    geometrySource: segment.geometrySource ?? "SCHEMATIC_DEMO"
  }))) ?? [];
  const driverOrderIds = vehicleId ? new Set(snapshot.backend ? snapshot.planState.acceptedExecution?.vehicleOrderIds[vehicleId] ?? [] : active?.plan.vehiclePlans.find((vehicle) => vehicle.vehicleId === vehicleId)?.orderedStops.flatMap((stop) => stop.orderIds) ?? []) : null;
  return {
    motion: {
      key: `${snapshot.decisionState.sessionId}:${snapshot.planState.acceptedExecution?.jobId ?? snapshot.planState.activeAcceptedPlanId ?? ''}:${vehicleId ?? 'all'}`,
      clock: snapshot.demoClock.now,
      playing: snapshot.backend ? Boolean(snapshot.backend.playback && !snapshot.backend.playback.paused && !snapshot.backend.stale && !snapshot.backend.error && !snapshot.backend.playbackConverging && !snapshot.backend.pendingEvents?.events.some(e => e.apply_allowed)) : Boolean(snapshot.demo.playback?.playing),
      stoppedVehicleIds: snapshot.backend?.executionView.vehicles.filter(v => v.awaiting_delivery || v.availability === 'UNAVAILABLE').map(v => v.vehicle_id) ?? [],
      segments: accepted
    },
    viewKey: `${snapshot.decisionState.sessionId}:${snapshot.planState.activeAcceptedPlanId ?? ''}:${proposed?.id ?? ''}`,
    accepted,
    proposed: preview,
    rain: snapshot.decisionState.context.rain?.polygon ?? null,
    markers: [
      ...snapshot.decisionState.locations.filter((location) => location.kind !== "DELIVERY").map((location): MapMarker => ({
        id: location.id, kind: "depot", coordinates: [location.longitude, location.latitude], label: `Depot ${location.id}`
      })),
      ...snapshot.decisionState.orders.filter((order) => !driverOrderIds || driverOrderIds.has(order.id)).map((order): MapMarker => ({
        id: order.id, kind: "order", coordinates: [order.longitude, order.latitude], label: `${order.id} · ${order.status}`,
        urgent: order.priority >= 3
      })),
      ...snapshot.decisionState.vehicles.filter((vehicle) => !vehicleId || vehicle.id === vehicleId).map((vehicle): MapMarker => ({
        id: vehicle.id, kind: "vehicle", coordinates: [vehicle.currentPosition.longitude, vehicle.currentPosition.latitude],
        label: `${vehicle.id} · ${vehicle.availability}`, unavailable: vehicle.availability === "UNAVAILABLE"
      }))
    ]
  };
}
