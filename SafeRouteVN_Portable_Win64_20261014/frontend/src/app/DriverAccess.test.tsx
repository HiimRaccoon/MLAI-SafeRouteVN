import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { DriverAccess } from './DriverAccess';
import { M3_TOKEN_KEY, type Member3Client } from '../integrations/member3/client';
const captured=vi.hoisted(()=>({clients:[] as Member3Client[]}));
vi.mock('../services/api/BackendDispatchApi',()=>({BackendDispatchApi:class {
  constructor(public options:{client:Member3Client;assignedSession:{session_id:string};storage:{getItem(key:string):string|null}}){captured.clients.push(options.client);}
}}));
vi.mock('./App',()=>({App:({api}:{api:{options:{assignedSession:{session_id:string};storage:{getItem(key:string):string|null}}}})=> <div>Driver workspace: {api.options.assignedSession.session_id} · pending: {api.options.storage.getItem('pending-test') ?? 'none'}</div>}));
const origin='https://laptop-s2lvd91p-1.tailbe6594.ts.net';
const first='a'.repeat(48),second='b'.repeat(48);
const grant=(token:string)=>'SRVN2.'+btoa(JSON.stringify({origin,token})).replace(/=/g,'');
function response(token:string,status=200,sid=assigned){return new Response(JSON.stringify({schema_version:'saferoute-m3-http-response/1',request_id:'test',status:status===200?'OK':'ERROR',diagnostics:[],data:status===200?{actor_id:token===first?'driver-A':'driver-B',role:'driver',scope:'session',session:{session_id:sid,scenario_id:'S0',build_sha256:'a'.repeat(64),catalog_sha256:'b'.repeat(64),fixture_sha256:'c'.repeat(64)}}:null}),{status});}
let release:(r:Response)=>void;
let defer=false;
let assigned='session-test', offline=false;
beforeEach(()=>{
  sessionStorage.clear();localStorage.clear();captured.clients.length=0;defer=false;assigned='session-test';offline=false;
  vi.stubEnv('VITE_REMOTE_DRIVER','1');vi.stubGlobal('location',{origin});
  vi.stubGlobal('fetch',vi.fn(async(_url,options)=>{
    if(offline)throw new TypeError('offline');
    if(defer){defer=false;return new Promise<Response>(resolve=>{release=resolve;});}
    return response(options.headers.Authorization.slice(7));
  }));
});
afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals();vi.unstubAllEnvs();});
it('refreshes the page without deleting the saved login or unresolved Play',async()=>{
  const reload=vi.fn();vi.stubGlobal('location',{origin,reload});
  const key='saferoute.remote-driver.driver-A.session-test.pending-test';
  localStorage.setItem(key,'unresolved-play');
  render(<DriverAccess/>);submit(first);await screen.findByText('driver-A');
  fireEvent.click(screen.getByRole('button',{name:'Làm mới'}));
  expect(reload).toHaveBeenCalledOnce();
  expect(sessionStorage.getItem(M3_TOKEN_KEY)).toBe(first);
  expect(localStorage.getItem(key)).toBe('unresolved-play');
});
function submit(token:string){fireEvent.change(screen.getByLabelText('Mã truy cập'),{target:{value:grant(token)}});fireEvent.click(screen.getByRole('button',{name:'Đăng nhập'}));}
it('does not let a delayed 401 from the logged-out account clear a new login',async()=>{
  render(<DriverAccess/>);submit(first);await screen.findByText('driver-A');
  defer=true;const oldRead=captured.clients[0].identity().catch(()=>{});
  fireEvent.click(screen.getByRole('button',{name:'Đăng xuất'}));submit(second);await screen.findByText('driver-B');
  await act(async()=>{release(response(first,401));await oldRead;});
  expect(screen.getByText('driver-B')).toBeInTheDocument();expect(sessionStorage.getItem(M3_TOKEN_KEY)).toBe(second);
});
it('does not persist a login that finishes after its screen has unmounted',async()=>{
  const view=render(<DriverAccess/>);defer=true;submit(first);
  await waitFor(()=>expect(fetch).toHaveBeenCalled());view.unmount();
  await act(async()=>{release(response(first));});
  expect(sessionStorage.getItem(M3_TOKEN_KEY)).toBeNull();
});

