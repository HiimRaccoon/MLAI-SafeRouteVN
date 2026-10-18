import { describe, expect, it, vi } from "vitest";
import { BackendDispatchApi } from "./BackendDispatchApi";
import { Member3Client } from "../../integrations/member3/client";
import { createMapScene } from "../../shared/components/mapScene";
import type { M3Vehicle, M3Projection, M3OrdersView } from "../../integrations/member3/types";
import { comparison } from "../../integrations/member3/testFixtures";
import { forecastView } from "../../integrations/member3/forecastTestFixture";
import { canAcceptSelectedPlan } from "../../integrations/member3/revision";
import { Member3Error } from "../../integrations/member3/errors";

it('binds built-in scenario switches to the admin session and preserves the source after a lost response',async()=>{
  const s=server();const api=new BackendDispatchApi({client:s.client,storage:s.storage});
  await api.loadScenario('S0');const source=s.state.basis.session_id;
  const fetcher:typeof fetch=async(url,options)=>{
    const reply=await s.fetcher(url,options);
    if(String(url).endsWith('/S1/load'))throw new TypeError('lost reply');
    return reply;
  };
  const failing=new BackendDispatchApi({client:new Member3Client({fetch:fetcher,token:()=> 'unit-only'}),storage:s.storage});
  await expect(failing.loadScenario('S1')).rejects.toMatchObject({code:'NETWORK_ERROR'});
  const first=s.requests.filter(r=>r.path.endsWith('/S1/load')).at(-1)!;
  expect(first.body).toMatchObject({replace_session_id:source});
  const restored=new BackendDispatchApi({client:s.client,storage:s.storage});
  await restored.getSnapshot();
  expect(s.requests.filter(r=>r.path.endsWith('/S1/load')).at(-1)!.body).toEqual(first.body);
});

it('reopens the scenario when a saved run belongs to another portable installation', async () => {
  const s=server();
  await new BackendDispatchApi({client:s.client,storage:s.storage}).loadScenario('S1');
  const key=[...s.values.keys()].find(k=>k.includes('session.v1'))!;
  const saved=JSON.parse(s.values.get(key)!);
  saved.session.run_id='run-'+'a'.repeat(32);
  s.storage.setItem(key,JSON.stringify(saved));
  vi.spyOn(s.client,'run').mockRejectedValue(new Member3Error('RUN_NOT_FOUND','Run not found',404));
  s.requests.length=0;
  const next=await new BackendDispatchApi({client:s.client,storage:s.storage}).getSnapshot();
  expect(next.decisionState.scenarioId).toBe('S1');
  expect(s.requests.filter(r=>r.method==='POST').map(r=>r.path)).toEqual(['/api/scenarios/S1/load']);
  const archive=[...s.values.entries()].find(([k])=>k.includes(':archive:'))!;
  expect(JSON.parse(archive[1])).toMatchObject({reason:'SAVED_SESSION_NOT_FOUND',pointer:saved});
  expect(JSON.parse(s.values.get(key)!).session.run_id).toBeUndefined();
});

it('archives an unfinished command without replaying it when its saved session is missing', async () => {
  const s=server();
  await new BackendDispatchApi({client:s.client,storage:s.storage}).loadScenario('S1');
  const pending={requestId:'old-accept',sessionId:s.state.basis.session_id,operation:'accept',body:{}};
  const commandKey=`saferoute.member3.command.v1:${s.client.baseUrl}`;
  s.storage.setItem(commandKey,JSON.stringify(pending));
  vi.spyOn(s.client,'state').mockRejectedValueOnce(new Member3Error('SESSION_NOT_FOUND','Session not found',404));
  s.requests.length=0;
  const api=new BackendDispatchApi({client:s.client,storage:s.storage});
  expect((await api.getSnapshot()).decisionState.scenarioId).toBe('S1');
  expect(s.requests.filter(r=>r.method==='POST').map(r=>r.path)).toEqual(['/api/scenarios/S1/load']);
  const archive=[...s.values.entries()].find(([k])=>k.includes(':archive:'))!;
  expect(JSON.parse(archive[1]).pendingCommand).toEqual(pending);
  expect(JSON.parse(s.values.get(commandKey)!)).toBeNull();
});

it.each(['FORBIDDEN','RUNTIME_TIMEOUT','NETWORK_ERROR'])('does not replace saved sessions after %s',async code=>{
  const s=server();
  await new BackendDispatchApi({client:s.client,storage:s.storage}).loadScenario('S1');
  const key=[...s.values.keys()].find(k=>k.includes('session.v1'))!;
  const saved=s.values.get(key);
  vi.spyOn(s.client,'state').mockRejectedValue(new Member3Error(code,'Unavailable',503));
  s.requests.length=0;
  await expect(new BackendDispatchApi({client:s.client,storage:s.storage}).getSnapshot()).rejects.toMatchObject({code});
  expect(s.values.get(key)).toBe(saved);
  expect(s.requests.filter(r=>r.method==='POST')).toEqual([]);
});

it('preserves the missing session when its browser archive cannot be written',async()=>{
  const s=server();
  await new BackendDispatchApi({client:s.client,storage:s.storage}).loadScenario('S1');
  const key=[...s.values.keys()].find(k=>k.includes('session.v1'))!;
  const saved=s.values.get(key);
  vi.spyOn(s.client,'state').mockRejectedValue(new Member3Error('SESSION_NOT_FOUND','Missing',404));
  s.requests.length=0;
  const storage={...s.storage,setItem:()=>{throw new Error('Storage full');}};
  await expect(new BackendDispatchApi({client:s.client,storage}).getSnapshot()).rejects.toMatchObject({code:'COMMAND_STORAGE_UNAVAILABLE'});
  expect(s.values.get(key)).toBe(saved);
  expect(s.requests.filter(r=>r.method==='POST')).toEqual([]);
});

it('opens the assigned Driver session without creating a dispatcher session', async () => {
  const s = server();
  const loaded = await s.client.loadScenario('S0', 'assigned-driver');
  const before = s.requests.filter(r => r.method === 'POST').length;
  const api = new BackendDispatchApi({client: s.client, storage: s.storage, assignedSession: loaded.session});
  const snapshot = await api.getSnapshot();
  expect(snapshot.backend?.basis.session_id).toBe(loaded.session.session_id);
  expect(s.requests.filter(r => r.method === 'POST')).toHaveLength(before);
});

it('recovers Driver startup after readiness fails without publishing an unverified world',async()=>{
  vi.useFakeTimers();
  const s=server();
  const loaded=await s.client.loadScenario('S0','driver-startup');
  const ready=vi.spyOn(s.client,'ready').mockRejectedValue(new Member3Error('BACKEND_NOT_READY','Restarting',503));
  const api=new BackendDispatchApi({client:s.client,storage:s.storage,assignedSession:loaded.session});
  const observed: import('../../shared/types/dispatch').DispatchSnapshot[]=[];
  const stop=api.subscribe(snapshot=>observed.push(snapshot));
  try {
    await expect(api.getSnapshot()).rejects.toMatchObject({code:'BACKEND_NOT_READY'});
    await vi.advanceTimersByTimeAsync(3000);
    expect(observed).toEqual([]);
    ready.mockResolvedValue({ready:true} as Awaited<ReturnType<typeof s.client.ready>>);
    await vi.advanceTimersByTimeAsync(15000);
    expect(observed.at(-1)?.backend?.basis.session_id).toBe(loaded.session.session_id);
    expect(observed.at(-1)?.backend?.scenarios).not.toHaveLength(0);
    expect(s.requests.filter(r=>r.method==='POST')).toHaveLength(1);
  } finally { stop();vi.useRealTimers(); }
});

it.each([false,true])('loads the versioned S4 source only when its manifest matches (tampered=%s)',async tampered=>{
  const s=server();const catalog=await s.client.scenarios();
  vi.spyOn(s.client,'scenarios').mockResolvedValue({...catalog,scenarios:[...catalog.scenarios,{...catalog.scenarios[0],scenario_id:'S4'}]});
  const runId='run-'+'4'.repeat(32);
  const run={run_id:runId,scenario_id:'S4' as const,status:'READY' as const,message:'Ready',epoch:s.state.current_time,
    source_kind:'IMPORTED_SCENARIO' as const,manifest_sha256:'d'.repeat(64),fixture_sha256:'e'.repeat(64),online_weather_enabled:false};
  const session={session_id:s.state.basis.session_id,scenario_id:'S4' as const,build_sha256:s.state.basis.build_sha256,
    run_id:runId,catalog_sha256:run.manifest_sha256,fixture_sha256:run.fixture_sha256};
  vi.spyOn(s.client,'loadScenario').mockResolvedValue({schema_version:'saferoute-m3-loaded-session/1',session,
    execution_view:s.state as unknown as import('../../integrations/member3/types').M3ExecutionView});
  vi.spyOn(s.client,'run').mockResolvedValue({...run,...(tampered?{fixture_sha256:'9'.repeat(64)}:{})});
  const api=new BackendDispatchApi({client:s.client,storage:s.storage});
  if(tampered) await expect(api.loadScenario('S4')).rejects.toMatchObject({code:'INVALID_RESPONSE'});
  else expect((await api.loadScenario('S4')).backend?.run?.run_id).toBe(runId);
});

it('resets a legacy S4 onto the verified rain variant without changing its scenario',async()=>{
  const s=server();const catalog=await s.client.scenarios();
  vi.spyOn(s.client,'scenarios').mockResolvedValue({...catalog,scenarios:[...catalog.scenarios,{...catalog.scenarios[0],scenario_id:'S4'}]});
  const api=new BackendDispatchApi({client:s.client,storage:s.storage});await api.loadScenario('S4');
  const runId='run-'+'4'.repeat(32);
  const run={run_id:runId,scenario_id:'S4' as const,status:'READY' as const,message:'Ready',epoch:s.state.current_time,
    source_kind:'IMPORTED_SCENARIO' as const,manifest_sha256:'d'.repeat(64),fixture_sha256:'e'.repeat(64),online_weather_enabled:false};
  vi.spyOn(s.client,'run').mockResolvedValue(run);
  vi.spyOn(s.client,'resetReplay').mockImplementation(async(sid,requestId)=>{
    const before=structuredClone(s.state.basis);s.state.basis.session_id='new-rain-session';
    const session={session_id:s.state.basis.session_id,scenario_id:'S4' as const,build_sha256:s.state.basis.build_sha256,
      run_id:runId,catalog_sha256:run.manifest_sha256,fixture_sha256:run.fixture_sha256};
    return {schema_version:'saferoute-m3-replay-reset/1',source_session_id:sid,session,
      execution_view:s.state as unknown as import('../../integrations/member3/types').M3ExecutionView,
      receipt:{schema_version:'saferoute-m3-replay-receipt/1',mutation_id:'reset-rain',session_id:sid,
        operation:'reset',status:'RESET',source:'M3_NEW_SESSION',input_basis:before,basis:structuredClone(s.state.basis),
        recorded_at:s.state.current_time,request_id:requestId,new_session_id:session.session_id,new_session:session,
        links:{state:'/api/sessions/new-rain-session/state',history:`/api/sessions/${sid}/replay/history`}}};
  });
  expect((await api.resetSession()).backend?.run?.run_id).toBe(runId);
});

it('projects the server rain polygon into the map and clears it after expiry', async () => {
  const s = server();
  const polygon = {type:'Polygon' as const,coordinates:[[[106.7,10.8],[106.72,10.8],[106.72,10.82],[106.7,10.8]]]};
  Object.assign(s.state.basis,{overlay_sha256:'e'.repeat(64)});
  Object.assign(s.locations,{rain:{polygon,ends_at:'2026-09-27T23:00:00+07:00'}});
  const api = new BackendDispatchApi({client:s.client,storage:s.storage});
  const snapshot = await api.loadScenario('S1');
  expect(snapshot.decisionState.context.rain?.polygon).toEqual(polygon);
  expect(createMapScene(snapshot).rain).toEqual(polygon);
  Object.assign(s.locations,{rain:{polygon,ends_at:s.state.current_time}});
  expect((await api.getSnapshot()).decisionState.context.rain).toBeNull();
});

it('keeps a verified weather-run session across reload instead of replacing it with the historical catalog', async () => {
  const s=server(); const runId='run-'+'a'.repeat(32);
  const run={run_id:runId,scenario_id:'S1' as const,status:'READY' as const,message:'Ready',epoch:s.state.current_time,
    manifest_sha256:'d'.repeat(64),fixture_sha256:'e'.repeat(64),online_weather_enabled:true};
  const session={session_id:s.state.basis.session_id,scenario_id:'S1' as const,build_sha256:s.state.basis.build_sha256,
    catalog_sha256:run.manifest_sha256,fixture_sha256:run.fixture_sha256,run_id:runId};
  vi.spyOn(s.client,'run').mockResolvedValue(run);
  vi.spyOn(s.client,'loadRun').mockResolvedValue({schema_version:'saferoute-m3-loaded-session/1',session,
    execution_view:s.state as unknown as import('../../integrations/member3/types').M3ExecutionView});
  const api=new BackendDispatchApi({client:s.client,storage:s.storage});
  await api.getSnapshot();
  expect((await api.loadWeatherRun(runId,'stable-load')).backend?.run?.run_id).toBe(runId);
  expect(s.client.loadRun).toHaveBeenCalledWith(runId,'stable-load',session.session_id);
  s.requests.length=0;
  expect((await new BackendDispatchApi({client:s.client,storage:s.storage}).getSnapshot()).backend?.run?.run_id).toBe(runId);
  expect(s.requests.filter(r=>r.method==='POST')).toEqual([]);
  vi.mocked(s.client.run).mockResolvedValue({...run,manifest_sha256:'9'.repeat(64)});
  await expect(new BackendDispatchApi({client:s.client,storage:s.storage}).getSnapshot()).rejects.toMatchObject({code:'SESSION_BINDING_CHANGED'});
});

