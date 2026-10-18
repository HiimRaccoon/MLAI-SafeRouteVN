import type { DispatchSnapshot } from "./types/dispatch";
export interface CustomerPresentation { name: string; phone: string; address: string; timeWindow: string; notes?: string }
export interface DriverPresentation { id: string; name: string; phone: string; vehicleModel: string; licensePlate: string; rating: string; avatar: string }
export interface DepotPresentation { id: string; name: string; address: string; phone: string }
export interface DispatchPresentation {
  demo: boolean;
  getCustomerInfo(id: string): CustomerPresentation;
  getDriverInfo(id: string): DriverPresentation;
  depot: DepotPresentation;
}
const unavailable = "Unavailable";
const serverPresentation: DispatchPresentation = {
  demo: false,
  getCustomerInfo: id => ({ name: id, phone: unavailable, address: unavailable, timeWindow: unavailable }),
  getDriverInfo: id => ({ id, name: id, phone: unavailable, vehicleModel: unavailable, licensePlate: unavailable, rating: unavailable, avatar: id.slice(0, 2) }),
  depot: { id: unavailable, name: "Depot — unavailable", address: unavailable, phone: unavailable }
};
/** Public M3 currently supplies identifiers, not contact or identity metadata. */
export function dispatchPresentation(api: { presentation?: DispatchPresentation }, snapshot: DispatchSnapshot | null): DispatchPresentation {
  const day=snapshot?.backend?.executionView.delivery_day;
  const base=snapshot?.backend?serverPresentation:api.presentation??serverPresentation;
  return {...base,
    getCustomerInfo:id=>{
      const fallback=base.getCustomerInfo(id);
      const customer=day?.customers[id], order=snapshot?.decisionState.orders.find(o=>o.id===id);
      return {...fallback,name:order?.customerName?.trim()||customer?.name||fallback.name,
        phone:order?.customerPhone?.trim()||customer?.phone||fallback.phone,
        address:order?.address?.trim()||(day&&order?`${order.latitude.toFixed(5)}, ${order.longitude.toFixed(5)}`:fallback.address),
        notes:order?.notes?.trim()||fallback.notes,
        timeWindow:day&&order?`${order.earliest} – ${order.hardDeadline}`:fallback.timeWindow};
    },
    getDriverInfo:id=>{
      const fallback=base.getDriverInfo(id), vehicle=snapshot?.decisionState.vehicles.find(v=>v.id===id);
      const name=vehicle?.driverName?.trim()||fallback.name;
      return {...fallback,name,phone:vehicle?.driverPhone?.trim()||fallback.phone,
        licensePlate:vehicle?.licensePlate?.trim()||fallback.licensePlate,
        vehicleModel:vehicle?.vehicleModel?.trim()||fallback.vehicleModel,
        avatar:vehicle?.driverName?.trim()?name.split(/\s+/).map(word=>word[0]).slice(-2).join('').toUpperCase():fallback.avatar};
    },
    depot:day?{id:day.depots[0]?.location_id||'DEPOT',name:day.depots.map(d=>d.location_id).join(', '),address:unavailable,phone:unavailable}:base.depot
  };
}
export function contactHref(phone: string): string | undefined {
  return /^[+\d][\d\s()-]+$/.test(phone) ? `tel:${phone}` : undefined;
}
