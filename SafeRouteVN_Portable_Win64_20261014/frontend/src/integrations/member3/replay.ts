import { parseBasis, sameBasis } from "./revision";
import { adaptAcceptedExecution } from "./executionViewAdapter";
import { checkEventContract as check, eventId, eventTimestamp, eventType } from "./events";
import type { M3ReplayReceipt, M3ReplayAudit, M3ReplayMutationView } from "./types";

export function parseReplayReceipt(raw: unknown, sid: string): M3ReplayReceipt {
  const r = raw as M3ReplayReceipt;
  check(r && typeof r === "object" && r.schema_version === "saferoute-m3-replay-receipt/1" && r.session_id === sid && eventId(r.mutation_id));
  const before = parseBasis(r.input_basis), after = parseBasis(r.basis);
  check(before.session_id === sid && typeof r.recorded_at === "string" && /(?:Z|[+-]\d\d:\d\d)$/.test(r.recorded_at) && Number.isFinite(Date.parse(r.recorded_at)));
  check(r.links && r.links.history === `/api/sessions/${sid}/replay/history`);
  if (r.operation === "reset") {
    check(r.status === "RESET" && r.source === "M3_NEW_SESSION" && eventId(r.new_session_id) && after.session_id === r.new_session_id &&
      r.new_session?.session_id === r.new_session_id && r.new_session.build_sha256 === before.build_sha256 && after.build_sha256 === before.build_sha256);
  } else {
    check(after.session_id === sid);
    if (r.operation === "pause") check(r.status === "PAUSED" && r.source === "M3_MANUAL_CONTROL" && r.mode === "STEP" && r.paused === true && sameBasis(before, after));
    else {
      check(r.source === "M2_PUBLIC_SDK");
      if (r.operation === "advance") check(["ADVANCED", "NOOP"].includes(r.status) && eventTimestamp(r.target_time));
      else if (r.operation === "deliver") check(r.status === "DISPATCHED" && eventId(r.vehicle_id) && eventId(r.order_id));
      else check(r.operation === "apply_event" && r.status === "APPLIED" && eventId(r.event_id) && eventType(r.event_type) && typeof r.event_sha256 === "string" && /^[a-f0-9]{64}$/.test(r.event_sha256));
      if (r.status === "NOOP") check(sameBasis(before, after));
      else {
        const rain = r.operation === "apply_event" && r.event_type === "LOCAL_RAIN_WHAT_IF";
        check(BigInt(after.head_version) === BigInt(before.head_version) + 1n && after.head_sha256 !== before.head_sha256 &&
          sameBasis({ ...before, head_version: after.head_version, head_sha256: after.head_sha256, ...(rain ? { overlay_sha256: after.overlay_sha256 } : {}) }, after));
        if (rain) check(typeof after.overlay_sha256 === "string");
      }
    }
  }
  check(r.links.state === `/api/sessions/${after.session_id}/state`);
  return r;
}
export function parseReplayAudit(raw: unknown, sid: string): M3ReplayAudit {
  const v = raw as M3ReplayAudit;
  check(v && typeof v === "object" && v.schema_version === "saferoute-m3-replay-history/1" && v.session_id === sid && Array.isArray(v.history) && v.history.length <= 100);
  v.history.forEach(r => parseReplayReceipt(r, sid));
  check(new Set(v.history.map(r => r.mutation_id)).size === v.history.length);
  return v;
}
export function parseReplayMutation(raw: unknown, sid: string, operation: "advance" | "apply_event", eid?: string): M3ReplayMutationView {
  const v = raw as M3ReplayMutationView;
  check(v && typeof v === "object" && v.schema_version === "saferoute-m3-replay-view/1");
  const receipt = parseReplayReceipt(v.receipt, sid);
  check(receipt.operation === operation && (operation !== "apply_event" || receipt.event_id === eid));
  check(v.execution_view && parseBasis(v.execution_view.basis).session_id === sid && v.execution_view.basis.build_sha256 === receipt.input_basis.build_sha256);
  try { adaptAcceptedExecution(v.execution_view); } catch { check(false); }
  return v;
}
