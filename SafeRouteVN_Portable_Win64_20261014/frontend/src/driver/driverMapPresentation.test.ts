import { describe, expect, it } from 'vitest';
import { tinySuppliedEngine } from '../test/tinySuppliedPlan';
import { createDriverMapPresentation } from './driverMapPresentation';
import { vehicleRouteColor } from '../shared/components/routeLegPresentation';
import { acceptedView } from '../integrations/member3/acceptedTestFixture';
import { adaptAcceptedExecution } from '../integrations/member3/executionViewAdapter';
import { controller } from '../integrations/member3/playbackTestFixture';
import { basis } from '../integrations/member3/testFixtures';

function accepted() {
  const engine = tinySuppliedEngine();
  engine.selectAlternative(engine.getSnapshot().planState.proposedAlternatives[0].id);
  return { engine, snapshot: engine.acceptSelectedPlan() };
}

describe('Driver route display', () => {
  it('changes only color between full-route and per-leg modes; each vehicle has a stable distinct color', () => {
    const { snapshot } = accepted(), before = structuredClone(snapshot);
    const all = createDriverMapPresentation(snapshot, 'V2', 'ALL');
    const legs = createDriverMapPresentation(snapshot, 'V2', 'LEGS');
    expect(new Set(all.scene.accepted.map(s => s.color))).toEqual(new Set(['#16a34a']));
    expect(new Set(legs.scene.accepted.map(s => s.color)).size).toBeGreaterThan(1);
    expect(all.scene.accepted.map(s => s.coordinates)).toEqual(legs.scene.accepted.map(s => s.coordinates));
    expect(new Set(['V1', 'V2', 'V3', 'V4', 'V5'].map(vehicleRouteColor)).size).toBe(5);
    expect(snapshot).toEqual(before);
  });

  it('draws only the current delivery leg and clips already-travelled demo geometry', () => {
    const { engine, snapshot } = accepted();
    const plan = snapshot.planState.acceptedPlans[0].plan.vehiclePlans.find(v => v.vehicleId === 'V2')!;
    for (const oid of plan.orderedStops[0].orderIds) engine.pickupOrder({ vehicleId: 'V2', orderId: oid });
    engine.advanceDemoClock(.01);
    const moving = engine.getSnapshot(), before = structuredClone(moving);
    const next = createDriverMapPresentation(moving, 'V2', 'NEXT');
    const full = createDriverMapPresentation(moving, 'V2', 'ALL');
    expect(next.scene.accepted.length).toBeGreaterThan(0);
    expect(next.scene.accepted.length).toBeLessThan(full.scene.accepted.length);
    expect(new Set(next.scene.accepted.map(s => s.legId)).size).toBe(1);
    const position = moving.decisionState.vehicles.find(v => v.id === 'V2')!.currentPosition;
    const start = next.scene.accepted[0].drawableCoordinates![0];
    expect(start[0]).toBeCloseTo(position.longitude, 8);
    expect(start[1]).toBeCloseTo(position.latitude, 8);
    expect(moving).toEqual(before);
  });

  it('preserves native clipped road geometry and hides later deliveries and depot return in NEXT mode', () => {
    const { snapshot } = accepted(), view = acceptedView();
    Object.assign(snapshot, adaptAcceptedExecution(view));
    snapshot.backend = { source: 'MEMBER3_HTTP', baseUrl: 'http://localhost', basis, executionView: view, executionMode: 'SIMULATED_REPLAY', realWorldObservation: false };
    const execution = snapshot.planState.acceptedExecution!, edge = execution.segments[0];
    execution.segments = [
      { ...edge, id: 'first', legId: 'V1:leg:1', returnToDepot: false, drawableCoordinates: [[106.7,10.8], [106.71,10.81]] },
      { ...edge, id: 'later', legId: 'V1:leg:2', returnToDepot: false },
      { ...edge, id: 'return', legId: 'V1:leg:3', returnToDepot: true },
    ];
    const before = structuredClone(snapshot);
    const next = createDriverMapPresentation(snapshot, 'V1', 'NEXT');
    expect(next.scene.accepted.map(s => s.id)).toEqual(['first']);
    expect(next.scene.accepted[0].drawableCoordinates).toEqual(execution.segments[0].drawableCoordinates);
    expect(createDriverMapPresentation(snapshot, 'V1', 'ALL').scene.accepted).toHaveLength(3);
    expect(snapshot).toEqual(before);
  });

  it('keeps the native destination visible immediately after acceptance and while awaiting delivery', () => {
    const { snapshot } = accepted(), view = acceptedView();
    Object.assign(snapshot, adaptAcceptedExecution(view));
    view.accepted_trajectory!.vehicle_routes[0].order_sequence = ['O001', 'O002'];
    snapshot.planState.acceptedExecution!.vehicleOrderIds.V1 = ['O001', 'O002'];
    snapshot.backend = { source:'MEMBER3_HTTP', baseUrl:'http://localhost', basis, executionView:view,
      executionMode:'SIMULATED_REPLAY', realWorldObservation:false };
    const execution = snapshot.planState.acceptedExecution!;
    execution.segments = [{ ...execution.segments[0], id:'first', legId:'V1:leg:1', returnToDepot:false }];
    const before = structuredClone(snapshot);
    const next = createDriverMapPresentation(snapshot, 'V1', 'NEXT');
    expect(next.scene.markers.filter(m => m.kind === 'order').map(m => m.id)).toEqual(['O001']);
    expect(next.scene.markers.find(m => m.id === 'O001')).toMatchObject({destination:true});
    expect(snapshot).toEqual(before);
    view.vehicles[0].awaiting_delivery = 'O001';
    const arrived = createDriverMapPresentation(snapshot, 'V1', 'NEXT');
    expect(arrived.scene.accepted).toEqual([]);
    expect(arrived.scene.markers.filter(m => m.kind === 'order').map(m => m.id)).toEqual(['O001']);
    view.vehicles[0].awaiting_delivery = null;
    view.delivered_prefix = ['O001'];
    view.vehicles[0].planned_suffix = ['O001', 'O002'];
    const following = createDriverMapPresentation(snapshot, 'V1', 'NEXT');
    expect(following.scene.markers.filter(m => m.kind === 'order').map(m => m.id)).toEqual(['O002']);
    expect(following.scene.viewKey).not.toEqual(next.scene.viewKey);
    view.delivered_prefix = ['O001', 'O002'];
    expect(createDriverMapPresentation(snapshot, 'V1', 'NEXT').scene.markers.filter(m => m.kind === 'order')).toEqual([]);
  });
});


