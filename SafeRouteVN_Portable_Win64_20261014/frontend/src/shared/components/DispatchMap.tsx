import { useRef } from "react";
import { CloudRain, Maximize2 } from "lucide-react";
import type { DispatchSnapshot, ProposedAlternative } from "../types/dispatch";
import type { MapScene } from "./mapScene";
import { createAdminMapPresentation } from "../../admin/adminMapPresentation";
import { LeafletCanvas, type LeafletCanvasHandle } from "./LeafletCanvas";

interface DispatchMapProps {
  snapshot: DispatchSnapshot;
  proposed?: ProposedAlternative;
  interactive?: boolean;
  scene?: MapScene;
  onPickPoint?(latitude: number, longitude: number): void;
}

export function DispatchMap({ snapshot, proposed, scene: suppliedScene, onPickPoint }: DispatchMapProps) {
  const handle = useRef<LeafletCanvasHandle | null>(null);
  const scene = suppliedScene ?? createAdminMapPresentation(snapshot, proposed).scene;
  return <section className="dispatch-map" aria-label="Dispatch map">
    <LeafletCanvas scene={scene} mapHandle={handle} onPickPoint={onPickPoint} />
    <div className="map-top-bar">
      <div className="map-view-switcher" role="group" aria-label="Map type">
        <button type="button" className="map-mode-btn active">Map</button>
      </div>
      {scene.rain && <div className="weather-alert-badge" role="status"><CloudRain size={13} strokeWidth={2} /><span>Simulated local rain</span></div>}
      <div className="map-zoom-controls">
        <button type="button" className="zoom-btn" onClick={() => handle.current?.zoomIn()} aria-label="Zoom in">+</button>
        <button type="button" className="zoom-btn" onClick={() => handle.current?.zoomOut()} aria-label="Zoom out">−</button>
        <button type="button" className="zoom-btn zoom-reset" onClick={() => handle.current?.reset()} aria-label="Reset view"><Maximize2 size={10} strokeWidth={2.5} /></button>
      </div>
    </div>
    {scene.accepted.length > 0 && <div className="map-layer-label" aria-label="Accepted route" />}
    {scene.proposed.length > 0 && <div className="map-layer-label" aria-label="Proposed — not dispatched" />}
    {scene.rain && <div className="map-layer-label" aria-label="Simulated rain area" />}
    <div className="map-legend" aria-label="Map legend">
      <span className="legend-item"><i className="legend-icon legend-depot" />Depot</span>
      <span className="legend-item"><i className="legend-icon legend-customer" />Customer stop</span>
      <span className="legend-item"><i className="legend-icon legend-vehicle-pill" />Vehicle</span>
      <span className="legend-item"><i className="legend-line legend-planned" />Planned route</span>
      {scene.rain && <span className="legend-item"><i className="legend-icon legend-rain-icon" />Rain area</span>}
    </div>
  </section>;
}
