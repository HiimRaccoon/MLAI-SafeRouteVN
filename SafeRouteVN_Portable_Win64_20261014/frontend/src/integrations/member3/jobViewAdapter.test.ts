import { describe, expect, it } from "vitest";
import { parseJobView, parseComparisonView, comparisonCanRank, comparisonOutcomeSummary } from "./jobViewAdapter";
import { job, comparison } from "./testFixtures";

describe("job contracts", () => {
  it('does not present a completed batch with failed children as success', () => {
    const view = parseComparisonView(comparison());
    expect(comparisonOutcomeSummary(view).tone).toBe('success');
    view.outcome = { comparison: null, reason: 'NO_CERTIFIED_WITNESS_FOR_EVERY_PROFILE' };
    for (const child of view.jobs) child.view = { ...child.view!, plan_available: false, job_status: 'FAILED',
      validation: { status: 'NOT_RUN', valid: null }, diagnostics: [{code:'BUDGET_EXHAUSTED',path:'job',message:'No witness'}] };
    expect(comparisonOutcomeSummary(view)).toMatchObject({label:'Chưa có kết quả',tone:'error'});
    expect(comparisonOutcomeSummary(view).message).toContain('0/3');
    expect(comparisonOutcomeSummary(view).message).toContain('hết thời gian');
    view.jobs[1].view = job();
    expect(comparisonOutcomeSummary(view)).toMatchObject({label:'Chỉ có 1/3 phương án',tone:'warning'});
    view.status = 'RUNNING'; view.outcome = null;
    expect(comparisonOutcomeSummary(view).label).toBe('Đang tối ưu');
  });
  it("distinguishes_lifecycle_from_business_outcome", () => {
    for (const status of ["QUEUED", "RUNNING", "FAILED", "COMPLETED"]) {
      const raw = { ...job(), job_status: status, plan_available: false, coverage_evaluated: false, served_orders: [],
        business_status: status === "COMPLETED" ? "TIME_LIMIT" : null, internal_status: status === "COMPLETED" ? "TIME_LIMIT" : null,
        validation: { status: "NOT_RUN", valid: null }, diagnostics: [{ code: status === "FAILED" ? "JOB_CANCELLED" : "TIME_LIMIT", path: "job", message: "No witness" }] };
      expect(parseJobView(raw)).toMatchObject({ job_status: status, plan_available: false, served_orders: [], unserved_orders: [] });
    }
    for (const internal of ["PARTIAL", "RETURN_ONLY"]) {
      const raw = { ...job(), internal_status: internal, business_status: internal === "RETURN_ONLY" ? "UNSUPPORTED" : internal,
        served_orders: internal === "RETURN_ONLY" ? [] : ["O1"], unserved_orders: [{ order_id: "O2", reason: "LIMIT" }] };
      expect(parseJobView(raw).internal_status).toBe(internal);
    }
    expect(() => parseJobView({ ...job(), job_status: "RUNNING" })).toThrow();
    expect(() => parseJobView({ ...job(), validation: { status: "VALIDATED", valid: false } })).toThrow();
  });
  it("comparison_verdict_controls_ranking", () => {
    expect(comparisonCanRank(parseComparisonView(comparison()))).toBe(true);
    const raw = comparison();
    raw.outcome.comparison.status = "NON_COMPARABLE";
    raw.outcome.comparison.reason = "AUTHENTICATED_BASIS_OR_PHYSICAL_DOMAIN_DIFFERS" as never;
    raw.outcome.reason = raw.outcome.comparison.reason;
    expect(comparisonCanRank(parseComparisonView(raw))).toBe(false);
    expect(comparisonCanRank(parseComparisonView({ ...comparison(), outcome: { comparison: null, reason: "NO_CERTIFIED_WITNESS_FOR_EVERY_PROFILE" } }))).toBe(false);
  });
  it("rejects child/session/profile/domain/metric binding corruption", () => {
    const raw = comparison();
    raw.jobs[0].view.input_basis = { ...raw.input_basis, generation: "1" };
    expect(() => parseComparisonView(raw)).toThrow();
    expect(() => parseComparisonView({ ...comparison(), jobs: [] })).toThrow();
    const metrics = comparison(); metrics.outcome.comparison.jobs[0].metrics.distance_m = NaN;
    expect(() => parseComparisonView(metrics)).toThrow();
  });
});
