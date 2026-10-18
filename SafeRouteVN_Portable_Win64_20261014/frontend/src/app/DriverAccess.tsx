import { useEffect, useRef, useState, type FormEvent } from 'react';
import { App } from './App';
import { Member3Client, M3_TOKEN_KEY, type DriverIdentity } from '../integrations/member3/client';
import { validSession } from '../integrations/member3/sessionReference';
import { BackendDispatchApi } from '../services/api/BackendDispatchApi';
import './driverAccess.css';
import { decodeDriverGrant } from './driverGrant';
import { transientM3Error } from '../integrations/member3/polling';

const prefix = 'saferoute.remote-driver.';
function assignedApi(client: Member3Client, who: DriverIdentity) {
  if (who.role !== 'driver' || !validSession(who.session)) throw new Error('Tài khoản chưa được cấp quyền driver và phiên làm việc.');
  const namespace = `${prefix}${who.actor_id}.${who.session.session_id}.`;
  return new BackendDispatchApi({ client, assignedSession: who.session, storage: {
    getItem: key => localStorage.getItem(namespace + key),
    setItem: (key, data) => localStorage.setItem(namespace + key, data),
  }, subscribeChanges: () => () => {} });
}
function clearPrivateState() {
  sessionStorage.removeItem(M3_TOKEN_KEY);
  for (const key of Object.keys(localStorage)) if (key.startsWith(prefix)) localStorage.removeItem(key);
}

