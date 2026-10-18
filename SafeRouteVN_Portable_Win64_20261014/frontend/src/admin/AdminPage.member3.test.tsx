import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";
import { App } from "../app/App";
import { MockDispatchApi } from "../services/api/MockDispatchApi";
import { basis, comparison } from "../integrations/member3/testFixtures";
import { phase2Capabilities } from "../integrations/member3/capabilities";
import type { DispatchSnapshot } from "../shared/types/dispatch";
import type { M3ComparisonView, M3ExecutionView, M3PendingEventsView, M3ReplayAudit } from "../integrations/member3/types";
import { acceptedView } from "../integrations/member3/acceptedTestFixture";
import { adaptAcceptedExecution } from "../integrations/member3/executionViewAdapter";
import { LABELS as L } from "./admin.labels";
import { forecastView } from "../integrations/member3/forecastTestFixture";
import { adaptJobForecast, parseJobForecast } from "../integrations/member3/forecastViewAdapter";
import userEvent from "@testing-library/user-event";
import type { M3Run } from '../integrations/member3/types';
import type { ScenarioId } from '../shared/types/scenario';

class BackendView extends MockDispatchApi {
  acceptEnabled = false;
  mutationPending = false;
  invalidWitness = false;
  eventView?: M3PendingEventsView;
  replayHistory?: M3ReplayAudit;
  triggeredEventIds: string[] = [];
  private previewSelection: string | null = null;
  constructor(private stale: boolean, private running: boolean, private metrics = false, private accepted = false, private preview = false) { super(); }
  override async selectAlternative(id: string) { this.previewSelection = id; return this.getSnapshot(); }
  private listener?: (snapshot: DispatchSnapshot) => void;
  override subscribe(listener: (snapshot: DispatchSnapshot) => void) { this.listener=listener; return () => {this.listener=undefined;}; }
  async refresh() { this.listener?.(await this.getSnapshot()); }
  override async triggerFixtureEvent(id: string) { this.triggeredEventIds.push(id); return this.getSnapshot(); }
  override async getSnapshot() {
    const s = await super.getSnapshot(); s.decisionState.sessionId = basis.session_id;
    const c = comparison() as M3ComparisonView;
    const emptyExecution = acceptedView(); emptyExecution.active_job_id = null; emptyExecution.accepted_trajectory = null;
    if (this.metrics) c.outcome!.comparison!.jobs.forEach(j => { j.metrics = { total_distance_m: 2500, total_travel_time_s: 90, total_exposure: 234, total_cost_vnd: 4000 }; });
    if (this.running) { c.status = "RUNNING"; c.outcome = null; c.jobs[0].view = null; }
    s.backend = { source: "MEMBER3_HTTP", baseUrl: "http://127.0.0.1:8000", executionMode: "SIMULATED_REPLAY", realWorldObservation: false,
      basis, executionView: emptyExecution, comparison: c, capabilities: phase2Capabilities(!this.stale), stale: this.stale,
      error: this.stale ? { code: "UNAUTHORIZED", message: "Token expired" } : undefined,
      scenarios: [{ id: "S0", orderCount: 17, vehicleCount: 4, initialTime: "2026-09-27T21:00:00+07:00", fixtureSha256: "a".repeat(64) }] };
    if (this.accepted) {
      const view = acceptedView();
      view.order_ids.push("O0", "O2"); view.delivered_prefix = ["O0"]; view.planned_served_suffix = ["O2"];
      const route = (view.accepted_trajectory!.vehicle_routes as Array<{ order_sequence: string[]; actions: Record<string, unknown>[] }>)[0];
      route.order_sequence = ["O2"];
      route.actions.push({ kind: "SERVICE", start_us: "2000000", end_us: "2000000", order_id: "O2", node_id: 2, load_after_kg: 0 });
      s.backend.executionView = view;
      Object.assign(s, adaptAcceptedExecution(view));
    }
    if (this.preview) {
      s.planState.proposedAlternatives = [adaptJobForecast(parseJobForecast(forecastView()), { basis, jobId: "job-return", profile: "SAFER", comparisonId: c.comparison_id, vehicleIds: ["V1"], deliveredPrefix: [] })!];
      s.planState.selectedAlternativeId = this.previewSelection;
      const job = s.planState.proposedAlternatives[0].nativeForecast!.jobView;
      s.backend.jobs = { [job.job_id]: this.invalidWitness ? { ...job, validation: { status: "NOT_RUN", valid: null } } : job };
      c.jobs[2] = { profile: "SAFER", job_id: job.job_id, view: job };
    }
    s.backend.capabilities!.accept = this.acceptEnabled;
    s.backend.mutationPending = this.mutationPending;
    s.backend.pendingEvents = this.eventView;
    s.backend.replayHistory = this.replayHistory;
    s.backend.capabilities!.applyEvent = !this.stale;
    return s;
  }
}

