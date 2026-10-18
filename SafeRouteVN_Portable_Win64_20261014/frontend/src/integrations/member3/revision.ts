import type { M3Basis, M3JobView } from "./types";
import type { DispatchSnapshot, ProposedAlternative } from "../../shared/types/dispatch";
import { Member3Error } from "./errors";

export interface ServerRevision { headVersion: string; generation: string }
const fields = ["session_id", "head_version", "generation", "build_sha256", "root_sha256", "head_sha256", "source_sha256", "context_version", "overlay_sha256"] as const;
export function parseBasis(raw: unknown): M3Basis {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Member3Error("INVALID_RESPONSE", "Missing M3 basis.");
  const b = raw as M3Basis;
  for (const field of fields) {
    const value = b[field];
    const valid = field === "overlay_sha256" && value === null || typeof value === "string" && value.length > 0 &&
      (field === "head_version" || field === "generation" ? /^(0|[1-9]\d*)$/.test(value) && BigInt(value) < (1n << 63n) :
        field.endsWith("sha256") ? /^[a-f0-9]{64}$/.test(value) : true);
    if (!valid) throw new Member3Error("INVALID_RESPONSE", `Invalid M3 basis ${field}.`);
  }
  return b;
}
export function sameBasis(a: M3Basis, b: M3Basis): boolean { return fields.every(field => a[field] === b[field]); }
export function expectedRevision(b: M3Basis) { parseBasis(b); return { head_version: b.head_version, generation: b.generation }; }
export function isAcceptableJob(job: M3JobView, current: M3Basis): boolean {
  return job.job_status === "COMPLETED" && job.plan_available && job.coverage_evaluated && job.validation.valid === true &&
    job.validation.status === "VALIDATED" && sameBasis(job.input_basis, current);
}
/** The UI and command boundary share the same guard; the server still owns CAS. */
export function canAcceptSelectedPlan(snapshot: DispatchSnapshot): boolean {
  const backend = snapshot.backend;
  const proposal = snapshot.planState.proposedAlternatives.find(p => p.id === snapshot.planState.selectedAlternativeId);
  if (!backend) return Boolean(proposal && proposalCurrency(proposal, snapshot) === "CURRENT");
  const origin = proposal?.origin, job = origin?.kind === "MEMBER3" ? backend.jobs?.[origin.jobId] : undefined;
  return Boolean(!backend.stale && !backend.error && !backend.mutationPending && backend.capabilities?.accept &&
    proposal && origin?.kind === "MEMBER3" && proposal.id === origin.jobId &&
    origin.comparisonId === backend.comparison?.comparison_id &&
    backend.comparison.jobs.some(row => row.job_id === origin.jobId && row.profile === origin.profile) &&
    proposal.nativeForecast?.jobView.job_id === origin.jobId && job && job.job_id === origin.jobId &&
    proposalCurrency(proposal, snapshot) === "CURRENT" && isAcceptableJob(job, backend.basis) &&
    isAcceptableJob(proposal.nativeForecast.jobView, backend.basis));
}
export function proposalCurrency(proposal: ProposedAlternative, snapshot: DispatchSnapshot): "CURRENT" | "STALE" {
  if (snapshot.backend) return proposal.origin?.kind === "MEMBER3" && proposal.origin.sessionId === snapshot.backend.basis.session_id &&
    sameBasis(proposal.origin.inputBasis, snapshot.backend.basis) ? "CURRENT" : "STALE";
  return proposal.generatedForSessionId === snapshot.decisionState.sessionId && proposal.generatedForStateVersion === snapshot.decisionState.version ? "CURRENT" : "STALE";
}
