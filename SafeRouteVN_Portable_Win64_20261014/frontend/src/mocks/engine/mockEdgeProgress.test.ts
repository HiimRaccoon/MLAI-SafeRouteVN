import { arrive } from "../../test/arrive";
import { expect, it } from 'vitest';
import { MockStateEngine } from './MockStateEngine';

it('completes supplied incoming EDGE segments on delivery without completing the return', () => {
  const snapshot = new MockStateEngine().optimize();
  const proposal = snapshot.planState.proposedAlternatives[0];
  const vehicle = proposal.content!.vehiclePlans.find(v => v.vehicleId === 'V1')!;
  vehicle.orderedStops = [
    { id: 'pickup', kind: 'DEPOT_PICKUP', orderIds: ['O001'], label: 'Pickup', location: { longitude: 106.7, latitude: 10.8 } },
    { id: 'delivery', kind: 'DELIVERY', orderIds: ['O001'], label: 'Delivery', location: { longitude: 106.704, latitude: 10.804 } }
  ];
  vehicle.routeSegments = [
    { id: 'incoming-1', fromStopId: 'pickup', toStopId: 'delivery', geometry: { type: 'LineString', coordinates: [[106.7,10.8],[106.701,10.801],[106.702,10.803]] }, distanceKm: .3, durationMinutes: 1, geometrySource: 'MEMBER2_SUPPLIED' },
    { id: 'incoming-2', fromStopId: 'pickup', toStopId: 'delivery', geometry: { type: 'LineString', coordinates: [[106.702,10.803],[106.704,10.804]] }, distanceKm: .2, durationMinutes: .5, geometrySource: 'MEMBER2_SUPPLIED' },
    { id: 'return', fromStopId: 'delivery', toStopId: 'final-depot', geometry: { type: 'LineString', coordinates: [[106.704,10.804],[106.705,10.802],[106.7,10.8]] }, distanceKm: .3, durationMinutes: 1.5, geometrySource: 'MEMBER2_SUPPLIED' }
  ];
  const expected = structuredClone(vehicle.routeSegments);
  snapshot.decisionState.vehicles.find(v => v.id === "V1")!.currentPosition = { ...vehicle.orderedStops[0].location, graphNodeId: null };
  const engine = new MockStateEngine(snapshot);
  engine.selectAlternative(proposal.id);
  const accepted = engine.acceptSelectedPlan(), planId = accepted.planState.activeAcceptedPlanId!;
  const picked = engine.pickupOrder({ vehicleId: 'V1', orderId: 'O001' });
  expect(picked.executionState.progressByPlanId[planId].V1.completedSegmentIds).toEqual([]);
  arrive(engine);
  const delivered = engine.deliverOrder({ vehicleId: 'V1', orderId: 'O001' });
  expect(delivered.executionState.progressByPlanId[planId].V1.completedSegmentIds).toEqual(['incoming-1','incoming-2']);
  expect(delivered.planState.acceptedPlans[0].plan.vehiclePlans.find(v => v.vehicleId === 'V1')!.routeSegments).toEqual(expected);
});
