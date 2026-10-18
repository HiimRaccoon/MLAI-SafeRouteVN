import { useEffect, useRef, useState } from 'react';
import { useDispatch } from '../app/DispatchContext';
import type { M3Run } from '../integrations/member3/types';
import type { ScenarioId } from '../shared/types/scenario';

const key='saferoute.weather-run.intent.v1';
interface Intent { requestId:string; scenarioId:ScenarioId; eventAfterMinutes:number; runId?:string; loadRequestId:string }
function readIntent():Intent|undefined { try { return JSON.parse(sessionStorage.getItem(key) ?? 'null') ?? undefined; } catch { return undefined; } }
const time=(value:string)=>new Date(value).toLocaleString('vi-VN',{timeZone:'Asia/Ho_Chi_Minh'});

export default function WeatherRunPanel() {
  const {api,snapshot,pending,invoke}=useDispatch();
  const [scenario,setScenario]=useState<ScenarioId>('S2');
  const [minutes,setMinutes]=useState(15);
  const [intent,setIntent]=useState<Intent|undefined>(readIntent);
  const [run,setRun]=useState<M3Run>();
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const active=snapshot?.backend?.run?.source_kind==='IMPORTED_SCENARIO' ? undefined : snapshot?.backend?.run;
  const previousActive=useRef(active?.run_id);
  useEffect(()=>{
    if(previousActive.current && !active) {
      sessionStorage.removeItem(key);setIntent(undefined);setRun(undefined);setError('');
    }
    previousActive.current=active?.run_id;
  },[active?.run_id]);
  function save(value:Intent) { sessionStorage.setItem(key,JSON.stringify(value)); setIntent(value); }
  async function create(retry=false) {
    if (!api.createWeatherRun) return;
    setBusy(true);setError('');
    try {
      const value=retry && intent ? intent : {requestId:crypto.randomUUID(),loadRequestId:crypto.randomUUID(),scenarioId:scenario,eventAfterMinutes:minutes};
      save(value); const result=await api.createWeatherRun(value); save({...value,runId:result.run_id});setRun(result);
    } catch(e) {setError(e instanceof Error?e.message:String(e));} finally {setBusy(false);}
  }
  useEffect(()=>{
    if (!intent?.runId || !api.weatherRun) return;
    let stopped=false;let timer:ReturnType<typeof setTimeout>;
    const poll=async()=>{try {const value=await api.weatherRun!(intent.runId!);if(stopped)return;setRun(value);setError('');if(value.status==='BUILDING')timer=setTimeout(poll,2000);}
      catch(e){if(stopped)return;setError(e instanceof Error?e.message:String(e));timer=setTimeout(poll,5000);}};
    void poll();return()=>{stopped=true;clearTimeout(timer);};
  },[api,intent?.runId]);
  if (!api.createWeatherRun) return null;
  const shown=active ?? run;
  return <section className="weather-run-panel" aria-label="Phiên với weather cập nhật">
    <h3>Weather cập nhật · Phiên chạy mới</h3>
    <p>Dự báo thời tiết theo vùng · Giao thông ước lượng · Thời gian mô phỏng riêng</p>
    <div className="weather-run-actions">
      <label>Kịch bản <select value={scenario} onChange={e=>setScenario(e.target.value as ScenarioId)} disabled={busy || run?.status==='BUILDING'}>
        <option value="S0">Ít đơn</option><option value="S1">Giao hàng thường</option><option value="S2">Thêm đơn khẩn</option><option value="S3">Xe không khả dụng</option><option value="S4">Mưa mô phỏng</option>
      </select></label>
      <label>Sự kiện sau (phút) <input aria-label="Sự kiện sau (phút)" type="number" min={1} max={120} value={minutes} onChange={e=>setMinutes(Number(e.target.value))}/></label>
      <button disabled={busy || pending || run?.status==='BUILDING' || !Number.isInteger(minutes) || minutes<1 || minutes>120} onClick={()=>void create()}>Fetch weather &amp; prepare session</button>
      {error && intent && !intent.runId && <button onClick={()=>void create(true)} disabled={busy}>Thử lại yêu cầu</button>}
      {!active && run?.status==='READY' && intent?.runId===run.run_id && <button disabled={pending || busy} onClick={()=>void invoke(()=>api.loadWeatherRun!(run.run_id,intent.loadRequestId))}>Open new session</button>}
      {active && <button disabled={pending || busy} onClick={()=>void invoke(()=>api.loadScenario(snapshot!.decisionState.scenarioId))}>Hủy phiên</button>}
    </div>
    {error && <p role="alert">{error}</p>}
    {shown && <div role="status"><p>{active ? `Đang chạy ${active.scenario_id} · Context weather cập nhật` : shown.message} · Mốc mô phỏng: {time(shown.epoch)}</p>
      {shown.weather && <p>Lấy lúc: {time(shown.weather.fetched_at)} · Dữ liệu tại: {time(shown.weather.valid_at)} · Hạn dùng để tạo phiên: {time(shown.weather.expires_at)}<br/>
        {shown.weather.provider} · {shown.weather.region_count} vùng · Snapshot được giữ cố định trong phiên · <a href="https://open-meteo.com/" target="_blank" rel="noreferrer">Weather data by Open-Meteo (CC BY 4.0)</a></p>}
    </div>}
    {active && <p>Context: {active.run_id} · Session: {snapshot?.decisionState.sessionId}.</p>}
  </section>;
}
