import type { DispatchSnapshot } from '../shared/types/dispatch';
import { createMapScene, type MapScene, type MapSegment } from '../shared/components/mapScene';
import { describeRouteLegs, describeSuppliedRouteLegs, vehicleRouteColor } from '../shared/components/routeLegPresentation';
import { distanceKm, alongLine } from '../shared/geo';

export type DriverRouteMode = 'ALL' | 'NEXT' | 'LEGS';

/** Clip only the demo's current supplied polyline; never invent a connecting road. */
function remainingLine(points: [number, number][], fraction: number): [number, number][] {
  if (fraction <= 0) return points;
  if (fraction >= 1) return [];
  const start = alongLine(points, fraction);
  const lengths = points.slice(1).map((p, i) => distanceKm(
    { longitude: points[i][0], latitude: points[i][1] }, { longitude: p[0], latitude: p[1] }));
  let remaining = lengths.reduce((a, b) => a + b, 0) * fraction;
  for (let i = 0; i < lengths.length; i++) {
    if (remaining < lengths[i]) return [[start.longitude, start.latitude], ...points.slice(i + 1)];
    remaining -= lengths[i];
  }
  return [];
}

export function createDriverMapPresentation(snapshot: DispatchSnapshot, vehicleId: string, mode: DriverRouteMode) {
  const base = createMapScene(snapshot, undefined, vehicleId);
  const active = snapshot.planState.acceptedPlans.find(p => p.id === snapshot.planState.activeAcceptedPlanId);
  const vehicle = active?.plan.vehiclePlans.find(v => v.vehicleId === vehicleId);
  const description = snapshot.backend ? describeSuppliedRouteLegs(base.accepted) : vehicle ? describeRouteLegs(vehicle) : undefined;
  const progress = active && snapshot.executionState.progressByPlanId[active.id]?.[vehicleId];
  const observed = snapshot.backend?.executionView.vehicles.find(v => v.vehicle_id === vehicleId);
  const currentStop = vehicle?.orderedStops.find(s => s.id === progress?.currentStopId);
  const nextSegment = base.accepted.find(s => !s.completed &&
    (snapshot.backend ? !s.returnToDepot : !currentStop || vehicle?.routeSegments.find(r => r.id === s.id)?.toStopId === currentStop.id));
  const nextLegId = nextSegment && description?.bySegmentId.get(nextSegment.id)?.id;
  const color = vehicleRouteColor(vehicleId);
  const accepted: MapSegment[] = base.accepted.flatMap(segment => {
    const leg = description?.bySegmentId.get(segment.id);
    // Keep native return geometry in full-route modes, even without a delivery.
    if (!leg) return [];
    if (mode === 'NEXT' && (segment.completed || leg.id !== nextLegId || observed?.awaiting_delivery)) return [];
    let drawableCoordinates = segment.drawableCoordinates;
    if (mode === 'NEXT' && !snapshot.backend && progress?.travel && vehicle && currentStop) {
      let elapsed = progress.travel.segmentId === currentStop.id ? progress.travel.elapsedMinutes : 0;
      for (const source of vehicle.routeSegments.filter(s => s.toStopId === currentStop.id)) {
        if (source.id === segment.id) {
          drawableCoordinates = remainingLine(source.geometry.coordinates, elapsed / Math.max(.000001, source.durationMinutes));
          break;
        }
        elapsed = Math.max(0, elapsed - source.durationMinutes);
      }
      if (drawableCoordinates && drawableCoordinates.length < 2) return [];
    }
    return [{ ...segment, legId: leg.id, color: mode === 'LEGS' ? leg.color : color,
      ...(drawableCoordinates ? { drawableCoordinates } : {}) }];
  });
  const delivered = new Set(snapshot.backend?.executionView.delivered_prefix ?? []);
  const orderSequence = snapshot.planState.acceptedExecution?.vehicleOrderIds[vehicleId];
  const nextOrderId = observed?.awaiting_delivery || observed?.planned_suffix?.find(id => !delivered.has(id))
    || orderSequence?.find(id => !delivered.has(id));
  const targetIds = snapshot.backend ? [nextOrderId] : currentStop?.orderIds ?? [];
  const markers = mode === 'NEXT' ? base.markers.filter(m => m.kind === 'vehicle' || targetIds.includes(m.id)
    || currentStop?.kind === 'DEPOT_PICKUP' && m.kind === 'depot').map(m =>
      m.kind === 'order' && targetIds.includes(m.id) ? { ...m, destination: true } : m) : base.markers;
  const scene: MapScene = { ...base, viewKey: `${base.viewKey}:${mode}:${mode === 'NEXT' ? targetIds.join(',') : ''}`, accepted,
    vehicleColors: { [vehicleId]: color },
    markers };
  const visible = new Set(accepted.map(s => s.legId));
  return { scene, legs: description?.legs.filter(leg => visible.has(leg.id)) ?? [] };
}
