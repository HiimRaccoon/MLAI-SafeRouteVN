import {act,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {beforeEach,expect,it,vi} from 'vitest';
import userEvent from '@testing-library/user-event';
beforeEach(()=>localStorage.removeItem('saferoute.explanationMode'));
import SelectedProfileExplanation from './SelectedProfileExplanation';
import type {DispatchSnapshot} from '../shared/types/dispatch';
import type {DispatchApi} from '../services/api/DispatchApi';
import {adaptJobForecast,parseJobForecast} from '../integrations/member3/forecastViewAdapter';
import {forecastView} from '../integrations/member3/forecastTestFixture';
import {basis} from '../integrations/member3/testFixtures';

function snapshot(selected='safer',cid='comparison') {
  const safer=adaptJobForecast(parseJobForecast(forecastView()),{basis,jobId:'job-return',profile:'SAFER',comparisonId:cid,vehicleIds:['V1'],deliveredPrefix:[]})!;
  safer.id='safer';const fastest=structuredClone(safer);fastest.id='fastest';
  if(fastest.origin?.kind==='MEMBER3')fastest.origin.profile='FASTEST';
  return {planState:{selectedAlternativeId:selected,proposedAlternatives:[safer,fastest]},backend:{basis,
    comparison:{session_id:basis.session_id,comparison_id:cid,input_basis:basis,status:'COMPLETED',outcome:{comparison:{status:'COMPARABLE'}}}}} as DispatchSnapshot;
}
function result(cid='comparison') {return {session_id:basis.session_id,comparison_id:cid,basis,status:'READY',model:'qwen3:4b',
  profiles:{SAFER:{sentences:['SAFER explanation'],fact_ids:['goal']},FASTEST:{sentences:['FASTEST explanation'],fact_ids:['goal']}},limitations:'Forecast only'};}

it('prefetches once and replaces the selected explanation without another request',async()=>{
  const request=vi.fn().mockResolvedValue(result());const api={profileExplanations:request} as unknown as DispatchApi;
  const view=render(<SelectedProfileExplanation snapshot={snapshot()} api={api}/>);
  await screen.findByText('SAFER explanation');
  view.rerender(<SelectedProfileExplanation snapshot={snapshot('fastest')} api={api}/>);
  expect(screen.getByText('FASTEST explanation')).toBeInTheDocument();
  expect(screen.queryByText('SAFER explanation')).not.toBeInTheDocument();expect(request).toHaveBeenCalledTimes(1);
});

it('ignores an old AI response after a new comparison replaces it',async()=>{
  let resolveOld!:(value:unknown)=>void;
  const request=vi.fn().mockImplementationOnce(()=>new Promise(r=>{resolveOld=r;})).mockResolvedValue(result('new-comparison'));
  const api={profileExplanations:request} as unknown as DispatchApi;
  const view=render(<SelectedProfileExplanation snapshot={snapshot()} api={api}/>);
  view.rerender(<SelectedProfileExplanation snapshot={snapshot('fastest','new-comparison')} api={api}/>);
  await screen.findByText('FASTEST explanation');
  await act(async()=>resolveOld({...result(),profiles:{FASTEST:{sentences:['OLD explanation']}}}));
  expect(screen.queryByText('OLD explanation')).not.toBeInTheDocument();
});

it('does not substitute a canned assessment when Ollama cannot be reached',async()=>{
  const api={profileExplanations:vi.fn().mockRejectedValue(new Error('Offline'))} as unknown as DispatchApi;
  render(<SelectedProfileExplanation snapshot={snapshot()} api={api}/>);
  await waitFor(()=>expect(screen.getByText(/AI local chưa sẵn sàng/)).toBeInTheDocument());
  expect(screen.getByRole('heading',{name:'Vì sao chọn SAFER?'})).toBeInTheDocument();
  expect(screen.queryByText(/Ưu tiên mức phơi nhiễm/)).not.toBeInTheDocument();
  expect(screen.queryAllByRole('listitem')).toHaveLength(0);
});


it('switches presentation without fetching again or changing selection, and remembers the chosen mode',async()=>{
 const request=vi.fn().mockResolvedValue(result());const api={profileExplanations:request} as unknown as DispatchApi;
 const state=snapshot(); const view=render(<SelectedProfileExplanation snapshot={state} api={api}/>);
 await screen.findByText('SAFER explanation');
 const user=userEvent.setup();
 expect(screen.getByRole('button',{name:'Giải thích nhanh'})).toHaveAttribute('aria-pressed','true');
 await user.click(screen.getByRole('button',{name:'Phân tích quyết định'}));
 expect(screen.getByRole('table')).toBeInTheDocument();
 expect(screen.getByText(/Chưa đủ ba phương án/)).toBeInTheDocument();
 expect(screen.getByText('Giả định và giới hạn')).toBeInTheDocument();
 expect(state.planState.selectedAlternativeId).toBe('safer');
 expect(request).toHaveBeenCalledTimes(1);
 expect(screen.queryByText('SAFER explanation')).not.toBeInTheDocument();
 await user.click(screen.getByRole('button',{name:'Giải thích nhanh'}));
 expect(screen.getByText('SAFER explanation')).toBeInTheDocument();
 expect(request).toHaveBeenCalledTimes(1);
 await user.click(screen.getByRole('button',{name:'Phân tích quyết định'}));
 view.unmount();
 render(<SelectedProfileExplanation snapshot={snapshot('fastest')} api={api}/>);
 expect(screen.getByRole('button',{name:'Phân tích quyết định'})).toHaveAttribute('aria-pressed','true');
 expect(screen.getByRole('heading',{name:'Phân tích quyết định · FASTEST'})).toBeInTheDocument();
 await user.click(screen.getByRole('button',{name:'Giải thích nhanh'}));
 expect(screen.queryByRole('table')).not.toBeInTheDocument();
});

it('shows calculated comparisons for all three profiles independently of AI availability',async()=>{
 const state=snapshot();
 const [safer,fastest]=state.planState.proposedAlternatives;
 const balanced=structuredClone(fastest); balanced.id='balanced';
 if(balanced.origin?.kind==='MEMBER3')balanced.origin.profile='BALANCED';
 state.planState.proposedAlternatives.push(balanced);
 for(const [p,time,cost,risk] of [[fastest,600,1000,10],[balanced,660,950,9],[safer,720,900,8]] as const){
  Object.assign(p.nativeForecast!.metrics.values,{total_travel_time_s:time,total_cost_vnd:cost,total_exposure:risk});
 }
 const api={profileExplanations:vi.fn().mockRejectedValue(new Error('Offline'))} as unknown as DispatchApi;
 render(<SelectedProfileExplanation snapshot={state} api={api}/>);
 await screen.findByText(/AI local chưa sẵn sàng/);
 await userEvent.setup().click(screen.getByRole('button',{name:'Phân tích quyết định'}));
 expect(screen.getByRole('table')).toHaveTextContent('12');
 expect(screen.getAllByText('Tổng thời gian đội xe: tăng 2 phút (20%).').length).toBeGreaterThan(0);
 expect(screen.getAllByText('Chi phí tuyến: giảm 100 đ (10%).').length).toBeGreaterThan(0);
 expect(screen.getAllByText('Exposure proxy: giảm 2 proxy (20%).').length).toBeGreaterThan(0);
 expect(screen.getAllByText(/không phải xác suất tai nạn/).length).toBeGreaterThan(0);
 expect(screen.getByRole('heading',{name:'Diễn giải từ số liệu'})).toBeInTheDocument();
 expect(screen.queryByText(/Chưa có diễn giải AI sẵn sàng/)).not.toBeInTheDocument();
});

it('renders the saved decision mode immediately without requesting AI, including a new comparison',async()=>{
 localStorage.setItem('saferoute.explanationMode','decision');
 const request=vi.fn().mockResolvedValue(result('new-comparison'));
 const api={profileExplanations:request} as unknown as DispatchApi;
 const view=render(<SelectedProfileExplanation snapshot={snapshot()} api={api}/>);
 expect(screen.getByRole('table')).toBeInTheDocument();
 expect(screen.getByRole('heading',{name:'Diễn giải từ số liệu'})).toBeInTheDocument();
 expect(screen.queryByText(/Đang chuẩn bị giải thích/)).not.toBeInTheDocument();
 view.rerender(<SelectedProfileExplanation snapshot={snapshot('fastest','new-comparison')} api={api}/>);
 expect(screen.getByRole('heading',{name:'Phân tích quyết định · FASTEST'})).toBeInTheDocument();
 expect(request).not.toHaveBeenCalled();
 await userEvent.setup().click(screen.getByRole('button',{name:'Giải thích nhanh'}));
 await screen.findByText('FASTEST explanation');
 expect(request).toHaveBeenCalledTimes(1);
});

it('stops AI polling in decision mode and resumes only when quick explanation is opened',async()=>{
 vi.useFakeTimers();
 try {
  const request=vi.fn().mockResolvedValueOnce({...result(),status:'PENDING',profiles:{}}).mockResolvedValue(result());
  const api={profileExplanations:request} as unknown as DispatchApi;
  render(<SelectedProfileExplanation snapshot={snapshot()} api={api}/>);
  await act(async()=>{});
  const signal=request.mock.calls[0][2] as AbortSignal;
  fireEvent.click(screen.getByRole('button',{name:'Phân tích quyết định'}));
  expect(signal.aborted).toBe(true);
  await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(request).toHaveBeenCalledTimes(1);
  expect(screen.queryByText(/Đang chuẩn bị giải thích/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'Giải thích nhanh'}));
  await act(async()=>{});
  expect(request).toHaveBeenCalledTimes(2);
  expect(screen.getByText('SAFER explanation')).toBeInTheDocument();
 } finally {vi.useRealTimers();}
});
