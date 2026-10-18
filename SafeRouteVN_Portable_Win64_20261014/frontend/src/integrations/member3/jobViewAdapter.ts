import { Member3Error } from "./errors";
import { parseBasis, sameBasis } from "./revision";
import type { M3JobView, M3ComparisonView, M3ComparisonReceipt } from "./types";

const profiles = ["FASTEST", "BALANCED", "SAFER"];
export const terminalComparison = (view: M3ComparisonView) => ["COMPLETED", "FAILED", "CANCELLED"].includes(view.status);
function check(condition: unknown): asserts condition { if (!condition) throw new Member3Error("INVALID_RESPONSE", "Invalid M3 job/comparison contract or binding."); }
const id = (value: unknown) => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$/.test(value);
const object = (value: unknown) => value !== null && typeof value === "object" && !Array.isArray(value);
export function parseJobView(raw: unknown): M3JobView {
  check(object(raw)); const v = raw as M3JobView;
  const keys = ["schema_version", "job_id", "job_status", "input_basis", "business_status", "internal_status", "diagnostics", "validation", "coverage_evaluated", "served_orders", "unserved_orders", "plan_available", "execution_view_required", "public_api_v1_dynamic_plan_available"];
  check(Object.keys(v).length === keys.length && keys.every(k => k in v));
  check(v.schema_version === "task02-m2-runtime-job-view/1" && id(v.job_id) && ["QUEUED", "RUNNING", "COMPLETED", "FAILED"].includes(v.job_status));
  parseBasis(v.input_basis);
  check(v.execution_view_required === true && v.public_api_v1_dynamic_plan_available === false && typeof v.plan_available === "boolean" && typeof v.coverage_evaluated === "boolean");
  check(Array.isArray(v.diagnostics) && v.diagnostics.every(d => object(d) && [d.code, d.path, d.message].every(x => typeof x === "string" && x.length > 0)));
  check(object(v.validation) && Array.isArray(v.served_orders) && v.served_orders.every(id) && Array.isArray(v.unserved_orders) && v.unserved_orders.every(u => object(u) && id(u.order_id) && typeof u.reason === "string" && u.reason.length > 0));
  const ids = [...v.served_orders, ...v.unserved_orders.map(u => u.order_id)]; check(new Set(ids).size === ids.length);
  if (v.plan_available) {
    check(v.job_status === "COMPLETED" && ["FEASIBLE", "PARTIAL", "RETURN_ONLY"].includes(v.internal_status ?? "") && v.coverage_evaluated);
    check(v.validation.status === "VALIDATED" && v.validation.valid === true && typeof v.validation.validator_version === "string");
    check(v.business_status === (v.internal_status === "RETURN_ONLY" ? "UNSUPPORTED" : v.internal_status));
    if (v.internal_status === "FEASIBLE") check(v.served_orders.length > 0 && v.unserved_orders.length === 0);
    if (v.internal_status === "PARTIAL") check(v.served_orders.length > 0 && v.unserved_orders.length > 0);
  } else {
    check(!v.coverage_evaluated && ids.length === 0 && v.validation.status === "NOT_RUN" && v.validation.valid === null && Object.keys(v.validation).length === 2);
    if (v.job_status === "COMPLETED") {
      check(["NO_SERVICE", "SEARCH_LIMIT", "TIME_LIMIT", "UNSUPPORTED", "INVALID_DATA"].includes(v.internal_status ?? ""));
      check(v.business_status === (v.internal_status === "NO_SERVICE" ? "UNSUPPORTED" : v.internal_status));
    } else check(v.business_status === null && v.internal_status === null);
    if (["COMPLETED", "FAILED"].includes(v.job_status)) check(v.diagnostics.length > 0);
  }
  return v;
}
export function parseComparisonReceipt(raw: unknown): M3ComparisonReceipt {
  check(object(raw)); const v = raw as M3ComparisonReceipt;
  check(v.schema_version === "saferoute-m3-profile-submission/1" && id(v.session_id) && id(v.comparison_id) && ["NEW_BATCH", "EXISTING_JOBS"].includes(v.mode));
  check(parseBasis(v.input_basis).session_id === v.session_id && Array.isArray(v.profiles) && v.profiles.length === 3 && profiles.every(p => v.profiles.includes(p as typeof v.profiles[number])));
  check(object(v.links) && typeof v.links.poll === "string" && typeof v.links.cancel === "string"); return v;
}
export function parseComparisonView(raw: unknown): M3ComparisonView {
  check(object(raw)); const v = raw as M3ComparisonView;
  check(v.schema_version === "saferoute-m3-profile-comparison/1" && id(v.session_id) && id(v.comparison_id) && ["NEW_BATCH", "EXISTING_JOBS"].includes(v.mode));
  check(["QUEUED", "RUNNING", "CANCEL_REQUESTED", "COMPLETED", "FAILED", "CANCELLED"].includes(v.status));
  check(parseBasis(v.input_basis).session_id === v.session_id && v.execution_mode === "SIMULATED_REPLAY" && v.real_world_observation === false && v.metric_scope === "FORECAST_ONLY" && v.exposure_is_proxy === true);
  check(object(v.links) && typeof v.links.poll === "string" && typeof v.links.cancel === "string");
  check(Array.isArray(v.jobs) && v.jobs.length === 3 && new Set(v.jobs.map(j => j.profile)).size === 3);
  const ids: string[] = [];
  for (const row of v.jobs) {
    check(object(row) && profiles.includes(row.profile) && (row.job_id === null || id(row.job_id)));
    if (row.job_id) ids.push(row.job_id);
    if (row.view !== null) {
      const child = parseJobView(row.view); check(child.job_id === row.job_id && child.input_basis.session_id === v.session_id && child.input_basis.build_sha256 === v.input_basis.build_sha256);
      if (v.mode === "NEW_BATCH") check(sameBasis(child.input_basis, v.input_basis));
    }
  }
  check(new Set(ids).size === ids.length);
  if (v.outcome !== null) {
    check(terminalComparison(v) && object(v.outcome) && (v.outcome.reason === null || typeof v.outcome.reason === "string"));
    const c = v.outcome.comparison;
    if (c !== null) {
      check(v.status === "COMPLETED" && object(c) && c.schema_version === "task02-m2-runtime-comparison/1" && ["COMPARABLE", "NON_COMPARABLE"].includes(c.status));
      check(c.reason === (c.status === "COMPARABLE" ? null : "AUTHENTICATED_BASIS_OR_PHYSICAL_DOMAIN_DIFFERS") && v.outcome.reason === c.reason);
      check(Array.isArray(c.jobs) && c.jobs.length === 3);
      for (let i = 0; i < 3; i++) {
        const result = c.jobs[i], child = v.jobs[i];
        check(object(result) && result.job_id === child.job_id && result.profile === child.profile && child.view?.plan_available && child.view.validation.valid === true);
        check(sameBasis(parseBasis(result.basis), child.view.input_basis) && /^[a-f0-9]{64}$/.test(result.domain_sha256));
        check(object(result.metrics) && Object.values(result.metrics).every(x => typeof x === "number" && Number.isFinite(x) && x >= 0));
      }
      if (c.status === "COMPARABLE") check(c.jobs.every(j => sameBasis(j.basis, c.jobs[0].basis) && j.domain_sha256 === c.jobs[0].domain_sha256));
    }
  } else check(!terminalComparison(v));
  return v;
}
export function comparisonCanRank(v: M3ComparisonView): boolean { return v.status === "COMPLETED" && v.outcome?.comparison?.status === "COMPARABLE"; }