class WeatherView extends BackendView {
  activeRun=false;
  optimized=false;
  failLoad=false;
  loads:ScenarioId[]=[];
  prepared:M3Run={run_id:'run-'+'a'.repeat(32),scenario_id:'S2',status:'READY',message:'Ready',epoch:'2026-10-10T18:00:00+07:00',manifest_sha256:'a'.repeat(64),online_weather_enabled:true};
  constructor(){super(false,false);}
  async createWeatherRun(){return this.prepared;}
  async weatherRun(){return this.prepared;}
  async loadWeatherRun(){await super.loadScenario('S2');this.activeRun=true;return this.getSnapshot();}
  override async loadScenario(id:ScenarioId){
    if(this.failLoad)throw new Error('Unavailable');
    this.loads.push(id);await super.loadScenario(id);this.activeRun=false;return this.getSnapshot();
  }
  override async getSnapshot(){const s=await super.getSnapshot();
    s.backend!.run=this.activeRun?this.prepared:undefined;
    if(!this.optimized)s.backend!.comparison=undefined;
    s.backend!.scenarios=['S0','S1','S2'].map(id=>({id:id as ScenarioId,orderCount:8,vehicleCount:2,initialTime:'2026-09-27T21:00:00+07:00',fixtureSha256:'a'.repeat(64)}));
    return s;
  }
}
async function openWeather(){
  sessionStorage.clear();const api=new WeatherView();const user=userEvent.setup();
  render(<MemoryRouter initialEntries={['/admin']}><App api={api}/></MemoryRouter>);
  await user.click(await screen.findByRole('button',{name:'Fetch weather & prepare session'}));
  await user.click(await screen.findByRole('button',{name:'Open new session'}));
  await screen.findByText(/Đang chạy S2/);return {api,user};
}
it('shows the active weather context and cancels back to a normal scenario without leftover messages',async()=>{
  const {api,user}=await openWeather();
  expect(screen.queryByRole('button',{name:'Open new session'})).not.toBeInTheDocument();
  await user.click(screen.getByRole('button',{name:'Hủy phiên'}));
  expect(api.loads).toEqual(['S2']);
  expect(screen.queryByText(/Đang chạy S2/)).not.toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'Hủy phiên'})).not.toBeInTheDocument();
  await waitFor(()=>expect(screen.queryByRole('button',{name:'Open new session'})).not.toBeInTheDocument());
  expect(sessionStorage.getItem('saferoute.weather-run.intent.v1')).toBeNull();
});
it('opening Demo tools before Optimize exits weather mode and allows normal scenario selection',async()=>{
  const {api,user}=await openWeather();await user.click(screen.getByText('Demo tools'));
  expect(api.loads).toEqual(['S2']);expect(screen.queryByRole('button',{name:'Hủy phiên'})).not.toBeInTheDocument();
  await user.selectOptions(screen.getByRole('combobox',{name:'Demo scenario'}),'S1');
  expect(api.loads.at(-1)).toBe('S1');
});
it('opening Demo tools after Optimize keeps the active weather run',async()=>{
  const {api,user}=await openWeather();api.optimized=true;await act(()=>api.refresh());
  await user.click(screen.getByText('Demo tools'));expect(api.loads).toEqual([]);
  expect(screen.getByRole('button',{name:'Hủy phiên'})).toBeInTheDocument();
});
it('keeps the active run visible if returning to Demo tools fails',async()=>{
  const {api,user}=await openWeather();api.failLoad=true;
  await user.click(screen.getByRole('button',{name:'Hủy phiên'}));
  expect(screen.getByRole('button',{name:'Hủy phiên'})).toBeInTheDocument();
  expect(screen.getByText(/Đang chạy S2/)).toBeInTheDocument();
});
it("uses server Apply readiness and never exposes an urgent OFF transition", async () => {
  const api = new BackendView(false, false);
  api.eventView = { schema_version: "saferoute-m3-pending-events/1", basis, current_time: "2026-09-27T21:15:00+07:00",
    execution_mode: "SIMULATED_REPLAY", real_world_observation: false,
    events: [{ event_id: "wire-urgent", event_type: "URGENT_ORDER", timestamp: "2026-09-27T21:15:00+07:00", apply_allowed: false }] };
  const user = userEvent.setup(); render(<MemoryRouter initialEntries={["/admin"]}><App api={api} /></MemoryRouter>);
  expect(await screen.findByRole("button", { name: "Apply URGENT_ORDER" })).toBeDisabled();
  expect(screen.getByText(/Not due/)).toBeInTheDocument();
  expect(screen.queryByRole("switch", { name: L.urgentOrder })).not.toBeInTheDocument();
  api.eventView.events[0].apply_allowed = true;
  await act(()=>api.refresh());
  await user.click(screen.getByRole("button", { name: "Apply URGENT_ORDER" }));
  expect(api.triggeredEventIds).toEqual(["wire-urgent"]);
  api.eventView.events = [];
  api.replayHistory = { schema_version: "saferoute-m3-replay-history/1", session_id: basis.session_id, history: [{
    schema_version: "saferoute-m3-replay-receipt/1", mutation_id: "event-1", session_id: basis.session_id,
    operation: "apply_event", status: "APPLIED", source: "M2_PUBLIC_SDK", input_basis: basis, basis: { ...basis, head_version: "3", head_sha256: "4".repeat(64) },
    event_id: "wire-urgent", event_type: "URGENT_ORDER", event_sha256: "e".repeat(64), recorded_at: "2026-10-08T00:00:00+07:00",
    links: { state: `/api/sessions/${basis.session_id}/state`, history: `/api/sessions/${basis.session_id}/replay/history` }
  }] };
  await act(()=>api.refresh());
  expect(screen.getByRole("button", { name: "Apply URGENT_ORDER" })).toHaveTextContent("Applied");
  expect(screen.getByRole("button", { name: "Apply URGENT_ORDER" })).toBeDisabled();
});
it("does not fabricate applied events when pending metadata and truncated history are empty", async () => {
  const api = new BackendView(false, false);
  api.eventView = { schema_version: "saferoute-m3-pending-events/1", basis, current_time: "2026-09-27T21:15:00+07:00", execution_mode: "SIMULATED_REPLAY", real_world_observation: false, events: [] };
  api.replayHistory = { schema_version: "saferoute-m3-replay-history/1", session_id: basis.session_id, history: [] };
  render(<MemoryRouter initialEntries={["/admin"]}><App api={api} /></MemoryRouter>);
  expect(await screen.findByText(/No pending events/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Apply / })).not.toBeInTheDocument();
  expect(screen.queryByText("Applied")).not.toBeInTheDocument();
});
it("enables Accept only for a selected current certified job and blocks pending or invalid jobs", async () => {
  const api = new BackendView(false, false, false, true, true); api.acceptEnabled = true;
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={["/admin"]}><App api={api} /></MemoryRouter>);
  await user.click(await screen.findByRole("button", { name: "Select SAFER" }));
  expect(screen.getByRole("button", { name: "Accept selected plan" })).toBeEnabled();
  api.mutationPending = true;
  await act(()=>api.refresh());
  expect(screen.getByRole("button", { name: "Accept selected plan" })).toBeDisabled();
  api.mutationPending = false; api.invalidWitness = true;
  await act(()=>api.refresh());
  expect(screen.getByRole("button", { name: "Accept selected plan" })).toBeDisabled();
});
it("lets Admin select a certified public preview while Accept stays unsupported", async () => {
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={["/admin"]}><App api={new BackendView(false, false, false, false, true)} /></MemoryRouter>);
  const select = await screen.findByRole("button", { name: "Select SAFER" });
  expect(select).toBeEnabled(); await user.click(select);
  expect(screen.getByRole("button", { name: "Select SAFER" })).toHaveTextContent("Selected");
  expect(screen.getByRole("button", { name: "Accept selected plan" })).toBeDisabled();
  expect(screen.getByText("Xem trước phương án SAFER")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "View V1 route" })).toBeEnabled();
  expect(screen.getByRole("switch", { name: "Show V1 route" })).toHaveAttribute("aria-checked", "true");
  await user.click(screen.getByRole("button", { name: "View V1 route" }));
  expect(screen.getByRole("switch", { name: "Show V1 route" })).toHaveAttribute("aria-checked", "true");
  expect(screen.getByText("Vì sao chọn SAFER?")).toBeInTheDocument();
  expect(screen.queryByText(/0 stops.*0.0 km/)).not.toBeInTheDocument();
});
it("does not expose unaccepted certified forecasts on Driver", async () => {
  render(<MemoryRouter initialEntries={["/driver"]}><App api={new BackendView(false, false, false, false, true)} /></MemoryRouter>);
  expect(await screen.findByText("No dispatch plan has been assigned yet.")).toBeInTheDocument();
  expect(screen.queryByText(/Accepted route.*SAFER/)).not.toBeInTheDocument();
});
it("renders server lifecycle/verdict without fake metrics, routes or recommendation", async () => {
  render(<MemoryRouter initialEntries={["/admin"]}><App api={new BackendView(false, false)} /></MemoryRouter>);
  expect(await screen.findByText('Đủ 3 phương án')).toHaveClass('di-outcome-success');
  expect(document.querySelector<HTMLElement>('.admin-page')!.dataset.comparisonStatus).toBe('COMPLETED');
  expect(JSON.parse(document.querySelector<HTMLElement>(".admin-page")!.dataset.basis!)).toEqual(basis);
  expect(screen.getByRole("option", { name: /17 orders.*4 vehicles/ })).toBeInTheDocument();
  expect(screen.getAllByText("Phương án khả thi")).toHaveLength(3);
  expect(screen.queryByText("Recommended")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Select FASTEST" })).toBeDisabled();
});
it("truthfully labels a native accepted Driver route while stop operations remain deferred", async () => {
  render(<MemoryRouter initialEntries={["/driver"]}><App api={new BackendView(false, false, false, true)} /></MemoryRouter>);
  expect(await screen.findByText(/Accepted route.*SAFER/)).toBeInTheDocument();
  expect(screen.queryByText("No dispatch plan has been assigned yet.")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Confirm Pickup|Confirm Delivery/ })).not.toBeInTheDocument();
});
it("labels native accepted geometry completion unavailable on Admin", async () => {
  render(<MemoryRouter initialEntries={["/admin"]}><App api={new BackendView(false, false, false, true)} /></MemoryRouter>);
  expect(await screen.findByText("Accepted route · Completion unavailable")).toBeInTheDocument();
});
it("uses native accepted execution for Admin status and public prefix/suffix coverage", async () => {
  render(<MemoryRouter initialEntries={["/admin"]}><App api={new BackendView(false, false, false, true)} /></MemoryRouter>);
  expect(await screen.findByText(L.statusAfterOptimize)).toBeInTheDocument();
  expect(screen.queryByText(L.statusBeforeOptimize)).not.toBeInTheDocument();
  expect(screen.getByText("2 optimized")).toBeInTheDocument();
  expect(screen.queryByText("Click Optimize to generate plans")).not.toBeInTheDocument();
});
it("does not infer accepted coverage from a completed comparison", async () => {
  render(<MemoryRouter initialEntries={["/admin"]}><App api={new BackendView(false, false)} /></MemoryRouter>);
  expect(await screen.findByText(L.statusBeforeOptimize)).toBeInTheDocument();
  expect(screen.getByText("0 optimized")).toBeInTheDocument();
});
it("renders native forecast units and provenance without inventing fuel or observed KPIs", async () => {
  render(<MemoryRouter initialEntries={["/admin"]}><App api={new BackendView(false, false, true)} /></MemoryRouter>);
  expect(await screen.findAllByText("234 proxy")).toHaveLength(3);
  expect(screen.getAllByText("1.5 min")).toHaveLength(3);
  expect(screen.getAllByText("4,000 VND")).toHaveLength(3);
  expect(screen.queryByText(/MEMBER3_HTTP.*FORECAST_ONLY/)).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Backend execution metrics")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Select FASTEST" })).toBeDisabled();
});
it("keeps stale or running Optimize disabled without technical refresh controls", async () => {
  render(<MemoryRouter initialEntries={["/admin"]}><App api={new BackendView(true, true)} /></MemoryRouter>);
  expect(await screen.findByRole("button", { name: /^Optimize$/ })).toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent("UNAUTHORIZED");
  expect(screen.queryByRole("button", { name: "Refresh backend" })).not.toBeInTheDocument();
});

it("keeps backend customer and driver presentation free of demo identities",async()=>{render(<MemoryRouter initialEntries={["/admin"]}><App api={new BackendView(false,false)} /></MemoryRouter>);await screen.findByRole("combobox",{name:"Demo scenario"});expect(screen.queryAllByText(/Nguy\u1ec5n V\u0103n Minh/)).toHaveLength(0);expect(screen.queryAllByText(/Nguy\u1ec5n V\u0103n A/)).toHaveLength(0);expect(screen.queryAllByText(/0389 123 456/)).toHaveLength(0);});
