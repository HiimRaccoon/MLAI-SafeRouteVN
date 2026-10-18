import { useEffect, useRef, useState } from "react";
import { useDispatch, useDispatchPresentation } from "../app/DispatchContext";
import { DriverMap } from "./DriverMap";
import { ReplayControls } from "./ReplayControls";
import { contactHref } from "../shared/presentation";
import { atStop } from '../shared/geo';
import { LiveEta } from './LiveEta';
import type { PlanStop, VehiclePlan } from "../shared/types/dispatch";
import {
  Bell,
  MapPin,
  Navigation,
  Package,
  Settings,
  Check,
  ArrowRight,
  Phone,
  Clock,
  AlertTriangle,
  X,
  Info,
  Sparkles,
  Wifi,
  BatteryMedium
} from "lucide-react";

// ─────────────────────────────────────────────────────────────────────────────
// Sub-components
// ─────────────────────────────────────────────────────────────────────────────

interface CurrentStopCardProps {
  stop?: PlanStop;
  v1Plan?: VehiclePlan;
  onPickup: (orderId: string) => void;
  onDeliver: (orderId: string) => void;
  onViewDetails: (orderId: string) => void;
  locked: boolean;
  travelling: boolean;
  pending: boolean;
  orders: { id: string; status: string; priority?: number }[];
  allStopsCount: number;
  currentStopIndex: number;
}

