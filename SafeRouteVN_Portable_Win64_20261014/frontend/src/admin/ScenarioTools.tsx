import { useState } from 'react';
import type { DispatchSnapshot } from '../shared/types/dispatch';
import type { DispatchApi } from '../services/api/DispatchApi';
import { exportScenarioFixture, validateScenario } from '../shared/scenarioImport';

// Available only when the API advertises local scenario editing.
export default function ScenarioTools({snapshot,api,invoke,selected,onSelect}:{snapshot:DispatchSnapshot;api:DispatchApi;invoke:(op:()=>Promise<DispatchSnapshot>)=>Promise<void>;selected:string;onSelect:(id:string)=>void}) {
  const [error,setError]=useState('');
  const locked=!!snapshot.planState.activeAcceptedPlanId;
  const exportScenario=()=>{
    const fixture=exportScenarioFixture(snapshot);
    const blob=new Blob([JSON.stringify(fixture,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');
    a.href=url;a.download='saferoute-scenario.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  return <section className="panel" aria-label="Demo scenario editor">
    <h3>Demo scenario</h3>
    <p>Import a fixture or select a waiting order, then click the map to place it. Routes are schematic estimates.</p>
    <label>Import scenario JSON <input type="file" accept=".json,application/json" onChange={async e=>{
      const file=e.target.files?.[0];if(!file)return;setError('');
      try{if(file.size>2_000_000)throw new Error('Maximum file size is 2 MB.');const value=validateScenario(JSON.parse(await file.text()));await invoke(()=>api.importScenario!(value));onSelect('');}
      catch(reason){setError(reason instanceof Error?reason.message:'Invalid file');}finally{e.target.value='';}
    }}/></label>
    <button type="button" onClick={exportScenario}>Export scenario</button>
    <label>Place order <select aria-label="Place order on map" value={selected} disabled={locked} onChange={e=>onSelect(e.target.value)}>
      <option value="">Map navigation</option>{snapshot.decisionState.orders.filter(o=>o.status==='WAITING').map(o=><option key={o.id} value={o.id}>{o.id}</option>)}
    </select></label>
    {locked&&<p>Reset the demo before editing an accepted plan.</p>}
    {selected&&<p role="status">Click the map to place {selected}. Re-optimize after editing.</p>}
    {error&&<p role="alert">{error}</p>}
  </section>;
}
