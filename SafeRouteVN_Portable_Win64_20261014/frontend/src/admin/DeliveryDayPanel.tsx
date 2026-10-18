import {useState} from 'react';
import {useDispatch} from '../app/DispatchContext';
import {isAcceptableJob,proposalCurrency} from '../integrations/member3/revision';
import {executionMetrics,forecastUnits} from '../integrations/member3/metricsAdapter';
import './deliveryDay.css';

const clock=(time:string)=>new Date(time).toLocaleString('vi-VN',{timeZone:'Asia/Ho_Chi_Minh'});
export function DeliveryDayPanel(){
  const {snapshot,api}=useDispatch();
  const view=snapshot?.backend?.executionView,day=view?.delivery_day;
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  const [name,setName]=useState(''),[phone,setPhone]=useState('');
  const [address,setAddress]=useState(''),[depot,setDepot]=useState('DEPOT');
  const [urgent,setUrgent]=useState(true),[recovery,setRecovery]=useState(30);
  if(!snapshot||!view||!api.deliveryDay)return null;
  const invoke=async(action:string,data:Record<string,unknown>={})=>{
    setBusy(true);setError('');try{await api.deliveryDay!(action,data);}catch(e){setError(e instanceof Error?e.message:String(e));}finally{setBusy(false);}
  };
  const locked=busy||snapshot.backend?.stale||snapshot.backend?.mutationPending;
  if(!day)return <section className="panel"><h3>Ngày giao hàng</h3><p>{snapshot.backend?.run && snapshot.backend.run.source_kind !== 'IMPORTED_SCENARIO'?'Dùng thời gian của context weather hiện tại; mốc nhận đơn thường là 08:00.':'Bắt đầu mô phỏng lúc 08:00.'} Scenario {snapshot.decisionState.scenarioId}. Dùng xác nhận Deliver, thu hồi hàng và queue qua ngày.</p>
    <button disabled={locked||Boolean(view.observed_metrics)} onClick={()=>void invoke('enable')}>Bắt đầu ngày giao hàng</button>{error&&<p role="alert">{error}</p>}</section>;
  const ids=Object.keys(day.catalog),chosen=address||ids[0];
  const next=day.pending_events[0]?.timestamp;
  const span=next?Date.parse(next)-Date.parse(day.cutoff):0;
  const fraction=span>0?Math.max(0,Math.min(100,(Date.parse(view.current_time)-Date.parse(day.cutoff))/span*100)):100;
  return <section className="panel delivery-day-panel" aria-label="Delivery day">
    <h3>Ngày giao hàng · {day.date}</h3><p>{clock(view.current_time)} · {snapshot.decisionState.scenarioId}</p>
    {next&&<><label>08:00 → sự kiện {clock(next)}</label><progress aria-label="Thời gian đến sự kiện" value={fraction} max={100}/></>}
    {error&&<p role="alert">{error}</p>}
    {day.complete&&<div role="status"><p>Đã xử lý xong các đơn trong ngày.</p><button disabled={locked} onClick={()=>void invoke('next_day')}>Bắt đầu ngày mới</button></div>}
    <details><summary>Thêm đơn bất ngờ</summary>
      <form onSubmit={e=>{e.preventDefault();void invoke('add_order',{order_id:'NEW-'+crypto.randomUUID().slice(0,8),template_id:chosen,depot_id:depot,name,phone,urgent});}}>
        <label>Tên khách<input required maxLength={120} value={name} onChange={e=>setName(e.target.value)}/></label>
        <label>Số điện thoại<input required type="tel" maxLength={40} value={phone} onChange={e=>setPhone(e.target.value)}/></label>
        <label>Địa chỉ giao (điểm trong scenario)<select value={chosen} onChange={e=>setAddress(e.target.value)}>{ids.map(id=><option key={id}>{id}</option>)}</select></label>
        <label>Depot lấy hàng<select value={depot} onChange={e=>setDepot(e.target.value)}>{day.depots.map(d=><option key={d.location_id}>{d.location_id}</option>)}</select></label>
        <label><input type="checkbox" checked={urgent} onChange={e=>setUrgent(e.target.checked)}/>Urgent — quay về lấy và giao trước</label>
        <button disabled={locked}>{urgent?'Thêm urgent + Re-optimize':'Thêm đơn thường'}</button>
      </form>
    </details>
    <details><summary>Depot và sự cố xe</summary>
      <label>Điểm đặt depot mới<select value={chosen} onChange={e=>setAddress(e.target.value)}>{ids.map(id=><option key={id}>{id}</option>)}</select></label>
      <button disabled={locked} onClick={()=>void invoke('add_depot',{depot_id:'DEPOT-'+(day.depots.length+1),location_order_id:chosen})}>Thêm depot</button>
      <label>Thu hồi sau (phút)<input type="number" min={1} max={1440} value={recovery} onChange={e=>setRecovery(Number(e.target.value))}/></label>
      {view.vehicles.map(v=><button key={v.vehicle_id} disabled={locked||v.availability!=='AVAILABLE'} onClick={()=>void invoke('unavailable',{vehicle_id:v.vehicle_id,recovery_minutes:recovery})}>{v.vehicle_id} unavailable + Re-optimize</button>)}
    </details>
    <details><summary>Đơn hàng và khách hàng ({ids.length})</summary>
      {snapshot.decisionState.orders.map(o=><div key={o.id} style={{borderBottom:'1px solid #dde3ee',padding:'8px 0'}}>
        <strong>{o.id}</strong><p>{day.customers[o.id]?.name||'Chưa có tên'} · {day.customers[o.id]?.phone||'Chưa có SĐT'} · {o.status}</p>
        <form onSubmit={e=>{e.preventDefault();const f=new FormData(e.currentTarget);void invoke('customer',{order_id:o.id,name:f.get('name'),phone:f.get('phone')});}}>
          <input aria-label={'Tên khách '+o.id} name="name" required maxLength={120} placeholder="Tên khách" defaultValue={day.customers[o.id]?.name}/>
          <input aria-label={'SĐT '+o.id} name="phone" required type="tel" maxLength={40} placeholder="Số điện thoại" defaultValue={day.customers[o.id]?.phone}/>
          <button disabled={locked}>Lưu khách hàng</button>
        </form>
        {o.status==='WAITING'&&<><select aria-label={'Depot '+o.id} disabled={locked} value={day.catalog[o.id]?.pickup_location_id} onChange={e=>void invoke('assign_depot',{order_id:o.id,depot_id:e.target.value})}>{day.depots.map(d=><option key={d.location_id}>{d.location_id}</option>)}</select>
          {!day.queue[o.id]&&<button disabled={locked} onClick={()=>void invoke('defer',{order_id:o.id})}>Chuyển ngày sau</button>}</>}
      </div>)}
    </details>
    <h4>Queue ngày sau ({Object.keys(day.queue).length})</h4>
    {Object.entries(day.queue).map(([id,q])=><p key={id}>{id} · {q.depot_id} · {q.date} · {q.reason}</p>)}
    {day.recoveries.map(r=><p key={r.order_id}>{r.order_id} → {r.depot_id} · {r.status==='RECOVERING'?'Đang thu hồi':'Đã về depot'} · {clock(r.ready_at)}</p>)}
    {!view.active_job_id&&day.recoveries.some(r=>r.status==='RECOVERING')&&<button disabled={locked} onClick={()=>void invoke('wait_recovery')}>Tới mốc thu hồi</button>}
  </section>;
}