function CurrentStopCard({
  stop,
  v1Plan,
  onPickup,
  onDeliver,
  onViewDetails,
  locked,
  travelling,
  pending,
  orders,
  allStopsCount,
  currentStopIndex,
}: CurrentStopCardProps) {
  const { getCustomerInfo, getDriverInfo, depot: DEPOT_PRESENTATION, demo: demoPresentation } = useDispatchPresentation();
  if (!stop) {
    return (
      <div className="drv-card drv-card--done" role="status">
        <div className="drv-done-icon-wrap">
          <Check size={26} strokeWidth={3} className="drv-done-check" />
        </div>
        <h2 className="drv-card-title drv-done-title">All assigned stops are complete.</h2>
        <p className="drv-done-desc">Great job! All orders have been delivered.</p>
        <span className="drv-demo-badge">{demoPresentation ? "Demo data" : "Simulated replay"}</span>
      </div>
    );
  }

  // ── DEPOT_PICKUP ──────────────────────────────────────────────────────────
  if (stop.kind === "DEPOT_PICKUP") {
    const waitingOrderIds = stop.orderIds.filter(
      (id) => orders.find((o) => o.id === id)?.status === "WAITING"
    );
    const activeOrderId = waitingOrderIds[0] ?? stop.orderIds[0];

    return (
      <div className="drv-card drv-current-card" data-kind="depot">
        {/* Header row: [Current Stop] [Index] [Status Pill] */}
        <div className="drv-card-header">
          <div className="drv-card-header-left">
            <h2 className="drv-card-title" id="drv-current-stop-heading">
              Current Stop
            </h2>
            <span className="drv-stop-index">
              {currentStopIndex + 1} / {allStopsCount}
            </span>
          </div>
          <span className="drv-status-pill drv-status-pill--moving">
            {travelling ? "Travelling to depot" : "At depot · Pickup"}
          </span>
        </div>

        {/* Body */}
        <div className="drv-card-body">
          <div className="drv-stop-avatar drv-avatar--depot">
            <Package size={20} strokeWidth={2.2} />
          </div>
          <div className="drv-stop-info">
            <div className="drv-customer-row">
              <strong className="drv-customer-name">
                Pickup: {DEPOT_PRESENTATION.name}
              </strong>
            </div>
            <p className="drv-stop-address">{DEPOT_PRESENTATION.address}</p>
            <div className="drv-meta-row">
              <a href={contactHref(DEPOT_PRESENTATION.phone)} aria-disabled={!contactHref(DEPOT_PRESENTATION.phone)} className="drv-phone-link">
                <Phone size={13} strokeWidth={2} />
                <span>{DEPOT_PRESENTATION.phone}</span>
              </a>
              <span className="drv-demo-badge">{demoPresentation ? "Demo data" : "Simulated replay"}</span>
            </div>
          </div>
        </div>

        {/* Pickup List items */}
        <div className="drv-pickup-section" aria-label="Pickup orders list">
          {stop.orderIds.map((orderId) => {
            const info = getCustomerInfo(orderId);
            const orderState = orders.find((o) => o.id === orderId);
            const isDone =
              orderState?.status === "ONBOARD" || orderState?.status === "DELIVERED";
            const isActive = orderId === activeOrderId && !isDone;

            return (
              <div
                key={orderId}
                className={`drv-pickup-row ${isDone ? "drv-pickup-row--done" : ""}`}
              >
                <div className="drv-pickup-left">
                  <span className="drv-order-chip">{orderId}</span>
                  <span className="drv-pickup-name">{info.name}</span>
                </div>
                {isDone ? (
                  <span className="drv-badge-done">
                    <Check size={12} strokeWidth={3} /> Picked up
                  </span>
                ) : isActive ? (
                  <button
                    type="button"
                    className="drv-btn drv-btn--primary drv-btn--pickup"
                    disabled={locked || pending}
                    onClick={() => onPickup(orderId)}
                  >
                    <span>Picked up</span>
                    <ArrowRight size={15} strokeWidth={2.5} />
                  </button>
                ) : (
                  <span className="drv-badge-queued">Queued</span>
                )}
              </div>
            );
          })}
        </div>

        {/* Actions row */}
        <div className="drv-actions-row">
          <button
            type="button"
            className="drv-btn drv-btn--outline"
            onClick={() => onViewDetails(activeOrderId)}
          >
            <Info size={15} strokeWidth={2} />
            <span>View details</span>
          </button>
        </div>

        {locked && (
          <div className="drv-locked-box" role="status">
            <AlertTriangle size={15} strokeWidth={2} />
            <span>{travelling ? "Arrive at this stop to unlock pickup." : "Vehicle is unavailable — actions temporarily paused."}</span>
          </div>
        )}
      </div>
    );
  }

  // ── DELIVERY ──────────────────────────────────────────────────────────────
  const orderId = stop.orderIds[0];
  const info = getCustomerInfo(orderId);
  const order = orders.find((o) => o.id === orderId);
  const isUrgent = (order?.priority ?? 0) >= 3;

  // Supplied road routes have multiple EDGE segments per incoming leg.
  const incomingSegments = v1Plan?.routeSegments.filter((s) => s.toStopId === stop.id) ?? [];
  const distanceLabel = incomingSegments.length ? `${incomingSegments.reduce((total, s) => total + s.distanceKm, 0).toFixed(1)} km` : "—";
  const durationLabel = incomingSegments.length ? `${incomingSegments.reduce((total, s) => total + s.durationMinutes, 0).toFixed(1)} min` : "—";

  return (
    <div className="drv-card drv-current-card" data-kind="delivery">
      {/* Header row: [Current Stop] [Index] [Status Pill] */}
      <div className="drv-card-header">
        <div className="drv-card-header-left">
          <h2 className="drv-card-title" id="drv-current-stop-heading">
            Current Stop
          </h2>
          <span className="drv-stop-index">
            {currentStopIndex + 1} / {allStopsCount}
          </span>
        </div>
        <span
          className={`drv-status-pill ${
            isUrgent ? "drv-status-pill--urgent" : "drv-status-pill--moving"
          }`}
        >
          {isUrgent ? "Urgent delivery" : "En route"}
        </span>
      </div>

      {/* Body: Stop Icon + Info */}
      <div className="drv-card-body">
        <div className={`drv-stop-avatar ${isUrgent ? "drv-avatar--urgent" : "drv-avatar--current"}`}>
          <span>{currentStopIndex + 1}</span>
        </div>

        <div className="drv-stop-info">
          <div className="drv-customer-row">
            <strong className="drv-customer-name">{info.name}</strong>
            <span className="drv-order-chip">{orderId}</span>
          </div>
          <p className="drv-stop-address">{info.address}</p>

          <div className="drv-meta-row">
            <a href={contactHref(info.phone)} aria-disabled={!contactHref(info.phone)} className="drv-phone-link">
              <Phone size={13} strokeWidth={2} />
              <span>{info.phone}</span>
            </a>
            {info.timeWindow && (
              <span className="drv-time-chip">
                <Clock size={12} strokeWidth={2} />
                <span>{info.timeWindow}</span>
              </span>
            )}
            <span className="drv-demo-badge">{demoPresentation ? "Demo data" : "Simulated replay"}</span>
          </div>

          {info.notes && (
            <p className="drv-notes-text">📝 {info.notes}</p>
          )}
        </div>

        {/* Distance / Duration on right */}
        <div className="drv-metrics-col">
          <strong className="drv-metric-km">{distanceLabel}</strong>
          <span className="drv-metric-min">{durationLabel}</span>
        </div>
      </div>

      {/* Action Buttons: View details & Delivered */}
      <div className="drv-actions-row">
        <button
          type="button"
          className="drv-btn drv-btn--outline"
          onClick={() => onViewDetails(orderId)}
        >
          <Info size={15} strokeWidth={2} />
          <span>View details</span>
        </button>

        <button
          type="button"
          className="drv-btn drv-btn--primary"
          disabled={locked || pending}
          aria-label={`Mark delivered for order ${orderId}`}
          onClick={() => onDeliver(orderId)}
        >
          <span>Delivered</span>
          <ArrowRight size={16} strokeWidth={2.5} />
        </button>
      </div>

      {locked && (
        <div className="drv-locked-box" role="status">
          <AlertTriangle size={15} strokeWidth={2} />
          <span>{travelling ? "Arrive at this stop to unlock delivery." : "Vehicle is unavailable — actions temporarily paused."}</span>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Order Details Modal
// ─────────────────────────────────────────────────────────────────────────────

function OrderDetailsModal({
  orderId,
  onClose,
  snapshot,
}: {
  orderId: string;
  onClose: () => void;
  snapshot: any;
}) {
  const { getCustomerInfo, getDriverInfo, depot: DEPOT_PRESENTATION, demo: demoPresentation } = useDispatchPresentation();
  const info = getCustomerInfo(orderId);
  const order = snapshot.decisionState.orders.find((o: any) => o.id === orderId);
  const isUrgent = (order?.priority ?? 0) >= 3;

  return (
    <div className="drv-modal-overlay" role="dialog" aria-modal="true">
      <div className="drv-modal-card">
        <div className="drv-modal-header">
          <div className="drv-modal-title-group">
            <h3>Order Details</h3>
            <span className="drv-order-chip">{orderId}</span>
          </div>
          <button
            type="button"
            className="drv-modal-close"
            onClick={onClose}
            aria-label="Close details"
          >
            <X size={18} strokeWidth={2} />
          </button>
        </div>

        <div className="drv-modal-body">
          <div className="drv-modal-row">
            <span className="drv-modal-label">Customer</span>
            <strong className="drv-modal-val">{info.name}</strong>
          </div>
          <div className="drv-modal-row">
            <span className="drv-modal-label">Phone</span>
            <a href={contactHref(info.phone)} aria-disabled={!contactHref(info.phone)} className="drv-phone-link">
              <Phone size={13} strokeWidth={2} />
              <span>{info.phone}</span>
            </a>
          </div>
          <div className="drv-modal-row">
            <span className="drv-modal-label">Delivery Address</span>
            <span className="drv-modal-val">{info.address}</span>
          </div>
          <div className="drv-modal-row">
            <span className="drv-modal-label">Time Window</span>
            <span className="drv-modal-val">{info.timeWindow || "Flexible"}</span>
          </div>
          <div className="drv-modal-row">
            <span className="drv-modal-label">Package Weight</span>
            <span className="drv-modal-val">{order ? `${order.demandKg} kg` : "—"}</span>
          </div>
          <div className="drv-modal-row">
            <span className="drv-modal-label">Priority</span>
            <span className={`drv-opill ${isUrgent ? "drv-opill--urgent" : "drv-opill--normal"}`}>
              {isUrgent ? "Urgent" : "Normal"}
            </span>
          </div>
          {info.notes && (
            <div className="drv-modal-row drv-modal-notes">
              <span className="drv-modal-label">Delivery Notes</span>
              <p className="drv-modal-val">{info.notes}</p>
            </div>
          )}
          <div className="drv-modal-footer">
            <span className="drv-demo-badge">{demoPresentation ? "Demo data" : "Simulated replay"}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main DriverPage Component
// ─────────────────────────────────────────────────────────────────────────────

export function DriverPage() {
  const { api, snapshot, pending, error, invoke } = useDispatch();
  const { getCustomerInfo, getDriverInfo, depot: DEPOT_PRESENTATION, demo: demoPresentation } = useDispatchPresentation();


  // Each tab can keep its own driver view; selecting a driver does not reassign cargo.
  const [selectedVehicleId, setSelectedVehicleId] = useState(()=>new URLSearchParams(window.location.search).get('vehicle')||"V1");
  const [activeTab, setActiveTab] = useState<"route" | "orders">("route");
  const [bottomNav, setBottomNav] =
    useState<"route" | "orders" | "notifications" | "settings">("route");
  const [showUpdateBanner, setShowUpdateBanner] = useState(false);
  const [inspectOrderId, setInspectOrderId] = useState<string | null>(null);
  const previousPlanId = useRef<string | null | undefined>(undefined);
  const initialSnapshotSeen = useRef(false);
  const [mapExpanded, setMapExpanded] = useState(false);
  const contentScrollRef = useRef<HTMLDivElement>(null);
  const expandedRoute = mapExpanded && activeTab === 'route' && bottomNav === 'route';
  function toggleMap() { setMapExpanded(value => !value); if (contentScrollRef.current) contentScrollRef.current.scrollTop = 0; }
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setMapExpanded(false); };
    const back = () => setMapExpanded(false);
    window.addEventListener('keydown', close); window.addEventListener('saferoute:back', back);
    return () => { window.removeEventListener('keydown', close); window.removeEventListener('saferoute:back', back); };
  }, []);

  useEffect(() => {
    if (!snapshot) return;
    if (!snapshot.decisionState.vehicles.some(v => v.id === selectedVehicleId)) {
      setSelectedVehicleId(snapshot.decisionState.vehicles[0]?.id ?? "");
    }
    setInspectOrderId(null);
    setShowUpdateBanner(false);
  }, [snapshot?.decisionState.sessionId]);

  // Banner: show only when activeAcceptedPlanId actually changes after initial mount
  useEffect(() => {
    if (!snapshot) return;
    const activeId = snapshot.planState.activeAcceptedPlanId;

    if (!initialSnapshotSeen.current) {
      initialSnapshotSeen.current = true;
      previousPlanId.current = activeId;
      return;
    }

    if (
      previousPlanId.current !== undefined &&
      previousPlanId.current !== activeId &&
      activeId !== null
    ) {
      setShowUpdateBanner(true);
    }
    previousPlanId.current = activeId;
  }, [snapshot?.planState.activeAcceptedPlanId]);

  if (!snapshot) {
    return (
      <main className="drv-page-loading">
        <div className="drv-spinner" />
        <span role={error ? "alert" : undefined}>{error ?? "Loading Driver workspace…"}</span>
      </main>
    );
  }

  // ── Derived data ──────────────────────────────────────────────────────────
  const activePlan = snapshot.planState.acceptedPlans.find(
    (p) => p.id === snapshot.planState.activeAcceptedPlanId
  );

  // One vehicle is visible at a time.
  const v1Plan: VehiclePlan | undefined = activePlan?.plan.vehiclePlans.find(
    (vp) => vp.vehicleId === selectedVehicleId
  );

  const vehicle = snapshot.decisionState.vehicles.find((v) => v.id === selectedVehicleId);
  const vehicleLocked = vehicle?.availability === "UNAVAILABLE";
  const nativeAccepted = snapshot.backend ? snapshot.planState.acceptedExecution : null;
  const observedVehicle = snapshot.backend?.executionView.vehicles.find(v => v.vehicle_id === selectedVehicleId);

  const progress =
    activePlan && v1Plan
      ? snapshot.executionState.progressByPlanId[activePlan.id]?.[selectedVehicleId]
      : undefined;

  // Determine current stop for the selected vehicle.
  const completedStopIds = progress?.completedStopIds ?? [];
  const incompleteStops = v1Plan?.orderedStops.filter((s) => !completedStopIds.includes(s.id)) ?? [];
  const currentStop: PlanStop | undefined =
    v1Plan?.orderedStops.find((s) => s.id === progress?.currentStopId) ?? incompleteStops[0];

  const currentStopIndex = v1Plan?.orderedStops.findIndex((s) => s.id === currentStop?.id) ?? 0;
  const allStopsCount = v1Plan?.orderedStops.length ?? 0;

  const driver = getDriverInfo(selectedVehicleId);

  // Orders for the selected vehicle only.
  const v1Orders = snapshot.decisionState.orders.filter(
    (order) =>
      order.assignedVehicleId === selectedVehicleId ||
      nativeAccepted?.vehicleOrderIds[selectedVehicleId]?.includes(order.id) ||
      v1Plan?.orderedStops.some((s) => s.orderIds.includes(order.id))
  );

  const needsReopt =
    snapshot.planState.operationalPlanAssessment?.status === "NEEDS_REOPTIMIZATION";

  // ── Action handlers ───────────────────────────────────────────────────────
  function handlePickup(orderId: string) {
    void invoke(() => api.pickupOrder({ vehicleId: selectedVehicleId, orderId }));
  }

  function handleDeliver(orderId: string) {
    void invoke(async () => {
      const updated=await api.deliverOrder({ vehicleId: selectedVehicleId, orderId });
      const b=updated.backend;
      if(b?.executionView.active_job_id&&!b.executionView.delivery_day?.complete&&!b.executionView.vehicles.some(v=>v.awaiting_delivery)&&!b.pendingEvents?.events.some(e=>e.apply_allowed)&&b.playback?.fully_paused&&api.replayStart){
        return api.replayStart(b.playback.speed);
      }
      return updated;
    });
  }

  // ─────────────────────────────────────────────────────────────────────────
  return (
    <main className={`drv-page${expandedRoute ? ' drv-page--map-expanded' : ''}`} data-session-id={snapshot.decisionState.sessionId} data-dispatch-source={snapshot.backend?.source ?? "MOCK"} data-basis={snapshot.backend ? JSON.stringify(snapshot.backend.basis) : undefined} data-current-time={snapshot.backend?.executionView.current_time} data-active-job-id={snapshot.backend?.executionView.active_job_id ?? undefined} data-stale={snapshot.backend?.stale ? "true" : "false"}>
      <div className="drv-shell">

        {/* ── Simulated Phone Status Bar (for desktop shell preview) ─────── */}
        <div className="drv-status-bar" aria-hidden="true">
          <span className="drv-status-time">{snapshot.demoClock.now.slice(11, 16)}</span>
          <div className="drv-status-notch" />
          <div className="drv-status-icons">
            <Wifi size={13} strokeWidth={2.2} />
            <BatteryMedium size={15} strokeWidth={2.2} />
          </div>
        </div>

        {/* ── App Header ──────────────────────────────────────────────── */}
        <header className="drv-header">
          <div className="drv-brand">
            <div className="drv-logo" aria-hidden="true">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
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
              </svg>
            </div>
            <div>
              <h1 className="drv-brand-name">
                SafeRoute VN<span className="sr-only"> · Tài xế</span>
              </h1>
              <small className="drv-brand-sub">
                Driver App · {driver.name}
              </small>
            </div>
          </div>

          {/* Bell Icon: Badge is 0 by contract (no badge rendered when 0) */}
          <button
            type="button"
            aria-label="Notifications"
            className="drv-bell-btn"
            onClick={() => setBottomNav("notifications")}
          >
            <Bell size={20} strokeWidth={1.8} />
          </button>
        </header>

        <div className="drv-settings-row" style={{padding:'12px 16px'}}>
          <label htmlFor="driver-selector">Tài xế đang xem</label>
          <select id="driver-selector" value={selectedVehicleId} disabled={pending} onChange={e=>{
            setSelectedVehicleId(e.target.value);setInspectOrderId(null);
            const url=new URL(window.location.href);url.searchParams.set('vehicle',e.target.value);window.history.replaceState(null,'',url);
          }}>
            {snapshot.decisionState.vehicles.map(v=><option key={v.id} value={v.id}>{v.id} · {getDriverInfo(v.id).name}{snapshot.backend?.executionView.vehicles.find(x=>x.vehicle_id===v.id)?.awaiting_delivery?' · Chờ Deliver':''}</option>)}
          </select>
        </div>
        {/* ── Route Updated Banner (Dismissible) ───────────────────────── */}
        {showUpdateBanner && (
          <div className="drv-banner drv-banner--update" role="status">
            <span>Route has been updated.</span>
            <button
              type="button"
              className="drv-banner-close"
              aria-label="Dismiss banner"
              onClick={() => setShowUpdateBanner(false)}
            >
              <X size={15} strokeWidth={2.5} />
            </button>
          </div>
        )}

        {/* ── Needs-reoptimization Banner ─────────────────────────────── */}
        {needsReopt && (
          <div className="drv-banner drv-banner--warn" role="status">
            <AlertTriangle size={15} strokeWidth={2} />
            <span>Current plan needs re-optimization.</span>
          </div>
        )}

        {/* ── Error Banner ─────────────────────────────────────────────── */}
        {error && (
          <div className="drv-banner drv-banner--error" role="alert">
            <AlertTriangle size={15} strokeWidth={2} />
            <span>{error}</span>
          </div>
        )}

        {/* ── Segmented Tabs: [Route] [My Orders] ─────────────────────── */}
        <nav className="drv-tabs" role="tablist" aria-label="Driver views">
          <button
            role="tab"
            aria-selected={activeTab === "route"}
            className={`drv-tab${activeTab === "route" ? " drv-tab--active" : ""}`}
            onClick={() => {
              setActiveTab("route");
              setBottomNav("route");
            }}
          >
            Route
          </button>
          <button
            role="tab"
            aria-selected={activeTab === "orders"}
            className={`drv-tab${activeTab === "orders" ? " drv-tab--active" : ""}`}
            onClick={() => {
              setActiveTab("orders");
              setBottomNav("orders");
            }}
          >
            My Orders
          </button>
        </nav>

        {/* ═════════════════════════════════════════════════════════════
            MAIN VIEW AREA
        ══════════════════════════════════════════════════════════════ */}
        {bottomNav === "notifications" ? (
          /* Notifications Tab Content */
          <div className="drv-tab-page" role="region" aria-label="Notifications view">
            <div className="drv-empty-placeholder">
              <div className="drv-empty-icon-circle">
                <Bell size={28} strokeWidth={1.8} />
              </div>
              <h3>No notifications</h3>
              <p>You'll be notified when the dispatcher assigns a route.</p>
              <button
                type="button"
                className="drv-btn drv-btn--outline"
                style={{ marginTop: 12 }}
                onClick={() => setBottomNav("route")}
              >
                Back to Route
              </button>
            </div>
          </div>
        ) : bottomNav === "settings" ? (
          /* Settings Tab Content */
          <div className="drv-tab-page" role="region" aria-label="Settings view">
            <div className="drv-settings-card">
              <div className="drv-profile-header">
                <div className="drv-avatar-circle">{driver.avatar}</div>
                <div>
                  <h3 className="drv-profile-name">{driver.name}</h3>
                  <p className="drv-profile-vehicle">{driver.vehicleModel} · {driver.licensePlate}</p>
                </div>
              </div>

              <div className="drv-settings-list">
                <div className="drv-settings-row">
                  <span>Driver ID</span>
                  <strong>{driver.id}</strong>
                </div>
                <div className="drv-settings-row">
                  <span>Phone Number</span>
                  <span>{driver.phone}</span>
                </div>
                <div className="drv-settings-row">
                  <span>Rating</span>
                  <span className="drv-rating-badge">{driver.rating}</span>
                </div>
                <div className="drv-settings-row">
                  <span>Vehicle Status</span>
                  <span className={`drv-opill ${vehicleLocked ? "drv-opill--urgent" : "drv-opill--done"}`}>
                    {vehicleLocked ? "Unavailable" : "Available"}
                  </span>
                </div>
                <div className="drv-settings-row">
                  <span>Presentation Data</span>
                  <span className="drv-demo-badge">{demoPresentation ? "Demo data" : "Simulated replay"}</span>
                </div>
              </div>

              <button
                type="button"
                className="drv-btn drv-btn--outline"
                style={{ marginTop: 16, width: "100%" }}
                onClick={() => setBottomNav("route")}
              >
                Back to Route
              </button>
            </div>
          </div>
        ) : (
          /* Route & Orders View Area */
            <div ref={contentScrollRef} className="drv-content-scroll">
            {/* Map (Rendered on Route view) */}
            {activeTab === "route" && (
              <div className="drv-map-section">
                <DriverMap
                  snapshot={snapshot}
                  vehicleId={selectedVehicleId}
                  activePlanId={activePlan?.id}
                  currentStopId={currentStop?.id}
                  expanded={mapExpanded}
                  onToggleExpanded={toggleMap}
                />
                <div className="drv-map-caption">
                  <span>Simulated location</span>
                  <span>·</span>
                  <span className="drv-demo-badge">{demoPresentation ? "Demo data" : "Simulated replay"}</span>
                </div>
              </div>
            )}

            {/* Current Stop Card or Empty State */}
            <ReplayControls api={api} snapshot={snapshot} pending={pending} invoke={invoke} />
            {!snapshot.backend && <LiveEta key={snapshot.decisionState.sessionId+selectedVehicleId} target={currentStop?.location} simulatedMinutes={currentStop&&v1Plan?Math.max(0,v1Plan.routeSegments.filter(s=>s.toStopId===currentStop.id).reduce((sum,s)=>sum+s.durationMinutes,0)-(progress?.travel?.elapsedMinutes??0)):undefined} />}
            {!snapshot.backend && currentStop && vehicle && !atStop(vehicle.currentPosition,currentStop.location) && <p role="status">Travelling to {currentStop.label}. Pickup / Delivered unlocks on arrival.</p>}
            {snapshot.backend && <section className="drv-stop-section" aria-label="Server execution">
              <div className="drv-empty-card">
                <p>Server time: {snapshot.backend.executionView.current_time}</p>
                <p>Load: {observedVehicle?.current_load_kg ?? "unavailable"} kg · Onboard: {observedVehicle?.onboard_order_ids.join(", ") || "none"}</p>
                <p>Delivered prefix: {snapshot.backend.executionView.delivered_prefix.join(", ") || "none"}</p>
                  {observedVehicle?.awaiting_delivery && <>
                    <p>{snapshot.backend.executionView.delivery_day?.customers[observedVehicle.awaiting_delivery]?.name || 'Chưa có tên khách'} · {snapshot.backend.executionView.delivery_day?.customers[observedVehicle.awaiting_delivery]?.phone || 'Chưa có SĐT'}</p>
                    <button disabled={pending || snapshot.backend.stale || snapshot.backend.mutationPending || observedVehicle.availability !== 'AVAILABLE'} onClick={()=>handleDeliver(observedVehicle.awaiting_delivery!)}>Deliver · {observedVehicle.awaiting_delivery}</button>
                  </>}
                <p>Position observed: {observedVehicle?.position_timestamp ?? "unavailable"}</p>
                <p>Location: {observedVehicle?.position.coordinates.join(", ") ?? "unavailable"}</p>
                <p>Planned suffix: {observedVehicle?.planned_suffix?.join(", ") || "none"}</p>
                {snapshot.backend.executionView.unserved.length > 0 && <p>Unserved: {snapshot.backend.executionView.unserved.map(o => `${o.order_id} (${o.reason})`).join(", ")}</p>}
                {error && <p role="alert">{error}</p>}
              </div>
            </section>}
            <section
              className="drv-stop-section"
              aria-labelledby="drv-current-stop-heading"
            >
              {nativeAccepted ? (
                <div className="drv-empty-card" role="status">
                  <h2 className="drv-empty-title" id="drv-current-stop-heading">Accepted route · {nativeAccepted.profile}</h2>
                  <p className="drv-empty-desc">{nativeAccepted.jobId} · Simulated replay</p>
                  <p className="drv-empty-desc">Observed progress comes from server replay. Planned deliveries remain forecasts.</p>
                  {!nativeAccepted.vehicleOrderIds[selectedVehicleId] && <p>No assigned continuation for {selectedVehicleId}.</p>}
                </div>
              ) : !activePlan || !v1Plan || v1Plan.orderedStops.length === 0 ? (
                /* Empty state before accept */
                <div className="drv-empty-card" role="status">
                  <div className="drv-empty-icon-wrap">
                    <MapPin size={26} strokeWidth={2} />
                  </div>
                  <h2 className="drv-empty-title" id="drv-current-stop-heading">
                    No dispatch plan has been assigned yet.
                  </h2>
                  <p className="drv-empty-desc">
                    You'll be notified when the dispatcher assigns a route.
                  </p>
                  <span className="drv-demo-badge">{demoPresentation ? "Demo data" : "Simulated replay"}</span>
                </div>
              ) : (
                <CurrentStopCard
                  stop={currentStop}
                  v1Plan={v1Plan}
                  onPickup={handlePickup}
                  onDeliver={handleDeliver}
                  onViewDetails={(oid) => setInspectOrderId(oid)}
                  locked={!!vehicleLocked || (!!currentStop && !!vehicle && !atStop(vehicle.currentPosition,currentStop.location))}
                  travelling={!vehicleLocked && !!currentStop && !!vehicle && !atStop(vehicle.currentPosition,currentStop.location)}
                  pending={pending}
                  orders={snapshot.decisionState.orders}
                  allStopsCount={allStopsCount}
                  currentStopIndex={currentStopIndex}
                />
              )}
            </section>

            {/* My Orders Section */}
            <section className="drv-orders-section" aria-label="My Orders list">
              <div className="drv-orders-header">
                <h2 className="drv-orders-title">
                  My Orders ({v1Orders.length})
                </h2>
                {v1Orders.length > 0 && (
                  <button
                    type="button"
                    className="drv-view-all-btn"
                    onClick={() => setActiveTab("orders")}
                  >
                    View all
                  </button>
                )}
              </div>

              <ul className="drv-orders-list">
                {v1Orders.map((order, idx) => {
                  const info = getCustomerInfo(order.id);
                  const isUrgent = (order.priority ?? 0) >= 3;
                  const isDelivered = order.status === "DELIVERED";
                  const isOnboard = order.status === "ONBOARD";

                  let statusText = "Waiting pickup";
                  let statusCls = "drv-opill--waiting";
                  if (isDelivered) {
                    statusText = "Delivered";
                    statusCls = "drv-opill--done";
                  } else if (isOnboard) {
                    statusText = "En route";
                    statusCls = "drv-opill--moving";
                  } else if (isUrgent) {
                    statusText = "Urgent";
                    statusCls = "drv-opill--urgent";
                  }

                  return (
                    <li key={order.id}>
                    <button
                      type="button"
                      className={`drv-order-item ${
                        isUrgent && !isDelivered ? "drv-order-item--urgent" : ""
                      } ${isDelivered ? "drv-order-item--done" : ""}`}
                      onClick={() => setInspectOrderId(order.id)}
                    >
                      <div
                        className={`drv-order-seq ${
                          isDelivered
                            ? "drv-order-seq--done"
                            : isUrgent
                            ? "drv-order-seq--urgent"
                            : idx === currentStopIndex
                            ? "drv-order-seq--current"
                            : ""
                        }`}
                      >
                        {isDelivered ? (
                          <Check size={12} strokeWidth={3} />
                        ) : (
                          idx + 1
                        )}
                      </div>

                      <div className="drv-order-body">
                        <div className="drv-order-name-row">
                          <span className="drv-order-name">{info.name}</span>
                          <span className="drv-order-chip">{order.id}</span>
                        </div>
                        <p className="drv-order-address">{info.address}</p>
                        {info.timeWindow && (
                          <span className="drv-order-window">
                            <Clock size={11} strokeWidth={2} />
                            <span>{info.timeWindow}</span>
                          </span>
                        )}
                      </div>

                      <span className={`drv-opill ${statusCls}`}>{statusText}</span>
                    </button>
                    </li>
                  );
                })}

                {v1Orders.length === 0 && (
                  <li className="drv-orders-empty">No orders yet</li>
                )}
              </ul>
            </section>
          </div>
        )}

        {/* ── Fixed Bottom Navigation (4 Tabs) ────────────────────────── */}
        <nav className="drv-bottom-nav" aria-label="Main navigation">
          <button
            type="button"
            className={`drv-nav-item ${
              bottomNav === "route" && activeTab === "route" ? "drv-nav-item--active" : ""
            }`}
            onClick={() => {
              setBottomNav("route");
              setActiveTab("route");
            }}
          >
            <Navigation size={20} strokeWidth={2.2} />
            <small>Route</small>
          </button>

          <button
            type="button"
            className={`drv-nav-item ${
              bottomNav === "orders" || (bottomNav === "route" && activeTab === "orders")
                ? "drv-nav-item--active"
                : ""
            }`}
            onClick={() => {
              setBottomNav("orders");
              setActiveTab("orders");
            }}
          >
            <Package size={20} strokeWidth={2} />
            <small>My Orders</small>
          </button>

          <button
            type="button"
            className={`drv-nav-item ${
              bottomNav === "notifications" ? "drv-nav-item--active" : ""
            }`}
            onClick={() => setBottomNav("notifications")}
          >
            <Bell size={20} strokeWidth={2} />
            <small>Notifications</small>
          </button>

          <button
            type="button"
            className={`drv-nav-item ${
              bottomNav === "settings" ? "drv-nav-item--active" : ""
            }`}
            onClick={() => setBottomNav("settings")}
          >
            <Settings size={20} strokeWidth={2} />
            <small>Settings</small>
          </button>
        </nav>

        {/* ── Order Details Modal ─────────────────────────────────────── */}
        {inspectOrderId && (
          <OrderDetailsModal
            orderId={inspectOrderId}
            onClose={() => setInspectOrderId(null)}
            snapshot={snapshot}
          />
        )}

      </div>{/* end drv-shell */}
    </main>
  );
}