it('keeps full accepted motion geometry across route modes and blocks stale or paused animation', () => {
  const { snapshot } = accepted(), view = acceptedView();
  Object.assign(snapshot, adaptAcceptedExecution(view));
  snapshot.backend = { source:'MEMBER3_HTTP', baseUrl:'http://localhost', basis, executionView:view,
    executionMode:'SIMULATED_REPLAY', realWorldObservation:false,
    playback:{schema_version:'saferoute-m3-playback-controller/1', ...controller({paused:false,fully_paused:false}), execution_view:view} };
  const full = createDriverMapPresentation(snapshot, 'V1', 'ALL').scene;
  const next = createDriverMapPresentation(snapshot, 'V1', 'NEXT').scene;
  expect(next.motion?.segments).toEqual(full.motion?.segments);
  expect(next.motion?.key).toEqual(full.motion?.key);
  expect(next.motion?.playing).toBe(true);
  view.vehicles[0].awaiting_delivery = 'O001';
  expect(createDriverMapPresentation(snapshot, 'V1', 'ALL').scene.motion?.stoppedVehicleIds).toContain(view.vehicles[0].vehicle_id);
  snapshot.backend.stale = true;
  expect(createDriverMapPresentation(snapshot, 'V1', 'ALL').scene.motion?.playing).toBe(false);
  snapshot.backend.stale = false;
  snapshot.backend.playback!.paused = true;
  expect(createDriverMapPresentation(snapshot, 'V1', 'ALL').scene.motion?.playing).toBe(false);
});
