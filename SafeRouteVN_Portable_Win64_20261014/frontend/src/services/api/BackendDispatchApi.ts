import { Member3Client } from "../../integrations/member3/client";
import { Member3Error } from "../../integrations/member3/errors";
import type { M3Basis, M3Catalog, M3Session, M3ComparisonView, M3JobForecast, CompareProfilesRequest, AcceptPlanRequest, M3AcceptanceReceipt } from "../../integrations/member3/types";
import { adaptJobForecast } from "../../integrations/member3/forecastViewAdapter";
import { sameBasis, expectedRevision, parseBasis, canAcceptSelectedPlan } from "../../integrations/member3/revision";
import { createPendingCommandStore, type PendingCommand, type PendingCommandStore } from "../../integrations/member3/requestId";
import { adaptScenarioCatalog, type ScenarioOption } from "../../integrations/member3/scenarioAdapter";
import { phase2Capabilities, canControlPlayback } from "../../integrations/member3/capabilities";
import { createPollCoordinator, type PollCoordinator, transientM3Error } from "../../integrations/member3/polling";
import { terminalComparison } from "../../integrations/member3/jobViewAdapter";
import { mapM3World } from "../../integrations/member3/worldState";
import { bindPendingEvents, eventType } from "../../integrations/member3/events";
import { validSpeed } from "../../integrations/member3/playback";
import type { PlaybackSpeed, M3ReplayReset, M3PlaybackControl, M3ReplayMutationView } from "../../integrations/member3/types";
import type { DispatchSnapshot } from "../../shared/types/dispatch";
import type { ScenarioId } from "../../shared/types/scenario";
import type { DispatchApi } from "./DispatchApi";

import { createSessionReference, validSession, type PointerStorage, type SessionPointer } from "../../integrations/member3/sessionReference";
function samePublicValue(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left)) return Array.isArray(right) && left.length === right.length && left.every((value, i) => samePublicValue(value, right[i]));
  if (left && right && typeof left === "object" && typeof right === "object" && !Array.isArray(right)) {
    const a = left as Record<string, unknown>, b = right as Record<string, unknown>;
    return Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(key => key in b && samePublicValue(a[key], b[key]));
  }
  return false;
}
function forecastCapabilities(fresh: boolean) { return { ...phase2Capabilities(fresh), forecastGeometry: fresh, accept: fresh, applyEvent: fresh, replay: fresh }; }
function browserStorage(): PointerStorage | undefined {
  try { return typeof window === "undefined" ? undefined : window.localStorage; } catch { return undefined; }
}
function commandTabId(): string | null | undefined {
  if (typeof window === "undefined") return undefined;
  const key = "saferoute.member3.command-tab.v1";
  try {
    const saved = window.sessionStorage.getItem(key);
    if (saved && /^[A-Za-z0-9-]{1,200}$/.test(saved)) return saved;
    const id = crypto.randomUUID(); window.sessionStorage.setItem(key, id); return id;
  } catch { return null; }
}

/** Native world authority and forecast job orchestration; never solves or accepts locally. */
export class BackendDispatchApi implements DispatchApi {
  private worldSnapshotSupported=false;
  private readonly client: Member3Client;
  private readonly assignedSession?: M3Session;
  private readonly storage?: PointerStorage;
  private readonly reference: ReturnType<typeof createSessionReference>;
  private stopReference?: () => void;
  private sessionEpoch = 0;
  private readonly listeners = new Set<(snapshot: DispatchSnapshot) => void>();
  private pointer: SessionPointer = { schemaVersion: 1 };
  private foundation?: Promise<M3Catalog>;
  private runtimeBuildSha256?: string;
  private initialRead?: Promise<DispatchSnapshot>;
  private operationQueue: Promise<void> = Promise.resolve();
  private commands: PendingCommandStore;
  private commandScope: string;
  private readonly browserCommandIdentity: boolean;
  private legacyCommands = false;
  private readonly polling: PollCoordinator;
  private snapshot?: DispatchSnapshot;
  private comparison?: M3ComparisonView;
  private readonly forecasts = new Map<string, M3JobForecast>();
  private readonly adaptedForecasts = new WeakMap<M3JobForecast, { key: string; proposal: ReturnType<typeof adaptJobForecast> }>();
  private scenarios: ScenarioOption[] = [];
  private pollWorldRead?: Promise<DispatchSnapshot>;
  private queuedOperations = 0;
  private acceptFlight?: Promise<DispatchSnapshot>;
  private applyFlight?: { eventId: string; promise: Promise<DispatchSnapshot> };
  private replayFlight?: { operation: string; speed?: PlaybackSpeed; promise: Promise<DispatchSnapshot> };
  private acceptances: M3AcceptanceReceipt[] = [];
  private runMetadata?: import('../../integrations/member3/types').M3Run;
  private readonly runLoadIntents = new Map<string, {runId:string;source:string|null}>();
  profileExplanations(sid:string,cid:string,signal?:AbortSignal) { return this.client.explanations(sid,cid,signal); }
  scenarioTemplate() { return this.client.scenarioTemplate(); }
  importedScenarios() { return this.client.importedScenarios(); }
  importNativeScenario(body:{request_id:string;name:string;fixture:Record<string,unknown>}) { return this.client.importNativeScenario(body); }

  createWeatherRun(input: {scenarioId:ScenarioId;eventAfterMinutes:number;requestId:string}) {
    return this.client.createRun({request_id:input.requestId,scenario_id:input.scenarioId,event_after_minutes:input.eventAfterMinutes});
  }
  weatherRun(id:string) { return this.client.run(id); }
  loadWeatherRun(id:string,requestId:string): Promise<DispatchSnapshot> { return this.enqueue(async () => {
    await this.ensureFoundation();
    const epoch=this.sessionEpoch;
    if (this.commands.read()) throw new Member3Error('PENDING_COMMAND','Resolve the pending operation before loading a new run.');
    const run=await this.client.run(id);
    if (epoch!==this.sessionEpoch) throw new DOMException('Old run response ignored','AbortError');
    // A load can commit before its following world read fails. Freeze the
    // predecessor with the request ID so retries (also after reload) stay identical.
    const intentKey=`saferoute.member3.run-load.v1:${this.client.baseUrl}:${encodeURIComponent(requestId)}`;
    const savedIntent=this.storage?.getItem(intentKey);
    const intent=this.runLoadIntents.get(requestId) ?? (savedIntent ? JSON.parse(savedIntent) :
      {runId:id,source:this.pointer.session?.session_id ?? null});
    if (intent?.runId!==id || !(intent.source===null || typeof intent.source==='string'))
      throw new Member3Error('IDEMPOTENCY_CONFLICT','Run load request differs from its saved intent.');
    this.runLoadIntents.set(requestId,intent);this.storage?.setItem(intentKey,JSON.stringify(intent));
    const loaded=await this.client.loadRun(id,requestId,intent.source ?? undefined);
    if (epoch!==this.sessionEpoch) throw new DOMException('Old run load response ignored','AbortError');
    if (run.status!=='READY' || loaded.schema_version!=='saferoute-m3-loaded-session/1' || !validSession(loaded.session) || loaded.session.run_id!==id || loaded.session.build_sha256!==this.runtimeBuildSha256 ||
        loaded.session.catalog_sha256!==run.manifest_sha256 || loaded.session.fixture_sha256!==run.fixture_sha256 ||
        loaded.session.scenario_id!==run.scenario_id || loaded.execution_view?.basis?.session_id!==loaded.session.session_id)
      throw new Member3Error('INVALID_RESPONSE','New run session binding differs from its manifest.');
    this.sessionEpoch++; this.pointer={schemaVersion:1,session:loaded.session}; this.runMetadata=run;
    this.comparison=undefined; this.forecasts.clear(); this.acceptances=[]; this.persist(true);
    return this.readWorld(loaded.session);
  }); }
  triggerAndReplan(id:string): Promise<DispatchSnapshot> { return this.enqueue(async () => {
    await this.performApplyEvent(id);
    return this.submitComparison();
  }); }
  replayToEvent(id:string): Promise<DispatchSnapshot> { return this.enqueue(async () => {
    const session=this.pointer.session;
    if (!session) throw new Member3Error('SESSION_UNAVAILABLE','Load a session first.');
    await this.readWorld(session);
    const event=this.snapshot?.backend?.pendingEvents?.events.find(e=>e.event_id===id);
    if (!event) throw new Member3Error('EVENT_UNAVAILABLE','Event is no longer pending.');
    return this.performReplay('replay_step',undefined,event.timestamp);
  }); }

