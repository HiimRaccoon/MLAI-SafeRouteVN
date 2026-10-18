// Tiny renderer/progress fixture only. No runtime call or SDK certification.
import type { DispatchSnapshot, RouteSegment, VehiclePlan } from "../shared/types/dispatch";
import { MockStateEngine } from "../mocks/engine/MockStateEngine";
import { MockDispatchApi, MOCK_STORAGE_KEY, type StorageLike } from "../services/api/MockDispatchApi";

export function tinySuppliedPlan(snapshot: DispatchSnapshot): DispatchSnapshot {
  if(snapshot.decisionState.scenarioId!=="S0" || snapshot.decisionState.orders.length!==3 ||
    snapshot.decisionState.context.rain || snapshot.decisionState.orders.some(o=>o.status!=="WAITING")) return snapshot;
  for(const proposal of snapshot.planState.proposedAlternatives){
    const content=proposal.content!;
    content.metrics={distanceKm:36.5,durationMinutes:79,exposureScore:71,onTimeRate:null,fuelCostVnd:91182};
    content.provenance={source:"Member 2 offline runtime",scenarioId:"S0",stateVersion:snapshot.decisionState.version,computedAt:"2026-09-27T21:00:00+07:00"};
    content.vehiclePlans=[['V1',['O002']],['V2',['O003','O001']]].map(([vehicleId,ids])=>{
      const id=vehicleId as string,orderIds=ids as string[];
      const depot={longitude:106.7162,latitude:10.8023};
      const orderedStops:VehiclePlan['orderedStops']=[{id:id+'-depot',kind:'DEPOT_PICKUP',orderIds,label:'Depot',location:depot},...orderIds.map(orderId=>{
        const order=snapshot.decisionState.orders.find(o=>o.id===orderId)!;
        return{id:id+'-'+orderId,kind:'DELIVERY' as const,orderIds:[orderId],label:orderId,location:{longitude:order.longitude,latitude:order.latitude}};
      })];
      const routeSegments:RouteSegment[]=orderedStops.slice(1).flatMap((stop,i)=>{
        const from=orderedStops[i],a:[number,number]=[from.location.longitude,from.location.latitude],b:[number,number]=[stop.location.longitude,stop.location.latitude];
        const mid:[number,number]=[(a[0]+b[0])/2,(a[1]+b[1])/2];
        return[a,mid].map((start,n)=>({id:`${id}-leg-${i+1}-edge-${n}`,fromStopId:from.id,toStopId:stop.id,geometry:{type:'LineString' as const,coordinates:[start,n===0?mid:b]},distanceKm:.3+n*.1,durationMinutes:1+n,geometrySource:'MEMBER2_SUPPLIED' as const}));
      });
      const last=orderedStops.at(-1)!;
      routeSegments.push({id:id+'-return',fromStopId:last.id,toStopId:id+'-return-depot',geometry:{type:'LineString',coordinates:[[last.location.longitude,last.location.latitude],[depot.longitude,depot.latitude]]},distanceKm:.5,durationMinutes:2,geometrySource:'MEMBER2_SUPPLIED'});
      return{vehicleId:id,orderedStops,routeSegments};
    });
  }
  return snapshot;
}
export function tinySuppliedEngine(){return new MockStateEngine(tinySuppliedPlan(new MockStateEngine().optimize()));}
export class TinySuppliedApi extends MockDispatchApi {
  private readonly fixtureStorage:StorageLike;
  constructor(options:{storage:StorageLike}){super(options);this.fixtureStorage=options.storage;}
  override async optimize(){const s=tinySuppliedPlan(await super.optimize());this.fixtureStorage.setItem(MOCK_STORAGE_KEY,JSON.stringify({schemaVersion:1,snapshot:s}));return s;}
}
