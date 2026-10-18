import type { DispatchSnapshot, ProposedAlternative } from '../shared/types/dispatch';
import { formatMetric } from '../integrations/member3/metricsAdapter';
import { nativeKpis } from './nativeKpis';
export function NativeKpiCards({snapshot, selected}: {snapshot: DispatchSnapshot; selected?: ProposedAlternative}) {
  const data = nativeKpis(snapshot, selected);
  const value = (key: string, unit?: string) => data.metrics ? formatMetric(data.metrics, key, unit) : '—';
  const rows = [
    ['Fleet travel time', value('total_travel_time_s', 'min'), data.unavailable],
    ['On-time · covered orders', data.onTime === null ? '—' : `${data.onTime.toFixed(1)}%`, 'Chưa đủ thời điểm hoàn thành và hạn giao; tính theo preferred due'],
    ['Route cost (est.)', value('total_cost_vnd'), data.unavailable],
    ['Exposure proxy', value('total_exposure'), data.unavailable]
  ];
  return <>{rows.map(([label, text, reason]) => <div className="kpi-card kpi-card--native" key={label} aria-label={label} title={`${data.scope}. ${text === '—' ? reason : label === 'On-time · covered orders' ? 'So với preferred due, chỉ các đơn được phục vụ' : 'Ước tính từ kế hoạch backend'}`}>
    <span className="kpi-label">{label}</span>
    <div className={`kpi-value-row${label === 'Route cost (est.)' ? ' kpi-value-row--money' : ''}`}><strong>{text}</strong></div>
  </div>)}</>;
}
