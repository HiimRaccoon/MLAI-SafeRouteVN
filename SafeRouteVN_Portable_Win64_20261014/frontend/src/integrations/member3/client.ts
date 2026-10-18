import { Member3Error } from "./errors";
import { parseComparisonReceipt, parseComparisonView, parseJobView } from "./jobViewAdapter";
import { parseJobForecast } from "./forecastViewAdapter";
import { parseAcceptanceView, parseAcceptanceAudit } from "./acceptance";
import { parsePendingEvents } from "./events";
import { parseReplayAudit, parseReplayMutation } from "./replay";
import { parsePlayback, parsePlaybackControl, parseReplayReset, validSpeed } from "./playback";
import type { StartPlaybackRequest, PlaybackSpeedRequest } from "./types";
import type { ReplayStepRequest, ApplyEventRequest } from "./types";
import type { AcceptPlanRequest, M3Session } from "./types";
import type { CompareProfilesRequest, M3ComparisonCancellation } from "./types";
import type { M3Capabilities, M3Catalog, M3Envelope, M3ExecutionView, M3LoadedSession, M3LocationsView, M3OrdersView, M3Ready, M3VehiclesView } from "./types";

export interface DriverIdentity { actor_id: string; role: string; session: M3Session | null; scope: string }
export const M3_TOKEN_KEY = "saferoute.member3.bearer";
export interface Member3ClientOptions { baseUrl?: string; token?: () => string | null; fetch?: typeof fetch; localProxy?: boolean; onUnauthorized?: () => void; readTimeoutMs?: number }

function browserToken(): string | null {
  try { return typeof window === "undefined" ? null : window.sessionStorage.getItem(M3_TOKEN_KEY); }
  catch { return null; }
}

