import type { DispatchSnapshot, ProposedAlternative } from '../shared/types/dispatch';
import { proposalCurrency } from '../integrations/member3/revision';
import { executionMetrics, adaptMetrics, forecastUnits } from '../integrations/member3/metricsAdapter';

export function nativeKpis(snapshot: DispatchSnapshot, selected?: ProposedAlternative) {
  const backend = snapshot.backend!;
  const preview = selected?.nativeForecast && proposalCurrency(selected, snapshot) === 'CURRENT' &&
    !backend.stale && !backend.error ? selected.nativeForecast : undefined;
  const accepted = snapshot.planState.acceptedExecution;
  const view = backend.executionView;
  const projected = executionMetrics(view).projected;
  const planned = executionMetrics(view).planned;
  const routes = (preview?.trajectory ?? view.accepted_trajectory)?.vehicle_routes as Array<Record<string, unknown>> | undefined;
  const totals = Object.fromEntries(Object.keys(forecastUnits).map(key => [key,
    routes?.length && routes.every(r => typeof r[key] === 'number' && Number.isFinite(r[key]))
      ? routes.reduce((sum, r) => sum + Number(r[key]), 0) : null]));
  const remaining = adaptMetrics(totals, 'PLANNED_SUFFIX', forecastUnits);
  const projectedAvailable = Object.values(projected.values).some(v => v !== null);
  const metrics = backend.stale || backend.error ? null : preview?.metrics ?? (accepted ? projectedAvailable ? projected : Object.values(planned.values).some(v => v !== null) ? planned : remaining : null);
  const scope = backend.stale || backend.error ? 'Dữ liệu chưa đồng bộ' : preview ? 'Dự kiến · phương án đang chọn' : accepted ? metrics?.scope === 'PROJECTED_WHOLE' ? 'Đang áp dụng · toàn tuyến' : 'Đang áp dụng · phần còn lại' : 'Chưa chọn kế hoạch';
  const covered = preview ? preview.jobView.served_orders : [...new Set([...view.delivered_prefix, ...view.planned_served_suffix])];
  const epoch = backend.run?.epoch ?? backend.scenarios?.find(s => s.id === snapshot.decisionState.scenarioId)?.initialTime;
  const completion = new Map<string, number>();
  if (epoch && Number.isFinite(Date.parse(epoch))) for (const route of routes ?? []) {
    for (const action of (route.actions ?? []) as Array<Record<string, unknown>>) {
      if (action.kind === 'SERVICE' && typeof action.order_id === 'string' && /^(0|[1-9]\d*)$/.test(String(action.end_us))) {
        completion.set(action.order_id, Date.parse(epoch) + Number(action.end_us) / 1000);
      }
    }
  }
  // The denominator is covered orders, never all orders or an inference from total lateness.
  const deadlines = covered.map(id => {
    const order = snapshot.decisionState.orders.find(o => o.id === id);
    const delivered = !preview && view.delivered_prefix.includes(id) ? order?.deliveredAt : undefined;
    const time = delivered ? Date.parse(delivered) : completion.get(id);
    return order && time !== undefined && Number.isFinite(time) && Number.isFinite(Date.parse(order.preferredDue)) ? time <= Date.parse(order.preferredDue) : null;
  });
  const onTime = !backend.stale && !backend.error && (accepted || preview) && covered.length && deadlines.every(v => v !== null) ? 100 * deadlines.filter(Boolean).length / covered.length : null;
  return { metrics, scope, preview: Boolean(preview), covered: accepted || preview ? covered.length : 0, onTime,
    unavailable: backend.stale || backend.error ? 'Dữ liệu chưa đồng bộ' : !accepted && !preview ? 'Chọn một phương án hoặc Accept để xem' : 'Server chưa cung cấp chỉ số này' };
}
