import { NativeKpiCards } from './NativeKpiCards';
import { nativeKpis } from './nativeKpis';
import { useState, useEffect, useRef } from "react";
import React from "react";
import {
  Zap,
  RefreshCw,
  Check,
  Bell,
  ChevronDown,
  Map,
  Navigation,
  CloudRain,
  ShieldCheck,
  Clock,
  Scale,
  Activity,
  Truck,
} from "lucide-react";
import { DispatchMap } from "../shared/components/DispatchMap";
import {AdminControls} from './AdminControls';
import ScenarioTools from './ScenarioTools';
import ScenarioImportPanel from './ScenarioImportPanel';
import { OrderQueueItem } from './OrderQueueItem';
import ProfileExplanation from './SelectedProfileExplanation';
import { WorkspacePanel } from './WorkspacePanel';
import {DeliveryDayPanel,DeliveryDayComparison} from './DeliveryDayPanel';
import { confirmedEvents } from "../integrations/member3/events";
import { adminVehicleColor, createAdminMapPresentation } from "./adminMapPresentation";
import { RouteVisibilityControls } from "./RouteVisibilityControls";
import { forecastRoutes, physicalRouteKey } from "./profileExplanation";
import type { ImmutablePlanContent, PlanProfile } from "../shared/types/dispatch";
import { useDispatch, useDispatchPresentation } from "../app/DispatchContext";
import { LABELS as L } from "./admin.labels";
import { terminalComparison, comparisonOutcomeSummary } from "../integrations/member3/jobViewAdapter";
import { sameBasis, proposalCurrency, canAcceptSelectedPlan } from "../integrations/member3/revision";
import { comparisonMetrics, formatMetric } from "../integrations/member3/metricsAdapter";

const profiles: PlanProfile[] = ["FASTEST", "BALANCED", "SAFER"];

const isOffline = (plan?: ImmutablePlanContent) => plan?.provenance.source === "Member 2 offline runtime";
const exposureText = (plan: ImmutablePlanContent) => plan.metrics.exposureScore == null ? "Unavailable" : `${(plan.metrics.exposureScore / 100).toFixed(2)}${isOffline(plan) ? "" : "x"}`;

const PROFILE_DESC: Record<PlanProfile, string> = {
  FASTEST: "Minimize total travel time",
  BALANCED: "Time / risk trade-off",
  SAFER: "Minimize exposure proxy",
};

const PROFILE_ICON: Record<PlanProfile, React.ReactElement> = {
  FASTEST: <Zap size={15} strokeWidth={2} style={{ color: "var(--color-primary)" }} />,
  BALANCED: <Scale size={15} strokeWidth={2} style={{ color: "var(--color-primary)" }} />,
  SAFER: <ShieldCheck size={15} strokeWidth={2} style={{ color: "var(--color-success)" }} />,
};

/* Vehicle accent colours (match map route colours) */
const vehicleColor = adminVehicleColor;

/* Simple iOS-style toggle switch */
function ToggleSwitch({
  checked,
  onTrigger,
  id,
  label,
  disabled,
  title,
}: {
  checked: boolean;
  onTrigger: () => void;
  id: string;
  label: string;
  disabled: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={title}
      id={id}
      className={`toggle-switch ${checked ? "toggle-on" : ""}`}
      disabled={disabled}
      onClick={onTrigger}
    />
  );
}

