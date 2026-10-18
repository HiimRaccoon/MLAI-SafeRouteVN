import { Member3Error } from "./errors";
import { parseBasis, sameBasis } from "./revision";
import type { M3PendingEventsView, M3ReplayAudit } from "./types";

export function checkEventContract(condition: unknown): asserts condition {
  if (!condition) throw new Member3Error("INVALID_RESPONSE", "Invalid M3 event/replay contract or binding.");
}
export const eventType = (value: unknown) => ["URGENT_ORDER", "VEHICLE_UNAVAILABLE", "LOCAL_RAIN_WHAT_IF"].includes(String(value));
export const eventId = (value: unknown) => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$/.test(value);
export const eventTimestamp = (value: unknown) => typeof value === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?\+07:00$/.test(value) && Number.isFinite(Date.parse(value));
export function parsePendingEvents(raw: unknown, sid: string): M3PendingEventsView {
  const v = raw as M3PendingEventsView;
  checkEventContract(v && typeof v === "object" && v.schema_version === "saferoute-m3-pending-events/1" &&
    v.execution_mode === "SIMULATED_REPLAY" && v.real_world_observation === false && eventTimestamp(v.current_time) && Array.isArray(v.events));
  checkEventContract(parseBasis(v.basis).session_id === sid);
  v.events.forEach(e => checkEventContract(e && eventId(e.event_id) && eventType(e.event_type) && eventTimestamp(e.timestamp) &&
    typeof e.apply_allowed === "boolean" && (!e.apply_allowed || e.timestamp === v.current_time)));
  checkEventContract(new Set(v.events.map(e => e.event_id)).size === v.events.length);
  return v;
}
export function bindPendingEvents(view: M3PendingEventsView, world: import("./types").M3ExecutionView) {
  if (!sameBasis(view.basis, world.basis) || view.current_time !== world.current_time || view.events.length !== world.pending_event_ids.length ||
      view.events.some(e => !world.pending_event_ids.includes(e.event_id))) throw new Member3Error("STATE_CHANGED", "M3 event metadata changed during the world read.");
  checkEventContract(!view.events.some(e => e.apply_allowed && world.observed_metrics === null));
}
export function confirmedEvents(pending: M3PendingEventsView | undefined, audit: M3ReplayAudit | undefined) {
  const rows = (pending?.events ?? []).map(e => ({ ...e, applied: false }));
  for (const receipt of audit?.history ?? []) {
    if (receipt.operation === "apply_event" && receipt.status === "APPLIED" && !rows.some(e => e.event_id === receipt.event_id)) {
      rows.push({ event_id: receipt.event_id!, event_type: receipt.event_type!, timestamp: receipt.recorded_at, apply_allowed: false, applied: true });
    }
  }
  return rows;
}