export function DriverAccess() {
  const [api, setApi] = useState<BackendDispatchApi>();
  const [identity, setIdentity] = useState<DriverIdentity>();
  const [activeClient, setActiveClient] = useState<Member3Client>();
  const [syncStatus, setSyncStatus] = useState('');
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);
  async function login(value: string) {
    const attempt = ++generation.current;
    setBusy(true); setError('');
    try {
      const client = new Member3Client({ baseUrl: location.origin, readTimeoutMs: 15000, token: () => generation.current === attempt ? value : null, onUnauthorized: () => {
        if (generation.current !== attempt) return;
        generation.current++; clearPrivateState(); setApi(undefined); setIdentity(undefined); setActiveClient(undefined); setSyncStatus(''); setBusy(false);
        setError('Quyền truy cập đã hết hạn hoặc bị thu hồi. Hãy đăng nhập lại.');
      } });
      // Validate before persisting the credential or mounting any driver data.
      const candidate = new Member3Client({ baseUrl: location.origin, readTimeoutMs: 15000, token: () => value });
      const who = await candidate.identity();
      if (generation.current !== attempt) return;
      if (who.role !== 'driver' || !validSession(who.session)) throw new Error('Tài khoản chưa được cấp quyền driver và phiên làm việc.');
      sessionStorage.setItem(M3_TOKEN_KEY, value);
      setToken(''); setIdentity(who);
      setApi(assignedApi(client, who)); setActiveClient(client); setSyncStatus('Đã kết nối');
    } catch (e) {
      if (generation.current !== attempt) return;
      if (transientM3Error(e)) {
        setError('Đang kết nối lại — mã truy cập được giữ nguyên.');
        return false;
      }
      clearPrivateState();
      setError(e instanceof Error ? e.message : 'Không thể đăng nhập. Kiểm tra kết nối và mã truy cập.');
    } finally { if (generation.current === attempt) setBusy(false); }
  }
  useEffect(() => {
    const saved = sessionStorage.getItem(M3_TOKEN_KEY);
    let stopped = !saved, running = false, expected = generation.current;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function restore() {
      if (stopped || running || generation.current !== expected) return;
      clearTimeout(timer); running = true;
      expected = generation.current + 1;
      const retry = await login(saved!);
      running = false;
      if (!stopped && generation.current === expected && retry === false) timer = setTimeout(() => void restore(), 3000);
      else stopped = true;
    }
    const wake = () => { if (document.visibilityState !== 'hidden') void restore(); };
    if (saved) void restore();
    window.addEventListener('online', wake); window.addEventListener('saferoute:resume', wake); document.addEventListener('visibilitychange', wake);
    return () => { stopped = true; clearTimeout(timer); generation.current++; window.removeEventListener('online', wake); window.removeEventListener('saferoute:resume', wake); document.removeEventListener('visibilitychange', wake); };
  }, []);
  useEffect(() => {
    if (!activeClient || !identity) return;
    const attempt = generation.current;
    let active = true, running = false, requested = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    const valid = () => active && generation.current === attempt;
    async function check() {
      if (!valid() || document.visibilityState === 'hidden') return;
      if (running) { requested = true; return; }
      running = true; controller = new AbortController();
      try {
        const who = await activeClient!.identity(controller.signal);
        if (!valid()) return;
        if (who.actor_id !== identity!.actor_id || who.role !== 'driver' || !validSession(who.session)) throw new Error('Invalid driver assignment');
        if (who.session.session_id !== identity!.session?.session_id || who.session.build_sha256 !== identity!.session?.build_sha256) {
          // A new namespace keeps an unresolved old-session Play isolated. Never
          // replay that command against the newly authorized session.
          setApi(assignedApi(activeClient!, who)); setIdentity(who);
          setSyncStatus('Đã nhận phiên mới từ admin');
        } else setSyncStatus('Đã kết nối');
      } catch {
        if (valid()) setSyncStatus('Đang kết nối lại — tự động kiểm tra phiên được cấp…');
      } finally {
        running = false; controller = undefined;
        if (valid()) { timer = setTimeout(() => { timer = undefined; void check(); }, requested ? 0 : 3000); requested = false; }
      }
    }
    function wake() { clearTimeout(timer); timer = undefined; void check(); }
    timer = setTimeout(() => { timer = undefined; void check(); }, 3000);
    document.addEventListener('visibilitychange', wake);
    window.addEventListener('online', wake); window.addEventListener('saferoute:resume', wake);
    return () => { active = false; clearTimeout(timer); controller?.abort(); document.removeEventListener('visibilitychange', wake); window.removeEventListener('online', wake); window.removeEventListener('saferoute:resume', wake); };
  }, [activeClient, identity]);
  function logout() { generation.current++; clearPrivateState(); setApi(undefined); setIdentity(undefined); setActiveClient(undefined); setSyncStatus(''); setToken(''); setError(''); setBusy(false); }
  function submit(event: FormEvent) {
    event.preventDefault();
    try { void login(decodeDriverGrant(token, location.origin)); }
    catch (e) { setError(e instanceof Error ? e.message : 'Mã không hợp lệ.'); }
  }
  if (api) return <div className="driver-access-workspace"><div className="driver-access-bar"><span>{identity?.actor_id}</span><span role="status">{syncStatus}</span><button onClick={() => location.reload()} title="Tải lại trang và nhận phiên mới từ admin">Làm mới</button><button onClick={logout}>Đăng xuất</button></div><App key={identity?.session?.session_id} api={api} /></div>;
  return <main className="driver-access-page"><form onSubmit={submit} className="driver-access-card">
    <p className="driver-access-brand">SafeRouteVN</p><h1>Đăng nhập Driver</h1>
    <p>Nhập mã truy cập riêng do người điều phối cấp.</p>
    <label htmlFor="driver-access-token">Mã truy cập</label>
    <input id="driver-access-token" type="password" placeholder="Dán mã SRVN2…" autoComplete="off" autoCapitalize="none" spellCheck={false} value={token} onChange={e => setToken(e.target.value)} required maxLength={4096} disabled={busy} aria-describedby={error ? 'driver-access-error' : undefined} />
    <button type="submit" disabled={busy}>{busy ? 'Đang kết nối…' : 'Đăng nhập'}</button>
    {error && <p id="driver-access-error" role="alert" className="driver-access-error">{error}</p>}
    <small>Vị trí xe hiện dùng GPS mô phỏng/replay. Ứng dụng cần kết nối mạng.</small>
  </form></main>;
}
