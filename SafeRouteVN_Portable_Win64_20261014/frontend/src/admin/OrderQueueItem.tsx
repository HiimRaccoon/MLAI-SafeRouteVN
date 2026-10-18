import { ChevronDown } from 'lucide-react';
import type { CustomerPresentation } from '../shared/presentation';
import type { Order } from '../shared/types/scenario';
import { LABELS as L } from './admin.labels';

const areas: Record<string, string> = { 'thu-duc': 'Thủ Đức', 'binh-thanh': 'Bình Thạnh' };
const statuses = { WAITING: 'Waiting for pickup', ONBOARD: 'On board', DELIVERED: 'Delivered' };
const clock = new Intl.DateTimeFormat('vi-VN', { timeZone: 'Asia/Ho_Chi_Minh', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const timestamp = new Intl.DateTimeFormat('vi-VN', { timeZone: 'Asia/Ho_Chi_Minh', day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const shortTimestamp = new Intl.DateTimeFormat('vi-VN', { timeZone: 'Asia/Ho_Chi_Minh', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const available = (value?: string) => Boolean(value && value !== 'Unavailable');
const time = (value: string | null) => value && Number.isFinite(Date.parse(value)) ? timestamp.format(new Date(value)) : 'Not yet';
const shortTime = (value: string) => Number.isFinite(Date.parse(value)) ? shortTimestamp.format(new Date(value)) : 'Unavailable';

export function OrderQueueItem({ order, info }: { order: Order; info: CustomerPresentation }) {
  const urgent = order.priority >= 3;
  const area = areas[order.deliveryRegionId] || order.deliveryRegionId || 'Delivery point';
  const customer = available(info.name) && info.name !== order.id ? info.name : null;
  const window = [order.earliest, order.hardDeadline].every(value => Number.isFinite(Date.parse(value)))
    ? `${clock.format(new Date(order.earliest))}–${clock.format(new Date(order.hardDeadline))}` : 'Time unavailable';
  return <details name="order-queue" className={`order-card ${urgent ? 'order-card-urgent' : ''}`}>
    <summary className={`order-row ${urgent ? 'row-urgent' : ''}`} aria-label={`Order ${order.id} details`}>
      <span className="order-id-badge">{order.id}</span>
      <span className="customer-name" title={area}>{area}</span>
      <span className="order-time" title={`${time(order.earliest)} – ${time(order.hardDeadline)} (+07:00)`}>{window}</span>
      <span className={urgent ? 'priority-badge urgent' : 'priority-badge normal'}>{urgent ? L.priorityUrgent : L.priorityNormal}</span>
      <ChevronDown className="order-expand-icon" size={13} aria-hidden="true" />
    </summary>
    <div className="order-expanded" role="region" aria-label={`Details for ${order.id}`}>
      <div className="order-quick-facts">
        <span className={`order-status order-status-${order.status.toLowerCase()}`}>{statuses[order.status]}</span>
        <span>{order.demandKg} kg</span>
        <span>{Number((order.serviceTimeHours * 60).toFixed(2))} min / stop</span>
      </div>
      <dl className="order-info-grid">
        <div><dt>Vehicle</dt><dd>{order.assignedVehicleId || 'Not assigned'}</dd></div>
        <div><dt>Pickup</dt><dd>{order.pickupLocationId}</dd></div>
        <div><dt>Preferred</dt><dd title={time(order.preferredDue)}>{shortTime(order.preferredDue)}</dd></div>
        <div><dt>Deadline</dt><dd title={time(order.hardDeadline)}>{shortTime(order.hardDeadline)}</dd></div>
      </dl>
      <details className="order-extra">
        <summary aria-label={`More details for ${order.id}`}><span>More details</span><span className="order-timezone">UTC+7</span></summary>
        <dl className="order-info-grid">
        <div className="order-info-wide"><dt>Delivery window</dt><dd>{time(order.earliest)} → {time(order.hardDeadline)}</dd></div>
        {customer && <div className="order-info-wide"><dt>Customer</dt><dd>{customer}</dd></div>}
        {available(info.phone) && <div className="order-info-wide"><dt>Phone</dt><dd>{info.phone}</dd></div>}
        {available(info.address) && <div className="order-info-wide"><dt>Address</dt><dd>{info.address}</dd></div>}
        <div className="order-info-wide"><dt>Coordinates</dt><dd>{order.latitude.toFixed(6)}, {order.longitude.toFixed(6)}</dd></div>
        {order.pickedUpAt && <div><dt>Picked up</dt><dd>{time(order.pickedUpAt)}</dd></div>}
        {order.deliveredAt && <div><dt>Delivered</dt><dd>{time(order.deliveredAt)}</dd></div>}
        {info.notes && <div className="order-info-wide"><dt>Notes</dt><dd>{info.notes}</dd></div>}
        </dl>
      </details>
    </div>
  </details>;
}