/** A finished batch is not necessarily a successful optimization. */
export function comparisonOutcomeSummary(view?: M3ComparisonView) {
  if (!view) return { label: 'Chưa tối ưu', tone: 'neutral', message: '' };
  const count = view.jobs.filter(j => j.view?.plan_available && j.view.validation.valid === true).length;
  if (!terminalComparison(view)) return { label: view.status === 'CANCEL_REQUESTED' ? 'Đang hủy' : 'Đang tối ưu', tone: 'neutral', message: '' };
  if (view.status === 'CANCELLED') return { label: 'Đã hủy', tone: 'neutral', message: '' };
  if (comparisonCanRank(view) && count === 3) return { label: 'Đủ 3 phương án', tone: 'success', message: '' };
  const timedOut = view.jobs.some(j => j.view?.diagnostics.some(d => d.code === 'BUDGET_EXHAUSTED'));
  return {
    label: count === 0 ? 'Chưa có kết quả' : count < 3 ? `Chỉ có ${count}/3 phương án` : 'Không thể so sánh',
    tone: count === 0 ? 'error' : 'warning',
    message: count < 3
      ? `${timedOut ? 'Đã hết thời gian tính toán.' : 'Lượt tối ưu đã kết thúc.'} ${count}/3 phương án có kết quả hợp lệ. Bạn có thể bấm Optimize/Re-optimize để thử lại.`
      : 'Ba phương án không có cùng dữ liệu đầu vào để so sánh.'
  };
}