export function AdminPage() {
  const [editingOrder,setEditingOrder]=useState('');
  const { api, snapshot, pending, error, invoke } = useDispatch();
  const { getCustomerInfo, getDriverInfo } = useDispatchPresentation();
  const [routeView, setRouteView] = useState<{ sessionId: string | null; visibility: Record<string, boolean> }>({ sessionId: null, visibility: {} });
  const [loadingScenario, setLoadingScenario] = useState<string | null>(null);

  /* Per-vehicle capacity state */
  const [capacities, setCapacities] = useState<Record<string, number>>({});
  /* Draft (string) values for each input while typing */
  const [capDrafts, setCapDrafts] = useState<Record<string, string>>({});
  /* Set to true if draft is invalid (NaN / out of range) */
  const [capInvalid, setCapInvalid] = useState<Record<string, boolean>>({});

  function handleCapChange(vid: string, raw: string) {
    setCapDrafts((prev) => ({ ...prev, [vid]: raw }));
    const n = parseInt(raw, 10);
    if (!raw || isNaN(n) || n < 1 || n > 200) {
      setCapInvalid((prev) => ({ ...prev, [vid]: true }));
    } else {
      setCapInvalid((prev) => ({ ...prev, [vid]: false }));
      setCapacities((prev) => ({ ...prev, [vid]: n }));
    }
  }

  /* Priority selector for Order Entry form */

  if (!snapshot) return <main className="page-loading" role={error ? "alert" : undefined}>{error ?? "Loading dispatch workspace…"}</main>;
  const backend = snapshot.backend;
  const comparison = backend?.comparison;
  const comparisonSummary = comparisonOutcomeSummary(comparison);
  const comparisonRunning = Boolean(comparison && !terminalComparison(comparison));
  const optimizeDisabled = pending || Boolean(backend && (backend.stale || !backend.capabilities?.optimize || comparisonRunning));
  const phaseControlsDisabled = pending || Boolean(backend);

  const selected = snapshot.planState.proposedAlternatives.find(
    (plan) => plan.id === snapshot.planState.selectedAlternativeId
  );
  const selectedPlan = selected?.content;
  const nativeSummary = snapshot.backend ? nativeKpis(snapshot, selected) : undefined;
  const acceptDisabled = pending || !canAcceptSelectedPlan(snapshot);
  const active = snapshot.planState.acceptedPlans.find(
    (plan) => plan.id === snapshot.planState.activeAcceptedPlanId
  );
  const hasAcceptedPlan = backend ? Boolean(snapshot.planState.acceptedExecution) : Boolean(active);
  const assessment = snapshot.planState.operationalPlanAssessment;
  const visibilityOverrides = routeView.sessionId === snapshot.decisionState.sessionId ? routeView.visibility : {};
  const routeVisibility = Object.fromEntries(snapshot.decisionState.vehicles.map(v => [v.id,
    visibilityOverrides[v.id] ?? Boolean(selected?.nativeForecast?.segments.some(s => s.vehicleId === v.id))]));
  const adminMap = createAdminMapPresentation(snapshot, selected, Object.keys(routeVisibility).filter((id) => routeVisibility[id]));
  const mapPlan = adminMap.source === "PROPOSED" ? selected?.content : adminMap.source === "ACCEPTED" ? active?.plan : undefined;
  const deliveredOrderIds = new Set(snapshot.decisionState.orders.filter(order => order.status === "DELIVERED").map(order => order.id));
  const ordersByVehicle = Object.fromEntries(snapshot.decisionState.vehicles.map(vehicle => {
    let ids: string[] = [];
    if (adminMap.source === "PROPOSED" && selected?.nativeForecast) {
      const route = forecastRoutes(selected).find(route => route.vehicle_id === vehicle.id);
      ids = Array.isArray(route?.order_sequence) ? route.order_sequence.map(String) : [];
    } else if (adminMap.source === "ACCEPTED" && snapshot.planState.acceptedExecution) {
      ids = snapshot.planState.acceptedExecution.vehicleOrderIds[vehicle.id] ?? [];
    } else {
      ids = mapPlan?.vehiclePlans.find(plan => plan.vehicleId === vehicle.id)?.orderedStops
        .filter(stop => stop.kind === "DELIVERY").flatMap(stop => stop.orderIds) ?? [];
    }
    return [vehicle.id, [...new Set(ids)].filter(id => !deliveredOrderIds.has(id))];
  }));
  const toggleVehicleRoute = (id: string) => setRouteView((current) => {
    const visibility = current.sessionId === snapshot.decisionState.sessionId ? current.visibility : {};
    return { sessionId: snapshot.decisionState.sessionId, visibility: { ...visibility, [id]: !routeVisibility[id] } };
  });

  const availableVehiclesCount = snapshot.decisionState.vehicles.filter(
    (v) => v.availability === "AVAILABLE"
  ).length;
  const totalVehiclesCount = snapshot.decisionState.vehicles.length;
  const currentCapacities = snapshot.decisionState.vehicles.map((vehicle) => capacities[vehicle.id] ?? vehicle.capacityKg);
  const totalCapacity = currentCapacities.reduce((sum, capacity) => sum + capacity, 0);
  const deliveryDay=backend?.executionView.delivery_day;
  const dayOrders=deliveryDay?snapshot.decisionState.orders.filter(o=>!deliveryDay.queue[o.id]&&(o.status!=='DELIVERED'||deliveryDay.confirmations?.[o.id]?.startsWith(deliveryDay.date))):snapshot.decisionState.orders;
  const totalOrdersCount = dayOrders.length;
  const optimizedOrdersCount = backend
    ? hasAcceptedPlan ? deliveryDay
      ? dayOrders.filter(o=>backend.executionView.delivered_prefix.includes(o.id)||backend.executionView.planned_served_suffix.includes(o.id)).length
      : new Set([...backend.executionView.delivered_prefix, ...backend.executionView.planned_served_suffix]).size
      : 0
    : active ? totalOrdersCount - active.plan.unserved.length : 0;
  const hasProposals = snapshot.planState.proposedAlternatives.length > 0;
  const roundEventId = snapshot.demo.roundEventId ?? snapshot.demo.availableEvents.find((event) => event.status !== "READY_TO_TRIGGER")?.id;
  const eventRoundUsed = Boolean(roundEventId);
  const eventByType = (type: "URGENT_ORDER" | "VEHICLE_UNAVAILABLE" | "LOCAL_RAIN_WHAT_IF") =>
    snapshot.demo.availableEvents.find((event) => event.type === type);
  const urgentEvent = eventByType("URGENT_ORDER");
  const urgentOn = urgentEvent?.status === "TRIGGERED";
  const urgentOrderId = snapshot.decisionState.events.find((event) => event.type === "URGENT_ORDER")?.fixtureEvent.orderPayload?.id;
  const urgentOrder = snapshot.decisionState.orders.find((order) => order.id === urgentOrderId);
  const urgentLocked = urgentOn && urgentOrder?.status !== "WAITING";
  const triggerEvent = (type: "URGENT_ORDER" | "VEHICLE_UNAVAILABLE" | "LOCAL_RAIN_WHAT_IF") => {
    const event = eventByType(type);
    if (event?.status === "READY_TO_TRIGGER" && !eventRoundUsed) {
      void invoke(() => api.triggerFixtureEvent(event.id));
    }
  };

  /* KPI deltas */
  const activeETA = active?.plan.metrics.durationMinutes;
  const selectedETA = selectedPlan?.metrics.durationMinutes;
  const etaDelta = activeETA != null && selectedETA != null ? selectedETA - activeETA : null;

  const activeOnTime = active?.plan.metrics.onTimeRate;
  const selectedOnTime = selectedPlan?.metrics.onTimeRate;
  const onTimeDelta = activeOnTime != null && selectedOnTime != null ? selectedOnTime - activeOnTime : null;

  const activeFuel = active?.plan.metrics.fuelCostVnd ?? null;
  const selectedFuel = selectedPlan?.metrics.fuelCostVnd ?? null;
  const mixedCostScopes = Boolean(active && selectedPlan && isOffline(active.plan) !== isOffline(selectedPlan));
  const fuelDelta = !mixedCostScopes && activeFuel != null && selectedFuel != null ? selectedFuel - activeFuel : null;

  const activeRisk = active?.plan.metrics.exposureScore != null ? (active.plan.metrics.exposureScore / 100).toFixed(2) : null;
  const selectedRisk = selectedPlan?.metrics.exposureScore != null ? (selectedPlan.metrics.exposureScore / 100).toFixed(2) : null;
  const riskDelta =
    active?.plan.metrics.exposureScore != null && selectedPlan?.metrics.exposureScore != null && isOffline(active.plan) === isOffline(selectedPlan)
      ? (selectedPlan.metrics.exposureScore - active.plan.metrics.exposureScore) / 100
      : null;

  return (
    <main className="admin-page" data-session-id={snapshot.decisionState.sessionId} data-dispatch-source={snapshot.backend?.source ?? "MOCK"} data-execution-mode={snapshot.backend?.executionMode} data-comparison-id={comparison?.comparison_id} data-comparison-status={comparison?.status} data-basis={snapshot.backend ? JSON.stringify(snapshot.backend.basis) : undefined} data-current-time={snapshot.backend?.executionView.current_time} data-active-job-id={snapshot.backend?.executionView.active_job_id ?? undefined} data-stale={snapshot.backend?.stale ? "true" : "false"}>
      <h1 className="sr-only">SafeRoute VN Dispatcher Workspace (Điều phối)</h1>

      {/* ── Top Navigation Bar ── */}
      <header className="app-header">
        <div className="brand-group">
          <div className="brand-icon" aria-hidden="true">
            {/* Compass / NavPin logo */}
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="12" r="10" stroke="white" strokeWidth="1.5" />
              <path
                d="M12 2v2M12 20v2M2 12h2M20 12h2"
                stroke="rgba(255,255,255,0.5)"
                strokeWidth="1.5"
                strokeLinecap="round"
              />
              <path
                d="M12 7l2.5 5-5 2L12 7z"
                fill="#60a5fa"
                stroke="white"
                strokeWidth="0.8"
                strokeLinejoin="round"
              />
              <path
                d="M12 17l-2.5-5 5-2L12 17z"
                fill="rgba(255,255,255,0.25)"
                stroke="rgba(255,255,255,0.4)"
                strokeWidth="0.8"
                strokeLinejoin="round"
              />
            </svg>
          </div>
          <div className="brand-text">
            <strong>{L.brandName}</strong>
            <small>{L.brandSlogan}</small>
          </div>
        </div>

        <div className="header-meta">
          <div className="dispatch-status-badge">
            <span className="live-dot pulse" />
            <span className="status-title">
              {hasAcceptedPlan ? L.statusAfterOptimize : L.statusBeforeOptimize}
            </span>
          </div>

          <div className="clock-display" title={snapshot.backend ? `${snapshot.backend.executionMode} · M3 session ${snapshot.decisionState.sessionId}` : "Demo time"}>
            <Clock size={13} strokeWidth={2} />
            <span>Demo time: {snapshot.demoClock.now}</span>
          </div>

          <button className="icon-badge-btn" aria-label={L.notificationsLabel} title={L.notificationsLabel}>
            <Bell size={15} strokeWidth={2} />
          </button>

          <AdminControls />
        </div>
      </header>

      {/* ── Banners ── */}
      {(assessment?.status === "NEEDS_REOPTIMIZATION" || backend?.needsReoptimization) && (
        <div className="stale-banner" role="status">
          <span className="banner-icon">⚠</span>
          <span>Current plan needs re-optimization due to order, weather or vehicle changes.</span>
        </div>
      )}
      {error && (
        <div className="error-banner" role="alert">
          <span className="banner-icon">✕</span>
          <span>{error}</span>
        </div>
      )}

      {/* ── Main 3-Column × 2-Row Workspace ── */}
      <div className="admin-layout">

        {/* ════════════════════════════════════════
            LEFT SIDEBAR
        ════════════════════════════════════════ */}
        <WorkspacePanel area="left">
        <aside className="admin-sidebar">
          {/* Scrollable content area */}
          <div className="sidebar-scroll">
          {!snapshot.backend && api.importScenario && <ScenarioTools snapshot={snapshot} api={api} invoke={invoke} selected={editingOrder} onSelect={setEditingOrder} />}
          {snapshot.backend && api.importNativeScenario && <ScenarioImportPanel />}

          {/* ① Order Entry */}
          <section className="panel card-order-entry">
            <div className="panel-title">
              <div className="title-with-badge">
                <span className="section-number">①</span>
                <h2>{L.orderEntry}</h2>
              </div>
            </div>
            <p>{deliveryDay ? "Các đơn của ngày hiện tại. Thêm đơn bất ngờ và xem queue ngày sau trong khung Ngày giao hàng." : snapshot.backend ? "Orders come from the selected server scenario. Apply its pending event to add an urgent order." : "Use Demo scenario above to import orders or place waiting delivery points on the map."}</p>

            {/* Current Queue */}
            <div className="current-queue-header">
              <h3>{L.currentQueue(dayOrders.length)}</h3>
            </div>
            <div className="order-queue">
              {dayOrders.map(order => <OrderQueueItem
                key={`${snapshot.decisionState.sessionId}:${order.id}`}
                order={order}
                info={getCustomerInfo(order.id)}
              />)}
            </div>
          </section>

          {/* ② Fleet / Preferences */}
          <section className="panel card-fleet">
            <div className="panel-title">
              <div className="title-with-badge">
                <span className="section-number">②</span>
                <h2>{L.fleetPreferences}</h2>
              </div>
              <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
                <span className="demo-data-badge">{snapshot.backend ? "Simulated replay" : "Demo data"}</span>
                <span className="status-chip">
                  {L.availableChip(availableVehiclesCount, totalVehiclesCount)}
                </span>
              </div>
            </div>

            {/* Row 1: Drivers/Vehicles + Vehicle Type */}
            <div className="fleet-dropdowns">
              <label className="fleet-field">
                <span className="fleet-label">{L.driversVehicles}</span>
                <select className="fleet-select">
                  <option>{availableVehiclesCount} / {totalVehiclesCount}</option>
                </select>
              </label>
              <label className="fleet-field">
                <span className="fleet-label">{L.vehicleType}</span>
                <select className="fleet-select">
                  <option>{L.vehicleTypeValue}</option>
                </select>
              </label>
            </div>

            {/* Row 2: Capacity per vehicle */}
            <div className="capacity-row">
              <span className="capacity-row-label">Capacity per vehicle</span>
              <div className="capacity-inputs-grid">
                {snapshot.decisionState.vehicles.map((v) => {
                  const color = vehicleColor(v.id);
                  const invalid = !!capInvalid[v.id];
                  return (
                    <div key={v.id} className="capacity-input-cell">
                      <div className="capacity-cell-label">
                        <span className="cap-dot" style={{ background: color }} />
                        <span className="cap-vid">{v.id}</span>
                      </div>
                      <div className={`capacity-input-wrap${invalid ? ' invalid' : ''}`}>
                        <input
                          type="number"
                          min={1}
                          max={200}
                          value={capDrafts[v.id] ?? String(v.capacityKg)}
                          aria-label={`Capacity ${v.id}`}
                          onChange={(e) => handleCapChange(v.id, e.target.value)}
                        />
                        <span className="capacity-kg-suffix">kg</span>
                      </div>
                    </div>
                  );
                })}
              </div>
              <div className="capacity-total">Total capacity: {totalCapacity} kg</div>
              {/* Warning chip: total order weight > total capacity OR single order > all caps */}
              {(() => {
                const totalOrderWeight = snapshot.decisionState.orders.reduce((sum, o) => sum + o.demandKg, 0);
                const minCap = Math.min(...currentCapacities);
                const heaviestOrder = Math.max(...snapshot.decisionState.orders.map(o => o.demandKg), 0);
                if (totalOrderWeight > totalCapacity) {
                  return <div className="capacity-warning-chip">⚠ Total orders ({totalOrderWeight} kg) exceed fleet capacity ({totalCapacity} kg)</div>;
                }
                if (heaviestOrder > 0 && heaviestOrder > minCap) {
                  return <div className="capacity-warning-chip">⚠ Order ({heaviestOrder} kg) exceeds smallest vehicle capacity ({minCap} kg)</div>;
                }
                return null;
              })()}
            </div>

          </section>

            {/* ③ Context & Events */}
            <section className="panel card-context">
              <div className="panel-title">
                <div className="title-with-badge">
                  <span className="section-number">③</span>
                  <h2>{L.contextEvents}</h2>
                </div>
                <span className="live-pill">
                  <span className="live-dot pulse" />
                  Demo time
                </span>
              </div>

              {/* Decision Epoch row with Demo time */}
              <div className="epoch-row">
                <div className="epoch-left">
                  <Clock size={12} strokeWidth={2} className="epoch-icon" />
                  <span className="epoch-label">Demo time:</span>
                </div>
                <span className="epoch-value live-value">{snapshot.demoClock.now}</span>
                <div className="epoch-controls" style={{ display: "flex", gap: "4px", marginLeft: "auto" }}>
                  <button
                    type="button"
                    className="epoch-advance-btn"
                    title="Advance clock by 10 minutes"
                    disabled={phaseControlsDisabled}
                    onClick={() => void invoke(() => api.advanceDemoClock(10))}
                  >
                    +10m
                  </button>
                  <button
                    type="button"
                    className="epoch-advance-btn"
                    title="Advance clock by 30 minutes"
                    disabled={phaseControlsDisabled}
                    onClick={() => void invoke(() => api.advanceDemoClock(30))}
                  >
                    +30m
                  </button>
                </div>
              </div>

              {/* Event Toggles — clean, no scenario code shown */}
              <div className="event-toggles">
                {backend && <DeliveryDayPanel />}
                {backend ? <>
                  {confirmedEvents(backend.pendingEvents, backend.replayHistory).length === 0 && <p>No pending events. History includes the last 100 receipts.</p>}
                  {confirmedEvents(backend.pendingEvents, backend.replayHistory).map(event => <div className="toggle-row" key={event.event_id}>
                    <span className="toggle-label">{event.event_type === "URGENT_ORDER" ? L.urgentOrder : event.event_type === "VEHICLE_UNAVAILABLE" ? L.driverUnavailable : L.localRain}
                      {!event.applied && !event.apply_allowed && <small style={{ display: "block" }}>{event.timestamp}</small>}
                    </span>
                    <button className="epoch-advance-btn" type="button" aria-label={`Apply ${event.event_type}`} data-event-id={event.event_id}
                      disabled={pending || backend.stale || backend.mutationPending || !backend.capabilities?.applyEvent || event.applied || !event.apply_allowed}
                      onClick={() => void invoke(() => api.triggerFixtureEvent(event.event_id))}>
                      {event.applied ? "Applied" : event.apply_allowed ? "Apply" : "Not due"}
                    </button>
                    {backend.run && !event.applied && <button className="epoch-advance-btn" type="button"
                      disabled={pending || backend.stale || backend.mutationPending || !backend.playback?.fully_paused || !backend.executionView.active_job_id}
                      onClick={()=>void invoke(()=>event.apply_allowed ? api.triggerAndReplan!(event.event_id) : api.replayToEvent!(event.event_id))}>
                      {event.apply_allowed ? 'Kích hoạt + replan' : 'Tới mốc sự kiện'}
                    </button>}
                  </div>)}
                </> : <>
                <div className="toggle-row">
                  <span className="toggle-label">{L.urgentOrder}</span>
                  <ToggleSwitch
                    id="toggle-urgent"
                    label={L.urgentOrder}
                    checked={urgentOn}
                    disabled={phaseControlsDisabled || !urgentEvent || (eventRoundUsed && roundEventId !== urgentEvent.id) || urgentLocked}
                    title={urgentLocked ? "The urgent order cannot be cancelled after pickup." : "Add or cancel the waiting urgent order"}
                    onTrigger={() => void invoke(() => api.setUrgentOrderEnabled(!urgentOn))}
                  />
                </div>
                <div className="toggle-row">
                  <span className="toggle-label">{L.driverUnavailable}</span>
                  <ToggleSwitch
                    id="toggle-driver"
                    label={L.driverUnavailable}
                    checked={eventByType("VEHICLE_UNAVAILABLE")?.status === "TRIGGERED"}
                    disabled={phaseControlsDisabled || eventRoundUsed || !eventByType("VEHICLE_UNAVAILABLE")}
                    onTrigger={() => triggerEvent("VEHICLE_UNAVAILABLE")}
                  />
                </div>
                <div className="toggle-row">
                  <span className="toggle-label">{L.localRain}</span>
                  <ToggleSwitch
                    id="toggle-rain"
                    label={L.localRain}
                    checked={eventByType("LOCAL_RAIN_WHAT_IF")?.status === "TRIGGERED"}
                    disabled={phaseControlsDisabled || eventRoundUsed || !eventByType("LOCAL_RAIN_WHAT_IF")}
                    onTrigger={() => triggerEvent("LOCAL_RAIN_WHAT_IF")}
                  />
                </div>
                </>}
              </div>

              {/* Demo tools — collapsed by default */}
              <details className="scenario-collapsible" onClickCapture={(event)=>{
                  if (backend?.run && backend.run.source_kind !== 'IMPORTED_SCENARIO' && !comparison && !pending && !(event.target as HTMLElement).closest('button')) {
                  void invoke(()=>api.loadScenario(snapshot.decisionState.scenarioId));
                }
              }}>
                <summary className="scenario-summary">
                  <span>Demo tools</span>
                  <ChevronDown size={12} strokeWidth={2} />
                </summary>
                <div className="scenario-body">
                  <select
                    className="fleet-select"
                    disabled={pending}
                      value={loadingScenario ?? (backend?.run?.source_kind === 'IMPORTED_SCENARIO' ? '__imported__' : snapshot.decisionState.scenarioId)}
                    aria-label="Demo scenario"
                    onChange={(ev) => {
                      const id = ev.target.value as "S0" | "S1" | "S2" | "S3" | "S4";
                      setLoadingScenario(id);
                      void invoke(() => api.loadScenario(id)).finally(() => setLoadingScenario(null));
                    }}
                  >
                      {backend?.run?.source_kind === 'IMPORTED_SCENARIO' && <option value="__imported__" disabled>Đang dùng: {backend.run.name}</option>}
                    {(backend ? (backend.scenarios ?? []).map(s => [s.id, `${s.id} — ${s.orderCount} orders, ${s.vehicleCount} vehicles · ${s.initialTime}`]) : Object.entries(L.scenarios)).map(([key, label]) => (
                      <option key={key} value={key}>{label}</option>
                    ))}
                  </select>
                  {loadingScenario && <p role="status">Đang tải {loadingScenario}… Bản đồ sẽ cập nhật khi dữ liệu sẵn sàng.</p>}
                  <button
                    className="text-button reset-button"
                    disabled={pending || Boolean(backend?.stale || backend?.mutationPending)}
                    onClick={() => void invoke(() => backend ? api.loadScenario("S0") : api.resetDemoSession())}
                  >
                    Reset demo to S0
                  </button>
                </div>
              </details>
            </section>
          </div>{/* end sidebar-scroll */}

          {/* ── Sticky Action Bar (horizontal) ── */}
          <div className="sidebar-cta sidebar-cta-row">
            {hasProposals ? (
              <>
                <button
                  className="cta-btn cta-reoptimize"
                  aria-label={L.reoptimize}
                  disabled={optimizeDisabled}
                  onClick={() => void invoke(() => api.optimize())}
                >
                  <RefreshCw size={14} strokeWidth={2} />
                  {L.reoptimize}
                </button>
                <button
                  className="cta-btn cta-accept"
                  aria-label={L.acceptSelectedPlan}
                  disabled={acceptDisabled}
                  onClick={() => void invoke(() => api.acceptSelectedPlan())}
                >
                  <Check size={14} strokeWidth={2} />
                  {L.acceptSelectedPlan}
                </button>
              </>
            ) : (
              <>
                <button
                  className="cta-btn cta-optimize"
                  aria-label={L.optimize}
                  disabled={optimizeDisabled}
                  onClick={() => void invoke(() => api.optimize())}
                >
                  <Zap size={14} strokeWidth={2} />
                  {L.optimize}
                </button>
                <button className="cta-btn cta-reoptimize" disabled>
                  <RefreshCw size={14} strokeWidth={2} />
                  {L.reoptimize}
                </button>
              </>
            )}
          </div>
        </aside>
        </WorkspacePanel>

        {/* ════════════════════════════════════════
            CENTER (row1+row2, col2)
        ════════════════════════════════════════ */}
        <section className="admin-center">
          {/* Center Header */}
          <div className="center-header">
            <div className="center-title-group">
              <Map size={20} strokeWidth={2} className="center-map-icon" />
              <div>
                <h2>{L.liveDispatchTitle}</h2>
                <p>{L.liveDispatchSubtitle}{mapPlan?.provenance.computedAt && (
                  <span aria-label="Source forecast time" title="Static offline forecast; separate from Demo time.">
                    {" · Source forecast: "}<time dateTime={mapPlan.provenance.computedAt}>{mapPlan.provenance.computedAt.replace("T", " ").replace(/(\+\d{2}:\d{2}|Z)$/, " $1")}</time>
                  </span>
                )}</p>
              </div>
            </div>
          </div>

          {/* 6 KPI Cards */}
          <div className="kpi-strip">
            {/* TOTAL ORDERS */}
            <div className="kpi-card">
              <span className="kpi-label">{L.kpiTotalOrders}</span>
              <div className="kpi-value-row">
                <strong>{totalOrdersCount}</strong>
                <span className={`kpi-badge ${optimizedOrdersCount ? "success" : "neutral"}`}>
                  {nativeSummary?.preview ? `${nativeSummary.covered} planned` : L.kpiOptimized(optimizedOrdersCount)}
                </span>
              </div>
            </div>

            {/* ACTIVE VEHICLES */}
            <div className="kpi-card">
              <span className="kpi-label">{L.kpiActiveVehicles}</span>
              <div className="kpi-value-row">
                <strong style={{ whiteSpace: "nowrap" }}>
                  {availableVehiclesCount}&nbsp;/&nbsp;{totalVehiclesCount}
                </strong>
                <span
                  className={`kpi-badge ${
                    availableVehiclesCount < totalVehiclesCount ? "warn" : "success"
                  }`}
                >
                  {availableVehiclesCount === totalVehiclesCount
                    ? L.kpiAllAvailable
                    : L.kpiVehicleIssue}
                </span>
              </div>
            </div>

            {backend ? <NativeKpiCards snapshot={snapshot} selected={selected} /> : <>
            {/* EST. TOTAL TRAVEL TIME */}
            <div className="kpi-card">
              <span className="kpi-label">{isOffline(active?.plan) ? "Fleet travel time" : L.kpiEstTravelTime}</span>
              <div className="kpi-value-row">
                <strong>{activeETA != null ? `${activeETA} min` : "\u2014"}</strong>
                {etaDelta != null && (
                  <span className={`kpi-diff ${etaDelta <= 0 ? "positive" : "negative"}`}>
                    {etaDelta <= 0 ? "" : "+"}
                    {etaDelta.toFixed(1)} min
                  </span>
                )}
              </div>
            </div>

            {/* ON-TIME DELIVERY RATE */}
            <div className="kpi-card">
              <span className="kpi-label">{L.kpiOnTimeRate}</span>
              <div className="kpi-value-row">
                <strong>{activeOnTime != null ? `${activeOnTime}%` : "\u2014"}</strong>
                {onTimeDelta != null && (
                  <span className={`kpi-diff ${onTimeDelta >= 0 ? "positive" : "negative"}`}>
                    {onTimeDelta >= 0 ? "+" : ""}
                    {onTimeDelta}%
                  </span>
                )}
              </div>
            </div>

            {/* FUEL COST (EST.) */}
            <div className="kpi-card">
              <span className="kpi-label">{isOffline(active?.plan) ? "Route cost (est.)" : L.kpiFuelCost}</span>
              <div className="kpi-value-row">
                <strong style={{ fontSize: "16px" }}>
                  {activeFuel != null ? `${activeFuel.toLocaleString("en-US", { maximumFractionDigits: 0 })} VND` : "\u2014"}
                </strong>
                {fuelDelta != null && (
                  <span className={`kpi-diff ${fuelDelta <= 0 ? "positive" : "negative"}`}>
                    {fuelDelta <= 0 ? "" : "+"}
                    {Math.round(Math.abs(fuelDelta) / 1000)}K
                  </span>
                )}
              </div>
            </div>

            {/* RELATIVE SAFETY EXPOSURE */}
            <div className="kpi-card">
              <span className="kpi-label">{isOffline(active?.plan) ? "Exposure proxy" : L.kpiSafetyExposure}</span>
              <div className="kpi-value-row">
                <strong style={{ fontSize: "20px" }}>
                  {activeRisk != null && active ? exposureText(active.plan) : "\u2014"}
                </strong>
                {riskDelta != null && (
                  <span className={`kpi-diff ${riskDelta <= 0 ? "positive" : "negative"}`}>
                    {riskDelta <= 0 ? "" : "+"}
                    {riskDelta.toFixed(2)}{isOffline(active?.plan) ? "" : "x"}
                  </span>
                )}
              </div>
            </div>
            </>}
          </div>

          {/* Map */}
          <DispatchMap snapshot={snapshot} proposed={selected} scene={adminMap.scene} onPickPoint={!snapshot.backend && editingOrder && !snapshot.planState.activeAcceptedPlanId ? (lat,lon)=>void invoke(()=>api.moveOrder!(editingOrder,lat,lon)) : undefined} />

          <WorkspacePanel area="comparison">
          <DeliveryDayComparison />
          {/* Before / After Event Comparison */}
          {!backend?.executionView.delivery_day && <section className="panel comparison">
            <div className="panel-title">
              <div className="title-with-badge">
                <span className="section-number">④</span>
                <h2>{L.comparisonTitle}</h2>
              </div>
              <span className="demo-data-badge">{snapshot.backend ? "Simulated replay" : L.comparisonDemoData}</span>
            </div>

            <div className="comparison-table-wrapper">
              <table className="comparison-table">
                <thead>
                  <tr>
                    <th>{L.colMetric}</th>
                    <th>{L.colBefore}</th>
                    <th>{L.colAfter}</th>
                    <th>{L.colChange}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>{isOffline(active?.plan) || isOffline(selected?.content) ? "Fleet travel time" : L.metricTravelTime}</td>
                    <td>{active ? `${active.plan.metrics.durationMinutes} min` : L.notSelected}</td>
                    <td className="after-col">
                      {selectedPlan ? `\u2192 ${selectedPlan.metrics.durationMinutes} min` : L.notSelected}
                    </td>
                    <td>
                      {selectedPlan && active ? (
                        <span className="diff-pill green">
                          {(selectedPlan.metrics.durationMinutes - active.plan.metrics.durationMinutes).toFixed(1)} min
                        </span>
                      ) : (
                        L.notSelected
                      )}
                    </td>
                  </tr>
                  <tr>
                    <td>{L.metricDistance}</td>
                    <td>{active ? `${active.plan.metrics.distanceKm.toFixed(1)} km` : L.notSelected}</td>
                    <td className="after-col">
                      {selectedPlan
                        ? `\u2192 ${selectedPlan.metrics.distanceKm.toFixed(1)} km`
                        : L.notSelected}
                    </td>
                    <td>
                      {selectedPlan && active ? (
                        <span className="diff-pill green">
                          {(selectedPlan.metrics.distanceKm - active.plan.metrics.distanceKm).toFixed(1)} km
                        </span>
                      ) : (
                        L.notSelected
                      )}
                    </td>
                  </tr>
                  <tr>
                    <td>{mixedCostScopes ? "Cost (different scopes)" : isOffline(active?.plan) || isOffline(selected?.content) ? "Route cost (est.)" : L.metricFuelCost}</td>
                    <td>
                      {active ? `${active.plan.metrics.fuelCostVnd.toLocaleString("en-US", { maximumFractionDigits: 0 })} VND` : L.notSelected}
                      {mixedCostScopes && (isOffline(active?.plan) ? " (route estimate)" : " (fuel estimate)")}
                    </td>
                    <td className="after-col">
                      {selectedPlan ? `\u2192 ${selectedPlan.metrics.fuelCostVnd.toLocaleString("en-US", { maximumFractionDigits: 0 })} VND` : L.notSelected}
                      {mixedCostScopes && (isOffline(selected?.content) ? " (route estimate)" : " (fuel estimate)")}
                    </td>
                    <td>
                      {fuelDelta != null ? (
                        <span className="diff-pill green">
                          {fuelDelta.toLocaleString("en-US", { maximumFractionDigits: 0 })} VND
                        </span>
                      ) : (
                        L.notSelected
                      )}
                    </td>
                  </tr>
                  <tr>
                    <td>{L.metricOnTimeRate}</td>
                    <td>{activeOnTime != null ? `${activeOnTime}%` : L.notSelected}</td>
                    <td className="after-col">
                      {selectedOnTime != null
                        ? `\u2192 ${selectedOnTime}%`
                        : L.notSelected}
                    </td>
                    <td>
                      {onTimeDelta != null ? (
                        <span className="diff-pill green">
                          {onTimeDelta}%
                        </span>
                      ) : (
                        L.notSelected
                      )}
                    </td>
                  </tr>
                  <tr>
                    <td>{L.metricRiskExposure}</td>
                    <td>{active ? exposureText(active.plan) : L.notSelected}</td>
                    <td className="after-col">
                      {selectedPlan
                        ? `\u2192 ${exposureText(selectedPlan)}`
                        : L.notSelected}
                    </td>
                    <td>
                      {riskDelta != null ? (
                        <span className="diff-pill green">
                          {riskDelta.toFixed(2)}{isOffline(active?.plan) ? "" : "x"}
                        </span>
                      ) : (
                        L.notSelected
                      )}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>}
          </WorkspacePanel>
        </section>


        {/* ════════════════════════════════════════
            RIGHT COLUMN — Decision Intelligence + Driver/Vehicle Status
        ════════════════════════════════════════ */}
        <WorkspacePanel area="right">
        <div className="admin-right">

          {/* ── Decision Intelligence ─────────────────────────── */}
          <section className="panel panel-di">

            {/* Header 44px */}
            <div className="di-header">
              <div className="di-header-left">
                <Activity size={18} strokeWidth={2} style={{ color: '#1F6FEB', flexShrink: 0 }} />
                <div>
                  <h2 className="di-title">{L.decisionIntelligence}</h2>
                  <p className="di-subtitle">3 alternative plans based on different optimization goals</p>
                </div>
              </div>
              <span className={`di-status-badge ${backend ? `di-outcome-${comparisonSummary.tone}` : (active || hasProposals) ? 'di-badge-optimized' : 'di-badge-idle'}`}>
                {backend ? comparisonSummary.label : (active || hasProposals) ? 'Optimized' : 'Not optimized'}
              </span>
            </div>

            {backend && <div className="di-hint-text" role="status">
              {comparison && !sameBasis(comparison.input_basis, backend.basis) &&
                !comparison.jobs.some(job => job.job_id === snapshot.planState.acceptedExecution?.jobId) &&
                <p>Dữ liệu đã thay đổi. Bấm Optimize để cập nhật phương án.</p>}
              {comparisonSummary.message && <p className="comparison-outcome-message">{comparisonSummary.message}</p>}
              {comparisonRunning && api.cancelComparison && <button type="button" className="text-button" disabled={pending || backend.stale} onClick={() => void invoke(() => api.cancelComparison!())}>Cancel comparison</button>}

            </div>}

            {/* 3 Alternative Cards */}
            {comparisonRunning && <p className="di-hint-text" role="status">Đang tối ưu · {snapshot.planState.proposedAlternatives.length}/3 tuyến đã sẵn sàng để xem trước.</p>}
            {backend && snapshot.planState.proposedAlternatives.length === 3 && <p className="di-hint-text" role="status">
              3 tiêu chí · {new Set(snapshot.planState.proposedAlternatives.map(physicalRouteKey)).size} phương án tuyến khác nhau.
            </p>}
            <div className="alt-cards-grid">
              {profiles.map((profile) => {
                const proposal = snapshot.planState.proposedAlternatives.find(
                  (item) => (item.origin?.kind === "MEMBER3" ? item.origin.profile : item.content?.profile) === profile
                );
                const isSelected = Boolean(proposal && selected && proposal.id === selected.id);
                const isBalanced = profile === "BALANCED";
                const hasData = Boolean(proposal);
                const onTimeVal = proposal?.content?.metrics.onTimeRate ?? null;
                const child = comparison?.jobs.find(row => row.profile === profile);
                const nativeMetrics = backend && !backend.stale ? proposal?.nativeForecast?.metrics ?? comparisonMetrics(comparison, profile, backend.basis) : null;

                return (
                  <article
                    key={profile}
                    className={`alt-card${isSelected ? " alt-card-selected selected" : ""}`}
                    onClick={() => !pending && !backend?.stale && proposal && proposalCurrency(proposal, snapshot) === "CURRENT" && void invoke(() => api.selectAlternative(proposal.id))}
                    style={{ cursor: proposal ? "pointer" : "default" }}
                  >
                    {/* Recommended badge (Stitch: blue, absolute above border) */}
                    {isBalanced && !backend && (
                      <em className={`recommended-badge${hasData ? "" : " dim"}`}>{L.recommendedTag}</em>
                    )}
                    {/* Check circle when selected */}
                    {isSelected && (
                      <span className="alt-check-icon"><Check size={10} strokeWidth={3} /></span>
                    )}

                    {/* Header: [Icon + Title]  |  [Description inline] */}
                    <div className="alt-card-header">
                      <div className="alt-title-group">
                        <span className="alt-icon-wrap">{PROFILE_ICON[profile]}</span>
                        <span className={`alt-title alt-title-${profile.toLowerCase()}`}>{profile}</span>
                      </div>
                      <span className="alt-desc">{PROFILE_DESC[profile]}</span>
                    </div>

                    {backend && <div className="di-hint-text" data-job-id={child?.job_id ?? undefined} data-job-status={child?.view?.job_status}>
                      <p>{child?.view?.plan_available
                        ? child.view.internal_status === "RETURN_ONLY" ? "Chỉ có tuyến trở về" : child.view.internal_status === "PARTIAL" ? "Phục vụ một phần" : "Phương án khả thi"
                        : child?.view?.job_status === "FAILED" ? "Không tạo được phương án"
                        : child?.view?.job_status === "COMPLETED" ? "Chưa có phương án hợp lệ"
                        : child?.job_id ? "Đang tính toán" : "Chưa tối ưu"}</p>
                      {child?.view?.coverage_evaluated && <p>{child.view.served_orders.length} đơn được phục vụ · {child.view.unserved_orders.length} chưa phục vụ</p>}
                    </div>}

                    {/* 2×2 Metrics grid (Stitch style) */}
                    {backend ? <>
                      <div className="alt-metrics-grid" data-metric-scope="FORECAST_ONLY">
                        <div className="alt-metric-item">Fleet travel: <strong className="alt-metric-val">{nativeMetrics ? formatMetric(nativeMetrics, "total_travel_time_s", "min") : "—"}</strong></div>
                        <div className="alt-metric-item">Route cost: <strong className="alt-metric-val">{nativeMetrics ? formatMetric(nativeMetrics, "total_cost_vnd") : "—"}</strong></div>
                        <div className="alt-metric-item">Soft lateness: <strong className="alt-metric-val">{nativeMetrics ? formatMetric(nativeMetrics, "total_soft_lateness_s", "s") : "—"}</strong></div>
                        <div className="alt-metric-item">Exposure proxy: <strong className="alt-metric-val">{nativeMetrics ? formatMetric(nativeMetrics, "total_exposure") : "—"}</strong></div>
                      </div>
                      {nativeMetrics && <p className="di-hint-text">Quãng đường: {formatMetric(nativeMetrics, "total_distance_m", "km")}</p>}
                    </> : <div className="alt-metrics-grid">
                      <div className="alt-metric-item">
                        {isOffline(proposal?.content) ? "Fleet travel:" : "ETA:"} <strong className="alt-metric-val">
                          {hasData ? `${proposal!.content!.metrics.durationMinutes} min` : "—"}
                        </strong>
                      </div>
                      <div className="alt-metric-item">
                        {isOffline(proposal?.content) ? "Route cost:" : "Fuel cost:"} <strong className="alt-metric-val">
                          {proposal ? `${Math.round(proposal.content!.metrics.fuelCostVnd / 1000)}K VND` : "—"}
                        </strong>
                      </div>
                      <div className="alt-metric-item">
                        On-time rate:{" "}
                        <strong className={`alt-metric-val${hasData && onTimeVal != null && onTimeVal >= 90 ? " m-success" : ""}`}>
                          {onTimeVal != null ? `${onTimeVal}%` : "—"}
                        </strong>
                      </div>
                      <div className="alt-metric-item">
                        Exposure proxy: <strong className="alt-metric-val">
                          {proposal ? exposureText(proposal.content!) : "—"}
                        </strong>
                      </div>
                    </div>}

                    {/* Select Alternative button */}
                    <button
                      type="button"
                      aria-label={`Select ${profile}`}
                      className={`alt-view-btn ${isSelected ? "alt-view-selected" : "alt-view-outline"}`}
                      disabled={!proposal || pending || Boolean(backend?.stale) || proposalCurrency(proposal, snapshot) === "STALE"}
                      onClick={(e) => {
                        e.stopPropagation();
                        if (proposal) void invoke(() => api.selectAlternative(proposal.id));
                      }}
                    >
                      {isSelected ? "Selected" : "View Details"}
                    </button>
                  </article>
                );
              })}
            </div>


            {/* Selected plan summary */}
            {backend && <ProfileExplanation snapshot={snapshot} api={api} />}
            <div className="plan-summary-block">
              <div className="plan-summary-header">
                <span className="plan-summary-label">SELECTED PLAN SUMMARY</span>
                {selected && (
                  <span className="plan-summary-plan-name">{selected.origin?.kind === "MEMBER3" ? selected.origin.profile : selectedPlan?.profile}</span>
                )}
              </div>
              {selected?.nativeForecast && <p className="di-hint-text">Xem trước phương án {selected.origin?.kind === "MEMBER3" ? selected.origin.profile : ""}</p>}
              {selected?.nativeForecast?.jobView.unserved_orders.map(item => <p key={item.order_id} className="di-hint-text">{item.order_id}: {item.reason}</p>)}
              {selected?.nativeForecast && snapshot.decisionState.vehicles.map(v => {
                const route = forecastRoutes(selected).find(r => r.vehicle_id === v.id);
                const orders = Array.isArray(route?.order_sequence) ? route.order_sequence.map(String) : [];
                const distance = typeof route?.total_distance_m === "number" ? `${(route.total_distance_m / 1000).toFixed(2)} km` : "—";
                const hasRoute = selected.nativeForecast!.segments.some(s => s.vehicleId === v.id);
                return <div key={v.id} className="plan-summary-row">
                  <span className="plan-summary-dot" style={{ background: vehicleColor(v.id) }} />
                  <span className="plan-summary-vid">{v.id}</span>
                  <span className="plan-summary-detail">{route ? `${orders.join(" → ") || "Return to depot"} · ${distance}` : "No assigned route"}</span>
                  <button type="button" disabled={!hasRoute || Boolean(backend?.stale)} aria-label={`View ${v.id} route`}
                    onClick={() => { setRouteView({ sessionId: snapshot.decisionState.sessionId, visibility: Object.fromEntries(snapshot.decisionState.vehicles.map(vehicle => [vehicle.id, vehicle.id === v.id])) }); document.querySelector(".admin-center")?.scrollIntoView?.({ behavior: "smooth", block: "start" }); }}>View route</button>
                </div>;
              })}
              {!backend && snapshot.decisionState.vehicles.map((v) => {
                const vPlan = selectedPlan?.vehiclePlans.find((vp) => vp.vehicleId === v.id);
                const stops = vPlan
                  ? vPlan.orderedStops.filter((s) => s.kind !== "DEPOT_PICKUP").length
                  : 0;
                const distKm = vPlan
                  ? vPlan.routeSegments.reduce((acc, s) => acc + s.distanceKm, 0)
                  : 0;
                const loadKg = snapshot.decisionState.orders
                  .filter((o) => vPlan?.orderedStops.some((s) => s.orderIds?.includes(o.id)))
                  .reduce((sum, o) => sum + o.demandKg, 0);
                const cap = capacities[v.id] ?? v.capacityKg;
                return (
                  <div key={v.id} className="plan-summary-row">
                    <span className="plan-summary-dot" style={{ background: vehicleColor(v.id) }} />
                    <span className="plan-summary-vid">{v.id}</span>
                    <span className="plan-summary-detail">
                      {selected
                        ? `${stops} stop${stops !== 1 ? "s" : ""} · ${distKm.toFixed(1)} km · ${loadKg.toFixed(1)}/${cap} kg`
                        : "—"}
                    </span>
                  </div>
                );
              })}
            </div>

            {/* Accept / hint / accepted state */}
            {hasProposals ? (
              <button
                className="accept-fullwidth"
                aria-label="Accept selected plan"
                disabled={acceptDisabled}
                onClick={() => void invoke(() => api.acceptSelectedPlan())}
              >
                <Check size={16} strokeWidth={2.5} />
                <span>{L.acceptSelectedPlan}</span>
              </button>
            ) : !hasAcceptedPlan ? (
              <p className="di-hint-text">Click Optimize to generate plans</p>
            ) : null}

            <RouteVisibilityControls vehicleIds={snapshot.decisionState.vehicles.map((v) => v.id)}
              visibility={routeVisibility} presentation={adminMap} ordersByVehicle={ordersByVehicle} onToggle={toggleVehicleRoute} />

          </section>{/* end panel-di */}

          {/* ── Driver / Vehicle Status ─────────────────────── */}
          <section className="panel panel-fleet-status">

            {/* Header 40px */}
            <div className="di-header" style={{ marginBottom: 0, minHeight: 40 }}>
              <div className="di-header-left">
                <Truck size={18} strokeWidth={2} style={{ color: '#1F6FEB', flexShrink: 0 }} />
                <h2 className="di-title">{L.driverVehicleStatus}</h2>
              </div>
              <span className="live-pill">
                <span className="live-dot pulse" />
                {L.liveStatus}
              </span>
            </div>

            {/* Fleet div-table */}
            <div className="fleet-table">
              <div className="fleet-thead">
                <div className="fleet-th">#</div>
                <div className="fleet-th">VEHICLE</div>
                <div className="fleet-th">DRIVER</div>
                <div className="fleet-th">CURRENT STOP</div>
                <div className="fleet-th">ETA</div>
                <div className="fleet-th">STATUS</div>
              </div>
              <div className="fleet-tbody">
                {snapshot.decisionState.vehicles.map((vehicle, idx) => {
                  const driver = getDriverInfo(vehicle.id);
                  const isAvailable = vehicle.availability === "AVAILABLE";
                  /* Use accepted plan first, then selected proposal, then no plan */
                  const displayVehiclePlans =
                    active?.plan.vehiclePlans ??
                    selectedPlan?.vehiclePlans ??
                    [];
                  const myVPlan = displayVehiclePlans.find((vp) => vp.vehicleId === vehicle.id);
                  const myStops = myVPlan
                    ? myVPlan.orderedStops.filter((s) => s.kind !== "DEPOT_PICKUP")
                    : [];
                  const isEnRoute = isAvailable && myStops.length > 0;
                  const isDelayed = false;
                  const currentStop = myStops[0];
                  const stopLabel = isEnRoute
                    ? `Stop 1/${myStops.length}`
                    : isAvailable
                    ? "Depot"
                    : "Unavailable";
                  const stopOrder = currentStop?.orderIds?.[0] ?? null;
                  const color = vehicleColor(vehicle.id);
                  const cap = capacities[vehicle.id] ?? vehicle.capacityKg;
                  const load = vehicle.currentLoadKg;
                  const loadPct = Math.min(100, (load / cap) * 100);
                  const overload = load > cap;
                  const statusClass = !isAvailable
                    ? "unavailable"
                    : isDelayed
                    ? "delayed"
                    : isEnRoute
                    ? "en-route"
                    : "idle";

                  return (
                    <div
                      key={vehicle.id}
                      className={`fleet-row${vehicle.driverPhone || vehicle.licensePlate ? " fleet-row--with-contact" : ""}${isDelayed ? " row-delayed" : ""}${!isAvailable ? " row-unavailable" : ""}`}
                    >
                      {/* # */}
                      <div className="fleet-td">
                        <span className="vehicle-row-num">{idx + 1}</span>
                      </div>
                      {/* VEHICLE */}
                      <div className="fleet-td">
                        <div className="veh-cell">
                          <span style={{ width: 10, height: 10, borderRadius: "50%", background: color, flexShrink: 0, display: "inline-block" }} />
                          <span style={{ fontSize: 14, fontWeight: 700, color: "#0F172A" }} title={[vehicle.vehicleModel,vehicle.licensePlate].filter(Boolean).join(' · ')}>{vehicle.id}</span>
                        </div>
                      </div>
                      {/* DRIVER */}
                      <div className="fleet-td">
                        <div className="driver-cell">
                          <div style={{ minWidth: 0, flex: 1 }}>
                            <span className="driver-name" title={driver.name}>{driver.name}</span>
                            {vehicle.driverPhone && <span className="driver-load" title={vehicle.driverPhone}>{vehicle.driverPhone}</span>}
                            {vehicle.licensePlate && <span className="driver-load" title={[vehicle.licensePlate,vehicle.vehicleModel].filter(Boolean).join(' · ')}>{vehicle.licensePlate}</span>}
                            <span className="driver-load" style={overload ? { color: "#E5252A", fontWeight: 700 } : {}}>
                              {load.toFixed(1)} / {cap} kg
                            </span>
                            <div className="load-bar-track">
                              <div
                                className="load-bar-fill"
                                style={{ width: `${loadPct}%`, background: overload ? "#E5252A" : color }}
                              />
                            </div>
                          </div>
                        </div>
                      </div>
                      {/* CURRENT STOP */}
                      <div className="fleet-td">
                        <div className="stop-cell">
                          <span className="stop-main">{stopLabel}</span>
                          {stopOrder && <span className="stop-order">{stopOrder}</span>}
                        </div>
                      </div>
                      {/* ETA */}
                      <div className="fleet-td">
                        <span className="eta-cell">{isEnRoute && !isOffline(active?.plan ?? selected?.content) ? "14:35" : "—"}</span>
                      </div>
                      {/* STATUS */}
                      <div className="fleet-td">
                        <span className={`status-pill ${statusClass}`}>
                          {!isAvailable
                            ? L.statusUnavailable
                            : isDelayed
                            ? L.statusDelayed
                            : isEnRoute
                            ? L.statusEnRoute
                            : L.statusIdle}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Fleet footer: legend + summary */}
            {(() => {
              const total = snapshot.decisionState.vehicles.length;
              const displayVehiclePlans =
                active?.plan.vehiclePlans ??
                selectedPlan?.vehiclePlans ??
                [];
              const enRouteCount = snapshot.decisionState.vehicles.filter(
                (v) =>
                  v.availability === "AVAILABLE" &&
                  displayVehiclePlans.some(
                    (vp) =>
                      vp.vehicleId === v.id &&
                      vp.orderedStops.some((s) => s.kind !== "DEPOT_PICKUP")
                  )
              ).length;
              const idleCount =
                total -
                enRouteCount -
                snapshot.decisionState.vehicles.filter(
                  (v) => v.availability === "UNAVAILABLE"
                ).length;
              return (
                <div className="fleet-footer">
                  <div className="fleet-legend">
                    {snapshot.decisionState.vehicles.map((v) => (
                      <div key={v.id} className="fleet-legend-item">
                        <span className="fleet-legend-dot" style={{ background: vehicleColor(v.id) }} />
                        <span>{v.id}</span>
                      </div>
                    ))}
                  </div>
                  <span className="fleet-summary-text">
                    {total} vehicles · {enRouteCount} en route · {idleCount} idle
                  </span>
                </div>
              );
            })()}

          </section>{/* end panel-fleet-status */}

        </div>{/* end admin-right */}
        </WorkspacePanel>

      </div>
    </main>
  );
}
