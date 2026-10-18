import type { MockStateEngine } from '../mocks/engine/MockStateEngine';
export function arrive(engine: MockStateEngine, vehicleId?: string) {
  const s=engine.getSnapshot(),p=s.planState.acceptedPlans.find(p=>p.id===s.planState.activeAcceptedPlanId);
  const minutes=Math.max(0,...(p?.plan.vehiclePlans.filter(v=>!vehicleId||v.vehicleId===vehicleId).map(v=>{
    const progress=s.executionState.progressByPlanId[p.id][v.vehicleId];
    return v.routeSegments.filter(e=>e.toStopId===progress.currentStopId).reduce((n,e)=>n+e.durationMinutes,0)-(progress.travel?.elapsedMinutes??0);
  })??[]));
  engine.advanceDemoClock(minutes+0.001);
}