it('keeps the original replacement source on retry after a failed imported world read and recreation', async () => {
  const s=server();const api=new BackendDispatchApi({client:s.client,storage:s.storage});
  await api.getSnapshot();const original=s.state.basis.session_id;
  const runId='run-'+'a'.repeat(32);
  const run={run_id:runId,scenario_id:'S0' as const,status:'READY' as const,message:'Ready',epoch:s.state.current_time,
    manifest_sha256:'d'.repeat(64),fixture_sha256:'e'.repeat(64),online_weather_enabled:true};
  vi.spyOn(s.client,'run').mockResolvedValue(run);
  vi.spyOn(s.client,'loadRun').mockImplementation(async(_id,_request,source)=>{
    if(source!==original)throw new Member3Error('IDEMPOTENCY_CONFLICT','Replacement changed');
    s.state.basis.session_id='imported-session';
    for(const p of [s.orders,s.vehicles,s.locations])p.basis=s.state.basis;
    return {schema_version:'saferoute-m3-loaded-session/1',session:{session_id:s.state.basis.session_id,
      scenario_id:'S0',build_sha256:s.state.basis.build_sha256,catalog_sha256:run.manifest_sha256,
      fixture_sha256:run.fixture_sha256,run_id:runId},execution_view:s.state as unknown as import('../../integrations/member3/types').M3ExecutionView};
  });
  vi.spyOn(s.client,'state').mockRejectedValueOnce(new Member3Error('NETWORK_ERROR','offline'));
  await expect(api.loadWeatherRun(runId,'same-intent')).rejects.toMatchObject({code:'NETWORK_ERROR'});
  sessionStorage.setItem('saferoute.member3.command-tab.v1',crypto.randomUUID());
  const reopened=new BackendDispatchApi({client:s.client,storage:s.storage});
  expect((await reopened.loadWeatherRun(runId,'same-intent')).decisionState.sessionId).toBe('imported-session');
  expect((await reopened.loadWeatherRun(runId,'same-intent')).decisionState.sessionId).toBe('imported-session');
});

it('does not replace a session selected in another tab with a late weather-run load', async () => {
  const s=server();let notify!:(key:string|null)=>void;
  const api=new BackendDispatchApi({client:s.client,storage:s.storage,subscribeChanges:callback=>{notify=callback;return()=>{};}});
  await api.getSnapshot();const stop=api.subscribe(()=>{});
  const runId='run-'+'a'.repeat(32);
  const run={run_id:runId,scenario_id:'S0' as const,status:'READY' as const,message:'Ready',epoch:s.state.current_time,
    manifest_sha256:'d'.repeat(64),fixture_sha256:'e'.repeat(64),online_weather_enabled:true};
  vi.spyOn(s.client,'run').mockResolvedValue(run);
  let release!:()=>void;
  const session={session_id:s.state.basis.session_id,scenario_id:'S0' as const,build_sha256:s.state.basis.build_sha256,
    catalog_sha256:run.manifest_sha256,fixture_sha256:run.fixture_sha256,run_id:runId};
  vi.spyOn(s.client,'loadRun').mockImplementation(async()=>{
    await new Promise<void>(r=>{release=r;});
    return {schema_version:'saferoute-m3-loaded-session/1',session,
      execution_view:s.state as unknown as import('../../integrations/member3/types').M3ExecutionView};
  });
  const loading=api.loadWeatherRun(runId,'load-late').catch(e=>e);
  await vi.waitFor(()=>expect(release).toBeDefined());
  const key=[...s.values.keys()].find(k=>k.includes('session.v1'))!;
  const pointer=JSON.parse(s.values.get(key)!);pointer.session.session_id='other-tab-session';
  s.state.basis={...s.state.basis,session_id:'other-tab-session'};
  for(const projection of [s.orders,s.vehicles,s.locations])projection.basis=s.state.basis;
  s.storage.setItem(key,JSON.stringify(pointer));notify(key);release();
  expect(await loading).toMatchObject({name:'AbortError'});
  expect(JSON.parse(s.values.get(key)!).session.session_id).toBe('other-tab-session');stop();
});

it.each(["build_sha256", "catalog_sha256", "fixture_sha256"])("archives an old %s binding and loads the same scenario on the current runtime", async field => {
  const s = server();
  await new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1");
  const key = [...s.values.keys()].find(k => k.includes("session.v1"))!;
  const old = JSON.parse(s.values.get(key)!);
  old.session.session_id = "old-installation-session"; old.session[field] = "a".repeat(64);
  s.storage.setItem(key, JSON.stringify(old)); s.requests.length = 0;
  const next = await new BackendDispatchApi({ client: s.client, storage: s.storage }).getSnapshot();
  expect(next.decisionState.scenarioId).toBe("S1");
  expect(s.requests.filter(r => r.method === "POST").map(r => r.path)).toEqual(["/api/scenarios/S1/load"]);
  expect(s.requests.some(r => r.path.includes("old-installation-session"))).toBe(false);
  const archive = [...s.values.entries()].find(([k]) => k.includes(":archive:"))!;
  expect(JSON.parse(archive[1]).pointer).toEqual(old);
  expect(JSON.parse(s.values.get(key)!).session.build_sha256).toBe(s.capabilities.build_sha256);
});

it("preserves an ambiguous old-runtime command in the archive without replaying it", async () => {
  const s = server(); await new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1");
  const key = [...s.values.keys()].find(k => k.includes("session.v1"))!;
  const pointer = JSON.parse(s.values.get(key)!); pointer.session.build_sha256 = "a".repeat(64);
  s.storage.setItem(key, JSON.stringify(pointer));
  const pending = { requestId: "old-accept", sessionId: pointer.session.session_id, operation: "accept", body: {} };
  const commandKey = `saferoute.member3.command.v1:${s.client.baseUrl}`;
  s.storage.setItem(commandKey, JSON.stringify(pending)); s.requests.length = 0;
  await new BackendDispatchApi({ client: s.client, storage: s.storage }).getSnapshot();
  expect(s.requests.filter(r => r.method === "POST").map(r => r.path)).toEqual(["/api/scenarios/S1/load"]);
  const archive = [...s.values.entries()].find(([k]) => k.includes(":archive:"))!;
  expect(JSON.parse(archive[1]).pendingCommand).toEqual(pending);
  expect(JSON.parse(s.values.get(commandKey)!)).toBeNull();
});

it("does not discard an old session or send a mutation when its archive cannot be written", async () => {
  const s = server(); await new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1");
  const key = [...s.values.keys()].find(k => k.includes("session.v1"))!;
  const pointer = JSON.parse(s.values.get(key)!); pointer.session.build_sha256 = "a".repeat(64);
  s.storage.setItem(key, JSON.stringify(pointer)); s.requests.length = 0;
  const storage = { ...s.storage, setItem: () => { throw new Error("Full"); } };
  await expect(new BackendDispatchApi({ client: s.client, storage }).getSnapshot()).rejects.toMatchObject({ code: "COMMAND_STORAGE_UNAVAILABLE" });
  expect(JSON.parse(s.values.get(key)!)).toEqual(pointer);
  expect(s.requests.some(r => r.method === "POST")).toBe(false);
});

it("bounds forecast hydration to one SDK read at a time while retaining certified alternatives", async () => {
  const s = await acceptHarness();
  const read = s.client.jobForecast.bind(s.client);
  let active = 0, maximum = 0;
  const observed = vi.spyOn(s.client, "jobForecast").mockImplementation(async (...args) => {
    active++; maximum = Math.max(maximum, active);
    try { await new Promise(resolve => setTimeout(resolve, 5)); return await read(...args); }
    finally { active--; }
  });
  const api = new BackendDispatchApi({ client: s.client, storage: s.storage });
  const next = await api.getSnapshot();
  expect(observed).toHaveBeenCalledTimes(3);
  expect(maximum).toBe(1);
  expect(next.planState.proposedAlternatives).toHaveLength(3);
});
it("publishes available route previews before a slow final forecast finishes", async () => {
  const s = await acceptHarness();
  const read = s.client.jobForecast.bind(s.client);
  let count=0, release!: () => void;
  const hold=new Promise<void>(resolve=>{release=resolve;});
  vi.spyOn(s.client,"jobForecast").mockImplementation(async (...args)=>{
    if (++count===3) await hold;
    return read(...args);
  });
  const api=new BackendDispatchApi({client:s.client,storage:s.storage});
  const published:number[]=[];const stop=api.subscribe(snapshot=>published.push(snapshot.planState.proposedAlternatives.length));
  const pending=api.getSnapshot();
  await vi.waitFor(()=>expect(count).toBe(3));
  expect(published).toContain(1);expect(published).toContain(2);
  release();expect((await pending).planState.proposedAlternatives).toHaveLength(3);stop();
});
it("reenables a tab after a transient observation failure only when a coherent poll succeeds", async () => {
  vi.useFakeTimers();
  try {
    const s = server(); let failNext = false;
    const client = new Member3Client({ token: () => "unit-only", fetch: async (url, options) => {
      if (failNext && String(url).endsWith("/state")) {
        failNext = false;
        return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "ERROR", request_id: "trace",
          data: null, diagnostics: [{ code: "RUNTIME_TIMEOUT", path: "runtime", message: "Bounded observation timed out" }] }), { status: 503 });
      }
      return s.fetcher(url, options);
    } });
    const api = new BackendDispatchApi({ client, storage: s.storage }); await api.loadScenario("S1");
    const seen: import("../../shared/types/dispatch").DispatchSnapshot[] = [];
    const stop = api.subscribe(next => seen.push(next)); failNext = true;
    await vi.advanceTimersByTimeAsync(2500);
    expect(seen.at(-1)!.backend).toMatchObject({ stale: true, error: { code: "RUNTIME_TIMEOUT" } });
    await vi.advanceTimersByTimeAsync(1000);
    expect(seen.at(-1)!.backend).toMatchObject({ stale: false, error: undefined, capabilities: { replay: true } });
    expect(s.requests.filter(r => r.method === "POST")).toHaveLength(1); stop();
  } finally { vi.useRealTimers(); }
});