export function DeliveryDayComparison(){
  const {snapshot}=useDispatch();const view=snapshot?.backend?.executionView,day=view?.delivery_day;
  if(!day||!view)return null;
  const before=day.before_event;
  const selected=snapshot.planState?.proposedAlternatives.find(p=>p.id===snapshot.planState.selectedAlternativeId);
  const fresh=!snapshot.backend?.stale&&!snapshot.backend?.error&&!snapshot.backend?.mutationPending;
  const preview=fresh&&selected?.nativeForecast&&proposalCurrency(selected,snapshot)==='CURRENT'&&
    isAcceptableJob(selected.nativeForecast.jobView,snapshot.backend!.basis)?selected.nativeForecast:undefined;
  let after:Record<string,number>|null=before&&fresh?view.projected_whole_metrics:null;
  if(before&&preview){
    const prefix=executionMetrics(view).observed.values;
    const mapping:[string,string,number][]=[['total_distance_m','distance_m',1],['total_travel_time_s','travel_time_us',1e6],['total_exposure','relative_exposure_proxy',1],['total_cost_vnd','cost_vnd',1]];
    after={};
    for(const [key,observed,scale] of mapping){
      const a=prefix[observed],b=preview.metrics.values[key];
      if(a!==null&&a!==undefined&&b!==null&&b!==undefined&&preview.metrics.units[key]===forecastUnits[key as keyof typeof forecastUnits])after[key]=a/scale+b;
    }
  }
  const metric=(values:Record<string,number>|null|undefined,key:string,scale:number)=>typeof values?.[key]==='number'&&Number.isFinite(values[key])?values[key]/scale:null;
  const format=(value:number|null,key:string)=>value===null?'—':key==='total_cost_vnd'?value.toLocaleString('vi-VN',{maximumFractionDigits:0}):value.toFixed(2);
  const rows:[string,string,number][]=[['Quãng đường (km)','total_distance_m',1000],['Thời gian chạy (phút)','total_travel_time_s',60],['Exposure proxy','total_exposure',1],['Chi phí (VND)','total_cost_vnd',1]];
  return <section className="panel comparison delivery-comparison"><h3>Before / After event</h3><p>Before: toàn hành trình nếu không có sự kiện. After: phần đã chạy + {preview?'phương án đang chọn':'kế hoạch đã nhận'} sau sự kiện.</p>
    <div className="comparison-table-wrapper"><table className="comparison-table"><thead><tr><th>Chỉ số</th><th>Before</th><th>After</th><th>Chênh lệch</th></tr></thead><tbody>{rows.map(([label,key,scale])=>{
      const rawBefore=metric(before,key,scale),rawAfter=metric(after,key,scale);
      const b=key==='total_cost_vnd'&&rawBefore!==null?Math.round(rawBefore):rawBefore;
      const a=key==='total_cost_vnd'&&rawAfter!==null?Math.round(rawAfter):rawAfter;
      const delta=b!==null&&a!==null?a-b:null;
      return <tr key={key}><td>{label}</td><td>{format(b,key)}</td><td className="after-col">{format(a,key)}</td><td>{delta!==null&&delta>0?'+':''}{format(delta,key)}</td></tr>;
    })}</tbody></table></div>
    <p role="status">{!before?'Chưa có số liệu Before: cần nhận kế hoạch trước khi kích hoạt sự kiện.':!fresh?'Đang đồng bộ dữ liệu so sánh.':preview?'Dự kiến theo phương án đang chọn, chưa Accept. After = phần đã chạy + tuyến còn lại.':!after?'Chờ chọn hoặc nhận kế hoạch mới sau sự kiện.':'Đang so sánh toàn hành trình trước và sau sự kiện. Chênh lệch = After − Before.'}</p>
    <p>Chi phí toàn hành trình = chi phí đã chạy + chi phí tuyến còn lại, theo đơn giá từng xe.</p>
    <p>Đã giao: {view.delivered_prefix.length} · Chờ ngày sau: {Object.keys(day.queue).length}</p></section>;
}
