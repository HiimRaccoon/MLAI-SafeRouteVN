import type {DispatchSnapshot, PlanProfile, ProposedAlternative} from '../shared/types/dispatch';
import {sameBasis} from '../integrations/member3/revision';
import {physicalRouteKey} from './profileExplanation';
export const decisionProfiles: PlanProfile[] = ['FASTEST','BALANCED','SAFER'];
export const decisionMetrics = [
  {key:'total_travel_time_s',label:'Tổng thời gian đội xe',unit:'phút',factor:1/60},
  {key:'total_cost_vnd',label:'Chi phí tuyến',unit:'đ',factor:1},
  {key:'total_exposure',label:'Exposure proxy',unit:'proxy',factor:1},
  {key:'total_distance_m',label:'Quãng đường',unit:'km',factor:1/1000},
  {key:'total_soft_lateness_s',label:'Trễ so với hạn ưu tiên',unit:'giây',factor:1}
];
export const decisionNumber = (value:number) => value.toLocaleString('vi-VN',{maximumFractionDigits:3});
export function decisionCandidates(snapshot: DispatchSnapshot) {
  const group=snapshot.backend?.comparison;
  return decisionProfiles.map(profile => snapshot.planState.proposedAlternatives.find(p =>
    p.origin?.kind==='MEMBER3' && p.origin.profile===profile && p.origin.comparisonId===group?.comparison_id &&
    sameBasis(p.origin.inputBasis,group.input_basis) && p.nativeForecast?.jobView.plan_available &&
    p.nativeForecast.jobView.validation.valid===true));
}
export function numericTradeoffs(selected: ProposedAlternative, peer: ProposedAlternative) {
  const values=selected.nativeForecast!.metrics.values, baseline=peer.nativeForecast!.metrics.values;
  return decisionMetrics.slice(0,3).map(metric=>{
    const a=values[metric.key], b=baseline[metric.key];
    if(a==null || b==null)return `${metric.label}: chưa đủ dữ liệu để so sánh.`;
    const delta=a-b;
    if(Math.abs(delta)<1e-9)return `${metric.label}: bằng nhau (${decisionNumber(a*metric.factor)} ${metric.unit}).`;
    return `${metric.label}: ${delta>0?'tăng':'giảm'} ${decisionNumber(Math.abs(delta)*metric.factor)} ${metric.unit}`+
      (b>0?` (${decisionNumber(Math.abs(delta)/b*100)}%)`:' (không tính % vì mốc so sánh bằng 0)')+'.';
  });
}
export function sameDecisionRoute(a:ProposedAlternative,b:ProposedAlternative){return physicalRouteKey(a)===physicalRouteKey(b);}

export function decisionSummary(selected:ProposedAlternative,peers:readonly ProposedAlternative[]){
  return decisionMetrics.slice(0,3).map(metric=>{
    const value=selected.nativeForecast?.metrics.values[metric.key];
    if(value==null || !Number.isFinite(value))return `${metric.label}: chưa có dữ liệu.`;
    const comparisons=new Map<string,string[]>();
    for(const peer of peers){
      if(peer.id===selected.id || peer.origin?.kind!=='MEMBER3')continue;
      const other=peer.nativeForecast?.metrics.values[metric.key];
      if(other==null || !Number.isFinite(other))continue;
      const relation=Math.abs(value-other)<1e-9?'bằng':value<other?'thấp hơn':'cao hơn';
      comparisons.set(relation,[...(comparisons.get(relation)??[]),peer.origin.profile]);
    }
    const relation=[...comparisons].map(([label,profiles])=>`${label} ${profiles.join(', ')}`).join('; ');
    return `${metric.label}: ${decisionNumber(value*metric.factor)} ${metric.unit}${relation?`; ${relation}`:''}.`;
  });
}
