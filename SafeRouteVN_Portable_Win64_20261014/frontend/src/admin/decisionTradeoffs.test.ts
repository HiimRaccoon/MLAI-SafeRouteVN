import {expect,it} from 'vitest';
import type {PlanProfile,ProposedAlternative} from '../shared/types/dispatch';
import {decisionSummary,numericTradeoffs} from './decisionTradeoffs';
import {adaptMetrics,forecastUnits} from '../integrations/member3/metricsAdapter';
import {basis,job} from '../integrations/member3/testFixtures';

function proposal(profile:PlanProfile,values:Record<string,number|null>):ProposedAlternative {
 return {id:profile,generatedForSessionId:basis.session_id,generatedForStateVersion:1,
  origin:{kind:'MEMBER3',profile,sessionId:basis.session_id,comparisonId:'comparison',jobId:profile,inputBasis:basis},
  nativeForecast:{source:'MEMBER3_HTTP',trajectory:{},segments:[],jobView:job(),metrics:adaptMetrics(values,'FORECAST_ONLY',forecastUnits)}};
}
it('handles a zero baseline and missing metrics without dividing by zero or inventing zeros',()=>{
 const candidate=(values:Record<string,number|null>)=>({nativeForecast:{metrics:{values}}}) as ProposedAlternative;
 const selected=candidate({total_travel_time_s:60,total_cost_vnd:0,total_exposure:null});
 const baseline=candidate({total_travel_time_s:0,total_cost_vnd:0,total_exposure:1});
 expect(numericTradeoffs(selected,baseline)).toEqual([
  'Tổng thời gian đội xe: tăng 1 phút (không tính % vì mốc so sánh bằng 0).',
  'Chi phí tuyến: bằng nhau (0 đ).',
  'Exposure proxy: chưa đủ dữ liệu để so sánh.'
 ]);
});

it('fills decision prose from the optimizer values and groups comparisons by profile',()=>{
 const candidate=(profile:PlanProfile,time:number,cost:number,risk:number)=>proposal(profile,{total_travel_time_s:time,total_cost_vnd:cost,total_exposure:risk});
 const fastest=candidate('FASTEST',11854.07208,198560.48060722093,28.735732457220475);
 const balanced=candidate('BALANCED',11863.062101,198311.59709709173,28.706084429658695);
 const safer=candidate('SAFER',14319.995879,200592.84495649295,25.96589148491786);
 expect(decisionSummary(balanced,[fastest,balanced,safer])).toEqual([
  'Tổng thời gian đội xe: 197,718 phút; cao hơn FASTEST; thấp hơn SAFER.',
  'Chi phí tuyến: 198.311,597 đ; thấp hơn FASTEST, SAFER.',
  'Exposure proxy: 28,706 proxy; thấp hơn FASTEST; cao hơn SAFER.'
 ]);
 balanced.nativeForecast!.metrics.values.total_cost_vnd=200592.84495649295;
 expect(decisionSummary(balanced,[fastest,balanced,safer])[1]).toBe('Chi phí tuyến: 200.592,845 đ; cao hơn FASTEST; bằng SAFER.');
});

it('does not invent missing values or comparisons when only the selected profile is available',()=>{
 const selected=proposal('BALANCED',{total_travel_time_s:0,total_cost_vnd:null,total_exposure:null});
 expect(decisionSummary(selected,[])).toEqual([
  'Tổng thời gian đội xe: 0 phút.',
  'Chi phí tuyến: chưa có dữ liệu.',
  'Exposure proxy: chưa có dữ liệu.'
 ]);
});
