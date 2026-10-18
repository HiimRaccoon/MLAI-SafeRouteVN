// Test-only contract examples; never imported by production.
export const basis = { session_id: "session-test", head_version: "1", generation: "0", build_sha256: "b".repeat(64), root_sha256: "a".repeat(64), head_sha256: "c".repeat(64), source_sha256: "d".repeat(64), context_version: "context-test", overlay_sha256: null };
export function job() {
  return { schema_version: "task02-m2-runtime-job-view/1" as const, job_id: "job-FASTEST", job_status: "COMPLETED" as const, input_basis: basis,
    business_status: "FEASIBLE", internal_status: "FEASIBLE", diagnostics: [], validation: { status: "VALIDATED", valid: true, validator_version: "v1" },
    coverage_evaluated: true, served_orders: ["O1"], unserved_orders: [], plan_available: true, execution_view_required: true as const, public_api_v1_dynamic_plan_available: false as const };
}
export function comparison() {
  return { schema_version: "saferoute-m3-profile-comparison/1", session_id: basis.session_id, comparison_id: "comparison-test", mode: "NEW_BATCH", status: "COMPLETED", input_basis: basis,
    jobs: ["FASTEST", "BALANCED", "SAFER"].map(profile => ({ profile, job_id: `job-${profile}`, view: { ...job(), job_id: `job-${profile}` } })),
    outcome: { reason: null, comparison: { schema_version: "task02-m2-runtime-comparison/1", status: "COMPARABLE", reason: null,
      jobs: ["FASTEST", "BALANCED", "SAFER"].map(profile => ({ profile, job_id: `job-${profile}`, basis, domain_sha256: "e".repeat(64), metrics: { distance_m: 100 } })) } },
    links: { poll: "/poll", cancel: "/cancel" }, execution_mode: "SIMULATED_REPLAY", real_world_observation: false, metric_scope: "FORECAST_ONLY", exposure_is_proxy: true };
}