/** Only talks to the configured M3 origin; never redirects bearer credentials or falls back to fixtures. */
export class Member3Client {
  private readonly localProxy: boolean;
  readonly baseUrl: string;
  private readonly token: () => string | null;
  private readonly fetcher: typeof fetch;
  private readonly onUnauthorized?: () => void;
  private readonly readTimeoutMs: number;
  constructor(options: Member3ClientOptions = {}) {
    const url = new URL(options.baseUrl ?? "http://127.0.0.1:8000");
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
      throw new Member3Error("INVALID_CONFIG", "M3 base URL must be an HTTP(S) server URL without credentials.");
    }
    this.baseUrl = url.href.replace(/\/$/, "");
    this.localProxy = options.localProxy === true;
    if (this.localProxy && (typeof window === 'undefined' || url.origin !== window.location.origin || url.hostname !== '127.0.0.1' || url.pathname !== '/')) {
      throw new Member3Error('INVALID_CONFIG','Local launcher proxy must use this page origin.');
    }
    this.onUnauthorized = options.onUnauthorized;
    this.readTimeoutMs = options.readTimeoutMs ?? 125000;
    this.token = options.token ?? browserToken;
    this.fetcher = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  identity(signal?: AbortSignal) { return this.request<DriverIdentity>('/api/auth/me', true, undefined, signal); }
  sessionRun(sid: string) { return this.request<import('./types').M3Run>(`/api/sessions/${encodeURIComponent(sid)}/run`); }
  ready() { return this.request<M3Ready>("/ready", false); }
  capabilities() { return this.request<M3Capabilities>("/api/runtime/capabilities"); }
  scenarios() { return this.request<M3Catalog>("/api/scenarios"); }
  scenarioTemplate() { return this.request<Record<string, unknown>>('/api/scenarios/import/template'); }
  importedScenarios() { return this.request<{imports: import('./types').M3Run[]}>('/api/scenarios/imports/list'); }
  importNativeScenario(body: {request_id:string;name:string;fixture:Record<string,unknown>}) {
    return this.request<import('./types').M3Run>('/api/scenarios/import', true, body);
  }
  async explanations(sid:string,cid:string,signal?:AbortSignal) {
    const value = await this.request<import('./types').M3Explanations>(`/api/sessions/${encodeURIComponent(sid)}/profiles/comparisons/${encodeURIComponent(cid)}/explanations`,true,undefined,signal);
    if (value.schema_version !== 'saferoute-explain/1' || value.session_id !== sid || value.comparison_id !== cid ||
        !['WAITING','PENDING','READY','FALLBACK'].includes(value.status) || !value.profiles ||
        Object.values(value.profiles).some(p=>!p || !Array.isArray(p.sentences) || p.sentences.some(s=>typeof s!=='string')))
      throw new Member3Error('INVALID_RESPONSE','Invalid explanation binding.');
    return value;
  }
  createRun(body: {request_id:string;scenario_id:string;event_after_minutes:number}) { return this.request<import('./types').M3Run>('/api/runs',true,body); }
  run(id:string) { return this.request<import('./types').M3Run>(`/api/runs/${encodeURIComponent(id)}`); }
  loadRun(id:string,request_id:string,replace_session_id?:string) { return this.request<M3LoadedSession>(`/api/runs/${encodeURIComponent(id)}/load`,true,{request_id,...(replace_session_id ? {replace_session_id} : {})}); }
  session(id: string) { return this.request<M3Session>(`/api/sessions/${encodeURIComponent(id)}`); }
  deliveryDay(sid:string,body:{request_id:string}) { return this.request<{status:string;basis:import('./types').M3Basis}>(`/api/sessions/${encodeURIComponent(sid)}/delivery-day`,true,body); }
  async world(sid: string, signal?: AbortSignal) {
    const v = await this.request<{state:M3ExecutionView;orders:M3OrdersView;vehicles:M3VehiclesView;locations:M3LocationsView;events:unknown;history:unknown;playback:unknown}>(`/api/sessions/${encodeURIComponent(sid)}/world`,true,undefined,signal);
    return {...v,events:parsePendingEvents(v.events,sid),history:parseReplayAudit(v.history,sid),playback:parsePlayback(v.playback,sid)};
  }
  async events(sid: string, signal?: AbortSignal) {
    return parsePendingEvents(await this.request(`/api/sessions/${encodeURIComponent(sid)}/events`, true, undefined, signal), sid);
  }
  async replayHistory(sid: string, signal?: AbortSignal) {
    return parseReplayAudit(await this.request(`/api/sessions/${encodeURIComponent(sid)}/replay/history`, true, undefined, signal), sid);
  }
  async step(sid: string, body: ReplayStepRequest) {
    const view = parseReplayMutation(await this.request(`/api/sessions/${encodeURIComponent(sid)}/replay/step`, true, body), sid, "advance");
    if (view.receipt.input_basis.head_version !== body.expected_revision.head_version || view.receipt.input_basis.generation !== body.expected_revision.generation) throw new Member3Error("INVALID_RESPONSE", "Replay receipt differs from requested revision.");
    return view;
  }
  async playback(sid: string, signal?: AbortSignal) {
    return parsePlayback(await this.request(`/api/sessions/${encodeURIComponent(sid)}/replay/playback`, true, undefined, signal), sid);
  }
  async startPlayback(sid: string, body: StartPlaybackRequest) {
    if (!validSpeed(body.speed)) throw new Member3Error("INVALID_SPEED", "Playback speed must be 1, 2, 4, 8, 16, 32, 64 or 128.");
    return parsePlaybackControl(await this.request(`/api/sessions/${encodeURIComponent(sid)}/replay/start`, true, body), sid, "start", body.request_id);
  }
  async setAutoDeliver(sid:string,body:{request_id:string;enabled:boolean}) {
    return parsePlaybackControl(await this.request(`/api/sessions/${encodeURIComponent(sid)}/replay/auto-deliver`,true,body),sid,'auto_deliver',body.request_id);
  }
  async setPlaybackSpeed(sid: string, body: PlaybackSpeedRequest) {
    if (!validSpeed(body.speed)) throw new Member3Error("INVALID_SPEED", "Playback speed must be 1, 2, 4, 8, 16, 32, 64 or 128.");
    return parsePlaybackControl(await this.request(`/api/sessions/${encodeURIComponent(sid)}/replay/speed`, true, body), sid, "speed", body.request_id);
  }
  async pausePlayback(sid: string, requestId: string) {
    return parsePlaybackControl(await this.request(`/api/sessions/${encodeURIComponent(sid)}/replay/playback/pause`, true, { request_id: requestId }), sid, "pause", requestId);
  }
  async resetReplay(sid: string, requestId: string) {
    return parseReplayReset(await this.request(`/api/sessions/${encodeURIComponent(sid)}/replay/reset`, true, { request_id: requestId }), sid);
  }
  async applyEvent(sid: string, eid: string, body: ApplyEventRequest) {
    const view = parseReplayMutation(await this.request(`/api/sessions/${encodeURIComponent(sid)}/events/${encodeURIComponent(eid)}/apply`, true, body), sid, "apply_event", eid);
    if (view.receipt.input_basis.head_version !== body.expected_revision.head_version || view.receipt.input_basis.generation !== body.expected_revision.generation) throw new Member3Error("INVALID_RESPONSE", "Event receipt differs from requested revision.");
    return view;
  }
  loadScenario(id: string, requestId: string, replace_session_id?:string) {
    return this.request<M3LoadedSession>(`/api/scenarios/${encodeURIComponent(id)}/load`, true, { request_id: requestId, ...(replace_session_id ? {replace_session_id} : {}) });
  }
  state(id: string, signal?: AbortSignal) { return this.request<M3ExecutionView>(`/api/sessions/${encodeURIComponent(id)}/state`, true, undefined, signal); }
  orders(id: string, signal?: AbortSignal) { return this.request<M3OrdersView>(`/api/sessions/${encodeURIComponent(id)}/orders`, true, undefined, signal); }
  vehicles(id: string, signal?: AbortSignal) { return this.request<M3VehiclesView>(`/api/sessions/${encodeURIComponent(id)}/vehicles`, true, undefined, signal); }
  locations(id: string, signal?: AbortSignal) { return this.request<M3LocationsView>(`/api/sessions/${encodeURIComponent(id)}/locations`, true, undefined, signal); }
  async compareProfiles(sid: string, body: CompareProfilesRequest, signal?: AbortSignal) {
    return parseComparisonReceipt(await this.request(`/api/sessions/${encodeURIComponent(sid)}/profiles/compare`, true, body, signal));
  }
  async comparison(sid: string, cid: string, signal?: AbortSignal) {
    const view = parseComparisonView(await this.request(`/api/sessions/${encodeURIComponent(sid)}/profiles/comparisons/${encodeURIComponent(cid)}`, true, undefined, signal));
    if (view.session_id !== sid || view.comparison_id !== cid) throw new Member3Error("INVALID_RESPONSE", "Comparison identity differs from requested resource.");
    return view;
  }
  async job(sid: string, jid: string, signal?: AbortSignal) {
    const view = parseJobView(await this.request(`/api/sessions/${encodeURIComponent(sid)}/jobs/${encodeURIComponent(jid)}`, true, undefined, signal));
    if (view.input_basis.session_id !== sid || view.job_id !== jid) throw new Member3Error("INVALID_RESPONSE", "Job identity differs from requested resource.");
    return view;
  }
  async jobForecast(sid: string, jid: string, signal?: AbortSignal) {
    const view = parseJobForecast(await this.request(`/api/sessions/${encodeURIComponent(sid)}/jobs/${encodeURIComponent(jid)}/forecast`, true, undefined, signal));
    if (view.session_id !== sid || view.job_id !== jid) throw new Member3Error("INVALID_RESPONSE", "Forecast identity differs from requested resource.");
    return view;
  }
  async cancelComparison(sid: string, cid: string, requestId: string): Promise<M3ComparisonCancellation> {
    const receipt = await this.request<M3ComparisonCancellation>(`/api/sessions/${encodeURIComponent(sid)}/profiles/comparisons/${encodeURIComponent(cid)}/cancel`, true, { request_id: requestId });
    if (receipt.schema_version !== "saferoute-m3-profile-cancellation/1" || receipt.session_id !== sid || receipt.comparison_id !== cid ||
        !["CANCEL_REQUESTED", "COMPLETED_IMMUTABLE"].includes(receipt.status) || receipt.affects_existing_jobs !== false) throw new Member3Error("INVALID_RESPONSE", "Invalid comparison cancellation receipt.");
    return receipt;
  }

