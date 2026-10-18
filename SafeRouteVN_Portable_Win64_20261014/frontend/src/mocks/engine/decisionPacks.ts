import type { DecisionState, PlanProfile, ProposedAlternative, VehiclePlan } from '../../shared/types/dispatch';
import { distanceKm } from '../../shared/geo';

// Deterministic schematic demo heuristic; never a certified road/safety solver.
const templates: Record<PlanProfile, Record<string, string[]>> = {
  FASTEST: { V1: ['O001','O003','O005','O007','O009'], V2: ['O002','O004','O006','O008'] },
  BALANCED: { V1: ['O002','O004','O006','O008'], V2: ['O001','O003','O005','O007','O009'] },
  SAFER: { V1: ['O003','O005','O007'], V2: ['O001','O002','O004','O006','O008','O009'] }
};
export function buildPreparedAlternatives(state: DecisionState): ProposedAlternative[] {
  return (['FASTEST', 'BALANCED', 'SAFER'] as PlanProfile[]).map(profile => {
    const assigned = new Map(state.vehicles.map(v => [v.id, state.orders.filter(o => o.status === 'ONBOARD' && o.assignedVehicleId === v.id)]));
    const unserved: { orderId: string; reason: string }[] = [];
    for (const order of state.orders.filter(o => o.status === 'WAITING')) {
      const eligible = state.vehicles.filter(v => v.availability === 'AVAILABLE' &&
        assigned.get(v.id)!.reduce((n, o) => n + o.demandKg, 0) + order.demandKg <= v.capacityKg + 1e-9);
      const preferred = eligible.find(v => templates[profile][v.id]?.includes(order.id));
      const vehicle = preferred ?? eligible.sort((a,b) => assigned.get(a.id)!.length - assigned.get(b.id)!.length || distanceKm(a.currentPosition,order)-distanceKm(b.currentPosition,order))[0];
      if (vehicle) assigned.get(vehicle.id)!.push(order);
      else unserved.push({ orderId: order.id, reason: 'No available vehicle with remaining pickup capacity' });
    }
    let duration = 0, distance = 0, cost = 0;
    const vehiclePlans: VehiclePlan[] = state.vehicles.map(vehicle => {
      const orders = [...assigned.get(vehicle.id)!];
      if (vehicle.availability !== 'AVAILABLE') {
        orders.forEach(o => unserved.push({orderId:o.id, reason:'Xe đang chở hàng không khả dụng'}));
        return {vehicleId:vehicle.id, orderedStops: orders.map(o=>({id:vehicle.id+'-'+o.id,kind:'DELIVERY',orderIds:[o.id],label:o.id,location:o})),routeSegments:[]};
      }
      const orderedStops: VehiclePlan['orderedStops'] = [];
      const waiting = orders.filter(o=>o.status==='WAITING');
      for (const depotId of [...new Set(waiting.map(o=>o.pickupLocationId))]) {
        const depot = state.locations.find(l=>l.id===depotId);
        if (!depot) throw new Error('Unknown pickup location');
        orderedStops.push({id:vehicle.id+'-depot-'+depotId,kind:'DEPOT_PICKUP',orderIds:waiting.filter(o=>o.pickupLocationId===depotId).map(o=>o.id),label:depotId,location:{latitude:depot.latitude,longitude:depot.longitude}});
      }
      let cursor = orderedStops.at(-1)?.location ?? vehicle.currentPosition;
      while(orders.length) {
        orders.sort((a,b)=> distanceKm(cursor,a)-distanceKm(cursor,b) || a.id.localeCompare(b.id));
        const order = orders.shift()!;
        orderedStops.push({id:vehicle.id+'-'+order.id,kind:'DELIVERY',orderIds:[order.id],label:order.id,location:{latitude:order.latitude,longitude:order.longitude}});
        cursor=order;
      }
      const speed = profile==='FASTEST'?30:profile==='BALANCED'?25:20;
      const routeSegments: VehiclePlan['routeSegments'] = [];
      let origin = vehicle.currentPosition, originId=vehicle.id+'-origin';
      for(const stop of orderedStops) {
        const km=distanceKm(origin,stop.location);
        if(km>0.000001) routeSegments.push({id:vehicle.id+'-'+profile+'-'+(routeSegments.length+1),fromStopId:originId,toStopId:stop.id,
          geometry:{type:'LineString',coordinates:[[origin.longitude,origin.latitude],[stop.location.longitude,stop.location.latitude]]},
          distanceKm:km,durationMinutes:km/speed*60,geometrySource:'SCHEMATIC_DEMO'});
        origin={...stop.location,graphNodeId:null}; originId=stop.id;
      }
      const km=routeSegments.reduce((n,s)=>n+s.distanceKm,0);
      distance+=km; cost+=km*Number(vehicle.costPerKmVnd??2500);
      duration=Math.max(duration,routeSegments.reduce((n,s)=>n+s.durationMinutes,0)+assigned.get(vehicle.id)!.reduce((n,o)=>n+o.serviceTimeHours*60,0));
      return {vehicleId:vehicle.id,orderedStops,routeSegments};
    });
    return {id:state.sessionId+'-'+state.scenarioId+'-'+state.version+'-'+profile,generatedForSessionId:state.sessionId,generatedForStateVersion:state.version,
      content:{profile,vehiclePlans,unserved,metrics:{distanceKm:distance,durationMinutes:duration,fuelCostVnd:cost,exposureScore:null,onTimeRate:null},
        provenance:{source:'Dữ liệu demo',scenarioId:state.scenarioId,stateVersion:state.version}}};
  });
}