async function playbackHarness() {
  const s = await acceptHarness(); await s.api.selectAlternative("job-BALANCED"); await s.api.acceptSelectedPlan();
  const calls: { path: string; body: Record<string, unknown> }[] = [], replies = new Map<string, unknown>();
  let lost = false, holdReply: Promise<void> | undefined, notify: ((key: string | null) => void) | undefined;
  const client = new Member3Client({ token: () => "unit-only", fetch: async (url, options) => {
    const path = new URL(String(url)).pathname;
    let data: unknown;
    if (options?.method === "POST" && path.includes("/replay/")) {
      const body = JSON.parse(String(options.body)); calls.push({ path, body });
      const operation = path.endsWith("/pause") ? "pause" : path.split("/").at(-1)!;
      if (!replies.has(body.request_id)) {
        if (operation === "step" || operation === "reset") {
          const before = structuredClone(s.state.basis), oldSid = before.session_id;
          if (operation === "step") {
            s.state.basis = { ...before, head_version: (BigInt(before.head_version) + 1n).toString(), head_sha256: "e".repeat(64) };
            s.state.current_time = "2026-09-27T21:14:00+07:00";
          } else {
            s.state.basis = { ...before, session_id: "reset-owned", generation: "0", head_version: "0" };
            s.state.active_job_id = null; s.state.accepted_trajectory = null; s.state.planned_served_suffix = [];
            s.state.unserved = [{ order_id: "M3-O1", reason: "NO_ACCEPTED_PLAN" }];
            s.orders.orders[0].planned_in_accepted_suffix = false;
          }
          for (const projection of [s.orders, s.vehicles, s.locations]) { projection.basis = s.state.basis; projection.current_time = s.state.current_time; }
          const next = { session_id: s.state.basis.session_id, scenario_id: "S1", build_sha256: before.build_sha256, catalog_sha256: "c".repeat(64), fixture_sha256: "f".repeat(64) };
          const receipt = { schema_version: "saferoute-m3-replay-receipt/1", mutation_id: body.request_id, session_id: oldSid, operation: operation === "step" ? "advance" : "reset", status: operation === "step" ? "ADVANCED" : "RESET", source: operation === "step" ? "M2_PUBLIC_SDK" : "M3_NEW_SESSION", input_basis: before, basis: s.state.basis, recorded_at: "2026-10-08T00:00:00Z", links: { state: `/api/sessions/${s.state.basis.session_id}/state`, history: `/api/sessions/${oldSid}/replay/history` }, ...(operation === "step" ? { target_time: s.state.current_time } : { new_session_id: next.session_id, new_session: next }) };
          data = { schema_version: operation === "step" ? "saferoute-m3-replay-view/1" : "saferoute-m3-replay-reset/1", receipt, execution_view: structuredClone(s.state), ...(operation === "reset" ? { source_session_id: oldSid, session: next } : {}) };
        } else {
          if (operation === "start") Object.assign(s.playback, { paused: false, fully_paused: false, mode: "AUTOMATIC", reason: "RUNNING" });
          if (operation === "pause") Object.assign(s.playback, { paused: true, fully_paused: false, in_flight: true, mode: "STEP", reason: "USER_PAUSE" });
          if (body.speed) s.playback.speed = body.speed;
          data = { schema_version: "saferoute-m3-playback-control/1", controller: s.playback, receipt: { schema_version: "saferoute-m3-playback-receipt/1", receipt_id: body.request_id, session_id: s.state.basis.session_id, operation, source: "M3_PLAYBACK_CONTROL", recorded_at: "2026-10-08T00:00:00Z", request_id: body.request_id, actor_id: "owner", controller: s.playback } };
        }
        replies.set(body.request_id, structuredClone(data));
      }
      data = replies.get(body.request_id);
      if (lost) { lost = false; throw new TypeError("Reply lost after durable commit"); }
      await holdReply;
    } else if (path.endsWith("/acceptances") && s.state.basis.session_id === "reset-owned") data = { schema_version: "saferoute-m3-acceptance-audit/1", session_id: "reset-owned", acceptances: [] };
    else return s.client.baseUrl && path.endsWith("/replay/playback") ? s.fetcher(url, options) : clientFallback(url, options);
    return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "OK", request_id: "trace", diagnostics: [], data }));
  } });
  // Retain the real test server's comparison and accepted world handlers.
  const clientFallback: typeof fetch = async (url, options) => {
    const path = new URL(String(url)).pathname;
    if (path.includes("/profiles/comparisons/")) return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "OK", request_id: "trace", diagnostics: [], data: s.c }));
    if (path.endsWith("/forecast")) return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "OK", request_id: "trace", diagnostics: [], data: s.forecasts.find(f => f.job_id === path.split("/").at(-2)) }));
    return s.fetcher(url, options);
  };
  const api = new BackendDispatchApi({ client, storage: s.storage, subscribeChanges: callback => { notify = callback; return () => {}; } }); await api.getSnapshot();
  return { ...s, api, client, calls, lose: () => { lost = true; }, hold: (promise: Promise<void>) => { holdReply = promise; }, notify: (key: string) => notify!(key) };
}
it("ignores a late Reset reply when another tab already switched the session", async () => {
  const s = await playbackHarness(); let release!: () => void; s.hold(new Promise<void>(r => { release = r; }));
  const stop = s.api.subscribe(() => {}), reset = s.api.resetSession().catch(e => e);
  await vi.waitFor(() => expect(s.calls).toHaveLength(1));
  const key = [...s.values.keys()].find(k => k.includes("session.v1"))!, pointer = JSON.parse(s.values.get(key)!);
  pointer.session.session_id = "later-owned"; delete pointer.comparison; delete pointer.previewJobId;
  s.state.basis = { ...s.state.basis, session_id: "later-owned" }; for (const p of [s.orders, s.vehicles, s.locations]) p.basis = s.state.basis;
  s.storage.setItem(key, JSON.stringify(pointer)); s.notify(key); release();
  expect(await reset).toMatchObject({ name: "AbortError" });
  await vi.waitFor(() => expect(JSON.parse(s.values.get(key)!).session.session_id).toBe("later-owned")); stop();
});
it("retries an ambiguous Step with the original identity and does not advance twice", async () => {
  const s = await playbackHarness(); s.lose();
  await expect(s.api.replayStep()).rejects.toMatchObject({ code: "NETWORK_ERROR" });
  const next = await s.api.getSnapshot();
  expect(s.calls).toHaveLength(2); expect(s.calls[0]).toEqual(s.calls[1]);
  expect(next.backend!.basis.head_version).toBe("9007199254740994");
  expect(next.demoClock.now).toBe("2026-09-27T21:14:00+07:00");
});
it("recovers an ambiguous Start after reload with the original identity and fresh server world", async () => {
  const s = await playbackHarness(); s.lose();
  await expect(s.api.replayStart(4)).rejects.toMatchObject({ code: "NETWORK_ERROR" });
  const reloaded = new BackendDispatchApi({ client: s.client, storage: s.storage });
  const next = await reloaded.getSnapshot();
  expect(s.calls).toHaveLength(2); expect(s.calls[1]).toEqual(s.calls[0]);
  expect(next.backend!.playback).toMatchObject({ paused: false, mode: "AUTOMATIC", speed: 4 });
  expect(next.backend!.executionView.basis).toEqual(s.state.basis);
  expect(next.backend!.mutationPending).toBe(false);
});
it("Pause exposes the reserved settling tick, blocks Step and speed does not resume", async () => {
  const s = await playbackHarness(); await s.api.replayStart(4);
  const paused = await s.api.replayPause(); expect(paused.backend!.playback).toMatchObject({ paused: true, in_flight: true, fully_paused: false });
  await expect(s.api.replayStep()).rejects.toMatchObject({ code: "REPLAY_UNAVAILABLE" });
  Object.assign(s.playback, { in_flight: false, fully_paused: true });
  await s.api.getSnapshot();
  const next = await s.api.replaySpeed(8); expect(next.backend!.playback).toMatchObject({ paused: true, speed: 8 });
  expect(s.calls.at(-1)!.body).toEqual({ request_id: expect.any(String), speed: 8 });
  expect(next.backend!.stale).toBe(false);
  expect(next.backend!.playbackConverging).toBe(false);
});
it("changing speed while fully paused keeps Play available without another observation poll", async () => {
  const s = await playbackHarness();
  const before = await s.api.getSnapshot();
  expect(before.backend!.playback!.fully_paused).toBe(true);
  const next = await s.api.replaySpeed(16);
  expect(next.backend).toMatchObject({ stale: false, mutationPending: false, playbackConverging: false, capabilities: { replay: true } });
  expect(next.backend!.executionView).toEqual(before.backend!.executionView);
  await s.api.replayStart(16);
  expect(s.calls.at(-1)!.path).toMatch(/\/replay\/start$/);
});
it("changing speed cannot make an unsettled paused world fresh", async () => {
  const s = await playbackHarness();await s.api.replayStart(4);await s.api.replayPause();
  const next = await s.api.replaySpeed(8);
  expect(next.backend).toMatchObject({ stale: true, playbackConverging: true });
  await expect(s.api.replayStart(8)).rejects.toMatchObject({ code: 'REPLAY_UNAVAILABLE' });
});
it("sends Pause despite observational churn and settles a confirmed Start before read convergence", async () => {
  const s = await playbackHarness();
  s.locations.basis = { ...s.state.basis, generation: "2" };
  const started = await s.api.replayStart(8).catch(e => e);
  // Start still requires a coherent input world. Prepare that input first.
  expect(started).toMatchObject({ code: "STATE_CHANGED" });
  s.locations.basis = s.state.basis; await s.api.getSnapshot();
  await s.api.replayStart(8);
  s.locations.basis = { ...s.state.basis, generation: "2" };
  await s.api.getSnapshot().catch(() => {});
  const paused = await s.api.replayPause();
  expect(s.calls.at(-1)!.path).toMatch(/\/replay\/playback\/pause$/);
  expect(paused.backend!.playback).toMatchObject({ paused: true, in_flight: true });
  expect(paused.backend!.mutationPending).toBe(false);
  expect(paused.backend!.stale).toBe(true);
});
it('settles a Reset rejected because an import already replaced its assignment',async()=>{
  const s=await playbackHarness();
  vi.spyOn(s.client,'resetReplay').mockRejectedValue(new Member3Error('ASSIGNMENT_CONFLICT','Import won',409));
  await expect(s.api.resetSession()).rejects.toMatchObject({code:'ASSIGNMENT_CONFLICT'});
  const refreshed=await new BackendDispatchApi({client:s.client,storage:s.storage}).getSnapshot();
  expect(refreshed.backend?.mutationPending).toBe(false);
  expect(s.client.resetReplay).toHaveBeenCalledOnce();
});
it("Reset replays its original ambiguous intent and clears old comparison/accepted pointers", async () => {
  const s = await playbackHarness(); s.lose();
  await expect(s.api.resetSession()).rejects.toMatchObject({ code: "NETWORK_ERROR" });
  const next = await s.api.getSnapshot();
  expect(s.calls).toHaveLength(2); expect(s.calls[0]).toEqual(s.calls[1]);
  expect(next.backend!.basis.session_id).toBe("reset-owned");
  expect(next.planState.acceptedExecution).toBeNull(); expect(next.planState.proposedAlternatives).toEqual([]);
  expect(next.backend!.comparison).toBeUndefined();
});
it("reloads a confirmed Reset through GET only after destination observation fails", async () => {
  const s = await playbackHarness(), read = s.client.state.bind(s.client);
  const pointerKey = [...s.values.keys()].find(k => k.includes("session.v1"))!;
  const originalSession = JSON.parse(s.values.get(pointerKey)!).session;
  vi.spyOn(s.client, "session").mockResolvedValue(originalSession);
  let failDestination = true;
  vi.spyOn(s.client, "state").mockImplementation(async (...args) => {
    if (args[0] === "reset-owned" && failDestination) { failDestination = false; throw new Error("destination GET unavailable"); }
    return read(...args);
  });
  await expect(s.api.resetSession()).rejects.toThrow("destination GET unavailable");
  const reloaded = new BackendDispatchApi({ client: s.client, storage: s.storage });
  const next = await reloaded.getSnapshot();
  expect(s.calls).toHaveLength(1);
  expect(next.backend!.basis.session_id).toBe("reset-owned");
  expect(next.backend!.mutationPending).toBe(false);
  expect(next.planState.acceptedExecution).toBeNull();
});
it("requires accepted execution before replay and rejects invalid speeds without sending", async () => {
  const s = server(), api = new BackendDispatchApi({ client: s.client, storage: s.storage }); await api.getSnapshot();
  await expect(api.replayStep()).rejects.toMatchObject({ code: "REPLAY_UNAVAILABLE" });
  await expect(api.replayStart(3 as 1)).rejects.toMatchObject({ code: "INVALID_SPEED" });
  expect(s.requests.filter(r => r.method === "POST" && r.path.includes("/replay/"))).toEqual([]);
});

it("switches cross-tab pointer read-only and fences a late old-session response", async () => {
  const s = server(); await new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1");
  let notify: ((key: string | null) => void) | undefined, release: (() => void) | undefined, held = false;
  const client = new Member3Client({ token: () => "unit-only", fetch: async (url, options) => {
    if (new URL(String(url)).pathname.endsWith("/state") && held) { held = false; const response = await s.fetcher(url, options); await new Promise<void>(r => { release = r; }); return response; }
    return s.fetcher(url, options);
  } });
  const api = new BackendDispatchApi({ client, storage: s.storage, subscribeChanges: callback => { notify = callback; return () => { notify = undefined; }; } });
  await api.getSnapshot(); const seen: string[] = []; const stop = api.subscribe(next => { if (!next.backend?.stale) seen.push(next.backend!.basis.session_id); });
  held = true; const oldRead = api.getSnapshot().catch(e => e); await vi.waitFor(() => expect(release).toBeDefined());
  const key = [...s.values.keys()].find(k => k.includes("session.v1"))!, pointer = JSON.parse(s.values.get(key)!);
  pointer.session.session_id = "new-tab-owned"; delete pointer.comparison;
  s.storage.setItem(key, JSON.stringify(pointer));
  s.state.basis = { ...s.state.basis, session_id: "new-tab-owned" }; for (const p of [s.orders, s.vehicles, s.locations]) p.basis = s.state.basis;
  notify!(key); release!(); await oldRead;
  await vi.waitFor(() => expect(seen.at(-1)).toBe("new-tab-owned"));
  expect(seen).not.toContain("m3-owned-session");
  expect(s.requests.filter(r => r.method === "POST")).toHaveLength(1);
  expect(JSON.parse(s.values.get(key)!).session.session_id).toBe("new-tab-owned"); stop();
});
it("reconciles a late successful comparison without contaminating the new session", async () => {
  const s = server(); await new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1");
  let notify!: (key: string | null) => void, release!: () => void;
  const original = structuredClone(s.state.basis);
  const client = new Member3Client({ token: () => "unit-only", fetch: async (url, options) => {
    if (String(url).endsWith('/profiles/compare')) {
      await new Promise<void>(r => { release = r; });
      return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "OK", request_id: "trace", diagnostics: [], data: { schema_version: "saferoute-m3-profile-submission/1", session_id: original.session_id, comparison_id: "old-comparison", mode: "NEW_BATCH", input_basis: original, profiles: ["FASTEST", "BALANCED", "SAFER"], links: { poll: `/api/sessions/${original.session_id}/profiles/comparisons/old-comparison`, cancel: `/api/sessions/${original.session_id}/profiles/comparisons/old-comparison/cancel` } } }));
    }
    return s.fetcher(url, options);
  } });
  const api = new BackendDispatchApi({ client, storage: s.storage, subscribeChanges: callback => { notify = callback; return () => {}; } }); await api.getSnapshot(); const stop = api.subscribe(() => {});
  const oldSubmit = api.optimize().catch(e => e); await vi.waitFor(() => expect(release).toBeDefined());
  const key = [...s.values.keys()].find(k => k.includes("session.v1"))!, pointer = JSON.parse(s.values.get(key)!); pointer.session.session_id = "comparison-new";
  s.state.basis = { ...s.state.basis, session_id: "comparison-new" }; for (const p of [s.orders, s.vehicles, s.locations]) p.basis = s.state.basis;
  s.storage.setItem(key, JSON.stringify(pointer)); notify(key); release();
  expect(await oldSubmit).toMatchObject({ name: "AbortError" });
  const fresh = await api.getSnapshot();
  expect(fresh.backend).toMatchObject({ basis: { session_id: "comparison-new" }, mutationPending: false, capabilities: { optimize: true } });
  expect(fresh.backend!.comparison).toBeUndefined(); expect(JSON.parse(s.values.get(key)!).comparison).toBeUndefined(); stop();
});
it("does not silently load another world when a cross-tab owner pointer is forbidden", async () => {
  const s = server(); await new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1");
  let notify: ((key: string | null) => void) | undefined;
  const client = new Member3Client({ token: () => "unit-only", fetch: async (url, options) => String(url).includes("foreign-owner") ? new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "ERROR", request_id: "trace", data: null, diagnostics: [{ code: "FORBIDDEN", message: "Not session owner" }] }), { status: 403 }) : s.fetcher(url, options) });
  const api = new BackendDispatchApi({ client, storage: s.storage, subscribeChanges: callback => { notify = callback; return () => {}; } });
  await api.getSnapshot(); const seen: unknown[] = []; const stop = api.subscribe(next => seen.push(next));
  const key = [...s.values.keys()].find(k => k.includes("session.v1"))!, pointer = JSON.parse(s.values.get(key)!); pointer.session.session_id = "foreign-owner"; s.storage.setItem(key, JSON.stringify(pointer)); notify!(key);
  await vi.waitFor(() => expect(seen.at(-1)).toMatchObject({ backend: { stale: true, error: { code: "FORBIDDEN" }, capabilities: { replay: false } } }));
  expect(s.requests.filter(r => r.method === "POST")).toHaveLength(1); stop();
});

