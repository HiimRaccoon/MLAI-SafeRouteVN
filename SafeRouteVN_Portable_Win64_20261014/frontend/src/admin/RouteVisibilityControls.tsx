import { adminVehicleColor, type AdminMapPresentation } from "./adminMapPresentation";

interface Props {
  vehicleIds: string[];
  visibility: Record<string, boolean>;
  presentation: AdminMapPresentation;
  ordersByVehicle: Record<string, string[]>;
  onToggle(vehicleId: string): void;
}

export function RouteVisibilityControls({ vehicleIds, visibility, presentation, ordersByVehicle, onToggle }: Props) {
  return <section className="route-visibility" aria-label="Route visibility">
    <div className="route-visibility-controls">
    <div className="route-visibility-heading">
    <h3>Route visibility</h3>
    <span className="route-vehicle-count">{vehicleIds.length} vehicles</span>
    <p className="route-visibility-source">
      {presentation.source === "PROPOSED" ? "Proposed route · Preview only" :
        presentation.source === "ACCEPTED" ? presentation.completionAvailable === false ? "Accepted route · Completion unavailable" : "Accepted route · Remaining travel" : "No plan to display yet"}
    </p>
    </div>
    <div className="route-visibility-switches">
    {vehicleIds.map((id) => {
      const enabled = Boolean(visibility[id]);
      return <div key={id} className="route-visibility-vehicle">
        <div className="route-visibility-row">
          <span className="route-visibility-name"><i className="route-leg-swatch" style={{ background: adminVehicleColor(id) }} aria-hidden="true" />{id} Route</span>
          <span className="route-visibility-state">{enabled ? "ON" : "OFF"}</span>
          <button type="button" role="switch" aria-label={`Show ${id} route`} aria-checked={enabled}
            className={`toggle-switch ${enabled ? "toggle-on" : ""}`} onClick={() => onToggle(id)} />
        </div>
      </div>;
    })}
    </div>
    </div>
    <div className="route-visibility-legends" tabIndex={0} aria-label="Đơn hàng cần giao">
    {vehicleIds.filter((id) => visibility[id]).map((id) => {
      const orders = ordersByVehicle[id] ?? [];
      return <div key={id} className="route-visibility-vehicle">
        <span className="route-visibility-legend-name">{id} · Đơn hàng cần giao</span>
        {orders.length > 0 && <ol className="route-leg-legend" aria-label={`${id} đơn hàng cần giao`}>
          {orders.map((orderId) => <li key={orderId}>
            <i className="route-leg-swatch" style={{ background: adminVehicleColor(id) }} aria-hidden="true" />
            <span>{orderId}</span>
          </li>)}
        </ol>}
        {!orders.length && <p className="route-visibility-empty">Không có đơn cần giao</p>}
      </div>;
    })}
    </div>
  </section>;
}
