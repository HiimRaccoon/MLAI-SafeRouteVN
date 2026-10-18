import type { VehiclePlan } from "../types/dispatch";
import type { MapSegment } from "./mapScene";

const PALETTES: Record<string, readonly string[]> = {
  V1: ["#2563eb", "#06b6d4", "#4f46e5", "#0284c7", "#38bdf8", "#1e40af"],
  V2: ["#16a34a", "#14532d", "#65a30d", "#22c55e", "#14b8a6", "#15803d"]
};
const FALLBACK_PALETTE = ["#475569", "#94a3b8", "#64748b", "#334155"];
const VEHICLE_COLORS = ['#2563eb', '#16a34a', '#ea580c', '#9333ea', '#dc2626', '#0891b2', '#c026d3', '#ca8a04', '#0d9488', '#4f46e5', '#be123c', '#65a30d', '#0369a1', '#a16207', '#7c3aed', '#15803d', '#c2410c', '#a21caf', '#0f766e', '#4338ca'];

function vehicleIndex(id: string): number {
  const match = /^V([1-9]\d*)$/.exec(id);
  return match && Number.isSafeInteger(Number(match[1])) ? Number(match[1]) - 1
    : [...id].reduce((hash, char) => (hash * 31 + char.charCodeAt(0)) >>> 0, 0);
}

export function vehicleRouteColor(vehicleId: string): string {
  const index = vehicleIndex(vehicleId);
  return index < VEHICLE_COLORS.length ? VEHICLE_COLORS[index] : `hsl(${Math.round(index * 137.508) % 360}, 70%, 42%)`;
}
export function vehicleLegColor(vehicleId: string, number: number): string {
  const palette = PALETTES[vehicleId] ?? [vehicleRouteColor(vehicleId), ...FALLBACK_PALETTE, '#06b6d4', '#9333ea', '#ea580c'];
  return palette[(number - 1) % palette.length];
}

export interface RouteLeg {
  id: string;
  vehicleId: string;
  number: number;
  targetLabel: string;
  color: string;
}

/** Decorates supplied EDGE actions before display filters; never mutates source geometry. */
export function describeSuppliedRouteLegs(segments: readonly MapSegment[]): { legs: RouteLeg[]; bySegmentId: Map<string, RouteLeg> } {
  const groups = new Map<string, RouteLeg>(), bySegmentId = new Map<string, RouteLeg>();
  const countByVehicle = new Map<string, number>();
  for (const segment of segments) {
    const id = segment.legId ?? segment.id;
    let leg = groups.get(id);
    if (!leg) {
      const next = (countByVehicle.get(segment.vehicleId) ?? 0) + 1;
      const ordinal = /:leg:([1-9]\d*)$/.exec(id)?.[1];
      const number = ordinal && Number.isSafeInteger(Number(ordinal)) ? Number(ordinal) : next;
      countByVehicle.set(segment.vehicleId, number);
      leg = { id, vehicleId: segment.vehicleId, number, color: vehicleLegColor(segment.vehicleId, number),
        targetLabel: segment.returnToDepot ? "Mandatory return continuation" : "Planned service" };
      groups.set(id, leg);
    }
    bySegmentId.set(segment.id, leg);
  }
  return { legs: [...groups.values()], bySegmentId };
}

/** Assign colors to supplied stop-to-stop legs before filtering progress.
 * No coordinates are generated or modified. Return forecasts stay in the plan.
 */
export function describeRouteLegs(vehicle: VehiclePlan): { legs: RouteLeg[]; bySegmentId: Map<string, RouteLeg> } {
  const groups = new Map<string, RouteLeg>();
  const bySegmentId = new Map<string, RouteLeg>();
  for (const segment of vehicle.routeSegments) {
    if (segment.toStopId.endsWith("return-depot")) continue;
    const key = JSON.stringify([segment.fromStopId, segment.toStopId]);
    let leg = groups.get(key);
    if (!leg) {
      const target = vehicle.orderedStops.find((s) => s.id === segment.toStopId);
      const number = groups.size + 1;
      leg = {
        id: `${vehicle.vehicleId}:${key}`,
        vehicleId: vehicle.vehicleId,
        number,
        targetLabel: target?.kind === "DELIVERY" ? target.orderIds.join(", ") : target?.kind === "DEPOT_PICKUP" ? "Depot pickup" : segment.toStopId,
        color: vehicleLegColor(vehicle.vehicleId, number)
      };
      groups.set(key, leg);
    }
    bySegmentId.set(segment.id, leg);
  }
  return { legs: [...groups.values()], bySegmentId };
}