  async accept(sid: string, jid: string, body: AcceptPlanRequest) {
    return parseAcceptanceView(await this.request(`/api/sessions/${encodeURIComponent(sid)}/jobs/${encodeURIComponent(jid)}/accept`, true, body), sid, jid);
  }
  async acceptances(sid: string, signal?: AbortSignal) {
    return parseAcceptanceAudit(await this.request(`/api/sessions/${encodeURIComponent(sid)}/acceptances`, true, undefined, signal), sid);
  }
  private async request<T>(path: string, authenticated = true, body?: { request_id: string }, signal?: AbortSignal): Promise<T> {
    const token = authenticated && !this.localProxy ? this.token()?.trim() : null;
    if (authenticated && !token && !this.localProxy) throw new Member3Error("AUTH_REQUIRED", "M3 bearer token required. Configure the token for this browser session.");
    const headers: Record<string, string> = { Accept: "application/json" };
    if (this.localProxy) headers['X-SafeRoute-Local'] = '1';
    if (token) headers.Authorization = `Bearer ${token}`;
    if (body) headers["Content-Type"] = "application/json";
    const timeout = AbortSignal.timeout(body ? 125000 : this.readTimeoutMs);
    const requestSignal = signal ? AbortSignal.any([signal, timeout]) : timeout;
    let response: Response;
    try {
      response = await this.fetcher(`${this.baseUrl}${path}`, {
        method: body ? "POST" : "GET", headers, body: body ? JSON.stringify(body) : undefined,
        cache: "no-store", redirect: "error", credentials: "omit", signal: requestSignal
      });
    } catch (error) {
      if (signal?.aborted) throw new DOMException("M3 read aborted", "AbortError");
      if (timeout.aborted || error instanceof DOMException && error.name === 'TimeoutError') {
        throw new Member3Error('RUNTIME_TIMEOUT', 'Máy chủ xử lý quá thời gian chờ. Kết quả thao tác chưa xác định; Refresh để đọc lại trạng thái.');
      }
      throw new Member3Error("NETWORK_ERROR", "Cannot reach M3. Check backend readiness, base URL and CORS.");
    }
    let value: M3Envelope<T>;
    try {
      value = await response.json();
      if (!value || value.schema_version !== "saferoute-m3-http-response/1" || typeof value.request_id !== "string" ||
          !["OK", "ERROR"].includes(value.status) || !Array.isArray(value.diagnostics) ||
          value.diagnostics.some((item) => !item || typeof item.code !== "string" || typeof item.message !== "string")) throw new Error();
    } catch (error) {
      if (signal?.aborted) throw new DOMException('M3 read aborted', 'AbortError');
      if (timeout.aborted || error instanceof DOMException && error.name === 'TimeoutError') throw new Member3Error('RUNTIME_TIMEOUT', 'Tải dữ liệu quá thời gian chờ. Đang thử kết nối lại.');
      if (error instanceof TypeError) throw new Member3Error('NETWORK_ERROR', 'Kết nối bị gián đoạn khi tải dữ liệu.');
      if ([502, 503, 504].includes(response.status)) throw new Member3Error('SERVICE_UNAVAILABLE', 'Máy chủ tạm thời chưa sẵn sàng.', response.status);
      if (response.status === 429) throw new Member3Error('RATE_LIMITED', 'Máy chủ đang bận. Vui lòng chờ kết nối lại.', response.status);
      throw new Member3Error("INVALID_RESPONSE", "M3 returned an invalid HTTP response envelope.", response.status);
    }
    if (!response.ok || value.status !== "OK") {
      if (response.status === 401) this.onUnauthorized?.();
      const first = value.diagnostics[0];
      throw new Member3Error(first?.code ?? "HTTP_ERROR", first?.message ?? "M3 request failed.", response.status, value.request_id, value.diagnostics);
    }
    if (value.data === null || typeof value.data !== "object" || Array.isArray(value.data)) {
      throw new Member3Error("INVALID_RESPONSE", "M3 response data is missing.", response.status, value.request_id);
    }
    return value.data;
  }
}