// Contract-shaped HTTP data, deliberately unrelated to frontend fixture IDs/coordinates.
function server() {
  const basis = { session_id: "m3-owned-session", build_sha256: "b".repeat(64), head_version: "2", generation: "0",
    root_sha256: "1".repeat(64), head_sha256: "2".repeat(64), source_sha256: "3".repeat(64), context_version: "context-1", overlay_sha256: null };
  const time = "2026-09-27T21:13:00+07:00";
  const physicalVehicle: M3Vehicle = { vehicle_id: "M3-V1", availability: "AVAILABLE", capacity_kg: 20, current_load_kg: 0,
    onboard_order_ids: [], remaining_range_m: 90000, position: { kind: "AT_NODE", node_id: "9007199254740993", coordinates: [106.72, 10.82], position_source: "SIMULATED" },
    position_timestamp: time, planned_suffix: [], activity: "IDLE" };
  const state = { schema_version: "task02-m2-execution-view/2", basis, current_time: time, execution_mode: "SIMULATED_REPLAY",
    real_world_observation: false, metric_scope: "OBSERVED_PREFIX_ONLY", order_ids: ["M3-O1"], delivered_prefix: [], planned_served_suffix: [],
    unserved: [{ order_id: "M3-O1", reason: "NO_ACCEPTED_PLAN" }], vehicles: [physicalVehicle], pending_event_ids: [], active_job_id: null,
    observed_metrics: null, planned_suffix_metrics: null, projected_whole_metrics: null, accepted_trajectory: null };
  const base: Omit<M3Projection, "schema_version"> = { basis, current_time: time, execution_mode: "SIMULATED_REPLAY", real_world_observation: false,
    units: { distance: "m", duration: "s", mass: "kg", money: "VND" } };
  const orders: M3OrdersView = { ...base, schema_version: "saferoute-m3-orders-view/1", orders: [{ order_id: "M3-O1", status: "WAITING", owner_vehicle_id: null,
    planned_in_accepted_suffix: false, unserved_reason: "NO_ACCEPTED_PLAN", demand_kg: 7, priority: 2, pickup_location_id: "M3-DEPOT", delivery_region_id: "M3-region",
    graph_node_id: "9007199254740993", coordinates: [106.73, 10.83], service_time_s: 900, earliest: time, preferred_due: time, hard_deadline: time }] };
  const vehicles = { ...base, schema_version: "saferoute-m3-vehicles-view/1", vehicles: [physicalVehicle], vehicle_metadata: [{ vehicle_id: "M3-V1",
    vehicle_type: "motorcycle", cost_per_km_vnd: 2300, range_m: 100000, working_start: time, working_end: time }] };
  const locations = { ...base, schema_version: "saferoute-m3-locations-view/1", locations: [
    { location_id: "M3-DEPOT", kind: "DEPOT", graph_node_id: "1", coordinates: [106.72, 10.82], opening_time: time, closing_time: time },
    { location_id: "delivery:M3-O1", kind: "DELIVERY", order_id: "M3-O1", graph_node_id: "9007199254740993", coordinates: [106.73, 10.83] }
  ] };
  const requests: { path: string; method: string; body: unknown }[] = [];
  const capabilities = { schema_version: "task02-m2-runtime-capabilities/1", build_sha256: basis.build_sha256 };
  const playback = { mode: "STEP", paused: true, fully_paused: true, in_flight: false, reason: "NOT_STARTED", speed: 1, controller_revision: "0", next_tick_at: null, end_time: null, step_seconds: 60, cadence: "DISCRETE_BEST_EFFORT", base_tick_interval_seconds: 1, catch_up: false, auto_apply_event: false, auto_accept_plan: false, execution_mode: "SIMULATED_REPLAY", real_world_observation: false };
  const fetcher: typeof fetch = async (url, options) => {
    const path = new URL(String(url)).pathname;
    requests.push({ path, method: options?.method ?? "GET", body: options?.body ? JSON.parse(String(options.body)) : null });
    const data = path === "/ready" ? { ready: true } : path.endsWith("/capabilities") ? capabilities
      : path === "/api/scenarios" ? { schema_version: "saferoute-m3-scenario-catalog/1", execution_mode: "SIMULATED_REPLAY", real_world_observation: false,
          scenarios: ["S0", "S1"].map((scenario_id) => ({ scenario_id, fixture_sha256: "f".repeat(64), initial_time: time, order_count: 1, vehicle_count: 1 })), catalog_sha256: "c".repeat(64) }
      : path.endsWith("/load") ? { schema_version: "saferoute-m3-loaded-session/1", session: { session_id: basis.session_id,
          scenario_id: path.split("/")[3], build_sha256: basis.build_sha256, catalog_sha256: "c".repeat(64), fixture_sha256: "f".repeat(64) }, execution_view: state }
      : path.endsWith("/acceptances") ? { schema_version: "saferoute-m3-acceptance-audit/1", session_id: state.basis.session_id, acceptances: [] }
      : path.endsWith("/events") ? { schema_version: "saferoute-m3-pending-events/1", basis: state.basis, current_time: state.current_time, execution_mode: "SIMULATED_REPLAY", real_world_observation: false, events: [] }
      : path.endsWith("/replay/history") ? { schema_version: "saferoute-m3-replay-history/1", session_id: state.basis.session_id, history: [] }
      : path.endsWith("/replay/playback") ? { schema_version: "saferoute-m3-playback-controller/1", ...playback, execution_view: state }
      : path.endsWith("/orders") ? orders : path.endsWith("/vehicles") ? vehicles : path.endsWith("/locations") ? locations : state;
    return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "OK", request_id: "trace-1", data, diagnostics: [] }));
  };
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
  const client = new Member3Client({ fetch: fetcher, token: () => "test-only-bearer" });
  return { client, storage, requests, values, state, orders, vehicles, locations, fetcher, capabilities, playback };
}

async function acceptHarness() {
  const s = server();
  s.state.basis.head_version = "9007199254740993";
  const c = comparison(); c.session_id = s.state.basis.session_id; c.input_basis = structuredClone(s.state.basis);
  c.jobs.forEach(row => { row.view.input_basis = structuredClone(c.input_basis); row.view.served_orders = ["M3-O1"]; });
  c.outcome.comparison.jobs.forEach(row => { row.basis = structuredClone(c.input_basis); });
  const forecasts = c.jobs.map(row => {
    const f = forecastView(); f.session_id = c.session_id; f.job_id = row.job_id; f.profile = row.profile;
    f.input_basis = structuredClone(c.input_basis); f.job_view = row.view;
    f.trajectory.job_id = row.job_id; f.trajectory.profile = row.profile;
    const route = (f.trajectory.vehicle_routes as Array<{ vehicle_id: string; order_sequence: string[]; actions: Record<string, unknown>[] }>)[0];
    route.vehicle_id = "M3-V1"; route.order_sequence = ["M3-O1"];
    route.actions.push({ kind: "SERVICE", start_us: "2000000", end_us: "2000000", order_id: "M3-O1", node_id: 2, load_after_kg: 0 });
    return f;
  });
  const history: Record<string, unknown>[] = [], posts: string[] = [], paths: string[] = [];
  let loseReply = false, loseBeforeCommit = false, staleOnPost = false, holdReply: Promise<void> | undefined;
  let rejection: { status: number; code: string } | undefined;
  function activate(jid: string, generation: string) {
    s.state.basis = { ...s.state.basis, generation };
    s.orders.basis = s.vehicles.basis = s.locations.basis = s.state.basis;
    Object.assign(s.state, { active_job_id: jid, accepted_trajectory: forecasts.find(f => f.job_id === jid)!.trajectory,
      planned_served_suffix: ["M3-O1"], unserved: [] });
    s.orders.orders[0].planned_in_accepted_suffix = true; s.orders.orders[0].unserved_reason = null;
  }
  const fetcher: typeof fetch = async (url, options) => {
    const path = new URL(String(url)).pathname; paths.push(path);
    let data: unknown;
    if (path.endsWith("/accept")) {
      posts.push(String(options!.body));
      if (loseBeforeCommit) throw new TypeError("Unknown submit outcome");
      if (rejection) return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "ERROR", request_id: "trace", data: null,
        diagnostics: [{ severity: "ERROR", code: rejection.code, path: "accept", message: "Definitive witness rejection; re-optimize" }] }), { status: rejection.status });
      if (staleOnPost) {
        activate("job-FASTEST", "1");
        return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "ERROR", request_id: "trace", data: null,
          diagnostics: [{ severity: "ERROR", code: "STALE_HEAD", path: "job_id", message: "Re-optimize from current state" }] }), { status: 409 });
      }
      const jid = path.split("/").at(-2)!;
      if (!history.length) {
        activate(jid, "1"); history.push({ schema_version: "saferoute-m3-plan-acceptance/1", acceptance_id: "accept-1", session_id: c.session_id,
          job_id: jid, status: "ACCEPTED", input_basis: c.input_basis, basis: structuredClone(s.state.basis), recorded_at: "2026-10-07T12:30:00+00:00",
          links: { state: `/api/sessions/${c.session_id}/state`, job: `/api/sessions/${c.session_id}/jobs/${jid}` } });
      }
      if (loseReply) { loseReply = false; throw new TypeError("Reply lost after commit"); }
      await holdReply;
      data = { schema_version: "saferoute-m3-acceptance-view/1", receipt: history[0], execution_view: s.state };
    } else if (path === `/api/sessions/${c.session_id}`) data = { session_id: c.session_id, scenario_id: "S1", build_sha256: c.input_basis.build_sha256, catalog_sha256: "c".repeat(64), fixture_sha256: "f".repeat(64) };
    else if (path.endsWith("/acceptances")) data = { schema_version: "saferoute-m3-acceptance-audit/1", session_id: c.session_id, acceptances: history };
    else if (path.includes("/profiles/comparisons/")) data = c;
    else if (path.endsWith("/forecast")) data = forecasts.find(f => f.job_id === path.split("/").at(-2));
    else return s.fetcher(url, options);
    return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "OK", request_id: "trace", data, diagnostics: [] }));
  };
  await new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1");
  const key = [...s.values.keys()].find(k => k.includes("session.v1"))!;
  const pointer = JSON.parse(s.values.get(key)!); pointer.comparison = { id: c.comparison_id, inputBasis: c.input_basis }; s.values.set(key, JSON.stringify(pointer));
  const client = new Member3Client({ fetch: fetcher, token: () => "test-only-bearer" });
  const api = new BackendDispatchApi({ client, storage: s.storage }); await api.getSnapshot();
  return { ...s, api, client, c, history, posts, paths, activate, forecasts,
    lose: () => { loseReply = true; }, stale: () => { staleOnPost = true; }, hold: (promise: Promise<void>) => { holdReply = promise; },
    unknown: (value: boolean) => { loseBeforeCommit = value; }, reject: (status: number, code: string) => { rejection = { status, code }; } };
}

