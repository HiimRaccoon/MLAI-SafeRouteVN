import {describe,it,expect} from 'vitest';
import {render,screen} from '@testing-library/react';
import {MockStateEngine} from '../mocks/engine/MockStateEngine';
import {acceptedView} from '../integrations/member3/acceptedTestFixture';
import {adaptAcceptedExecution} from '../integrations/member3/executionViewAdapter';
import {forecastView} from '../integrations/member3/forecastTestFixture';
import {adaptJobForecast} from '../integrations/member3/forecastViewAdapter';
import {nativeKpis} from './nativeKpis';
import {NativeKpiCards} from './NativeKpiCards';
function fixture() {
 const snapshot=new MockStateEngine().getSnapshot(), view=acceptedView();
 snapshot.decisionState.sessionId=view.basis.session_id;
 snapshot.backend={source:'MEMBER3_HTTP',baseUrl:'http://localhost',basis:view.basis,executionView:view,executionMode:'SIMULATED_REPLAY',realWorldObservation:false};
 Object.assign(snapshot,adaptAcceptedExecution(view));
 return {snapshot,view};
}
describe('native KPI sources',()=>{
 it('renders projected accepted metrics even though legacy acceptedPlans is empty',()=>{
  const {snapshot,view}=fixture();
  view.projected_whole_metrics={total_travel_time_s:720,total_cost_vnd:12345,total_exposure:42};
  expect(snapshot.planState.acceptedPlans).toEqual([]);
  render(<NativeKpiCards snapshot={snapshot}/>);
  expect(screen.getByText('12 min')).toBeInTheDocument();
  expect(screen.getByText('12,345 VND')).toBeInTheDocument();
  expect(screen.getByText('42 proxy')).toBeInTheDocument();
  expect(screen.queryByText('Đang áp dụng · toàn tuyến')).not.toBeInTheDocument();
  expect(screen.getByLabelText('Fleet travel time')).toHaveAttribute('title',expect.stringContaining('Đang áp dụng · toàn tuyến'));
  expect(screen.queryByText(/Fuel cost/)).not.toBeInTheDocument();
 });
 it('uses current selected forecast with a preview label and rejects stale previews',()=>{
  const {snapshot}=fixture();
  const forecast=forecastView();
  const selected=adaptJobForecast(forecast,{basis:snapshot.backend!.basis,jobId:forecast.job_id,profile:forecast.profile as 'SAFER',comparisonId:'comparison-test',vehicleIds:['V1'],deliveredPrefix:[]})!;
  snapshot.planState.proposedAlternatives=[selected];
  expect(nativeKpis(snapshot,selected).scope).toBe('Dự kiến · phương án đang chọn');
  expect(nativeKpis(snapshot,selected).metrics?.values.total_cost_vnd).toBe(100);
  snapshot.backend!.stale=true;
  expect(nativeKpis(snapshot,selected).metrics).toBeNull();
 });
 it('labels remaining metrics and never turns missing values into zero',()=>{
  const {snapshot,view}=fixture();
  view.planned_suffix_metrics={total_travel_time_s:60};
  const data=nativeKpis(snapshot);
  expect(data.scope).toBe('Đang áp dụng · phần còn lại');
  expect(data.metrics?.values.total_cost_vnd).toBeNull();
  expect(data.onTime).toBeNull();
 });
 it('computes on-time coverage from exact service offsets and preferred due, including delivered orders',()=>{
  const {snapshot,view}=fixture();
  const [first,second]=snapshot.decisionState.orders;
  const epoch='2026-09-27T21:00:00+07:00';
  snapshot.backend!.scenarios=[{id:snapshot.decisionState.scenarioId,initialTime:epoch,orderCount:2,vehicleCount:1,fixtureSha256:'a'.repeat(64)}];
  first.preferredDue='2026-09-27T21:01:00+07:00';
  second.preferredDue='2026-09-27T21:01:00+07:00';
  second.deliveredAt='2026-09-27T21:02:00+07:00';
  view.planned_served_suffix=[first.id]; view.delivered_prefix=[second.id];
  const routes=view.accepted_trajectory!.vehicle_routes as Array<{actions:unknown[]}>;
  routes[0].actions.push({kind:'SERVICE',order_id:first.id,end_us:'30000000'});
  expect(nativeKpis(snapshot).onTime).toBe(50);
  snapshot.backend!.scenarios=undefined;
  expect(nativeKpis(snapshot).onTime).toBeNull();
 });
});
