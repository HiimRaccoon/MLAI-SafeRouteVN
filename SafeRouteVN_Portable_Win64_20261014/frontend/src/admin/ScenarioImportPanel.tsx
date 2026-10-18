import {useEffect,useState} from 'react';
import {useDispatch} from '../app/DispatchContext';
import type {M3Run} from '../integrations/member3/types';

export default function ScenarioImportPanel() {
  const {api,snapshot,pending,invoke}=useDispatch();
  const [runs,setRuns]=useState<M3Run[]>([]),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [chosen,setChosen]=useState('');
  const [loadId,setLoadId]=useState(()=>crypto.randomUUID());
  const active=snapshot?.backend?.run;
  // A load receipt may be committed even if its following world read fails.
  // After observing another session, opening this selection is a new intent;
  // replaying the old receipt would move Admin behind the Driver's successor.
  useEffect(()=>{setLoadId(crypto.randomUUID());},[snapshot?.decisionState.sessionId]);
  useEffect(()=>{let cancelled=false;if(api.importedScenarios)void api.importedScenarios().then(v=>{if(!cancelled)setRuns(v.imports);}).catch(()=>{if(!cancelled)setError('Chưa tải được danh sách scenario đã import.');});return()=>{cancelled=true;};},[api]);
  async function template() {
    setError('');
    try {
      const value=await api.scenarioTemplate!();
      const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));
      const a=document.createElement('a');a.href=url;a.download='saferoute-native-scenario.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(e){setError(e instanceof Error?e.message:String(e));}
  }
  return <section className="weather-run-panel scenario-import-panel" aria-label="Import scenario">
    <h3>Import scenario</h3>
    <p>Nạp file JSON theo mẫu của dự án: đơn hàng, xe và sự kiện trên mạng đường hiện có. Dùng context mô phỏng cố định trong mẫu.</p>
    <button type="button" onClick={()=>void template()} disabled={busy}>Tải JSON mẫu</button>
    <label>Chọn file scenario JSON <input type="file" accept=".json,application/json" disabled={busy||pending} onChange={async e=>{
      const input=e.currentTarget,file=input.files?.[0];if(!file)return;
      setBusy(true);setError('');
      try {
        if(file.size>900_000)throw new Error('File tối đa 900 KB.');
        const text=await file.text();const fixture=JSON.parse(text);
        if(!fixture || typeof fixture!=='object' || Array.isArray(fixture))throw new Error('Cần một object scenario JSON.');
        const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(file.name+'\n'+text));
        const request_id='import-'+Array.from(new Uint8Array(digest),v=>v.toString(16).padStart(2,'0')).join('');
        const run=await api.importNativeScenario!({request_id,name:file.name.slice(0,100),fixture});
        setRuns(old=>[run,...old.filter(r=>r.run_id!==run.run_id)]);setChosen(run.run_id);setLoadId(crypto.randomUUID());
      }catch(e){setError(e instanceof Error?e.message:String(e));}finally{input.value='';setBusy(false);}
    }}/></label>
    {busy&&<p role="status">Đang kiểm tra và lưu scenario…</p>}
    <label>Scenario đã import <select aria-label="Scenario đã import" value={chosen} onChange={e=>{setChosen(e.target.value);setLoadId(crypto.randomUUID());}}>
      <option value="">Chọn scenario</option>{runs.map(r=><option key={r.run_id} value={r.run_id}>{r.name ?? r.scenario_id} · {r.run_id.slice(-8)}</option>)}
    </select></label>
    <button disabled={!chosen||busy||pending||Boolean(snapshot?.backend?.playback&&!snapshot.backend.playback.fully_paused)}
      onClick={()=>void invoke(async()=>{
        const next=await api.loadWeatherRun!(chosen,loadId);
        setLoadId(crypto.randomUUID());
        return next;
      })}>Mở scenario đã import</button>
    {snapshot?.backend?.playback&&!snapshot.backend.playback.fully_paused&&<p>Pause replay trước khi mở scenario khác.</p>}
    {active?.source_kind==='IMPORTED_SCENARIO'&&<p role="status">Đang chạy: {active.name} · Dữ liệu mô phỏng đã import.</p>}
    {error&&<p role="alert">{error}</p>}
  </section>;
}
