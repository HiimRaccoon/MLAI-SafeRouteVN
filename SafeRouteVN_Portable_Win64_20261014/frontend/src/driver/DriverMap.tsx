import { useRef, useState } from "react";
import { CloudRain, Navigation, Plus, Minus, Maximize2, Minimize2 } from "lucide-react";
import type { DispatchSnapshot } from "../shared/types/dispatch";
import { LeafletCanvas, type LeafletCanvasHandle } from "../shared/components/LeafletCanvas";
import { createDriverMapPresentation, type DriverRouteMode } from './driverMapPresentation';

interface DriverMapProps {
  snapshot: DispatchSnapshot;
  vehicleId?: string;
  activePlanId?: string | null;
  currentStopId?: string | null;
  initialMode?: DriverRouteMode;
  expanded?: boolean;
  onToggleExpanded?(): void;
}

export function DriverMap({ snapshot, vehicleId = "V1", initialMode = 'ALL', expanded = false, onToggleExpanded }: DriverMapProps) {
  const handle = useRef<LeafletCanvasHandle | null>(null);
  const [mode, setMode] = useState<DriverRouteMode>(initialMode);
  const { scene, legs } = createDriverMapPresentation(snapshot, vehicleId, mode);
  return <section className="drv-map-card" aria-label="Driver route map">
    <LeafletCanvas scene={scene} mapHandle={handle} driver />
    <label className="drv-route-mode">Route display
      <select aria-label="Route display" value={mode} onChange={event => setMode(event.target.value as DriverRouteMode)}>
        <option value="ALL">All stops · one color</option>
        <option value="NEXT">Next delivery only</option>
        <option value="LEGS">Each leg · different colors</option>
      </select>
    </label>
    {scene.rain && <div className="drv-weather-chip" role="status"><CloudRain size={13} strokeWidth={2} /><span>Simulated local rain</span></div>}
    <div className="drv-map-controls">
      {onToggleExpanded && <button type="button" className="drv-zoom-btn drv-expand-btn" aria-label={expanded ? 'Thu nhỏ bản đồ' : 'Mở rộng bản đồ'} title={expanded ? 'Thu nhỏ bản đồ' : 'Mở rộng bản đồ'} aria-expanded={expanded} onClick={onToggleExpanded}>{expanded ? <Minimize2 size={18}/> : <Maximize2 size={18}/>}</button>}
      <button type="button" className="drv-zoom-btn" onClick={() => handle.current?.zoomIn()} aria-label="Zoom in"><Plus size={16} strokeWidth={2.5} /></button>
      <button type="button" className="drv-zoom-btn" onClick={() => handle.current?.zoomOut()} aria-label="Zoom out"><Minus size={16} strokeWidth={2.5} /></button>
      <button type="button" className="drv-zoom-btn drv-recenter-btn" onClick={() => handle.current?.reset()} aria-label={`Center on ${vehicleId}`}><Navigation size={15} strokeWidth={2.2} /></button>
    </div>
    {scene.accepted.length > 0 && <div className="map-layer-label drv-accepted-route" aria-label="Accepted route" />}
    {scene.rain && <div className="map-layer-label" aria-label="Simulated rain area" />}
    {mode === 'LEGS' && legs.length > 0 && <div className="drv-route-leg-legend" role="list" aria-label="Route legs">
      {legs.map((leg) => <span key={leg.id} role="listitem"><i style={{ background: leg.color }} />{leg.number} → {leg.targetLabel}</span>)}
    </div>}
  </section>;
}
