import { describe, expect, it } from 'vitest';
import { MockStateEngine } from './MockStateEngine';
import { getFixtureScenario } from '../fixtureCatalog';
import { exportScenarioFixture, validateScenario } from '../../shared/scenarioImport';
import { arrive } from '../../test/arrive';
import { distanceKm } from '../../shared/geo';
import compact from '../../../../docs/DEMO_COMPACT_SCENARIO.json';

function pickedUp() {
  const engine = new MockStateEngine();
  const proposed = engine.optimize().planState.proposedAlternatives[0];
  engine.selectAlternative(proposed.id);
  engine.acceptSelectedPlan();
  const plan = proposed.content!.vehiclePlans.find(v => v.vehicleId === 'V1')!;
  for (const orderId of plan.orderedStops[0].orderIds) engine.pickupOrder({vehicleId:'V1',orderId});
  return {engine,plan,target:plan.orderedStops[1]};
}

describe('local workflow regression', () => {
  it('imports the compact sample with four real, nearby delivery coordinates', () => {
    const snapshot = new MockStateEngine().importScenario(compact);
    expect(snapshot.decisionState.orders).toHaveLength(4);
    const depot=snapshot.decisionState.locations[0];
    for(const order of snapshot.decisionState.orders)expect(distanceKm(depot,order)).toBeLessThan(3);
  });
  it.each(['S0','S1','S2','S3','S4'] as const)('%s routes meet the real stop coordinates for all profiles', scenario => {
    const engine = new MockStateEngine();
    engine.loadScenario(scenario);
    for (const proposal of engine.optimize().planState.proposedAlternatives) {
      for (const plan of proposal.content!.vehiclePlans) {
        for (const segment of plan.routeSegments) {
          const from=plan.orderedStops.find(s=>s.id===segment.fromStopId)?.location ?? engine.getSnapshot().decisionState.vehicles.find(v=>v.id===plan.vehicleId)!.currentPosition;
          const to=plan.orderedStops.find(s=>s.id===segment.toStopId)!.location;
          expect(segment.geometry.coordinates[0]).toEqual([from.longitude,from.latitude]);
          expect(segment.geometry.coordinates.at(-1)).toEqual([to.longitude,to.latitude]);
        }
      }
      expect(proposal.content!.metrics.exposureScore).toBeNull();
    }
  });

  it('rejects delivery from the depot atomically, then permits it after travel', () => {
    const {engine,target}=pickedUp();
    const before=engine.getSnapshot();
    expect(()=>engine.deliverOrder({vehicleId:'V1',orderId:target.orderIds[0]})).toThrow('arrive');
    expect(engine.getSnapshot()).toEqual(before);
    arrive(engine,'V1');
    const delivered=engine.deliverOrder({vehicleId:'V1',orderId:target.orderIds[0]});
    expect(delivered.decisionState.orders.find(o=>o.id===target.orderIds[0])?.status).toBe('DELIVERED');
  });

  it('interpolates travel, pauses playback, and never auto-delivers', () => {
    const {engine,plan,target}=pickedUp();
    const duration=plan.routeSegments.filter(s=>s.toStopId===target.id).reduce((n,s)=>n+s.durationMinutes,0);
    const before=engine.getSnapshot().decisionState.vehicles[0].currentPosition;
    engine.advanceDemoClock(duration/2);
    const halfway=engine.getSnapshot().decisionState.vehicles[0].currentPosition;
    expect(distanceKm(halfway,target.location)).toBeLessThan(distanceKm(before,target.location));
    expect(distanceKm(halfway,target.location)).toBeGreaterThan(0.025);
    engine.setPlayback(false);
    const paused=engine.getSnapshot();
    expect(engine.tick(Date.now()+10000)).toEqual(paused);
    arrive(engine,'V1');
    expect(engine.getSnapshot().decisionState.orders.find(o=>o.id===target.orderIds[0])?.status).toBe('ONBOARD');
  });

  it('does not move an unavailable vehicle or transfer onboard custody', () => {
    const engine=new MockStateEngine(); engine.loadScenario('S3');
    const proposal=engine.optimize().planState.proposedAlternatives[0];
    engine.selectAlternative(proposal.id);engine.acceptSelectedPlan();engine.triggerFixtureEvent('S3-E1');
    const before=engine.getSnapshot().decisionState.vehicles.find(v=>v.id==='V1')!;
    engine.advanceDemoClock(60);
    expect(engine.getSnapshot().decisionState.vehicles.find(v=>v.id==='V1')).toEqual(before);
    expect(engine.getSnapshot().decisionState.orders.find(o=>o.id==='O001')?.assignedVehicleId).toBe('V1');
  });

  it('validates imports before mutation and resets an imported scenario to its original state', () => {
    const engine=new MockStateEngine(), before=engine.getSnapshot();
    const invalid=structuredClone(getFixtureScenario('S1'));
    invalid.initialState.orders[0].latitude=999;
    expect(()=>engine.importScenario(invalid)).toThrow();expect(engine.getSnapshot()).toEqual(before);
    const fixture=getFixtureScenario('S1');
    const imported=engine.importScenario(fixture);
    expect(imported.decisionState.sessionId).not.toBe(before.decisionState.sessionId);
    const order=fixture.initialState.orders.find(o=>o.status==='WAITING')!;
    engine.moveOrder(order.id,10.8,106.7);
    expect(engine.resetDemoSession().decisionState.orders).toEqual(fixture.initialState.orders);
  });

  it('invalidates proposals after moving an order and locks edits after accept', () => {
    const engine=new MockStateEngine(); engine.optimize();
    const order=engine.getSnapshot().decisionState.orders.find(o=>o.status==='WAITING')!;
    const edited=engine.moveOrder(order.id,10.8,106.7);
    expect(edited.planState.proposedAlternatives).toEqual([]);
    expect(edited.decisionState.orders.find(o=>o.id===order.id)).toMatchObject({latitude:10.8,longitude:106.7});
    const plan=engine.optimize().planState.proposedAlternatives[0];engine.selectAlternative(plan.id);engine.acceptSelectedPlan();
    expect(()=>engine.moveOrder(order.id,10.81,106.71)).toThrow('Reset');
  });

  it('exports a round-trip fixture after triggering an urgent order without duplicating the event', () => {
    const engine=new MockStateEngine();engine.triggerFixtureEvent('S2-E1');
    const exported=exportScenarioFixture(engine.getSnapshot());
    expect(()=>validateScenario(exported)).not.toThrow();
    expect(exported.events.some(e=>e.eventId==='S2-E1')).toBe(false);
    expect(new MockStateEngine().importScenario(exported).decisionState.orders).toEqual(engine.getSnapshot().decisionState.orders);
  });

  it('rejects inconsistent custody and overloaded vehicles', () => {
    const fixture=structuredClone(getFixtureScenario('S3'));
    fixture.initialState.vehicles[0].onboardOrderIds=[];
    expect(()=>validateScenario(fixture)).toThrow();
    const overloaded=structuredClone(getFixtureScenario('S3'));
    overloaded.initialState.vehicles[0].capacityKg=0.01;
    expect(()=>validateScenario(overloaded)).toThrow();
  });
});
