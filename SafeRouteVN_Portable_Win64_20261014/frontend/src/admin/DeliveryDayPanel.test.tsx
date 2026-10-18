import {render,screen,fireEvent,waitFor,within} from '@testing-library/react';
import {it,expect,vi,beforeEach} from 'vitest';
import {DeliveryDayPanel,DeliveryDayComparison} from './DeliveryDayPanel';
import {basis,job} from '../integrations/member3/testFixtures';
import {adaptMetrics,forecastUnits} from '../integrations/member3/metricsAdapter';
const context=vi.hoisted(()=>({value:{} as any}));
vi.mock('../app/DispatchContext',()=>({useDispatch:()=>context.value}));
beforeEach(()=>{
  context.value={api:{deliveryDay:vi.fn().mockResolvedValue({})},snapshot:{decisionState:{scenarioId:'S2',orders:[{id:'O1',status:'WAITING'}]},backend:{executionView:{current_time:'2026-10-11T08:15:00+07:00',active_job_id:null,delivered_prefix:[],vehicles:[],delivery_day:{date:'2026-10-11',cutoff:'2026-10-11T08:00:00+07:00',complete:false,catalog:{O1:{pickup_location_id:'DEPOT'}},customers:{},depots:[{location_id:'DEPOT'}],queue:{},recoveries:[],pending_events:[],before_event:null}}}}};
});
it('does not offer the new-day action before completion',()=>{
  render(<DeliveryDayPanel/>);expect(screen.queryByRole('button',{name:'Bắt đầu ngày mới'})).toBeNull();
});
it('offers an eight oclock start for imported scenarios',()=>{
  delete context.value.snapshot.backend.executionView.delivery_day;
  context.value.snapshot.backend.run={source_kind:'IMPORTED_SCENARIO'};
  render(<DeliveryDayPanel/>);
  expect(screen.getByText(/Bắt đầu mô phỏng lúc 08:00/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'Bắt đầu ngày giao hàng'}));
  expect(context.value.api.deliveryDay).toHaveBeenCalledWith('enable',{});
});
it('offers a new day without requiring vehicles to return',async()=>{
  context.value.snapshot.backend.executionView.delivery_day.complete=true;
  render(<DeliveryDayPanel/>);fireEvent.click(screen.getByRole('button',{name:'Bắt đầu ngày mới'}));
  await waitFor(()=>expect(context.value.api.deliveryDay).toHaveBeenCalledWith('next_day',{}));
});
it('submits the customer, depot and urgent policy together',async()=>{
  render(<DeliveryDayPanel/>);
  fireEvent.change(screen.getByLabelText('Tên khách'),{target:{value:'An'}});
  fireEvent.change(screen.getByLabelText('Số điện thoại'),{target:{value:'0901234567'}});
  fireEvent.click(screen.getByRole('button',{name:'Thêm urgent + Re-optimize'}));
  await waitFor(()=>expect(context.value.api.deliveryDay).toHaveBeenCalledWith('add_order',expect.objectContaining({name:'An',phone:'0901234567',urgent:true,depot_id:'DEPOT',template_id:'O1'})));
});
it('distinguishes missing after-plan metrics from zero',()=>{
  context.value.snapshot.backend.executionView.delivery_day.before_event={total_distance_m:2500,total_travel_time_s:120,total_exposure:3,total_cost_vnd:1000};
  render(<DeliveryDayComparison/>);expect(screen.getByText('2.50')).toBeInTheDocument();
  expect(screen.getByText('Chờ chọn hoặc nhận kế hoạch mới sau sự kiện.')).toBeInTheDocument();
  expect(screen.getAllByText('—')).toHaveLength(8);
});

