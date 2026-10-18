import {orderDisplay,vehicleDisplay} from './displayMetadata';
import type { ScenarioFixture } from './types/scenario';
import { DispatchError } from './dispatchErrors';
import type { DispatchSnapshot } from './types/dispatch';

export function exportScenarioFixture(snapshot: DispatchSnapshot): ScenarioFixture {
  return validateScenario({contextVersion:'demo-local',deliveryAreaVersion:'demo-local',
    initialState:{currentTime:snapshot.demoClock.now,stateVersion:0,orders:snapshot.decisionState.orders,
      vehicles:snapshot.decisionState.vehicles,locations:snapshot.decisionState.locations,currentPlans:[]},
    events:snapshot.decisionState.events.filter(e=>e.status==='READY_TO_TRIGGER').map(e=>e.fixtureEvent)});
}

/** Validate before touching active state. Import is a separate demo session. */
export function validateScenario(value: unknown): ScenarioFixture {
  const fail = () => { throw new DispatchError('INVALID_SCENARIO', 'Invalid scenario coordinates, timestamps, IDs, capacity or custody.'); };
  const obj = (x: unknown): x is Record<string, any> => !!x && typeof x === 'object' && !Array.isArray(x);
  const number = (x: unknown) => typeof x === 'number' && Number.isFinite(x);
  const id = (x: unknown) => typeof x === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(x);
  const time = (x: unknown) => typeof x === 'string' && /(?:Z|[+-]\d\d:\d\d)$/.test(x) && Number.isFinite(Date.parse(x));
  const point = (x: any) => obj(x) && number(x.latitude) && number(x.longitude) && Math.abs(x.latitude)<=90 && Math.abs(x.longitude)<=180;
  if(!obj(value)||!obj(value.initialState)||!Array.isArray(value.events)||value.events.length>20) return fail();
  const s=value.initialState;
  if(!time(s.currentTime)||!Number.isSafeInteger(s.stateVersion)||s.stateVersion<0) return fail();
  for(const [key,max] of [['orders',100],['vehicles',20],['locations',120]] as const) {
    if(!Array.isArray(s[key])||!s[key].length||s[key].length>max||s[key].some((o:any)=>!obj(o)||!id(o.id))||new Set(s[key].map((o:any)=>o.id)).size!==s[key].length) return fail();
  }
  for(const l of s.locations) if(!point(l)||!time(l.openingTime)||!time(l.closingTime)||Date.parse(l.openingTime)>Date.parse(l.closingTime)) return fail();
  const orderValid=(o:any)=>obj(o)&&id(o.id)&&point(o)&&number(o.demandKg)&&o.demandKg>0&&number(o.priority)&&o.priority>=0&&
    s.locations.some((l:any)=>l.id===o.pickupLocationId&&l.kind!=='DELIVERY')&&['WAITING','ONBOARD','DELIVERED'].includes(o.status)&&
    time(o.earliest)&&time(o.preferredDue)&&time(o.hardDeadline)&&Date.parse(o.earliest)<=Date.parse(o.preferredDue)&&Date.parse(o.preferredDue)<=Date.parse(o.hardDeadline)&&
    number(o.serviceTimeHours)&&o.serviceTimeHours>=0;
  for(const o of s.orders) { orderDisplay(o); if(!orderValid(o)) return fail(); }
  for(const v of s.vehicles) {
    vehicleDisplay(v);
    if(!point(v.currentPosition)||!number(v.capacityKg)||v.capacityKg<=0||!number(v.currentLoadKg)||v.currentLoadKg<0||v.currentLoadKg>v.capacityKg||
      !['AVAILABLE','UNAVAILABLE'].includes(v.availability)||!Array.isArray(v.onboardOrderIds)||new Set(v.onboardOrderIds).size!==v.onboardOrderIds.length||
      !time(v.workingStart)||!time(v.workingEnd)||Date.parse(v.workingStart)>Date.parse(v.workingEnd)||
      (v.costPerKmVnd!==undefined&&(!number(v.costPerKmVnd)||v.costPerKmVnd<0))) return fail();
    const onboard=s.orders.filter((o:any)=>o.status==='ONBOARD'&&o.assignedVehicleId===v.id);
    if(onboard.length!==v.onboardOrderIds.length||onboard.some((o:any)=>!v.onboardOrderIds.includes(o.id))||Math.abs(onboard.reduce((n:number,o:any)=>n+o.demandKg,0)-v.currentLoadKg)>1e-6) return fail();
  }
  for(const o of s.orders) if(o.status==='ONBOARD'&&!s.vehicles.some((v:any)=>v.id===o.assignedVehicleId&&v.onboardOrderIds.includes(o.id))) return fail();
  const eventIds=new Set<string>(), orderIds=new Set(s.orders.map((o:any)=>o.id));
  for(const e of value.events) {
    if(!obj(e)||!id(e.eventId)||eventIds.has(e.eventId)||!time(e.timestamp)) return fail();
    eventIds.add(e.eventId);
    if(e.type==='VEHICLE_UNAVAILABLE') {if(!s.vehicles.some((v:any)=>v.id===e.vehicleId))return fail();}
    else if(e.type==='URGENT_ORDER') {orderDisplay(e.orderPayload);if(!orderValid(e.orderPayload)||e.orderPayload.status!=='WAITING'||orderIds.has(e.orderPayload.id))return fail();orderIds.add(e.orderPayload.id);}
    else if(e.type==='LOCAL_RAIN_WHAT_IF') {
      if(!time(e.endTime)||Date.parse(e.endTime)<=Date.parse(e.timestamp)||!obj(e.polygon)||e.polygon.type!=='Polygon'||!Array.isArray(e.polygon.coordinates)||!e.polygon.coordinates.length||
        e.polygon.coordinates.some((ring:any)=>!Array.isArray(ring)||ring.length<4||ring.length>1000||ring.some((p:any)=>!Array.isArray(p)||p.length!==2||!point({longitude:p[0],latitude:p[1]}))||JSON.stringify(ring[0])!==JSON.stringify(ring.at(-1))))return fail();
    } else return fail();
  }
  return structuredClone(value) as unknown as ScenarioFixture;
}