async function eventHarness(scenario: "S1" | "S2" | "S3" | "S4" = "S2") {
  const s = server();
  const event = { event_id: `wire-${scenario}`, event_type: scenario === "S3" ? "VEHICLE_UNAVAILABLE" : scenario === "S4" ? "LOCAL_RAIN_WHAT_IF" : "URGENT_ORDER",
    timestamp: "2026-09-27T21:15:00+07:00", apply_allowed: false };
  const pending = scenario === "S1" ? [] : [event];
  Object.assign(s.state, { pending_event_ids: pending.map(e => e.event_id) });
  const receipts: Record<string, unknown>[] = [], posts: { path: string; body: Record<string, unknown> }[] = [];
  const replies = new Map<string, Record<string, unknown>>();
  let preview = comparison(), compareCount = 0;
  const comparePosts: Record<string, unknown>[] = [];
  function bindPreview() {
    preview = comparison(); preview.session_id = s.state.basis.session_id;
    preview.comparison_id = `event-comparison-${compareCount}`; preview.input_basis = structuredClone(s.state.basis);
    preview.jobs.forEach(row => { row.job_id = `event-${compareCount}-${row.profile}`; row.view.job_id = row.job_id;
      row.view.input_basis = structuredClone(preview.input_basis); row.view.served_orders = ["M3-O1"]; });
    preview.outcome.comparison.jobs.forEach(row => { row.job_id = `event-${compareCount}-${row.profile}`; row.basis = structuredClone(preview.input_basis); });
  }
  let loseReply = false, rejectCode: string | undefined, wrongBasis = false, corruptReply = false;
  function sync() {
    s.orders.basis = s.vehicles.basis = s.locations.basis = s.state.basis;
    s.orders.current_time = s.vehicles.current_time = s.locations.current_time = s.state.current_time;
  }
  function barrier(observed: boolean) {
    s.state.current_time = event.timestamp;
    Object.assign(s.state, { observed_metrics: observed ? { distance_m: 0 } : null });
    event.apply_allowed = observed; sync();
  }
  function receipt() {
    return { schema_version: "saferoute-m3-replay-receipt/1", mutation_id: "event-mutation-1", session_id: s.state.basis.session_id,
      operation: "apply_event", status: "APPLIED", source: "M2_PUBLIC_SDK", input_basis: structuredClone(s.state.basis),
      basis: { ...s.state.basis, head_version: "3", head_sha256: "4".repeat(64), overlay_sha256: scenario === "S4" ? "e".repeat(64) : null },
      event_id: event.event_id, event_type: event.event_type, event_sha256: "e".repeat(64), recorded_at: "2026-10-08T00:00:00+07:00",
      links: { state: `/api/sessions/${s.state.basis.session_id}/state`, history: `/api/sessions/${s.state.basis.session_id}/replay/history` } };
  }
  const fetcher: typeof fetch = async (url, options) => {
    const path = new URL(String(url)).pathname;
    let data: unknown;
    if (path.endsWith("/profiles/compare")) {
      comparePosts.push(JSON.parse(String(options!.body))); compareCount++; bindPreview();
      data = { schema_version: "saferoute-m3-profile-submission/1", session_id: preview.session_id, comparison_id: preview.comparison_id,
        mode: "NEW_BATCH", input_basis: preview.input_basis, profiles: ["FASTEST", "BALANCED", "SAFER"], links: preview.links };
    } else if (path.includes("/profiles/comparisons/")) data = preview;
    else if (path.endsWith("/forecast")) {
      const row = preview.jobs.find(row => row.job_id === path.split("/").at(-2))!;
      const f = forecastView(); f.session_id = preview.session_id; f.job_id = row.job_id; f.profile = row.profile;
      f.input_basis = structuredClone(preview.input_basis); f.job_view = row.view;
      f.trajectory.job_id = row.job_id; f.trajectory.profile = row.profile;
      const route = (f.trajectory.vehicle_routes as Array<{ vehicle_id: string; order_sequence: string[]; actions: Record<string, unknown>[] }>)[0];
      route.vehicle_id = "M3-V1"; route.order_sequence = ["M3-O1"];
      route.actions.push({ kind: "SERVICE", start_us: "2000000", end_us: "2000000", order_id: "M3-O1", node_id: 2, load_after_kg: 0 }); data = f;
    } else if (path.endsWith("/events")) data = { schema_version: "saferoute-m3-pending-events/1", basis: { ...s.state.basis, ...(wrongBasis ? { generation: "7" } : {}) },
      current_time: s.state.current_time, execution_mode: "SIMULATED_REPLAY", real_world_observation: false, events: pending };
    else if (path.endsWith("/replay/history")) data = { schema_version: "saferoute-m3-replay-history/1", session_id: s.state.basis.session_id, history: receipts };
    else if (path.endsWith("/apply")) {
      const body = JSON.parse(String(options!.body)); posts.push({ path, body });
      const code = rejectCode ?? (!replies.has(body.request_id) && !pending.length ? "EVENT_ALREADY_APPLIED" : undefined);
      if (code) return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "ERROR", request_id: "trace-event", data: null,
        diagnostics: [{ severity: "ERROR", code, path: "event_id", message: "Server event conflict" }] }), { status: 409 });
      if (!replies.has(body.request_id)) {
        const r = receipt(); replies.set(body.request_id, r); receipts.unshift(r);
        Object.assign(s.state.basis, r.basis); pending.splice(0); s.state.pending_event_ids = [];
        if (scenario === "S2") {
          s.state.order_ids.push("WIRE-URGENT"); s.state.unserved.push({ order_id: "WIRE-URGENT", reason: "NO_ACCEPTED_PLAN" });
          s.orders.orders.push({ ...s.orders.orders[0], order_id: "WIRE-URGENT", priority: 3 });
          s.locations.locations.push({ location_id: "delivery:WIRE-URGENT", kind: "DELIVERY", order_id: "WIRE-URGENT", graph_node_id: "9007199254740993", coordinates: [106.73, 10.83] });
        } else if (scenario === "S3") Object.assign(s.state.vehicles[0], { availability: "UNAVAILABLE", activity: "IMMOBILIZED" });
        sync();
      }
      if (loseReply) { loseReply = false; throw new TypeError("Reply lost after event commit"); }
      data = { schema_version: "saferoute-m3-replay-view/1", receipt: replies.get(body.request_id), execution_view: s.state };
      if (corruptReply) data = { ...(data as Record<string, unknown>), receipt: { ...replies.get(body.request_id), event_type: "VEHICLE_UNAVAILABLE" } };
    } else if (path === `/api/sessions/${s.state.basis.session_id}`) data = { session_id: s.state.basis.session_id, scenario_id: scenario, build_sha256: s.state.basis.build_sha256, catalog_sha256: "c".repeat(64), fixture_sha256: "f".repeat(64) };
    else if (path === "/api/scenarios") {
      const response = await s.fetcher(url, options); const envelope = await response.json();
      envelope.data.scenarios = ["S0", "S1", "S2", "S3", "S4"].map(scenario_id => ({ scenario_id, fixture_sha256: "f".repeat(64), initial_time: s.state.current_time, order_count: 1, vehicle_count: 1 }));
      return new Response(JSON.stringify(envelope));
    } else return s.fetcher(url, options);
    return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "OK", request_id: "trace-event", data, diagnostics: [] }));
  };
  const client = new Member3Client({ fetch: fetcher, token: () => "test-only-bearer" });
  const api = new BackendDispatchApi({ client, storage: s.storage }); await api.loadScenario(scenario);
  return { ...s, api, client, event, pending, receipts, posts, barrier, sync, receipt, comparePosts,
    lose: () => { loseReply = true; }, reject: (code: string) => { rejectCode = code; }, mismatch: () => { wrongBasis = true; }, corrupt: (value: boolean) => { corruptReply = value; } };
}

describe("BackendDispatchApi Phase 5", () => {
  it("invalidates old forecasts and re-optimizes using the new full rain basis", async () => {
    const h = await eventHarness("S4"); await h.api.optimize();
    const before = await h.api.getSnapshot(); expect(before.planState.proposedAlternatives).toHaveLength(3);
    await h.api.selectAlternative(before.planState.proposedAlternatives[0].id);
    h.barrier(true); await h.api.getSnapshot();
    const applied = await h.api.triggerFixtureEvent(h.event.event_id);
    expect(applied.planState.proposedAlternatives).toEqual([]); expect(applied.planState.selectedAlternativeId).toBeNull();
    await h.api.optimize(); const next = await h.api.getSnapshot();
    expect(h.comparePosts[1].expected_revision).toEqual({ head_version: "3", generation: "0" });
    expect(next.backend!.comparison!.input_basis).toEqual(h.state.basis);
    expect(next.backend!.comparison!.input_basis).not.toEqual(before.backend!.comparison!.input_basis);
    expect(next.planState.proposedAlternatives).toHaveLength(3);
    expect(next.planState.proposedAlternatives.every(p => p.origin?.kind === "MEMBER3" && p.origin.inputBasis.overlay_sha256 === "e".repeat(64))).toBe(true);
  });
  it("coalesces simultaneous Apply calls into one durable command", async () => {
    const h = await eventHarness(); h.barrier(true); await h.api.getSnapshot();
    const first = h.api.triggerFixtureEvent(h.event.event_id), second = h.api.triggerFixtureEvent(h.event.event_id);
    await Promise.all([first, second]); expect(h.posts).toHaveLength(1); expect(h.receipts).toHaveLength(1);
  });
  it("applies_once_then_refreshes from server projections and confirmed history", async () => {
    const h = await eventHarness(); h.barrier(true); await h.api.getSnapshot();
    const next = await h.api.triggerFixtureEvent(h.event.event_id).catch(error => error);
    expect(next.backend?.replayHistory?.history).toEqual(h.receipts);
    expect(next.decisionState?.orders.map((o: { id: string }) => o.id)).toEqual(["M3-O1", "WIRE-URGENT"]);
    expect(next.backend?.needsReoptimization).toBe(true);
    expect(h.posts).toHaveLength(1);
    expect(h.posts[0].body).toMatchObject({ expected_revision: { head_version: "2", generation: "0" }, request_id: expect.any(String) });
    expect(Object.keys(h.posts[0].body).sort()).toEqual(["expected_revision", "request_id"]);
    await expect(h.client.applyEvent(h.state.basis.session_id, h.event.event_id, { request_id: "new-event-intent", expected_revision: { head_version: "3", generation: "0" } })).rejects.toMatchObject({ code: "EVENT_ALREADY_APPLIED" });
  });
  it("retries lost Apply with the original identity after another tab changed the session pointer", async () => {
    const h = await eventHarness(); h.barrier(true); await h.api.getSnapshot(); h.lose();
    await expect(h.api.triggerFixtureEvent(h.event.event_id)).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    Object.assign(h.state.basis, { head_version: "4", head_sha256: "5".repeat(64) }); h.sync();
    const key = [...h.values.keys()].find(k => k.includes("session.v1"))!;
    const pointer = JSON.parse(h.values.get(key)!); pointer.session.session_id = "other-tab-session"; h.values.set(key, JSON.stringify(pointer));
    const next = await new BackendDispatchApi({ client: h.client, storage: h.storage }).getSnapshot();
    expect(h.posts).toHaveLength(2); expect(h.posts[1]).toEqual(h.posts[0]);
    expect(next.backend!.basis.head_version).toBe("4"); expect(next.backend!.replayHistory!.history[0].basis.head_version).toBe("3");
    expect(next.decisionState.orders.filter(o => o.id === "WIRE-URGENT")).toHaveLength(1);
    expect(next.backend!.mutationPending).toBe(false);
  });
  it.each(["EVENT_NOT_DUE", "EVENT_ALREADY_APPLIED", "STALE_HEAD", "ACCEPTED_REPLAY_REQUIRED", "EVENT_UNKNOWN", "EVENT_TYPE_UNSUPPORTED", "PLAN_NOT_ACTIVE"])("settles definite %s but preserves its diagnostic without a new Apply", async code => {
    const h = await eventHarness(); h.barrier(true); await h.api.getSnapshot(); h.reject(code);
    await expect(h.api.triggerFixtureEvent(h.event.event_id)).rejects.toMatchObject({ code });
    await h.api.getSnapshot(); await h.api.loadScenario("S0"); expect(h.posts).toHaveLength(1);
  });
  it("keeps the original intent on receipt integrity failure and recovers it without duplicating the event", async () => {
    const h = await eventHarness(); h.barrier(true); await h.api.getSnapshot(); h.corrupt(true);
    await expect(h.api.triggerFixtureEvent(h.event.event_id)).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
    await expect(h.api.loadScenario("S0")).rejects.toMatchObject({ code: "PENDING_COMMAND" });
    h.corrupt(false); const next = await h.api.getSnapshot();
    expect(h.posts).toHaveLength(2); expect(h.posts[1]).toEqual(h.posts[0]); expect(h.receipts).toHaveLength(1);
    expect(next.backend!.mutationPending).toBe(false);
  });
  it("preserves S3 onboard ownership and delivered prefix through Apply", async () => {
    const h = await eventHarness("S3"); h.barrier(true);
    h.state.vehicles[0].onboard_order_ids = ["M3-O1"]; h.state.vehicles[0].current_load_kg = 7;
    h.orders.orders[0].status = "ONBOARD"; h.orders.orders[0].owner_vehicle_id = "M3-V1";
    const before = structuredClone(h.state.vehicles[0]); await h.api.getSnapshot();
    const next = await h.api.triggerFixtureEvent(h.event.event_id).catch(error => error);
    expect(next.decisionState?.vehicles[0]).toMatchObject({ availability: "UNAVAILABLE", currentLoadKg: 7, onboardOrderIds: ["M3-O1"] });
    expect(next.decisionState?.orders[0]).toMatchObject({ status: "ONBOARD", assignedVehicleId: "M3-V1" });
    expect(h.state.vehicles[0].position).toEqual(before.position); expect(h.state.delivered_prefix).toEqual([]);
  });
  it("does not fabricate rain geometry or erase the server overlay when server time passes the rain window", async () => {
    const h = await eventHarness("S4"); h.barrier(true); await h.api.getSnapshot();
    const applied = await h.api.triggerFixtureEvent(h.event.event_id).catch(error => error);
    expect(applied.backend?.basis.overlay_sha256).toBe("e".repeat(64));
    h.state.current_time = "2026-09-27T23:00:00+07:00"; h.sync();
    const next = await h.api.getSnapshot();
    expect(next.decisionState.context.rain).toBeNull(); expect(next.backend!.basis.overlay_sha256).toBe("e".repeat(64));
    expect(next.demoClock.now).toBe("2026-09-27T23:00:00+07:00");
  });
  it("does not invent an applied transition when an event disappears from capped history", async () => {
    const h = await eventHarness(); h.pending.splice(0); h.state.pending_event_ids = [];
    const next = await h.api.getSnapshot();
    expect(next.backend?.replayHistory?.history).toEqual([]); expect(next.decisionState.events).toEqual([]);
  });
  it("event_is_not_due_until_observed_exact_barrier", async () => {
    const h = await eventHarness();
    expect((await h.api.getSnapshot()).backend!.pendingEvents?.events[0]?.apply_allowed).toBe(false);
    h.barrier(false);
    expect((await h.api.getSnapshot()).backend!.pendingEvents?.events[0]?.apply_allowed).toBe(false);
    h.barrier(true);
    expect((await h.api.getSnapshot()).backend!.pendingEvents?.events[0]?.apply_allowed).toBe(true);
    expect((await h.api.capabilities()).applyEvent).toBe(true);
    expect(h.posts).toEqual([]);
  });
  it("reads only native events and does not invent S1 fixture events or rain polygons", async () => {
    const h = await eventHarness("S1"); const snapshot = await h.api.getSnapshot();
    expect(snapshot.backend!.pendingEvents?.events).toEqual([]);
    expect(snapshot.decisionState.events).toEqual([]); expect(snapshot.decisionState.context.rain).toBeNull();
  });
  it("fails closed when event metadata differs from the coherent full basis", async () => {
    const h = await eventHarness(); h.mismatch();
    await expect(h.api.getSnapshot()).rejects.toMatchObject({ code: "STATE_CHANGED" });
    expect(h.posts).toEqual([]);
  });
});