function previewComparison(){
  const snapshot=context.value.snapshot,view=snapshot.backend.executionView;
  view.planned_suffix_metrics=null;view.projected_whole_metrics=null;
  snapshot.backend.basis={...basis};view.basis=snapshot.backend.basis;
  view.delivery_day.before_event={total_cost_vnd:212126.79778026557,total_distance_m:84850.71911210624,total_exposure:27.187139486835637,total_travel_time_s:14407.739405};
  view.observed_metrics={cost_vnd:17407.717155211274,distance_m:6963.08686208451,relative_exposure_proxy:2.241753547210411,travel_time_us:'1200000000'};
  const proposal={id:'job-FASTEST',origin:{kind:'MEMBER3',sessionId:basis.session_id,inputBasis:{...basis}},
    nativeForecast:{jobView:job(),metrics:adaptMetrics({total_cost_vnd:200592.84495649295,total_distance_m:80237.13798259717,total_exposure:25.96589148491786,total_travel_time_s:14319.995879},'FORECAST_ONLY',forecastUnits)}};
  snapshot.planState={proposedAlternatives:[proposal],selectedAlternativeId:proposal.id};
  return {snapshot,view,proposal};
}

it('previews whole-trip cost before Accept, adding the observed prefix exactly once',()=>{
  const {proposal}=previewComparison();
  const forecastValues={...proposal.nativeForecast.metrics.values};
  Object.freeze(proposal.nativeForecast.metrics.values);
  render(<DeliveryDayComparison/>);
  expect(screen.getByText('212.127')).toBeInTheDocument();
  expect(screen.getByText('218.001')).toBeInTheDocument();
  expect(screen.getByText('+5.874')).toBeInTheDocument();
  expect(screen.getByText('87.20')).toBeInTheDocument();
  expect(screen.getByText('258.67')).toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('chưa Accept');
  // The right-hand alternatives continue to read the original solver forecast.
  expect(proposal.nativeForecast.metrics.values).toEqual(forecastValues);
  expect(proposal.nativeForecast.metrics.values.total_cost_vnd).toBe(200592.84495649295);
});

it('uses accepted whole-trip metrics after the head changes without adding the prefix again',()=>{
  const {snapshot,view}=previewComparison();snapshot.backend.basis={...basis,head_version:'2'};
  view.active_job_id='accepted';view.projected_whole_metrics={total_cost_vnd:218000.56211170423};
  render(<DeliveryDayComparison/>);
  expect(screen.getByText('218.001')).toBeInTheDocument();
  expect(screen.queryByText('235.408')).not.toBeInTheDocument();
  expect(screen.getByRole('status')).not.toHaveTextContent('chưa Accept');
});

it('keeps an unknown observed cost missing instead of counting it as zero',()=>{
  const {view}=previewComparison();delete view.observed_metrics.cost_vnd;
  render(<DeliveryDayComparison/>);
  const row=screen.getByText('Chi phí (VND)').closest('tr')!;
  expect(within(row).getAllByText('—')).toHaveLength(2);
});

it('does not preview an outdated or unsynchronised result',()=>{
  const {snapshot}=previewComparison();snapshot.backend.stale=true;
  render(<DeliveryDayComparison/>);
  expect(screen.queryByText('218.001')).not.toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('Đang đồng bộ');
});

it('keeps displayed rounded VND differences consistent with the two displayed totals',()=>{
  const {proposal}=previewComparison();proposal.nativeForecast.metrics.values.total_cost_vnd=198311.59709709173;
  render(<DeliveryDayComparison/>);
  const row=screen.getByText('Chi phí (VND)').closest('tr')!;
  expect(within(row).getByText('212.127')).toBeInTheDocument();
  expect(within(row).getByText('215.719')).toBeInTheDocument();
  expect(within(row).getByText('+3.592')).toBeInTheDocument();
});
it('always shows four columns without inventing baseline or after values',()=>{
  context.value.snapshot.backend.executionView.projected_whole_metrics={total_distance_m:5000};
  render(<DeliveryDayComparison/>);
  expect(screen.getAllByRole('columnheader').map(x=>x.textContent)).toEqual(['Chỉ số','Before','After','Chênh lệch']);
  expect(screen.getAllByText('—')).toHaveLength(12);
});
it('computes signed differences and preserves missing metrics',()=>{
  const view=context.value.snapshot.backend.executionView;
  view.delivery_day.before_event={total_distance_m:2500,total_exposure:0};
  view.projected_whole_metrics={total_distance_m:3000,total_exposure:0};
  render(<DeliveryDayComparison/>);
  expect(screen.getByText('+0.50')).toBeInTheDocument();
  expect(screen.getAllByText('0.00')).toHaveLength(3);
  expect(screen.getAllByText('—')).toHaveLength(6);
});