  constructor(options: { client?: Member3Client; storage?: PointerStorage; assignedSession?: M3Session; subscribeChanges?(callback: (key: string | null) => void): () => void } = {}) {
    this.client = options.client ?? new Member3Client();
    this.assignedSession = options.assignedSession;
    this.storage = options.storage ?? browserStorage();
    this.reference = createSessionReference({ baseUrl: this.client.baseUrl, storage: this.storage, subscribeChanges: options.subscribeChanges });
    const tabId = commandTabId();
    this.browserCommandIdentity = typeof tabId === "string";
    this.commandScope = tabId ? `${this.client.baseUrl}:tab:${tabId}` : this.client.baseUrl;
    const commandStorage = tabId === null ? undefined : this.storage;
    const legacy = createPendingCommandStore(commandStorage, this.client.baseUrl);
    this.legacyCommands = Boolean(legacy.read());
    this.commands = this.legacyCommands ? legacy : createPendingCommandStore(commandStorage, this.commandScope);
    this.polling = createPollCoordinator({
      worldIntervalMs: this.assignedSession ? 1500 : undefined,
      readWorld: (sid, signal) => {
        if (this.queuedOperations || this.pointer.session?.session_id !== sid) return Promise.reject(new DOMException("Stale read", "AbortError"));
        const session = this.pointer.session;
        const read = this.readWorld(session, signal, false);
        this.pollWorldRead = read;
        void read.finally(() => { if (this.pollWorldRead === read) this.pollWorldRead = undefined; }).catch(() => {});
        return read;
      },
      readComparison: (sid, cid, signal) => this.readComparison(sid, cid, signal),
      publishWorld: snapshot => {
        const failure = this.snapshot?.backend?.error;
        const recoveredObservation = failure && transientM3Error(new Member3Error(failure.code, failure.message));
        this.publish(snapshot, Boolean(this.snapshot?.backend?.playbackConverging || recoveredObservation));
      },
      publishComparison: view => { this.bindComparison(view); if (this.snapshot) this.publish(this.snapshot); },
      onError: error => { this.markStale(error); }, now: Date.now,
      schedule: (callback, ms) => { const timer = setTimeout(callback, ms); return () => clearTimeout(timer); },
      isVisible: () => typeof document === "undefined" || document.visibilityState !== "hidden",
      subscribeVisibility: callback => {
        if (typeof document === "undefined") return () => {};
        document.addEventListener("visibilitychange", callback);
        window.addEventListener("online", callback);
        window.addEventListener("saferoute:resume", callback);
        return () => { document.removeEventListener("visibilitychange", callback); window.removeEventListener("online", callback); window.removeEventListener("saferoute:resume", callback); };
      }
    });
    this.pointer = this.reference.read() ?? { schemaVersion: 1 };
    if (this.assignedSession) this.pointer = { schemaVersion: 1, session: this.assignedSession };
  }
  subscribe(listener: (snapshot: DispatchSnapshot) => void) {
    this.listeners.add(listener);
    this.stopReference ??= this.reference.subscribe(pointer => this.referenceChanged(pointer));
    const saved = this.reference.read();
    if (saved?.session && this.pointer.session && saved.session.session_id !== this.pointer.session.session_id) this.referenceChanged(saved);
    this.resumePolling();
    return () => { this.listeners.delete(listener); if (!this.listeners.size) { this.polling.stop(); this.stopReference?.(); this.stopReference = undefined; } };
  }
  private referenceChanged(pointer: SessionPointer | null) {
    if (this.assignedSession) return;
    if (pointer?.session?.session_id === this.pointer.session?.session_id) return;
    this.sessionEpoch++; this.polling.stop();
    this.markStale(new Member3Error("SESSION_CHANGED", "Session reference changed; reading the server before enabling actions."));
    if (!pointer?.session) { this.markStale(new Member3Error("SESSION_REFERENCE_UNAVAILABLE", "Session pointer was removed or invalid.")); return; }
    this.pointer = { schemaVersion: 1, session: pointer.session };
    this.comparison = undefined; this.forecasts.clear(); this.acceptances = [];
    void this.enqueue(async () => {
      const catalog = await this.ensureFoundation();
      const replacement = this.reconcileSessionBinding(catalog);
      if (replacement) return this.performLoadScenario(replacement);
      const session = this.pointer.session!;
      if (session.build_sha256 !== this.runtimeBuildSha256 || !session.run_id && (session.catalog_sha256 !== catalog.catalog_sha256 || session.fixture_sha256 !== catalog.scenarios.find(s => s.scenario_id === session.scenario_id)?.fixture_sha256)) throw new Member3Error("SESSION_BINDING_CHANGED", "Changed pointer belongs to a different runtime/catalog.");
      const world = await this.readWorld(session, undefined, false);
      await this.readAcceptances(session.session_id);
      return this.publish(world, true);
    }).catch(() => {});
  }
  private assertCurrent(session: M3Session, epoch = this.sessionEpoch) {
    if (epoch !== this.sessionEpoch || session.session_id !== this.pointer.session?.session_id) throw new DOMException("Old session response ignored", "AbortError");
  }
  getSnapshot(): Promise<DispatchSnapshot> {
    // Share the initial read/load during React StrictMode's two mounts.
    if (!this.initialRead) this.initialRead = this.enqueue(() => this.currentWorld()).finally(() => { this.initialRead = undefined; });
    return this.initialRead;
  }
  private async currentWorld() {
    const catalog = await this.ensureFoundation();
    if (this.assignedSession) {
      if (this.assignedSession.build_sha256 !== this.runtimeBuildSha256) throw new Member3Error('SESSION_BINDING_CHANGED', 'Assigned session belongs to another runtime.');
      const pending = this.commands.read();
      if (pending && pending.sessionId !== this.assignedSession.session_id) throw new Member3Error('PENDING_COMMAND', 'Pending command belongs to another assigned session.');
      if (pending?.operation.startsWith('replay_')) return this.performReplay(pending.operation, pending.body.speed as PlaybackSpeed | undefined);
      const world = await this.readWorld(this.assignedSession, undefined, false);
      await this.readAcceptances(this.assignedSession.session_id);
      return this.publish(world, true);
    }
    const savedSession = this.pointer.session;
    const epoch = this.sessionEpoch;
    try {
      return await this.restoreCurrentWorld(catalog);
    } catch (error) {
      // A portable copy can reuse a browser origin whose saved session belongs
      // to another installation, even when both run the exact same build.
      if (!(error instanceof Member3Error) || !["RUN_NOT_FOUND", "SESSION_NOT_FOUND"].includes(error.code) ||
          !savedSession || this.pointer.session !== savedSession || epoch !== this.sessionEpoch) throw error;
      const pending = this.commands.read();
      if (pending?.sessionId && pending.sessionId !== savedSession.session_id) throw error;
      return this.performLoadScenario(this.archiveSessionReference(catalog, "SAVED_SESSION_NOT_FOUND"));
    }
  }
  private async restoreCurrentWorld(catalog: M3Catalog) {
    const replacement = this.reconcileSessionBinding(catalog);
    if (replacement) return this.performLoadScenario(replacement);
    const pending = this.commands.read();
    if (pending && ["accept", "apply_event", "replay_step", "replay_start", "replay_speed", "replay_pause", "replay_reset"].includes(pending.operation) && pending.sessionId && pending.sessionId !== this.pointer.session?.session_id) {
      // The shared pointer can change in another tab; recover this command's owned session.
      const recovered = await this.client.session(pending.sessionId);
      if (!validSession(recovered) || recovered.session_id !== pending.sessionId || recovered.build_sha256 !== pending.inputBasis?.build_sha256) {
        throw new Member3Error("INVALID_RESPONSE", "Pending command session binding is invalid.");
      }
      this.pointer = { schemaVersion: 1, session: recovered };
      this.comparison = undefined; this.forecasts.clear(); this.acceptances = [];
      this.persist(true);
    }
    await this.ensureFoundation();
    if (this.pointer.pending) return this.performLoadScenario(this.pointer.pending.scenarioId);
    if (!this.pointer.session) return this.performLoadScenario("S0");
    if (pending?.sessionId === this.pointer.session.session_id && pending.operation.startsWith("replay_")) return this.performReplay(pending.operation, pending.body.speed as PlaybackSpeed | undefined);
    await this.readWorld(this.pointer.session);
    if (pending?.sessionId === this.pointer.session.session_id) {
      if (pending.operation === "compare") return this.submitComparison();
      if (pending.operation === "cancelComparison") return this.performCancelComparison();
      if (pending.operation === "accept") return this.performAccept();
      if (pending.operation === "apply_event") return this.performApplyEvent(pending.resourceId ?? "");
      throw new Member3Error("PENDING_COMMAND", "The pending backend command cannot be reconciled by this phase.");
    }
    if (pending) throw new Member3Error("PENDING_COMMAND", "Pending command belongs to another session; restore its session before retrying.");
    await this.readAcceptances(this.pointer.session.session_id);
    if (this.pointer.comparison) this.bindComparison(await this.readComparison(this.pointer.session.session_id, this.pointer.comparison.id));
    return this.publish(this.snapshot!, true);
  }
  loadScenario(id: ScenarioId): Promise<DispatchSnapshot> { return this.enqueue(async () => {
    if (this.snapshot?.backend?.run && this.snapshot.backend.playback && !this.snapshot.backend.playback.fully_paused) {
      await this.performReplay('replay_pause');
      if (!this.snapshot?.backend?.playback?.fully_paused) throw new Member3Error('PLAYBACK_TICK_IN_PROGRESS','Replay is stopping; retry after the current tick settles.');
    }
    return this.performLoadScenario(id);
  }); }
  private enqueue(operation: () => Promise<DispatchSnapshot>) {
    this.queuedOperations++; this.polling.stop();
    const activeRead = this.pollWorldRead;
    const result = this.operationQueue.then(async () => { await activeRead?.catch(() => {}); return operation(); })
      .catch(error => { this.markStale(error); throw error; })
      .finally(() => { this.queuedOperations--; if (!this.queuedOperations) this.resumePolling(); });
    this.operationQueue = result.then(() => {}, () => {});
    return result;
  }
  private async performLoadScenario(id: ScenarioId): Promise<DispatchSnapshot> {
    const catalog = await this.ensureFoundation();
    this.reconcileSessionBinding(catalog);
    const epoch = this.sessionEpoch;
    if (this.commands.read()) throw new Member3Error("PENDING_COMMAND", "Resolve the pending backend command before changing session.");
    const scenario = catalog.scenarios.find((item) => item.scenario_id === id);
    if (!scenario) throw new Member3Error("SCENARIO_UNAVAILABLE", "Scenario is not available in M3.");
    const pending = this.pointer.pending?.scenarioId === id ? this.pointer.pending : { scenarioId: id, requestId: `m4-load-${crypto.randomUUID()}`, replaceSessionId:this.pointer.session?.session_id };
    this.pointer.pending = pending;
    this.persist(); // Persist request identity before POST; ambiguous failures retry the same ID.
    let loaded;
    try { loaded = await this.client.loadScenario(id, pending.requestId, pending.replaceSessionId); }
    catch (error) {
      if (epoch === this.sessionEpoch && error instanceof Member3Error && error.status===409 && error.code==='ASSIGNMENT_CONFLICT') {
        delete this.pointer.pending;this.persist();
      }
      throw error;
    }
    if (epoch !== this.sessionEpoch) throw new DOMException("Old load response ignored", "AbortError");
    if (loaded.schema_version !== "saferoute-m3-loaded-session/1" || !validSession(loaded.session) || loaded.session.scenario_id !== id ||
        loaded.session.build_sha256 !== this.runtimeBuildSha256 || loaded.execution_view?.basis?.session_id !== loaded.session.session_id) {
      throw new Member3Error("INVALID_RESPONSE", "M3 returned an invalid loaded-session binding.");
    }
    let run: typeof this.runMetadata;
    if (loaded.session.run_id) {
      run = await this.verifyDemoRun(loaded.session);
      if (epoch !== this.sessionEpoch) throw new DOMException("Old load response ignored", "AbortError");
    } else if (loaded.session.fixture_sha256 !== scenario.fixture_sha256 || loaded.session.catalog_sha256 !== catalog.catalog_sha256) {
      throw new Member3Error("INVALID_RESPONSE", "M3 returned an invalid loaded-session binding.");
    }
    this.pointer = { schemaVersion: 1, session: loaded.session };
    this.runMetadata = run;
    this.comparison = undefined;
    this.forecasts.clear();
    this.acceptances = [];
    this.persist();
    const world = await this.readWorld(loaded.session, undefined, false); // Load receipt is historical; re-read current state.
    await this.readAcceptances(loaded.session.session_id);
    return this.publish(world, true);
  }
  private async verifyDemoRun(session: M3Session) {
    const run = await this.client.run(session.run_id!);
    if (session.scenario_id !== "S4" || run.source_kind !== "IMPORTED_SCENARIO" || run.status !== "READY" ||
        run.run_id !== session.run_id || run.scenario_id !== session.scenario_id ||
        run.manifest_sha256 !== session.catalog_sha256 || run.fixture_sha256 !== session.fixture_sha256 ||
        session.build_sha256 !== this.runtimeBuildSha256) {
      throw new Member3Error("INVALID_RESPONSE", "Demo session differs from its verified source manifest.");
    }
    return run;
  }
  private ensureFoundation(): Promise<M3Catalog> {
    if (!this.foundation) this.foundation = (async () => {
      const [ready, capabilities, catalog] = await Promise.all([
        this.client.ready(), this.client.capabilities(), this.client.scenarios()
      ]);
      if (ready.ready !== true) throw new Member3Error("BACKEND_NOT_READY", "M3 is not ready.");
      this.worldSnapshotSupported=(capabilities as typeof capabilities & {m3_world_snapshot?:boolean}).m3_world_snapshot===true;
      if (capabilities.schema_version !== "task02-m2-runtime-capabilities/1" || catalog.schema_version !== "saferoute-m3-scenario-catalog/1" ||
          !/^[a-f0-9]{64}$/.test(capabilities.build_sha256) || !Array.isArray(catalog.scenarios) ||
          catalog.scenarios.some((item) => !item || !/^S[0-8]$/.test(item.scenario_id) || !/^[a-f0-9]{64}$/.test(item.fixture_sha256)) ||
          !/^[a-f0-9]{64}$/.test(catalog.catalog_sha256) || catalog.execution_mode !== "SIMULATED_REPLAY" || catalog.real_world_observation !== false) {
        throw new Member3Error("INVALID_RESPONSE", "M3 runtime/catalog contract is invalid.");
      }
      this.runtimeBuildSha256 = capabilities.build_sha256;
      this.scenarios = adaptScenarioCatalog(catalog);
      return catalog;
    })().catch((error) => { this.foundation = undefined; throw error; });
    return this.foundation;
  }
  private reconcileSessionBinding(catalog: M3Catalog): ScenarioId | undefined {
    const session = this.pointer.session;
    if (session?.run_id && session.build_sha256 === this.runtimeBuildSha256) return;
    if (!session || (session.build_sha256 === this.runtimeBuildSha256 && session.catalog_sha256 === catalog.catalog_sha256 &&
        session.fixture_sha256 === catalog.scenarios.find(item => item.scenario_id === session.scenario_id)?.fixture_sha256)) return;
    return this.archiveSessionReference(catalog, "SESSION_BINDING_CHANGED");
  }
  private archiveSessionReference(catalog: M3Catalog, reason: string): ScenarioId {
    const session = this.pointer.session!;
    // Preserve the old reference and ambiguous intent for inspection/recovery on
    // its original installation. Never replay that intent against a new build.
    const pending = this.commands.read();
    if (pending && pending.sessionId !== session.session_id && pending.inputBasis?.build_sha256 !== session.build_sha256) {
      throw new Member3Error("PENDING_COMMAND", "A command from another session needs recovery before switching runtimes.");
    }
    try {
      if (!this.storage) throw new Error("Storage unavailable");
      this.storage.setItem(`${this.reference.key}:archive:${crypto.randomUUID()}`, JSON.stringify({
        schemaVersion: 1, reason, archivedAt: new Date().toISOString(),
        pointer: this.pointer, pendingCommand: pending, commandScope: this.commandScope
      }));
      if (pending) this.commands.complete(pending.requestId);
      this.reference.write({ schemaVersion: 1 });
    } catch {
      throw new Member3Error("COMMAND_STORAGE_UNAVAILABLE", "Cannot preserve the previous session before switching runtimes. Free browser storage and reload.");
    }
    this.sessionEpoch++; this.polling.stop();
    this.pointer = { schemaVersion: 1 };
    this.comparison = undefined; this.forecasts.clear(); this.acceptances = [];
    this.runMetadata = undefined;
    this.snapshot = undefined;
    return catalog.scenarios.some(item => item.scenario_id === session.scenario_id) ? session.scenario_id as ScenarioId : "S0";
  }
  private async readWorld(session: M3Session, signal?: AbortSignal, publish = true): Promise<DispatchSnapshot> {
    const epoch = this.sessionEpoch;
    if (this.assignedSession) {
      await this.ensureFoundation();
      this.assertCurrent(session, epoch);
      if (session.build_sha256 !== this.runtimeBuildSha256) throw new Member3Error('SESSION_BINDING_CHANGED', 'Assigned session belongs to another runtime.');
    }
    if (session.run_id && this.runMetadata?.run_id !== session.run_id) {
      const run=await (this.assignedSession ? this.client.sessionRun(session.session_id) : this.client.run(session.run_id));
      this.assertCurrent(session,epoch);
      if (run.status!=='READY' || run.manifest_sha256!==session.catalog_sha256 || run.fixture_sha256!==session.fixture_sha256)
        throw new Member3Error('SESSION_BINDING_CHANGED','Run metadata no longer matches this session.');
      this.runMetadata=run;
    }
    for (let attempt = 0; attempt < 3; attempt++) {
      signal?.throwIfAborted();
      const world = this.worldSnapshotSupported ? await this.client.world(session.session_id,signal) : undefined;
      const state = world?.state ?? await this.client.state(session.session_id, signal);
      this.assertCurrent(session, epoch);
      const orders = world?.orders ?? await this.client.orders(session.session_id, signal);
      const vehicles = world?.vehicles ?? await this.client.vehicles(session.session_id, signal);
      const locations = world?.locations ?? await this.client.locations(session.session_id, signal);
      const events = world?.events ?? await this.client.events(session.session_id, signal);
      const history = world?.history ?? await this.client.replayHistory(session.session_id, signal);
      const playback = world?.playback ?? await this.client.playback(session.session_id, signal);
      this.assertCurrent(session, epoch);
      try {
        const snapshot = mapM3World(session, state, orders, vehicles, locations, this.client.baseUrl);
        if (session.run_id) snapshot.backend!.run=this.runMetadata;
        bindPendingEvents(events, state);
        if (!sameBasis(playback.execution_view.basis, state.basis) || playback.execution_view.current_time !== state.current_time) throw new Member3Error("STATE_CHANGED", "Playback world changed during read.");
        snapshot.backend!.playback = playback;
        snapshot.backend!.pendingEvents = events;
        if (history.history.some(r => r.input_basis.build_sha256 !== session.build_sha256)) throw new Member3Error("INVALID_RESPONSE", "Replay history belongs to another build.");
        if (history.history.some(r => BigInt(r.input_basis.head_version) > BigInt(state.basis.head_version) || BigInt(r.input_basis.generation) > BigInt(state.basis.generation) ||
          r.basis.session_id === session.session_id && (BigInt(r.basis.head_version) > BigInt(state.basis.head_version) || BigInt(r.basis.generation) > BigInt(state.basis.generation)))) throw new Member3Error("STATE_CHANGED", "Replay history is newer than this world read.");
        snapshot.backend!.replayHistory = history;
        snapshot.backend!.needsReoptimization = state.active_job_id === null && state.observed_metrics !== null;
        signal?.throwIfAborted();
        return publish ? this.publish(snapshot, true) : snapshot;
      } catch (error) {
        if (!(error instanceof Member3Error) || error.code !== "STATE_CHANGED" || attempt === 2) throw error;
      }
    }
    throw new Member3Error("STATE_CHANGED", "M3 world changed repeatedly during the read.");
  }
  private persist(required = false) {
    try {
      if (required && !this.storage) throw new Error("Storage required");
      if (this.storage) this.reference.write(this.pointer);
    } catch {
      if (required) throw new Member3Error("COMMAND_STORAGE_UNAVAILABLE", "Cannot persist the comparison reference; the original command remains available for recovery.");
      // Phase 1 read/load pointers keep their existing optional persistence behavior.
    }
  }
  private unsupported(): Promise<DispatchSnapshot> {
    return Promise.reject(new Member3Error("PHASE_NOT_SUPPORTED", "This action is not connected in backend Phase 2."));
  }
  private completeCommand(requestId: string) {
    this.commands.complete(requestId);
    if (this.legacyCommands && !this.commands.read()) {
      this.legacyCommands = false;
      this.commands = createPendingCommandStore(this.storage, this.commandScope);
    }
  }
  private beginCommand(intent: Omit<PendingCommand, "requestId">): PendingCommand {
    if (!this.commands.read() && this.browserCommandIdentity) {
      try {
        // sessionStorage is copied into opener-created tabs. Allocate a new namespace
        // for each new intent; retain the saved namespace only when replaying it.
        const activeId = window.sessionStorage.getItem("saferoute.member3.command-tab.v1");
        const active = activeId ? createPendingCommandStore(this.storage, `${this.client.baseUrl}:tab:${activeId}`) : undefined;
        if (active?.read()) this.commands = active;
        else {
          const id = crypto.randomUUID();
          window.sessionStorage.setItem("saferoute.member3.command-tab.v1", id);
          this.commandScope = `${this.client.baseUrl}:tab:${id}`;
          this.commands = createPendingCommandStore(this.storage, this.commandScope);
        }
      } catch { throw new Member3Error("COMMAND_STORAGE_UNAVAILABLE", "Cannot persist the command recovery identity."); }
    }
    return this.commands.begin(intent);
  }
  optimize() { return this.enqueue(async () => {
    if (!this.pointer.session) await this.currentWorld();
    await this.readWorld(this.pointer.session!);
    if (this.comparison && !terminalComparison(this.comparison) && !this.commands.read()) throw new Member3Error("COMPARISON_IN_PROGRESS", "A comparison is already running; refresh or cancel it.");
    return this.submitComparison();
  }); }
  private async submitComparison(): Promise<DispatchSnapshot> {
    const epoch = this.sessionEpoch;
    const session = this.pointer.session!, basis = this.snapshot!.backend!.basis;
    const existing = this.commands.read();
    if (existing && (existing.operation !== "compare" || existing.sessionId !== session.session_id)) throw new Member3Error("PENDING_COMMAND", "Another backend command needs reconciliation.");
    const intent = existing ? { operation: existing.operation, sessionId: existing.sessionId, body: existing.body, inputBasis: existing.inputBasis } :
      { operation: "compare", sessionId: session.session_id, body: { expected_revision: expectedRevision(basis) }, inputBasis: basis };
    const pending = this.beginCommand(intent);
    if (!pending.inputBasis || parseBasis(pending.inputBasis).session_id !== session.session_id ||
        JSON.stringify(pending.body) !== JSON.stringify({ expected_revision: expectedRevision(pending.inputBasis) })) throw new Member3Error("INVALID_RESPONSE", "Stored compare intent has invalid revision/basis binding.");
    let receipt;
    try { receipt = await this.retryCommand(() => this.client.compareProfiles(session.session_id, { ...pending.body, request_id: pending.requestId } as CompareProfilesRequest)); }
    catch (error) {
      // Native ProfileService rejects STALE_HEAD before creating the batch (after looking up any historical receipt).
      if (error instanceof Member3Error && error.status === 409 && error.code === "STALE_HEAD") {
        this.completeCommand(pending.requestId); await this.readWorld(session);
      }
      throw error;
    }
    if (receipt.session_id !== session.session_id || receipt.mode !== "NEW_BATCH" || !pending.inputBasis || !sameBasis(receipt.input_basis, parseBasis(pending.inputBasis))) throw new Member3Error("INVALID_RESPONSE", "Comparison receipt differs from original intent basis.");
    if (epoch !== this.sessionEpoch || session.session_id !== this.pointer.session?.session_id) {
      // This original intent is confirmed; do not attach its historical group to the new session.
      this.completeCommand(pending.requestId); throw new DOMException("Old comparison submission ignored", "AbortError");
    }
    this.pointer.comparison = { id: receipt.comparison_id, inputBasis: receipt.input_basis }; this.persist(true);
    this.comparison = { schema_version: "saferoute-m3-profile-comparison/1", session_id: receipt.session_id, comparison_id: receipt.comparison_id, input_basis: receipt.input_basis,
      mode: receipt.mode, status: "QUEUED", jobs: receipt.profiles.map(profile => ({ profile, job_id: null, view: null })), outcome: null,
      links: receipt.links, execution_mode: "SIMULATED_REPLAY", real_world_observation: false, metric_scope: "FORECAST_ONLY", exposure_is_proxy: true };
    this.completeCommand(pending.requestId);
    return this.publish(this.snapshot!, true);
  }
  cancelComparison() { return this.enqueue(() => this.performCancelComparison()); }
  private async performCancelComparison() {
    const session = this.pointer.session, target = this.pointer.comparison;
    if (!session || !target) throw new Member3Error("NO_COMPARISON", "No comparison to cancel.");
    const intent = { sessionId: session.session_id, operation: "cancelComparison", resourceId: target.id, body: {} };
    const pending = this.beginCommand(intent);
    await this.retryCommand(() => this.client.cancelComparison(session.session_id, target.id, pending.requestId));
    this.completeCommand(pending.requestId);
    this.bindComparison(await this.readComparison(session.session_id, target.id));
    return this.publish(this.snapshot!, true);
  }
  capabilities() { return Promise.resolve(forecastCapabilities(Boolean(this.snapshot && !this.snapshot.backend?.stale && !this.commands.read() && !this.queuedOperations))); }
  private async retryCommand<T>(operation: () => Promise<T>): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      try { return await operation(); }
      catch (error) {
        if (!transientM3Error(error) || error instanceof Member3Error && error.code === "NETWORK_ERROR" || attempt >= 3) throw error;
        await new Promise(resolve => setTimeout(resolve, 1000 * 2 ** attempt));
      }
    }
  }
  private bindComparison(view: M3ComparisonView) {
    const reference = this.pointer.comparison;
    if (!reference || view.comparison_id !== reference.id || !sameBasis(view.input_basis, reference.inputBasis)) throw new Member3Error("INVALID_RESPONSE", "Comparison binding changed.");
    this.comparison = view;
  }
  private async readComparison(sid: string, cid: string, signal?: AbortSignal) {
    const epoch = this.sessionEpoch;
    const view = await this.client.comparison(sid, cid, signal);
    if (epoch !== this.sessionEpoch || sid !== this.pointer.session?.session_id) throw new DOMException("Old comparison ignored", "AbortError");
    const reference = this.pointer.comparison;
    if (!reference || view.comparison_id !== reference.id || !sameBasis(view.input_basis, reference.inputBasis)) throw new Member3Error("INVALID_RESPONSE", "Comparison binding changed.");
    // Show certified metrics immediately, then make each route preview available
    // as it arrives. A slow third forecast must not hide the first two.
    this.bindComparison(view);
    if (this.snapshot) this.publish(this.snapshot);
    // The SDK serializes bridge access. Bound this lane to one forecast read so
    // three cold forecasts do not consume each other's server read deadlines.
    for (const row of view.jobs.filter(row => row.job_id && row.view?.plan_available && !this.forecasts.has(row.job_id))) {
      signal?.throwIfAborted();
      if (epoch !== this.sessionEpoch || sid !== this.pointer.session?.session_id) throw new DOMException("Old forecasts ignored", "AbortError");
      const forecast = await this.client.jobForecast(sid, row.job_id!, signal);
      if (forecast.profile !== row.profile || !sameBasis(forecast.input_basis, row.view!.input_basis) || !samePublicValue(forecast.job_view, row.view)) {
        throw new Member3Error("INVALID_RESPONSE", "Forecast differs from certified comparison job.");
      }
      signal?.throwIfAborted();
      if (epoch !== this.sessionEpoch || sid !== this.pointer.session?.session_id) throw new DOMException("Old forecasts ignored", "AbortError");
      this.forecasts.set(forecast.job_id, forecast);
      if (this.snapshot) this.publish(this.snapshot);
    }
    signal?.throwIfAborted();
    if (epoch !== this.sessionEpoch || sid !== this.pointer.session?.session_id) throw new DOMException("Old forecasts ignored", "AbortError");
    for (const row of view.jobs) {
      const forecast = row.job_id && this.forecasts.get(row.job_id);
      if (forecast && (forecast.profile !== row.profile || !samePublicValue(forecast.job_view, row.view))) {
        throw new Member3Error("INVALID_RESPONSE", "Certified forecast job binding changed.");
      }
    }
    return view;
  }
  private publish(snapshot: DispatchSnapshot, fresh = false): DispatchSnapshot {
    if (snapshot.backend?.basis.session_id !== this.pointer.session?.session_id) throw new DOMException("Old world ignored", "AbortError");
    const oldError = fresh ? undefined : this.snapshot?.backend?.error;
    const mutationPending = Boolean(this.commands.read());
    const proposedAlternatives = oldError ? [] : (this.comparison?.jobs ?? []).flatMap(row => {
      const forecast = row.job_id && this.forecasts.get(row.job_id);
      if (!forecast || !row.view?.plan_available) return [];
      const binding = { basis: snapshot.backend!.basis, jobId: row.job_id!, profile: row.profile,
        comparisonId: this.comparison!.comparison_id, vehicleIds: snapshot.decisionState.vehicles.map(v => v.id), deliveredPrefix: snapshot.backend!.executionView.delivered_prefix };
      const key = JSON.stringify(binding), saved = this.adaptedForecasts.get(forecast);
      const proposal = saved?.key === key ? saved.proposal : adaptJobForecast(forecast, binding);
      if (saved?.key !== key) this.adaptedForecasts.set(forecast, { key, proposal });
      return proposal ? [proposal] : [];
    });
    const selectedAlternativeId = proposedAlternatives.some(p => p.id === this.pointer.previewJobId) ? this.pointer.previewJobId! : null;
    const next: DispatchSnapshot = { ...snapshot, backend: { ...snapshot.backend!, scenarios: this.scenarios, comparison: this.comparison,
      jobs: Object.fromEntries((this.comparison?.jobs ?? []).flatMap(row => row.view ? [[row.view.job_id, row.view]] : [])),
      stale: Boolean(oldError || snapshot.backend?.playbackConverging), error: oldError, mutationPending, acceptances: this.acceptances,
      capabilities: forecastCapabilities(!oldError && !snapshot.backend?.playbackConverging && !mutationPending) },
      planState: { ...snapshot.planState, proposedAlternatives, selectedAlternativeId } };
    this.snapshot = next;
    for (const listener of this.listeners) listener(next);
    return next;
  }
  private markStale(error: unknown) {
    if (!this.snapshot?.backend || error instanceof DOMException && error.name === "AbortError") return;
    const value = error instanceof Member3Error ? { code: error.code, message: error.message } : { code: "BACKEND_ERROR", message: "M3 read/action failed." };
    this.snapshot = { ...this.snapshot, planState: { ...this.snapshot.planState, proposedAlternatives: [], selectedAlternativeId: null },
      backend: { ...this.snapshot.backend, stale: true, error: value, capabilities: forecastCapabilities(false) } };
    for (const listener of this.listeners) listener(this.snapshot);
  }
  private resumePolling() {
    const session = this.pointer.session;
    if (!session || !this.listeners.size || this.queuedOperations) return;
    this.polling.watchWorld(session.session_id);
    if (this.comparison && !terminalComparison(this.comparison)) this.polling.watchComparison(session.session_id, this.comparison.comparison_id);
  }
  selectAlternative(id: string): Promise<DispatchSnapshot> {
    if (!this.snapshot || this.snapshot.backend?.stale || this.queuedOperations || this.commands.read() || !this.snapshot.planState.proposedAlternatives.some(p => p.id === id)) {
      return Promise.reject(new Member3Error("PREVIEW_UNAVAILABLE", "No current certified forecast is available for preview."));
    }
    this.pointer.previewJobId = id; this.persist();
    return Promise.resolve(this.publish(this.snapshot));
  }
  acceptSelectedPlan(): Promise<DispatchSnapshot> {
    if (this.acceptFlight) return this.acceptFlight;
    const pending = this.commands.read();
    if (!pending && (!this.snapshot || this.queuedOperations || !canAcceptSelectedPlan(this.snapshot))) {
      return Promise.reject(new Member3Error("ACCEPT_UNAVAILABLE", "Select a current certified plan from a fresh world before Accept."));
    }
    this.acceptFlight = this.enqueue(() => this.performAccept()).finally(() => { this.acceptFlight = undefined; });
    return this.acceptFlight;
  }
  private async readAcceptances(sid: string) {
    const epoch = this.sessionEpoch;
    const audit = await this.client.acceptances(sid);
    if (epoch !== this.sessionEpoch || sid !== this.pointer.session?.session_id) throw new DOMException("Old audit ignored", "AbortError");
    if (audit.acceptances.some(r => r.input_basis.build_sha256 !== this.pointer.session?.build_sha256)) throw new Member3Error("INVALID_RESPONSE", "Acceptance history belongs to another build.");
    this.acceptances = audit.acceptances;
  }
  private async performAccept(): Promise<DispatchSnapshot> {
    const session = this.pointer.session;
    if (!session) throw new Member3Error("ACCEPT_UNAVAILABLE", "No server session is loaded.");
    const existing = this.commands.read();
    if (existing && (existing.operation !== "accept" || existing.sessionId !== session.session_id)) throw new Member3Error("PENDING_COMMAND", "Another backend command needs reconciliation.");
    if (!existing) {
      await this.readWorld(session);
      if (!canAcceptSelectedPlan(this.snapshot!)) throw new Member3Error("ACCEPT_UNAVAILABLE", "Proposal is no longer current; re-optimize before Accept.");
    }
    const proposal = this.snapshot!.planState.proposedAlternatives.find(p => p.id === this.snapshot!.planState.selectedAlternativeId);
    const intent = existing ? { operation: existing.operation, sessionId: existing.sessionId, resourceId: existing.resourceId, body: existing.body, inputBasis: existing.inputBasis } :
      { operation: "accept", sessionId: session.session_id, resourceId: proposal!.origin!.kind === "MEMBER3" ? proposal!.origin!.jobId : "",
        body: { expected_revision: expectedRevision(this.snapshot!.backend!.basis) }, inputBasis: this.snapshot!.backend!.basis };
    const pending = this.beginCommand(intent);
    if (!pending.resourceId || !pending.inputBasis || parseBasis(pending.inputBasis).session_id !== session.session_id ||
        pending.inputBasis.build_sha256 !== session.build_sha256 ||
        JSON.stringify(pending.body) !== JSON.stringify({ expected_revision: expectedRevision(pending.inputBasis) })) throw new Member3Error("INVALID_RESPONSE", "Stored Accept intent has invalid revision/basis binding.");
    this.publish(this.snapshot!);
    let result;
    try { result = await this.retryCommand(() => this.client.accept(session.session_id, pending.resourceId!, { ...pending.body, request_id: pending.requestId } as AcceptPlanRequest)); }
    catch (error) {
      if (error instanceof Member3Error && error.status === 409 && ["STALE_HEAD", "JOB_STALE", "WITNESS_REQUIRED", "WITNESS_INVALID"].includes(error.code)) {
        this.completeCommand(pending.requestId);
        await this.readWorld(session); await this.readAcceptances(session.session_id);
      }
      throw error;
    }
    if (!sameBasis(result.receipt.input_basis, pending.inputBasis)) throw new Member3Error("INVALID_RESPONSE", "Accept receipt differs from original intent basis.");
    // Never publish the receipt or response view as current physical authority.
    const world = await this.readWorld(session, undefined, false);
    await this.readAcceptances(session.session_id);
    if (!this.acceptances.some(r => r.acceptance_id === result.receipt.acceptance_id)) this.acceptances = [result.receipt, ...this.acceptances];
    this.completeCommand(pending.requestId);
    return this.publish(world, true);
  }
  triggerFixtureEvent(id: string) { return this.applyEvent(id); }
  applyEvent(id: string): Promise<DispatchSnapshot> {
    if (this.applyFlight) return this.applyFlight.eventId === id ? this.applyFlight.promise : Promise.reject(new Member3Error("PENDING_COMMAND", "Another event command needs reconciliation."));
    const pending = this.commands.read();
    if (pending && (pending.operation !== "apply_event" || pending.resourceId !== id)) return Promise.reject(new Member3Error("PENDING_COMMAND", "Resolve the original command before another Apply."));
    if (!pending && (!this.snapshot || this.queuedOperations || this.snapshot.backend?.stale || !this.snapshot.backend?.capabilities?.applyEvent)) return Promise.reject(new Member3Error("EVENT_UNAVAILABLE", "Refresh the server world before Apply."));
    const promise = this.enqueue(() => this.performApplyEvent(id)).finally(() => { this.applyFlight = undefined; });
    this.applyFlight = { eventId: id, promise }; return promise;
  }
  private async performApplyEvent(id: string): Promise<DispatchSnapshot> {
    const session = this.pointer.session;
    if (!session) throw new Member3Error("EVENT_UNAVAILABLE", "No owned session is loaded.");
    const existing = this.commands.read();
    if (existing && (existing.operation !== "apply_event" || existing.resourceId !== id || existing.sessionId !== session.session_id)) throw new Member3Error("PENDING_COMMAND", "Resolve the original backend command.");
    if (!existing) await this.readWorld(session);
    const event = this.snapshot?.backend?.pendingEvents?.events.find(e => e.event_id === id);
    if (!existing && !event) throw new Member3Error(this.snapshot?.backend?.replayHistory?.history.some(r => r.operation === "apply_event" && r.event_id === id) ? "EVENT_ALREADY_APPLIED" : "EVENT_UNAVAILABLE", "Event is not pending in this server session.");
    if (!existing && !event!.apply_allowed) throw new Member3Error("EVENT_NOT_DUE", "Replay the accepted plan to the observed event barrier first.");
    const intent = existing ? { operation: existing.operation, sessionId: existing.sessionId, resourceId: existing.resourceId, body: existing.body, inputBasis: existing.inputBasis, eventType: existing.eventType } :
      { operation: "apply_event", sessionId: session.session_id, resourceId: id, body: { expected_revision: expectedRevision(this.snapshot!.backend!.basis) }, inputBasis: this.snapshot!.backend!.basis, eventType: event!.event_type };
    const pending = this.beginCommand(intent);
    if (!pending.resourceId || !pending.inputBasis || parseBasis(pending.inputBasis).session_id !== session.session_id || pending.inputBasis.build_sha256 !== session.build_sha256 ||
      !eventType(pending.eventType) || JSON.stringify(pending.body) !== JSON.stringify({ expected_revision: expectedRevision(pending.inputBasis) })) throw new Member3Error("INVALID_RESPONSE", "Stored Apply intent has invalid event/basis binding.");
    this.publish(this.snapshot!);
    let result;
    try { result = await this.retryCommand(() => this.client.applyEvent(session.session_id, pending.resourceId!, { expected_revision: expectedRevision(pending.inputBasis!), request_id: pending.requestId })); }
    catch (error) {
      if (error instanceof Member3Error && (error.status === 409 && ["STALE_HEAD", "EVENT_NOT_DUE", "EVENT_ALREADY_APPLIED", "ACCEPTED_REPLAY_REQUIRED", "EVENT_UNKNOWN", "EVENT_TYPE_UNSUPPORTED", "PLAN_NOT_ACTIVE"].includes(error.code) || error.status === 404 && error.code === "EVENT_NOT_FOUND")) {
        this.completeCommand(pending.requestId); await this.readWorld(session); await this.readAcceptances(session.session_id);
      }
      throw error;
    }
    if (!sameBasis(result.receipt.input_basis, pending.inputBasis) || result.receipt.event_type !== pending.eventType) throw new Member3Error("INVALID_RESPONSE", "Apply receipt differs from original event intent.");
    const world = await this.readWorld(session, undefined, false);
    await this.readAcceptances(session.session_id);
    if (!world.backend!.replayHistory!.history.some(r => r.mutation_id === result.receipt.mutation_id)) world.backend!.replayHistory!.history.unshift(result.receipt);
    this.completeCommand(pending.requestId);
    return this.publish(world, true);
  }
  setUrgentOrderEnabled(_enabled: boolean) { return this.unsupported(); }
  replayStep() { return this.replayCommand("replay_step"); }
  replayStart(speed: PlaybackSpeed = 1) { return this.replayCommand("replay_start", speed); }
  replaySpeed(speed: PlaybackSpeed) { return this.replayCommand("replay_speed", speed); }
  replayPause() { return this.replayCommand("replay_pause"); }
  resetSession() { return this.replayCommand("replay_reset"); }
  setAutoDeliver(enabled:boolean) { return this.enqueue(async()=>{
    const session=this.pointer.session,epoch=this.sessionEpoch;
    if(!session)throw new Member3Error('SESSION_REQUIRED','Load a scenario first.');
    if(this.commands.read())throw new Member3Error('PENDING_COMMAND','Finish the pending command first.');
    const body={request_id:crypto.randomUUID(),enabled};
    await this.retryCommand(()=>this.client.setAutoDeliver(session.session_id,body));
    if(epoch!==this.sessionEpoch)throw new DOMException('Old setting ignored','AbortError');
    return this.readWorld(session);
  }); }
  private replayCommand(operation: string, speed?: PlaybackSpeed): Promise<DispatchSnapshot> {
    if (["replay_start", "replay_speed"].includes(operation) && !validSpeed(speed)) return Promise.reject(new Member3Error("INVALID_SPEED", "Playback speed must be 1, 2, 4, 8, 16, 32, 64 or 128."));
    if (this.replayFlight) return this.replayFlight.operation === operation && this.replayFlight.speed === speed ? this.replayFlight.promise : Promise.reject(new Member3Error("PENDING_COMMAND", "Another replay command is pending."));
    const pending = this.commands.read();
    if (pending && (pending.operation !== operation || pending.body.speed !== speed)) return Promise.reject(new Member3Error("PENDING_COMMAND", "Retry the original pending command."));
    const controllerOnly = ["replay_pause", "replay_speed"].includes(operation) && this.snapshot && canControlPlayback(this.snapshot) && this.snapshot.backend!.basis.session_id === this.pointer.session?.session_id;
    if (!pending && (this.queuedOperations || !controllerOnly && (!this.snapshot?.backend || this.snapshot.backend.stale || !this.snapshot.backend.capabilities?.replay))) return Promise.reject(new Member3Error("REPLAY_UNAVAILABLE", "Refresh the owned server session before replay."));
    const promise = this.enqueue(() => this.performReplay(operation, speed)).finally(() => { this.replayFlight = undefined; });
    this.replayFlight = { operation, speed, promise }; return promise;
  }
  private async performReplay(operation: string, speed?: PlaybackSpeed, targetTime?: string): Promise<DispatchSnapshot> {
    const epoch = this.sessionEpoch;
    const session = this.pointer.session;
    if (!session) throw new Member3Error("REPLAY_UNAVAILABLE", "No owned server session is loaded.");
    const existing = this.commands.read();
    if (existing && (existing.sessionId !== session.session_id || existing.operation !== operation)) throw new Member3Error("PENDING_COMMAND", "Resolve the original intent.");
    targetTime = existing?.body.target_time as string | undefined ?? targetTime;
    const targetFields=targetTime===undefined ? {} : {target_time:targetTime};
    if (targetTime!==undefined && (operation!=='replay_step' || !Number.isFinite(Date.parse(targetTime)))) throw new Member3Error('INVALID_RESPONSE','Invalid replay target.');
    if (!existing && !["replay_pause", "replay_speed"].includes(operation)) {
      await this.readWorld(session);
      const backend = this.snapshot!.backend!, controller = backend.playback!;
      if (["replay_step", "replay_start"].includes(operation) && (!backend.executionView.active_job_id || !controller.fully_paused || backend.pendingEvents?.events.some(e => e.apply_allowed))) throw new Member3Error("REPLAY_UNAVAILABLE", "Accept a plan, settle Pause and Apply any due event before replay.");
      if (operation === "replay_reset" && !controller.fully_paused) throw new Member3Error("REPLAY_UNAVAILABLE", "Pause and wait for the reserved tick to settle before Reset.");
    }
    const basis = existing?.inputBasis ?? this.snapshot!.backend!.basis;
    const body = existing?.body ?? { ...(["replay_step", "replay_start"].includes(operation) ? { expected_revision: expectedRevision(basis) } : {}), ...(speed === undefined ? {} : { speed }), ...targetFields };
    const pending = this.beginCommand({ operation, sessionId: session.session_id, body, inputBasis: basis });
    const expectedBody = { ...(["replay_step", "replay_start"].includes(operation) ? { expected_revision: expectedRevision(parseBasis(basis)) } : {}), ...(speed === undefined ? {} : { speed }), ...targetFields };
    if (basis.session_id !== session.session_id || basis.build_sha256 !== session.build_sha256 || !samePublicValue(body, expectedBody) || speed !== undefined && !validSpeed(speed)) throw new Member3Error("INVALID_RESPONSE", "Stored replay intent is invalid.");
    if (this.snapshot) this.publish(this.snapshot);
    let result;
    try {
      result = await this.retryCommand<M3PlaybackControl | M3ReplayMutationView | M3ReplayReset>(() => {
        const requestId = pending.requestId;
        if (operation === "replay_step") return this.client.step(session.session_id, { request_id: requestId, expected_revision: expectedRevision(basis), ...targetFields });
        if (operation === "replay_start") return this.client.startPlayback(session.session_id, { request_id: requestId, expected_revision: expectedRevision(basis), speed: speed! });
        if (operation === "replay_speed") return this.client.setPlaybackSpeed(session.session_id, { request_id: requestId, speed: speed! });
        if (operation === "replay_pause") return this.client.pausePlayback(session.session_id, requestId);
        if (operation === "replay_reset") return this.client.resetReplay(session.session_id, requestId);
        throw new Member3Error("INVALID_RESPONSE", "Unknown stored replay operation.");
      });
    } catch (error) {
      if (error instanceof Member3Error && error.status === 409 && ["ASSIGNMENT_CONFLICT", "STALE_HEAD", "ACCEPTED_PLAN_REQUIRED", "ACCEPTED_TRAJECTORY_REQUIRED", "PLAN_COMPLETE", "EVENT_TRANSITION_REQUIRED", "PLAYBACK_TICK_IN_PROGRESS", "TIME_REWIND"].includes(error.code)) {
        this.completeCommand(pending.requestId); await this.readWorld(session);
      }
      throw error;
    }
    this.assertCurrent(session, epoch);
    if (result.schema_version === "saferoute-m3-playback-control/1") {
      // Acknowledged controller metadata is durable independently of later world-read convergence.
      this.completeCommand(pending.requestId);
      const previous = this.snapshot;
      if (!previous) {
        // Reload recovery has no last confirmed world to render. The command is
        // already settled; later read failures must lead to GET, never a new POST.
        const world = await this.readWorld(session, undefined, false);
        await this.readAcceptances(session.session_id);
        return this.publish(world, true);
      }
      // A speed change on a fully paused controller does not move a vehicle.
      // Preserve the already confirmed world instead of disabling Play until
      // another polling round. Running/settling or stale worlds still converge.
      const unchangedPausedWorld = operation === 'replay_speed' && !previous.backend!.stale && !previous.backend!.error &&
        previous.backend!.playback?.fully_paused && result.controller.fully_paused;
      return this.publish({ ...previous, backend: { ...previous.backend!, playbackConverging: !unchangedPausedWorld,
        playback: { ...result.controller, schema_version: "saferoute-m3-playback-controller/1", execution_view: previous.backend!.executionView } } }, true);
    }
    let destination = session;
    if (operation === "replay_reset") {
      const reset = result as M3ReplayReset;
      if (reset.session.scenario_id !== session.scenario_id) throw new Member3Error("INVALID_RESPONSE", "Reset changed scenario binding.");
      if (reset.session.fixture_sha256 !== session.fixture_sha256 || reset.session.catalog_sha256 !== session.catalog_sha256) {
        // A bundled demo release can move a legacy S4 to its versioned native
        // source on Reset. Existing versioned runs must retain their source.
        if (session.run_id || !reset.session.run_id) throw new Member3Error("INVALID_RESPONSE", "Reset changed fixture/catalog binding.");
        const run = await this.verifyDemoRun(reset.session);
        this.assertCurrent(session, epoch);
        this.runMetadata = run;
      }
      destination = reset.session;
      this.pointer = { schemaVersion: 1, session: destination }; this.comparison = undefined; this.forecasts.clear(); this.acceptances = []; this.persist(true);
      // The validated Reset receipt and new pointer are durable. A later GET
      // failure must recover by observing this destination, not replaying Reset.
      this.completeCommand(pending.requestId);
    } else if (operation === "replay_step" && result.schema_version === "saferoute-m3-replay-view/1" && !sameBasis(result.receipt.input_basis, basis)) throw new Member3Error("INVALID_RESPONSE", "Step receipt differs from original full basis.");
    const world = await this.readWorld(destination, undefined, false);
    await this.readAcceptances(destination.session_id);
    this.completeCommand(pending.requestId);
    return this.publish(world, true);
  }
  pickupOrder(_input: { vehicleId: string; orderId: string }) { return this.unsupported(); }
  deliverOrder(input: { vehicleId: string; orderId: string }) { return this.deliveryDay('deliver',{vehicle_id:input.vehicleId,order_id:input.orderId}); }
  deliveryDay(action:string,data:Record<string,unknown>={}) { return this.enqueue(async()=> {
    const session=this.pointer.session;
    if(!session) throw new Member3Error('SESSION_REQUIRED','Load a scenario first.');
    if(this.commands.read()) throw new Member3Error('PENDING_COMMAND','Finish the pending command first.');
    const epoch=this.sessionEpoch;
    const world=await this.readWorld(session);
    if(epoch!==this.sessionEpoch) throw new DOMException('Old dispatch ignored','AbortError');
    const basis=world.backend!.basis;
    const body={request_id:crypto.randomUUID(),expected_revision:expectedRevision(basis),basis,action,data};
    const receipt=await this.retryCommand(()=>this.client.deliveryDay(session.session_id,body));
    if(epoch!==this.sessionEpoch) throw new DOMException('Old dispatch ignored','AbortError');
    if(receipt.status!=='DISPATCHED'||receipt.basis.session_id!==session.session_id) throw new Member3Error('INVALID_RESPONSE','Invalid dispatch receipt');
    this.comparison=undefined;this.forecasts.clear();delete this.pointer.comparison;this.persist(true);
    const updated=await this.readWorld(session);
    if(action==='unavailable'||action==='add_order'&&data.urgent===true) return this.submitComparison();
    return updated;
  }); }
  advanceDemoClock(_minutes: number) { return this.unsupported(); }
  resetDemoSession() { return this.unsupported(); }
}