describe("BackendDispatchApi Phase 4", () => {
  it("keeps distinct durable intents when tabs inherit an identical sessionStorage identity", async () => {
    sessionStorage.setItem("saferoute.member3.command-tab.v1", "copied-tab");
    const h = await acceptHarness(); const other = new BackendDispatchApi({ client: h.client, storage: h.storage });
    await other.getSnapshot(); await other.selectAlternative("job-FASTEST"); await h.api.selectAlternative("job-SAFER"); h.unknown(true);
    await expect(h.api.acceptSelectedPlan()).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    const firstIdentity = sessionStorage.getItem("saferoute.member3.command-tab.v1")!;
    sessionStorage.setItem("saferoute.member3.command-tab.v1", "copied-tab");
    await expect(other.acceptSelectedPlan()).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    const intents = () => [...h.values.entries()].filter(([key, value]) => key.includes("command.v1") && JSON.parse(value) !== null);
    expect(intents()).toHaveLength(2);
    sessionStorage.setItem("saferoute.member3.command-tab.v1", firstIdentity); h.unknown(false);
    const restored = await new BackendDispatchApi({ client: h.client, storage: h.storage }).getSnapshot();
    expect(restored.planState.acceptedExecution!.jobId).toBe("job-SAFER"); expect(h.posts[2]).toBe(h.posts[0]);
    expect(intents()).toHaveLength(1); expect(JSON.parse(intents()[0][1]).resourceId).toBe("job-FASTEST");
    sessionStorage.removeItem("saferoute.member3.command-tab.v1");
  });
  it.each(["WITNESS_INVALID", "WITNESS_REQUIRED", "JOB_STALE"])("settles definitive %s rejection without retrying it forever", async code => {
    const h = await acceptHarness(); await h.api.selectAlternative("job-SAFER"); h.reject(409, code);
    await expect(h.api.acceptSelectedPlan()).rejects.toMatchObject({ code });
    await h.api.getSnapshot(); expect(h.posts).toHaveLength(1);
    await expect(h.api.loadScenario("S0")).resolves.toMatchObject({ decisionState: { scenarioId: "S0" } });
    expect(h.posts).toHaveLength(1);
  });
  it("restores the pending Accept's owned session when another tab switched the shared pointer", async () => {
    const h = await acceptHarness(); await h.api.selectAlternative("job-SAFER"); h.lose();
    await expect(h.api.acceptSelectedPlan()).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    const key = [...h.values.keys()].find(key => key.includes("session.v1"))!;
    const pointer = JSON.parse(h.values.get(key)!); pointer.session = { ...pointer.session, session_id: "other-owned-session", scenario_id: "S0" }; delete pointer.comparison;
    h.values.set(key, JSON.stringify(pointer));
    const restored = await new BackendDispatchApi({ client: h.client, storage: h.storage }).getSnapshot();
    expect(restored.decisionState.sessionId).toBe(h.c.session_id); expect(restored.planState.acceptedExecution!.jobId).toBe("job-SAFER");
    expect(h.posts).toHaveLength(2); expect(h.posts[1]).toBe(h.posts[0]); expect(h.paths).toContain(`/api/sessions/${h.c.session_id}`);
  });
  it("permits authenticated world reads but blocks commands when per-tab identity cannot persist", async () => {
    sessionStorage.removeItem("saferoute.member3.command-tab.v1");
    const write = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Storage denied"); });
    try {
      const h = await acceptHarness(); await h.api.selectAlternative("job-SAFER");
      await expect(h.api.acceptSelectedPlan()).rejects.toMatchObject({ code: "COMMAND_STORAGE_UNAVAILABLE" });
      expect(h.posts).toEqual([]);
    } finally { write.mockRestore(); }
  });
  it("keeps a lost Accept durable when another tab reads and starts a different command", async () => {
    sessionStorage.setItem("saferoute.member3.command-tab.v1", "first-tab");
    const h = await acceptHarness(); await h.api.selectAlternative("job-SAFER"); h.lose();
    await expect(h.api.acceptSelectedPlan()).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    const recoveryIdentity = sessionStorage.getItem("saferoute.member3.command-tab.v1")!;
    sessionStorage.setItem("saferoute.member3.command-tab.v1", "second-tab");
    const other = new BackendDispatchApi({ client: h.client, storage: h.storage });
    await other.getSnapshot(); expect(h.posts).toHaveLength(1);
    await expect(other.optimize()).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
    sessionStorage.setItem("saferoute.member3.command-tab.v1", recoveryIdentity);
    const restored = await new BackendDispatchApi({ client: h.client, storage: h.storage }).getSnapshot();
    expect(h.posts).toHaveLength(2); expect(h.posts[1]).toBe(h.posts[0]); expect(restored.backend!.basis.generation).toBe("1");
    sessionStorage.removeItem("saferoute.member3.command-tab.v1");
  });
  it("accept_guard_checks_full_basis_and_witness including generation, every hash, and registry binding", async () => {
    const h = await acceptHarness(); const snapshot = await h.api.selectAlternative("job-SAFER");
    expect(canAcceptSelectedPlan(snapshot)).toBe(true);
    for (const field of Object.keys(snapshot.backend!.basis)) {
      const changed = structuredClone(snapshot); Object.assign(changed.backend!.basis, { [field]: field === "generation" ? "1" : "changed" });
      expect(canAcceptSelectedPlan(changed), field).toBe(false);
    }
    for (const patch of [{ job_status: "QUEUED" }, { job_status: "FAILED" }, { plan_available: false }, { validation: { status: "NOT_RUN", valid: null } }, { job_id: "wrong-job" }]) {
      const changed = structuredClone(snapshot); Object.assign(changed.backend!.jobs!["job-SAFER"], patch);
      expect(canAcceptSelectedPlan(changed)).toBe(false);
    }
    for (const patch of [{ stale: true }, { mutationPending: true }, { error: { code: "AUTH_REQUIRED", message: "Missing token" } }]) {
      const changed = structuredClone(snapshot); Object.assign(changed.backend!, patch); expect(canAcceptSelectedPlan(changed)).toBe(false);
    }
  });
  it("select_is_ui_only even with a distinct accepted route", async () => {
    const h = await acceptHarness(); h.activate("job-FASTEST", "0"); const before = await h.api.getSnapshot();
    const count = h.paths.length; const after = await h.api.selectAlternative("job-SAFER");
    expect(h.paths).toHaveLength(count); expect(h.posts).toEqual([]);
    expect(after.decisionState).toEqual(before.decisionState); expect(after.executionState).toEqual(before.executionState);
    expect(after.planState.acceptedExecution).toEqual(before.planState.acceptedExecution);
    expect(after.planState.selectedAlternativeId).toBe("job-SAFER");
    expect(createMapScene(after, after.planState.proposedAlternatives[2], "M3-V1").accepted[0].id).toContain("job-FASTEST");
  });
  it("accepts the selected job with exact string revision then reads fresh physical state", async () => {
    const h = await acceptHarness(); await h.api.selectAlternative("job-SAFER"); const before = await h.api.getSnapshot();
    const next = await h.api.acceptSelectedPlan();
    expect(h.posts).toHaveLength(1); expect(JSON.parse(h.posts[0])).toMatchObject({ expected_revision: { head_version: "9007199254740993", generation: "0" }, request_id: expect.any(String) });
    expect(h.paths.some(p => p.endsWith("/jobs/job-SAFER/accept"))).toBe(true);
    expect(h.paths.slice(h.paths.findIndex(p => p.endsWith("/accept")) + 1)).toContain(`/api/sessions/${h.c.session_id}/state`);
    expect(next.backend!.basis.generation).toBe("1"); expect(next.planState.acceptedExecution!.jobId).toBe("job-SAFER");
    expect(next.decisionState.orders).toEqual(before.decisionState.orders); expect(next.decisionState.vehicles).toEqual(before.decisionState.vehicles);
    expect(next.demoClock).toEqual(before.demoClock); expect(next.backend!.executionView.delivered_prefix).toEqual([]);
    expect(next.backend!.acceptances).toEqual(h.history); expect(next.planState.acceptedPlans).toEqual([]);
    expect(next.planState.selectedAlternativeId).toBeNull();
  });
  it("lost_accept_response_retries_without_double_accept or rewinding a newer active job", async () => {
    const h = await acceptHarness(); await h.api.selectAlternative("job-SAFER"); h.lose();
    await expect(h.api.acceptSelectedPlan()).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await expect(h.api.loadScenario("S0")).rejects.toMatchObject({ code: "PENDING_COMMAND" });
    h.activate("job-FASTEST", "2");
    const restored = await new BackendDispatchApi({ client: h.client, storage: h.storage }).getSnapshot();
    expect(h.posts).toHaveLength(2); expect(h.posts[1]).toBe(h.posts[0]); expect(h.history).toHaveLength(1);
    expect(restored.backend!.basis.generation).toBe("2"); expect(restored.planState.acceptedExecution!.jobId).toBe("job-FASTEST");
    expect(restored.backend!.acceptances![0].job_id).toBe("job-SAFER"); expect(restored.planState.acceptedPlans).toEqual([]);
    expect([...h.values.values()].join()).not.toContain("coordinates");
  });
  it("STALE_HEAD refreshes the race winner and requires a new Optimize without another Accept", async () => {
    const h = await acceptHarness(); await h.api.selectAlternative("job-SAFER"); h.stale();
    await expect(h.api.acceptSelectedPlan()).rejects.toMatchObject({ code: "STALE_HEAD" });
    const next = await h.api.getSnapshot();
    expect(h.posts).toHaveLength(1); expect(next.backend!.basis.generation).toBe("1");
    expect(next.planState.acceptedExecution!.jobId).toBe("job-FASTEST"); expect(next.planState.proposedAlternatives).toEqual([]);
    await expect(h.api.acceptSelectedPlan()).rejects.toMatchObject({ code: "ACCEPT_UNAVAILABLE" }); expect(h.posts).toHaveLength(1);
  });
  it("rechecks full basis with a fresh world before creating the durable Accept intent", async () => {
    const h = await acceptHarness(); await h.api.selectAlternative("job-SAFER"); h.activate("job-FASTEST", "1");
    await expect(h.api.acceptSelectedPlan()).rejects.toMatchObject({ code: "ACCEPT_UNAVAILABLE" });
    expect(h.posts).toEqual([]);
    expect((await h.api.getSnapshot()).planState.acceptedExecution!.jobId).toBe("job-FASTEST");
    expect([...h.values.entries()].filter(([key]) => key.includes("command.v1"))).toEqual([]);
  });
  it("rehydrates confirmed history without cloning current geometry into historical plans", async () => {
    const h = await acceptHarness(); await h.api.selectAlternative("job-SAFER"); await h.api.acceptSelectedPlan(); h.activate("job-FASTEST", "2");
    const next = await new BackendDispatchApi({ client: h.client, storage: h.storage }).getSnapshot();
    expect(h.posts).toHaveLength(1); expect(next.backend!.acceptances![0].job_id).toBe("job-SAFER");
    expect(next.planState.acceptedExecution!.jobId).toBe("job-FASTEST"); expect(next.planState.acceptedPlans).toEqual([]);
  });
  it("double-click shares one intent and pending Accept blocks selection", async () => {
    const h = await acceptHarness(); await h.api.selectAlternative("job-SAFER");
    let release!: () => void; h.hold(new Promise<void>(resolve => { release = resolve; }));
    const first = h.api.acceptSelectedPlan(), second = h.api.acceptSelectedPlan();
    await expect(h.api.selectAlternative("job-FASTEST")).rejects.toMatchObject({ code: "PREVIEW_UNAVAILABLE" });
    await vi.waitFor(() => expect(h.posts).toHaveLength(1));
    expect((await h.api.capabilities()).accept).toBe(false);
    release(); await Promise.all([first, second]); expect(h.posts).toHaveLength(1);
  });
});

