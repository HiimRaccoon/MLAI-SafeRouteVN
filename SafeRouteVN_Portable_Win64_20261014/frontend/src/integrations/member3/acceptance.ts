import { Member3Error } from "./errors";
import { parseBasis, sameBasis } from "./revision";
import { adaptAcceptedExecution } from "./executionViewAdapter";
import type { M3AcceptanceReceipt, M3AcceptanceView, M3AcceptanceAudit } from "./types";

function check(condition: unknown): asserts condition {
  if (!condition) throw new Member3Error("INVALID_RESPONSE", "Invalid M3 acceptance contract or binding.");
}
const object = (value: unknown) => value !== null && typeof value === "object" && !Array.isArray(value);
const id = (value: unknown) => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$/.test(value);
export function parseAcceptanceReceipt(raw: unknown, sid: string): M3AcceptanceReceipt {
  check(object(raw)); const r = raw as M3AcceptanceReceipt;
  check(r.schema_version === "saferoute-m3-plan-acceptance/1" && r.session_id === sid && id(r.acceptance_id) && id(r.job_id) && r.status === "ACCEPTED");
  const input = parseBasis(r.input_basis), basis = parseBasis(r.basis);
  check(input.session_id === sid && sameBasis({ ...input, generation: basis.generation }, basis) && BigInt(basis.generation) === BigInt(input.generation) + 1n);
  check(typeof r.recorded_at === "string" && /(?:Z|[+-]\d\d:\d\d)$/.test(r.recorded_at) && Number.isFinite(Date.parse(r.recorded_at)));
  check(object(r.links) && r.links.state === `/api/sessions/${sid}/state` && r.links.job === `/api/sessions/${sid}/jobs/${r.job_id}`);
  return r;
}
export function parseAcceptanceView(raw: unknown, sid: string, jid: string): M3AcceptanceView {
  check(object(raw)); const v = raw as M3AcceptanceView;
  check(v.schema_version === "saferoute-m3-acceptance-view/1");
  const receipt = parseAcceptanceReceipt(v.receipt, sid);
  check(receipt.job_id === jid && object(v.execution_view));
  const basis = parseBasis(v.execution_view.basis);
  check(basis.session_id === sid && basis.build_sha256 === receipt.input_basis.build_sha256);
  // Receipt is historical: current active job and counters may have changed again.
  try { adaptAcceptedExecution(v.execution_view); }
  catch { throw new Member3Error("INVALID_RESPONSE", "Invalid acceptance execution view."); }
  return v;
}
export function parseAcceptanceAudit(raw: unknown, sid: string): M3AcceptanceAudit {
  check(object(raw)); const v = raw as M3AcceptanceAudit;
  check(v.schema_version === "saferoute-m3-acceptance-audit/1" && v.session_id === sid && Array.isArray(v.acceptances) && v.acceptances.length <= 100);
  v.acceptances.forEach(r => parseAcceptanceReceipt(r, sid));
  check(new Set(v.acceptances.map(r => r.acceptance_id)).size === v.acceptances.length);
  return v;
}