it('switches to a reassigned session without sending an old pending Play into the new session',async()=>{
  vi.useFakeTimers();render(<DriverAccess/>);
  localStorage.setItem('saferoute.remote-driver.driver-A.session-test.pending-test','old-play');
  await act(async()=>{submit(first);await vi.advanceTimersByTimeAsync(0);});
  expect(screen.getByText(/pending: old-play/)).toBeInTheDocument();
  assigned='session-reset';
  await act(async()=>{await vi.advanceTimersByTimeAsync(4000);});
  expect(screen.getByText(/Driver workspace: session-reset · pending: none/)).toBeInTheDocument();
  expect(sessionStorage.getItem(M3_TOKEN_KEY)).toBe(first);
  expect(localStorage.getItem('saferoute.remote-driver.driver-A.session-test.pending-test')).toBe('old-play');
});

it('continues following imported and built-in sessions without a page refresh',async()=>{
  vi.useFakeTimers();render(<DriverAccess/>);
  await act(async()=>{submit(first);await vi.advanceTimersByTimeAsync(0);});
  for(const sid of ['import-one','builtin-s1','import-two','builtin-s4','builtin-s0']){
    assigned=sid;
    await act(async()=>{await vi.advanceTimersByTimeAsync(3100);});
    expect(screen.getByText(new RegExp(`Driver workspace: ${sid}`))).toBeInTheDocument();
    expect(sessionStorage.getItem(M3_TOKEN_KEY)).toBe(first);
  }
});

it('retains login during an outage and resumes identity checking on the native foreground event',async()=>{
  vi.useFakeTimers();const view=render(<DriverAccess/>);
  await act(async()=>{submit(first);await vi.advanceTimersByTimeAsync(0);});
  offline=true;
  await act(async()=>{await vi.advanceTimersByTimeAsync(60000);});
  expect(sessionStorage.getItem(M3_TOKEN_KEY)).toBe(first);
  expect(screen.getByText(/Đang kết nối lại/)).toBeInTheDocument();
  offline=false;assigned='session-after-restart';
  await act(async()=>{window.dispatchEvent(new Event('saferoute:resume'));await vi.advanceTimersByTimeAsync(0);});
  expect(screen.getByText(/Driver workspace: session-after-restart/)).toBeInTheDocument();
  view.unmount();const count=vi.mocked(fetch).mock.calls.length;
  await act(async()=>{await vi.advanceTimersByTimeAsync(60000);});
  expect(fetch).toHaveBeenCalledTimes(count);
});

it('ignores a late identity reply from before logout',async()=>{
  vi.useFakeTimers();render(<DriverAccess/>);
  await act(async()=>{submit(first);await vi.advanceTimersByTimeAsync(0);});
  defer=true;await act(async()=>{await vi.advanceTimersByTimeAsync(3000);});
  fireEvent.click(screen.getByRole('button',{name:'Đăng xuất'}));
  await act(async()=>{submit(second);await vi.advanceTimersByTimeAsync(0);});
  await act(async()=>{release(response(first,200,'old-delayed-session'));await vi.advanceTimersByTimeAsync(0);});
  expect(screen.getByText('driver-B')).toBeInTheDocument();
  expect(screen.queryByText(/old-delayed-session/)).not.toBeInTheDocument();
});

it('preserves a saved grant and pending intent when the app opens during a server outage',async()=>{
  vi.useFakeTimers();offline=true;
  sessionStorage.setItem(M3_TOKEN_KEY,first);
  const key='saferoute.remote-driver.driver-A.session-test.pending-test';
  localStorage.setItem(key,'unresolved-play');
  render(<DriverAccess/>);
  await act(async()=>{await vi.advanceTimersByTimeAsync(6000);});
  expect(sessionStorage.getItem(M3_TOKEN_KEY)).toBe(first);
  expect(localStorage.getItem(key)).toBe('unresolved-play');
  offline=false;
  await act(async()=>{window.dispatchEvent(new Event('online'));await vi.advanceTimersByTimeAsync(0);});
  expect(screen.getByText('driver-A')).toBeInTheDocument();
});
