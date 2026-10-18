import { checkEventContract as check, eventId } from "./events";
import { parseBasis } from "./revision";
import { adaptAcceptedExecution } from "./executionViewAdapter";
import { parseReplayReceipt } from "./replay";
import type { M3PlaybackState, M3PlaybackView, M3PlaybackControl, M3ReplayReset } from "./types";
import { REPLAY_SPEEDS, type ReplaySpeed } from '../../shared/replaySpeeds';

export function validSpeed(speed: unknown): speed is ReplaySpeed { return REPLAY_SPEEDS.includes(speed as ReplaySpeed); }
export function playbackComplete(view: Pick<M3PlaybackView, "reason" | "end_time" | "execution_view">): boolean {
  if (view.execution_view.delivery_day && view.execution_view.vehicles.some(v=>v.awaiting_delivery&&v.availability==='AVAILABLE')) return false;
  if (view.reason === "PLAN_COMPLETE") return true;
  if (!view.end_time) return false;
  // Availability compares two server timestamps; it never advances the clock.
  // Date.parse alone truncates the SDK's microseconds before the final return.
  const microseconds = (value: string) => {
    const ms = Date.parse(value);
    if (!Number.isFinite(ms)) return undefined;
    const fraction = value.match(/\.(\d+)(?:Z|[+-]\d\d:\d\d)$/)?.[1] ?? "";
    return BigInt(Math.floor(ms / 1000)) * 1_000_000n + BigInt(fraction.padEnd(6, "0").slice(0, 6));
  };
  const current = microseconds(view.execution_view.current_time), end = microseconds(view.end_time);
  return current !== undefined && end !== undefined && current >= end;
}
function isServerRevision(value: unknown) { return typeof value === "string" && /^(0|[1-9]\d*)$/.test(value) && BigInt(value) < (1n << 63n); }
function state(raw: unknown, extended = true): M3PlaybackState {
  const c = raw as M3PlaybackState;
  check(c && typeof c === "object" && ["STEP", "AUTOMATIC"].includes(c.mode) && typeof c.paused === "boolean" && typeof c.in_flight === "boolean" &&
    c.fully_paused === (c.paused && !c.in_flight) && (c.mode === "STEP") === c.paused && validSpeed(c.speed) && isServerRevision(c.controller_revision) && typeof c.reason === "string" &&
    [c.next_tick_at, c.end_time].every(t => t === null || typeof t === "string" && /(?:Z|[+-]\d\d:\d\d)$/.test(t) && Number.isFinite(Date.parse(t))));
  if (extended) check(Number.isInteger(c.step_seconds) && c.step_seconds >= 1 && c.step_seconds <= 3600 && c.cadence === "DISCRETE_BEST_EFFORT" && c.base_tick_interval_seconds === 1 && c.catch_up === false && c.auto_apply_event === false && c.auto_accept_plan === false && c.execution_mode === "SIMULATED_REPLAY" && c.real_world_observation === false);
  check(c.auto_deliver === undefined || typeof c.auto_deliver === "boolean");
  return c;
}
export function parsePlayback(raw: unknown, sid: string): M3PlaybackView {
  const v = raw as M3PlaybackView;
  state(v); check(v.schema_version === "saferoute-m3-playback-controller/1" && parseBasis(v.execution_view?.basis).session_id === sid);
  try { adaptAcceptedExecution(v.execution_view); } catch { check(false); }
  return v;
}
export function parsePlaybackControl(raw: unknown, sid: string, operation: "start" | "speed" | "pause" | "auto_deliver", requestId: string): M3PlaybackControl {
  const v = raw as M3PlaybackControl, r = v?.receipt;
  check(v && v.schema_version === "saferoute-m3-playback-control/1"); state(v.controller);
  check(r && r.schema_version === "saferoute-m3-playback-receipt/1" && r.session_id === sid && r.operation === operation && r.request_id === requestId && r.source === "M3_PLAYBACK_CONTROL" && eventId(r.receipt_id) && eventId(r.actor_id) && typeof r.recorded_at === "string" && Number.isFinite(Date.parse(r.recorded_at)));
  state(r.controller, false);
  return v;
}
export function parseReplayReset(raw: unknown, sid: string): M3ReplayReset {
  const v = raw as M3ReplayReset;
  check(v && v.schema_version === "saferoute-m3-replay-reset/1" && v.source_session_id === sid);
  const r = parseReplayReceipt(v.receipt, sid);
  check(r.operation === "reset" && v.session?.session_id === r.new_session_id && v.session.session_id !== sid &&
    v.session.build_sha256 === r.input_basis.build_sha256 && /^S[0-4]$/.test(v.session.scenario_id) &&
    [v.session.catalog_sha256, v.session.fixture_sha256].every(s => typeof s === "string" && /^[a-f0-9]{64}$/.test(s)) &&
    JSON.stringify(v.session) === JSON.stringify(r.new_session) && parseBasis(v.execution_view?.basis).session_id === v.session.session_id && v.execution_view.basis.build_sha256 === v.session.build_sha256);
  try { adaptAcceptedExecution(v.execution_view); } catch { check(false); }
  return v;
}