describe("BackendDispatchApi foundation", () => {
  it("reloads certified forecasts and selects a preview locally with zero Accept POSTs", async () => {
    const s = server(); const c = comparison();
    c.session_id = s.state.basis.session_id; c.input_basis = s.state.basis;
    c.jobs.forEach(row => { row.view.input_basis = s.state.basis; row.view.served_orders = ["M3-O1"]; });
    c.outcome.comparison.jobs.forEach(row => { row.basis = s.state.basis; });
    const reads: string[] = [], mutations: string[] = []; let failForecast = false;
    const fetcher: typeof fetch = async (url, options) => {
      const path = new URL(String(url)).pathname;
      if (options?.method === "POST") mutations.push(path);
      let data: unknown;
      if (path.includes("/profiles/comparisons/")) data = c;
      else if (path.endsWith("/forecast")) {
        if (failForecast) return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "ERROR", request_id: "trace", data: null, diagnostics: [{ code: "FORECAST_UNAVAILABLE", path: "forecast", message: "Read unavailable" }] }), { status: 503 });
        reads.push(path); const jid = path.split("/").at(-2)!; const row = c.jobs.find(r => r.job_id === jid)!;
        const f = forecastView(); f.session_id = c.session_id; f.job_id = jid; f.profile = row.profile;
        f.input_basis = s.state.basis; f.job_view = { ...Object.fromEntries(Object.entries(row.view).reverse()), ...row.view };
        f.trajectory.job_id = jid; f.trajectory.profile = row.profile;
        const route = (f.trajectory.vehicle_routes as Array<{ vehicle_id: string; order_sequence: string[]; actions: Record<string, unknown>[] }>)[0];
        route.vehicle_id = "M3-V1"; route.order_sequence = ["M3-O1"];
        route.actions.push({ kind: "SERVICE", start_us: "2000000", end_us: "2000000", order_id: "M3-O1", node_id: 2, load_after_kg: 0 });
        data = f;
      } else return s.fetcher(url, options);
      return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", status: "OK", request_id: "trace", data, diagnostics: [] }));
    };
    await new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1");
    const key = [...s.values.keys()].find(k => k.includes("session.v1"))!;
    const pointer = JSON.parse(s.values.get(key)!); pointer.comparison = { id: c.comparison_id, inputBasis: c.input_basis }; s.values.set(key, JSON.stringify(pointer));
    const client = new Member3Client({ fetch: fetcher, token: () => "test-bearer" });
    const api = new BackendDispatchApi({ client, storage: s.storage });
    const before = await api.getSnapshot();
    expect(before.planState.proposedAlternatives).toHaveLength(3); expect(reads).toHaveLength(3);
    expect(before.backend!.capabilities!.forecastGeometry).toBe(true);
    const selected = await api.selectAlternative("job-SAFER");
    expect(selected.planState.selectedAlternativeId).toBe("job-SAFER");
    expect(createMapScene(selected, selected.planState.proposedAlternatives[2]).proposed).toHaveLength(1);
    expect(selected.planState.acceptedExecution).toBeNull(); expect(selected.executionState.activePlanId).toBeNull();
    expect(createMapScene(selected, selected.planState.proposedAlternatives[2], "M3-V1").proposed).toEqual([]);
    expect(selected.decisionState).toEqual(before.decisionState);
    const restored = await new BackendDispatchApi({ client, storage: s.storage }).getSnapshot();
    expect(restored.planState.selectedAlternativeId).toBe("job-SAFER"); expect(mutations).toEqual([]);
    expect(JSON.stringify([...s.values.values()])).not.toContain("coordinates");
    c.jobs[2].view.served_orders = ["M3-O2"];
    await expect(api.getSnapshot()).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
    c.jobs[2].view.served_orders = ["M3-O1"];
    failForecast = true;
    const failing = new BackendDispatchApi({ client, storage: s.storage }); const observed: import("../../shared/types/dispatch").DispatchSnapshot[] = [];
    const stop = failing.subscribe(next => observed.push(next));
    await expect(failing.getSnapshot()).rejects.toMatchObject({ code: "FORECAST_UNAVAILABLE" }); stop();
    expect(observed.at(-1)!.backend!.stale).toBe(true); expect(observed.at(-1)!.planState.proposedAlternatives).toEqual([]);
    expect(observed.at(-1)!.backend!.capabilities!.forecastGeometry).toBe(false);
    expect(observed.at(-1)!.planState.selectedAlternativeId).toBeNull();
    failForecast = false;
    s.state.basis = { ...s.state.basis, source_sha256: "f".repeat(64) }; s.orders.basis = s.vehicles.basis = s.locations.basis = s.state.basis;
    const stale = await api.getSnapshot(); expect(stale.planState.proposedAlternatives).toEqual([]); expect(stale.planState.selectedAlternativeId).toBeNull();
  });
  it("gives a session mutation priority over coalesced periodic reads and fences the late reply", async () => {
    vi.useFakeTimers();
    try {
      const s = server(); let hold = false, release!: () => void, pollSignal!: AbortSignal;
      const fetcher: typeof fetch = async (url, options) => {
        if (hold && String(url).endsWith("/state")) { hold = false; pollSignal = options!.signal!;
          await new Promise<void>(resolve => { release = resolve; }); }
        return s.fetcher(url, options);
      };
      const api = new BackendDispatchApi({ client: new Member3Client({ fetch: fetcher, token: () => "test-only-bearer" }), storage: s.storage });
      await api.loadScenario("S1"); const published: string[] = []; const unsub = api.subscribe(next => published.push(next.decisionState.scenarioId));
      hold = true; await vi.advanceTimersByTimeAsync(2500);
      const next = api.loadScenario("S0"); expect(pollSignal.aborted).toBe(true);
      await vi.advanceTimersByTimeAsync(10000);
      expect(s.requests.filter(r => r.path.endsWith("/load"))).toHaveLength(1);
      release(); expect((await next).decisionState.scenarioId).toBe("S0");
      expect(published).toEqual(["S0"]); unsub();
      const readCount = s.requests.length; await vi.advanceTimersByTimeAsync(10000); expect(s.requests).toHaveLength(readCount);
    } finally { vi.useRealTimers(); }
  });
  it("definite STALE_HEAD refreshes world and permits a new explicit intent", async () => {
    const s = server(); let rejected = true; const posts: string[] = [];
    const fetcher: typeof fetch = async (url, options) => {
      if (String(url).endsWith("/profiles/compare")) {
        posts.push(options!.body as string);
        if (rejected) { rejected = false; s.state.basis.generation = "1";
          return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", request_id: "trace", status: "ERROR", data: null, diagnostics: [{ code: "STALE_HEAD", path: "compare", message: "World changed" }] }), { status: 409 }); }
        const receipt = { schema_version: "saferoute-m3-profile-submission/1", session_id: s.state.basis.session_id, comparison_id: "comparison-new", mode: "NEW_BATCH", input_basis: s.state.basis, profiles: ["FASTEST", "BALANCED", "SAFER"], links: { poll: "/poll", cancel: "/cancel" } };
        return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", request_id: "trace", status: "OK", data: receipt, diagnostics: [] }));
      }
      return s.fetcher(url, options);
    };
    const api = new BackendDispatchApi({ client: new Member3Client({ fetch: fetcher, token: () => "test-only-bearer" }), storage: s.storage }); await api.loadScenario("S1");
    await expect(api.optimize()).rejects.toMatchObject({ code: "STALE_HEAD" });
    expect((await api.getSnapshot()).backend?.basis.generation).toBe("1"); expect(posts).toHaveLength(1);
    await api.optimize(); expect(posts).toHaveLength(2);
    expect(JSON.parse(posts[1]).request_id).not.toBe(JSON.parse(posts[0]).request_id);
    expect(JSON.parse(posts[1]).expected_revision.generation).toBe("1");
  });
  it("does not submit an undurable intent after storage fails then Refresh", async () => {
    const s = server(); let failedWrites = 0, posts = 0;
    const storage = { getItem: s.storage.getItem, setItem: (k: string, v: string) => { if (k.includes("command.v1")) { failedWrites++; throw new Error("Storage unavailable"); } s.storage.setItem(k, v); } };
    const fetcher: typeof fetch = async (url, options) => { if (String(url).endsWith("/profiles/compare")) { posts++; throw new Error("Must not submit"); } return s.fetcher(url, options); };
    const api = new BackendDispatchApi({ client: new Member3Client({ fetch: fetcher, token: () => "test-only-bearer" }), storage }); await api.loadScenario("S1");
    await expect(api.optimize()).rejects.toThrow();
    await api.getSnapshot().catch(() => {});
    expect(failedWrites).toBeGreaterThan(0); expect(posts).toBe(0);
  });
  it("retains the same recovery ID when comparison pointer persistence fails", async () => {
    const s = server(); let failPointer = true; const posts: string[] = [];
    const storage = { getItem: s.storage.getItem, setItem: (k: string, v: string) => { if (failPointer && k.includes("session.v1") && JSON.parse(v).comparison) throw new Error("Pointer unavailable"); s.storage.setItem(k, v); } };
    const receipt = { schema_version: "saferoute-m3-profile-submission/1", session_id: s.state.basis.session_id, comparison_id: "comparison-stable", mode: "NEW_BATCH", input_basis: s.state.basis, profiles: ["FASTEST", "BALANCED", "SAFER"], links: { poll: "/poll", cancel: "/cancel" } };
    const fetcher: typeof fetch = async (url, options) => {
      if (String(url).endsWith("/profiles/compare")) { posts.push(options!.body as string); return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", request_id: "trace", status: "OK", data: receipt, diagnostics: [] })); }
      return s.fetcher(url, options);
    };
    const client = new Member3Client({ fetch: fetcher, token: () => "test-only-bearer" });
    const api = new BackendDispatchApi({ client, storage }); await api.loadScenario("S1");
    await expect(api.optimize()).rejects.toThrow(); failPointer = false;
    const next = await new BackendDispatchApi({ client, storage }).getSnapshot();
    expect(posts).toHaveLength(2); expect(posts[1]).toBe(posts[0]); expect(next.backend?.comparison?.comparison_id).toBe("comparison-stable");
  });
  it.each([[401, "UNAUTHORIZED"], [403, "FORBIDDEN"], [409, "IDEMPOTENCY_CONFLICT"]])("fails closed for %s/%s and retains the exact intent", async (status, code) => {
    const s = server(); const posts: string[] = [];
    const fetcher: typeof fetch = async (url, options) => {
      if (String(url).endsWith("/profiles/compare")) { posts.push(options!.body as string); return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", request_id: "trace", status: "ERROR", data: null, diagnostics: [{ severity: "ERROR", code, path: "compare", message: "Rejected" }] }), { status: status as number }); }
      return s.fetcher(url, options);
    };
    const api = new BackendDispatchApi({ client: new Member3Client({ fetch: fetcher, token: () => "test-only-bearer" }), storage: s.storage }); await api.loadScenario("S1");
    await expect(api.optimize()).rejects.toMatchObject({ code });
    await expect(api.getSnapshot()).rejects.toMatchObject({ code });
    expect(posts).toHaveLength(2); expect(posts[1]).toBe(posts[0]);
    await expect(api.loadScenario("S0")).rejects.toMatchObject({ code: "PENDING_COMMAND" });
  });
  it("retries BUSY with bounded backoff and the same body", async () => {
    vi.useFakeTimers();
    try {
      const s = server(); let posts = 0; const bodies: string[] = [];
      const fetcher: typeof fetch = async (url, options) => {
        if (String(url).endsWith("/profiles/compare")) { posts++; bodies.push(options!.body as string); return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", request_id: "trace", status: "ERROR", data: null, diagnostics: [{ severity: "ERROR", code: "RUNTIME_BUSY", path: "compare", message: "Busy" }] }), { status: 503 }); }
        return s.fetcher(url, options);
      };
      const api = new BackendDispatchApi({ client: new Member3Client({ fetch: fetcher, token: () => "test-only-bearer" }), storage: s.storage }); await api.loadScenario("S1");
      const failed = expect(api.optimize()).rejects.toMatchObject({ code: "RUNTIME_BUSY" });
      await vi.advanceTimersByTimeAsync(7001); await failed;
      expect(posts).toBe(4); expect(new Set(bodies).size).toBe(1);
    } finally { vi.useRealTimers(); }
  });
  it("compare_submits_current_revision_once and survives lost reply/refresh", async () => {
    const s = server(); const posts: string[] = []; let lose = true;
    const receipt = { schema_version: "saferoute-m3-profile-submission/1", session_id: s.state.basis.session_id, comparison_id: "comparison-test", mode: "NEW_BATCH", input_basis: s.state.basis, profiles: ["FASTEST", "BALANCED", "SAFER"], links: { poll: "/poll", cancel: "/cancel" } };
    const fetcher: typeof fetch = async (url, options) => {
      if (String(url).endsWith("/profiles/compare")) { posts.push(options!.body as string); if (lose) { lose = false; throw new TypeError("Lost reply"); }
        return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", request_id: "trace", status: "OK", data: receipt, diagnostics: [] }), { status: 202 }); }
      return s.fetcher(url, options);
    };
    const client = new Member3Client({ fetch: fetcher, token: () => "test-only-bearer" });
    const api = new BackendDispatchApi({ client, storage: s.storage }); await api.loadScenario("S1");
    await expect(api.optimize()).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    const next = await new BackendDispatchApi({ client, storage: s.storage }).getSnapshot();
    expect(posts).toHaveLength(2); expect(posts[1]).toBe(posts[0]);
    expect(JSON.parse(posts[0])).toMatchObject({ expected_revision: { head_version: "2", generation: "0" } });
    expect(JSON.parse(posts[0])).not.toHaveProperty("job_ids");
    expect(next.backend?.comparison).toMatchObject({ comparison_id: "comparison-test", status: "QUEUED" });
    expect(next.planState.proposedAlternatives).toEqual([]); expect(next.backend?.basis).toEqual(s.state.basis);
  });
  it("publishes bound lifecycle and preserves registry on fresh world reads", async () => {
    const s = server(); const c = comparison(); c.session_id = s.state.basis.session_id; c.input_basis = { ...s.state.basis };
    for (const j of c.jobs) j.view.input_basis = { ...s.state.basis };
    for (const j of c.outcome.comparison.jobs) j.basis = { ...s.state.basis };
    const receipt = { schema_version: "saferoute-m3-profile-submission/1", session_id: c.session_id, comparison_id: c.comparison_id, mode: "NEW_BATCH", input_basis: c.input_basis, profiles: ["FASTEST", "BALANCED", "SAFER"], links: c.links };
    const fetcher: typeof fetch = async (url, options) => {
      const path = new URL(String(url)).pathname;
      if (path.includes("/profiles/")) return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", request_id: "trace", status: "OK", data: path.endsWith("/compare") ? receipt : c, diagnostics: [] }));
      if (path.endsWith("/forecast")) {
        const jid = path.split("/").at(-2)!, row = c.jobs.find(r => r.job_id === jid)!;
        const f = forecastView(); f.session_id = c.session_id; f.job_id = jid; f.profile = row.profile; f.input_basis = row.view.input_basis; f.job_view = row.view;
        f.trajectory.job_id = jid; f.trajectory.profile = row.profile;
        const route = (f.trajectory.vehicle_routes as Array<{ vehicle_id: string; order_sequence: string[]; actions: Record<string, unknown>[] }>)[0];
        route.vehicle_id = "M3-V1"; route.order_sequence = ["O1"];
        route.actions.push({ kind: "SERVICE", start_us: "2000000", end_us: "2000000", order_id: "O1", node_id: 2, load_after_kg: 0 });
        return new Response(JSON.stringify({ schema_version: "saferoute-m3-http-response/1", request_id: "trace", status: "OK", data: f, diagnostics: [] }));
      }
      return s.fetcher(url, options);
    };
    const api = new BackendDispatchApi({ client: new Member3Client({ fetch: fetcher, token: () => "test-only-bearer" }), storage: s.storage });
    await api.loadScenario("S1"); await api.optimize();
    const refreshed = await api.getSnapshot();
    expect(refreshed.backend?.comparison?.status).toBe("COMPLETED");
    expect(Object.keys(refreshed.backend?.jobs ?? {})).toHaveLength(3);
    s.state.basis.generation = "1";
    const newer = await api.getSnapshot(); expect(newer.backend?.comparison?.input_basis.generation).toBe("0");
    expect(newer.backend?.basis.generation).toBe("1");
  });
  it("renders server-owned world, preserving IDs, units, depot/delivery kinds and empty plans", async () => {
    const s = server();
    const api = new BackendDispatchApi({ client: s.client, storage: s.storage });
    const snapshots: unknown[] = [];
    api.subscribe((snapshot) => snapshots.push(snapshot));
    const snapshot = await api.loadScenario("S1");
    expect(snapshot.decisionState).toMatchObject({ sessionId: "m3-owned-session", scenarioId: "S1", version: 0 });
    expect(snapshot.decisionState.orders[0]).toMatchObject({ id: "M3-O1", latitude: 10.83, longitude: 106.73, demandKg: 7,
      status: "WAITING", assignedVehicleId: null, serviceTimeHours: 0.25, graphNodeId: "9007199254740993", pickedUpAt: null, deliveredAt: null });
    expect(snapshot.decisionState.vehicles[0]).toMatchObject({ id: "M3-V1", capacityKg: 20, currentPosition: { latitude: 10.82, longitude: 106.72, graphNodeId: "9007199254740993" } });
    expect(snapshot.decisionState.locations.map((item) => [item.id, item.kind])).toEqual([["M3-DEPOT", "DEPOT"], ["delivery:M3-O1", "DELIVERY"]]);
    expect(snapshot.demoClock.now).toBe("2026-09-27T21:13:00+07:00");
    expect(snapshot.backend).toMatchObject({ source: "MEMBER3_HTTP", executionMode: "SIMULATED_REPLAY", realWorldObservation: false });
    expect(snapshot.planState).toMatchObject({ acceptedPlans: [], proposedAlternatives: [], activeAcceptedPlanId: null });
    const scene = createMapScene(snapshot);
    expect(scene.markers.map((marker) => [marker.id, marker.kind])).toEqual([["M3-DEPOT", "depot"], ["M3-O1", "order"], ["M3-V1", "vehicle"]]);
    expect(scene.accepted).toEqual([]);
    expect(scene.proposed).toEqual([]);
    expect(snapshots).toHaveLength(1);
    expect(s.requests.map((item) => item.path)).toEqual(["/ready", "/api/runtime/capabilities", "/api/scenarios", "/api/scenarios/S1/load",
      "/api/sessions/m3-owned-session/state", "/api/sessions/m3-owned-session/orders", "/api/sessions/m3-owned-session/vehicles", "/api/sessions/m3-owned-session/locations",
      "/api/sessions/m3-owned-session/events", "/api/sessions/m3-owned-session/replay/history", "/api/sessions/m3-owned-session/replay/playback", "/api/sessions/m3-owned-session/acceptances"]);
  });

  it("refresh re-reads the saved M3 pointer and ignores poisoned mock authority", async () => {
    const s = server();
    s.values.set("saferoute.phase1.dispatch.v1", JSON.stringify({ schemaVersion: 1, snapshot: { decisionState: { orders: ["fake"] } } }));
    await new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1");
    s.requests.length = 0;
    s.state.current_time = s.orders.current_time = s.vehicles.current_time = s.locations.current_time = "2026-09-27T21:14:00+07:00";
    const next = await new BackendDispatchApi({ client: s.client, storage: s.storage }).getSnapshot();
    expect(next.decisionState.sessionId).toBe("m3-owned-session");
    expect(next.decisionState.orders.map((item) => item.id)).toEqual(["M3-O1"]);
    expect(next.demoClock.now).toBe("2026-09-27T21:14:00+07:00");
    expect(s.requests.every((item) => item.method === "GET")).toBe(true);
    expect([...s.values.values()].some((value) => value.includes('"orders"') && value.includes('"M3-O1"'))).toBe(false);
  });

  it("rejects torn reads instead of merging projections from another revision", async () => {
    const s = server();
    s.orders.basis = { ...s.orders.basis, head_sha256: "other-head" };
    await expect(new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1")).rejects.toMatchObject({ code: "STATE_CHANGED" });
  });

  it.each([
    ["0", "0"],
    ["9007199254740993", "9007199254740995"],
    ["9223372036854775807", "9223372036854775807"]
  ])("preserves the complete string revision (%s, %s) without using the legacy mock version", async (headVersion, generation) => {
    const s = server();
    s.state.basis.head_version = headVersion;
    s.state.basis.generation = generation;
    const snapshot = await new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1");
    expect(snapshot.backend?.basis).toEqual({ session_id: "m3-owned-session", build_sha256: "b".repeat(64),
      head_version: headVersion, generation, root_sha256: "1".repeat(64), head_sha256: "2".repeat(64),
      source_sha256: "3".repeat(64), context_version: "context-1", overlay_sha256: null });
    expect(snapshot.backend?.executionView.basis.head_version).toBe(headVersion);
    expect(snapshot.backend?.executionView.basis.generation).toBe(generation);
    expect(snapshot.decisionState.version).toBe(0);
  });

  it("refreshes generation-only changes while the head and legacy mock version stay unchanged", async () => {
    const s = server();
    const api = new BackendDispatchApi({ client: s.client, storage: s.storage });
    const before = await api.loadScenario("S1");
    s.state.basis = { ...s.state.basis, generation: "1" };
    s.orders.basis = s.vehicles.basis = s.locations.basis = s.state.basis;
    const after = await api.getSnapshot();
    expect(before.backend?.basis).toMatchObject({ head_version: "2", generation: "0" });
    expect(after.backend?.basis).toMatchObject({ head_version: "2", generation: "1" });
    expect(before.decisionState.version).toBe(0);
    expect(after.decisionState.version).toBe(0);
    expect(after.backend?.basis).not.toEqual(before.backend?.basis);
  });

  it("rejects projections whose generation differs even when their head version matches", async () => {
    const s = server();
    s.orders.basis = { ...s.orders.basis, generation: "1" };
    await expect(new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1"))
      .rejects.toMatchObject({ code: "STATE_CHANGED" });
  });

  it.each(["head_version", "generation"])("rejects invalid %s without converting it to a number", async (field) => {
    for (const value of [2, "01", "-1", "9223372036854775808"]) {
      const s = server();
      Reflect.set(s.state.basis, field, value);
      await expect(new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1"))
        .rejects.toMatchObject({ code: "INVALID_RESPONSE" });
    }
  });

  it("rejects malformed coverage and invalid coordinates rather than substituting fixtures", async () => {
    const s = server();
    s.orders.orders[0].coordinates = [300, 10];
    await expect(new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1")).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
    s.orders.orders[0].coordinates = [106.73, 10.83];
    s.orders.orders[0].order_id = "unexpected";
    await expect(new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1")).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
  });

  it("refuses unsupported manual physics/replay controls without HTTP mutations", async () => {
    const s = server();
    const api = new BackendDispatchApi({ client: s.client, storage: s.storage });
    await expect(api.selectAlternative("x")).rejects.toMatchObject({ code: "PREVIEW_UNAVAILABLE" });
    await expect(api.acceptSelectedPlan()).rejects.toMatchObject({ code: "ACCEPT_UNAVAILABLE" });
    for (const action of [
      () => api.setUrgentOrderEnabled(true), () => api.pickupOrder({ vehicleId: "v", orderId: "o" }),
      () => api.advanceDemoClock(10), () => api.resetDemoSession()]) {
      await expect(action()).rejects.toMatchObject({ code: "PHASE_NOT_SUPPORTED" });
    }
    expect(s.requests).toEqual([]);
    await expect(api.deliverOrder({vehicleId:'v',orderId:'o'})).rejects.toMatchObject({code:'SESSION_REQUIRED'});
  });

  it("uses the runtime physical status/custody rather than a source fixture default", async () => {
    const s = server();
    s.state.vehicles[0].onboard_order_ids = ["M3-O1"];
    s.state.vehicles[0].current_load_kg = 7;
    s.orders.orders[0].status = "ONBOARD";
    s.orders.orders[0].owner_vehicle_id = "M3-V1";
    const snapshot = await new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1");
    expect(snapshot.decisionState.orders[0]).toMatchObject({ status: "ONBOARD", assignedVehicleId: "M3-V1", pickedUpAt: null });
    expect(snapshot.decisionState.vehicles[0]).toMatchObject({ currentLoadKg: 7, onboardOrderIds: ["M3-O1"] });
  });

  it("shares concurrent initial reads so StrictMode does not create duplicate sessions", async () => {
    const s = server();
    const api = new BackendDispatchApi({ client: s.client, storage: s.storage });
    const [first, second] = await Promise.all([api.getSnapshot(), api.getSnapshot()]);
    expect(first.decisionState.sessionId).toBe(second.decisionState.sessionId);
    expect(s.requests.filter((item) => item.method === "POST")).toHaveLength(1);
  });

  it("retries an interrupted load with the persisted request ID after browser refresh", async () => {
    const s = server();
    let interrupted = false;
    const client = new Member3Client({ token: () => "test-only-bearer", fetch: async (url, options) => {
      const response = await s.fetcher(url, options);
      if (String(url).endsWith("/load") && !interrupted) { interrupted = true; throw new Error("response lost after commit"); }
      return response;
    } });
    await expect(new BackendDispatchApi({ client, storage: s.storage }).loadScenario("S1")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    const result = await new BackendDispatchApi({ client, storage: s.storage }).getSnapshot();
    expect(result.decisionState.scenarioId).toBe("S1");
    const bodies = s.requests.filter((item) => item.method === "POST").map((item) => item.body);
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
  });

  it("rejects a newly loaded session from a different runtime build", async () => {
    const s = server();
    s.capabilities.build_sha256 = "d".repeat(64);
    await expect(new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1")).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
  });

  it("keeps overlapping scenario selections in call order instead of publishing an older world last", async () => {
    const s = server();
    let release!: () => void;
    let firstReadStarted!: () => void;
    const blocked = new Promise<void>((resolve) => { release = resolve; });
    const started = new Promise<void>((resolve) => { firstReadStarted = resolve; });
    let first = true;
    const client = new Member3Client({ token: () => "test-only-bearer", fetch: async (url, options) => {
      if (String(url).endsWith("/state") && first) { first = false; firstReadStarted(); await blocked; }
      return s.fetcher(url, options);
    } });
    const api = new BackendDispatchApi({ client, storage: s.storage });
    const published: string[] = [];
    api.subscribe((snapshot) => published.push(snapshot.decisionState.scenarioId));
    const older = api.loadScenario("S0");
    await started;
    const newer = api.loadScenario("S1");
    await new Promise((resolve) => setTimeout(resolve, 10));
    release();
    await Promise.all([older, newer]);
    expect(published).toEqual(["S0", "S1"]);
    expect((await api.getSnapshot()).decisionState.scenarioId).toBe("S1");
  });

  it("accepts the native pre-plan vehicle shape without inventing unavailable position metadata", async () => {
    const s = server();
    // Observed on the native M3 initial state: these four fields appear only after a plan exists.
    for (const field of ["position_timestamp", "planned_suffix", "activity"]) Reflect.deleteProperty(s.state.vehicles[0], field);
    Reflect.deleteProperty(s.state.vehicles[0].position, "position_source");
    const snapshot = await new BackendDispatchApi({ client: s.client, storage: s.storage }).loadScenario("S1");
    expect(snapshot.decisionState.vehicles[0].positionTimestamp).toBeNull();
    expect(snapshot.backend?.executionMode).toBe("SIMULATED_REPLAY");
    expect(snapshot.planState.acceptedPlans).toEqual([]);
  });
});
