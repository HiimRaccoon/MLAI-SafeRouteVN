import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { MapScene } from "./mapScene";
import { ConfirmedVehicleMotion } from './vehicleMotion';

const ROUTE_COLORS = ["#16a34a", "#1f6feb", "#f59e0b"];

function latLng([longitude, latitude]: [number, number]): L.LatLngTuple {
  return [latitude, longitude];
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char] ?? char);
}

export interface LeafletCanvasHandle {
  zoomIn(): void;
  zoomOut(): void;
  reset(): void;
}

interface LeafletCanvasProps {
  scene: MapScene;
  mapHandle: React.RefObject<LeafletCanvasHandle | null>;
  driver?: boolean;
  onTileUnavailable?(unavailable: boolean): void;
  onPickPoint?(latitude: number, longitude: number): void;
}

export function LeafletCanvas({ scene, mapHandle, driver = false, onTileUnavailable, onPickPoint }: LeafletCanvasProps) {
  const elementRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const overlayRef = useRef<L.LayerGroup | null>(null);
  const routeLayerRef = useRef<L.LayerGroup | null>(null);
  const routeSignatureRef = useRef('');
  const markerLayersRef = useRef(new Map<string, { marker: L.Marker; iconSignature: string }>());
  const boundsRef = useRef<L.LatLngBounds | null>(null);
  const vehicleMotions = useRef(new Map<string, ConfirmedVehicleMotion>());
  const motionSession = useRef<string | undefined>(undefined);
  const motionClock = useRef<string | undefined>(undefined);
  const motionRoads = useRef<MapScene['accepted']>([]);
  const lastFitKeyRef = useRef<string | null>(null);
  const tileLayerRef = useRef<L.TileLayer | null>(null);
  const retryBasemapRef = useRef<() => void>(() => tileLayerRef.current?.redraw());
  const [tileUnavailable, setTileUnavailable] = useState(false);
  const [zoomRevision, setZoomRevision] = useState(0);
  const fittingRef=useRef(false);
  const pickRef=useRef(onPickPoint);pickRef.current=onPickPoint;
  const onTileUnavailableRef = useRef(onTileUnavailable);
  onTileUnavailableRef.current = onTileUnavailable;

  useEffect(() => {
    if (!elementRef.current) return;
    const map = L.map(elementRef.current, { zoomControl: false, attributionControl: true, preferCanvas: false });
    mapRef.current = map;
    map.setView([10.82, 106.72], 12);
    map.on('click', (event: L.LeafletMouseEvent)=>pickRef.current?.(event.latlng.lat,event.latlng.lng));
    map.on('zoomend',()=>{if(!fittingRef.current)setZoomRevision(v=>v+1);});
    map.attributionControl.setPrefix(false);
    let disposed = false;
    let vectorLayer: { remove(): void } | undefined;
    let basemapAttempt = 0;
    const setUnavailable = (value: boolean) => {
      if (!disposed) { setTileUnavailable(value); onTileUnavailableRef.current?.(value); }
    };
    if (import.meta.env.VITE_REMOTE_DRIVER === '1') {
      const load = async () => {
        const attempt = ++basemapAttempt;
        vectorLayer?.remove(); vectorLayer = undefined;
        try {
          const { attachDriverBasemap } = await import('./driverBasemap');
          if (!disposed && attempt === basemapAttempt) vectorLayer = attachDriverBasemap(map, setUnavailable);
        } catch { if (!disposed && attempt === basemapAttempt) setUnavailable(true); }
      };
      retryBasemapRef.current = () => { void load(); };
      void load();
    } else {
    const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      referrerPolicy: 'strict-origin-when-cross-origin',
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    });
    tileLayerRef.current = tiles;
    tiles.on("tileerror", () => {
      setTileUnavailable(true);
      onTileUnavailableRef.current?.(true);
    });
    tiles.addTo(map);
    tiles.on('tileload', () => setUnavailable(false));
    }
    overlayRef.current = L.layerGroup().addTo(map);
    routeLayerRef.current = L.layerGroup().addTo(overlayRef.current);
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => map.invalidateSize()) : null;
    observer?.observe(elementRef.current);
    const timer = window.setTimeout(() => map.invalidateSize(), 0);
    return () => {
      disposed = true; vectorLayer?.remove();
      window.clearTimeout(timer);
      observer?.disconnect();
      map.remove();
      mapRef.current = null;
      overlayRef.current = null;
      routeLayerRef.current = null;
      routeSignatureRef.current = '';
      markerLayersRef.current.clear();
      boundsRef.current = null;
      lastFitKeyRef.current = null;
      tileLayerRef.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    const overlay = overlayRef.current;
    if (!map || !overlay) return;
    const session = scene.motion?.key ?? scene.viewKey;
    if (motionSession.current !== session || (scene.motion && motionClock.current && scene.motion.clock < motionClock.current)) {
      vehicleMotions.current.clear();
      motionRoads.current = [];
      motionSession.current = session;
    }
    motionClock.current = scene.motion?.clock;
    // Retain the accepted road suffix seen when this plan first appeared. Later
    // native snapshots may omit roads already traversed during the display buffer.
    if (!motionRoads.current.length) motionRoads.current = scene.motion?.segments ?? scene.accepted;
    let frame: number | undefined;
    const moving: { motion: ConfirmedVehicleMotion; marker: L.Marker }[] = [];
    const routeLayer = routeLayerRef.current!;
    const routeSignature = JSON.stringify([scene.accepted, scene.proposed, scene.rain, scene.vehicleColors, driver]);
    const routesChanged = routeSignature !== routeSignatureRef.current;
    if (routesChanged) {
      routeLayer.clearLayers();
      routeSignatureRef.current = routeSignature;
    }
    const allPoints=[...scene.markers.map(m=>latLng(m.coordinates)),...[...scene.accepted,...scene.proposed].flatMap(s=>(s.drawableCoordinates??s.coordinates).map(latLng)),...(scene.rain?.coordinates.flatMap(r=>r.map(p=>latLng([p[0],p[1]])))??[])];
    if(allPoints.length){
      const bounds=L.latLngBounds(allPoints);boundsRef.current=bounds;
      const key=`${driver}:${scene.viewKey??scene.markers.map(m=>m.id).join(',')}`;
      if(key!==lastFitKeyRef.current){fittingRef.current=true;map.fitBounds(bounds,{padding:driver?[28,28]:[48,48],maxZoom:driver?15:14,animate:false});fittingRef.current=false;lastFitKeyRef.current=key;}
    }
    const positions: L.LatLngTuple[] = [];
    const colorByVehicle = new Map<string, string>();
    const colorFor = (id: string) => {
      if (scene.vehicleColors?.[id]) return scene.vehicleColors[id];
      if (!colorByVehicle.has(id)) colorByVehicle.set(id, ROUTE_COLORS[colorByVehicle.size % ROUTE_COLORS.length]);
      return colorByVehicle.get(id)!;
    };
    const renderRoutes = (segments: MapScene["accepted"], proposed: boolean) => {
      const groups = new Map<string, { vehicleId: string; completed: boolean; color?: string; lines: L.LatLngTuple[][] }>();
      for (const segment of segments) {
        const points = (segment.drawableCoordinates ?? segment.coordinates).map(latLng);
        positions.push(...points);
        const completed = !proposed && segment.completed;
        const key = JSON.stringify([segment.vehicleId, completed, segment.legId ?? null, segment.color ?? null]);
        const group = groups.get(key) ?? { vehicleId: segment.vehicleId, completed, color: segment.color, lines: [] };
        group.lines.push(points);
        groups.set(key, group);
      }
      for (const group of groups.values()) {
        // Nested polylines retain every EDGE independently: never join disconnected endpoints.
        const points = group.lines.length === 1 ? group.lines[0] : group.lines;
        L.polyline(points, proposed
          ? { color: group.color ?? colorFor(group.vehicleId), weight: 3.5, opacity: 0.75, dashArray: "7 6" }
          : { color: group.color ?? (driver ? "#2563eb" : colorFor(group.vehicleId)), weight: driver ? 4 : 4.5,
              opacity: group.completed ? 0.35 : 0.9, className: group.completed ? "completed" : "" }
        ).addTo(routeLayer);
      }
    };
    if (routesChanged) {
      renderRoutes(scene.accepted, false);
      renderRoutes(scene.proposed, true);
    }
    if (routesChanged && scene.rain) {
      const rings = scene.rain.coordinates.map((ring) => ring.map((point) => latLng([point[0], point[1]])));
      rings.forEach((ring) => positions.push(...ring));
      L.polygon(rings, { color: '#2563eb', fillColor: '#60a5fa', fillOpacity: 0.12, weight: 2,
        dashArray: '6 5', className: 'rain-bounds', interactive: false })
        .bindTooltip('Vùng mưa', { permanent: true, direction: 'top' }).addTo(routeLayer);
    }
    const markerGroups = new Map<string, typeof scene.markers>();
    const grouped = new Map<string,string>();
    for(const marker of scene.markers) {
      const pixel=map.project(latLng(marker.coordinates));
      const near=scene.markers.find(other=>grouped.has(other.id)&&pixel.distanceTo(map.project(latLng(other.coordinates)))<36);
      grouped.set(marker.id,near?grouped.get(near.id)!:marker.id);
    }
    const badgeWidth = (kind: string) => kind === "vehicle" ? 34 : 24;
    for (const marker of scene.markers) {
      const key = grouped.get(marker.id)!;
      const group = markerGroups.get(key) ?? [];
      group.push(marker);
      markerGroups.set(key, group);
    }
    for (const marker of scene.markers) {
      const coordinate = latLng(marker.coordinates);
      positions.push(coordinate);
      const label = escapeHtml(marker.label);
      const color = marker.unavailable ? "#e5252a" : marker.kind === "depot" ? "#1f6feb" : marker.kind === "vehicle" ? (driver && !scene.vehicleColors?.[marker.id] ? "#1d70f5" : colorFor(marker.id)) : marker.urgent ? "#ef4444" : "#e5252a";
      // Separate coincident badges in screen pixels; geographic positions stay exact.
      const group = markerGroups.get(grouped.get(marker.id)!)!;
      const gap = 8;
      const index = group.indexOf(marker);
      const width = badgeWidth(marker.kind);
      const totalWidth = group.reduce((total, item) => total + badgeWidth(item.kind), 0) + gap * (group.length - 1);
      const precedingWidth = group.slice(0, index).reduce((total, item) => total + badgeWidth(item.kind) + gap, 0);
      const centerX = group.reduce((sum,item)=>sum+map.project(latLng(item.coordinates)).x,0)/group.length;
      const offsetX = centerX-map.project(coordinate).x-totalWidth/2+precedingWidth+width/2;
      const icon = L.divIcon({
        className: "saferoute-map-marker",
        html: `<span class="saferoute-marker saferoute-marker--${marker.kind}" style="background:${color}">${escapeHtml(marker.kind === "order" ? (/^O[0-9]+$/.test(marker.id) ? marker.id.replace(/^O0*/, "") : marker.id.slice(-4)) : marker.kind === "depot" ? "⌂" : marker.id)}</span>`,
        iconSize: marker.kind === "vehicle" ? [34, 22] : [24, 24],
        iconAnchor: [width / 2 - offsetX, marker.kind === "vehicle" ? 11 : 12]
      });
      const markerKey = `${marker.kind}:${marker.id}`;
      const iconSignature = JSON.stringify(icon.options);
      const existing = markerLayersRef.current.get(markerKey);
      const rendered = existing?.marker ?? L.marker(coordinate, { icon, title: marker.label }).addTo(overlay);
      if (!rendered.getTooltip() || rendered.getTooltip()?.options.permanent) {
        rendered.unbindTooltip().bindTooltip(label);
      }
      if (existing && existing.iconSignature !== iconSignature) rendered.setIcon(icon);
      if (existing) {
        rendered.setTooltipContent(label);
        const element = rendered.getElement();
        if (element) element.title = marker.label;
      }
      markerLayersRef.current.set(markerKey, { marker: rendered, iconSignature });
      if (marker.kind !== 'vehicle') rendered.setLatLng(coordinate);
      if (marker.kind === 'vehicle') {
        const motion = vehicleMotions.current.get(marker.id) ?? new ConfirmedVehicleMotion();
        vehicleMotions.current.set(marker.id, motion);
        const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
        const canAnimate = Boolean(scene.motion?.playing && !scene.motion.stoppedVehicleIds.includes(marker.id) && !reduced && !marker.unavailable && !document.hidden);
        const roads = motionRoads.current.filter(s => s.vehicleId === marker.id).map(s => s.coordinates);
        rendered.setLatLng(latLng(motion.observe(marker.coordinates, roads, performance.now(), canAnimate, scene.motion ? Date.parse(scene.motion.clock) : undefined)));
        moving.push({ motion, marker: rendered });
      }
    }
    const visibleMarkers = new Set(scene.markers.map(marker => `${marker.kind}:${marker.id}`));
    for (const [key, item] of markerLayersRef.current) {
      if (!visibleMarkers.has(key)) {
        overlay.removeLayer(item.marker);
        markerLayersRef.current.delete(key);
      }
    }
    // Update Leaflet positions directly: React and route layers do not re-render
    // at animation frequency. One animation loop serves every visible vehicle.
    const animate = (now: number) => {
      if (document.hidden) return;
      let active = false;
      for (const item of moving) {
        const position = item.motion.sample(now);
        if (position) item.marker.setLatLng(latLng(position));
        active ||= item.motion.moving;
      }
      if (active) frame = requestAnimationFrame(animate);
    };
    if (moving.some(item => item.motion.moving)) frame = requestAnimationFrame(animate);
    const visibility = () => {
      if (frame !== undefined) cancelAnimationFrame(frame);
      if (!document.hidden) animate(performance.now());
    };
    document.addEventListener('visibilitychange', visibility);
    return () => {
      if (frame !== undefined) cancelAnimationFrame(frame);
      document.removeEventListener('visibilitychange', visibility);
    };
  }, [scene, driver, zoomRevision]);

  mapHandle.current = {
    zoomIn: () => mapRef.current?.zoomIn(),
    zoomOut: () => mapRef.current?.zoomOut(),
    reset: () => { if (mapRef.current && boundsRef.current) mapRef.current.fitBounds(boundsRef.current, { padding: [32, 32], maxZoom: driver ? 15 : 14 }); }
  };

  return <>
    {/* Keep className stable: Leaflet adds its own classes to this element. */}
    <div ref={elementRef} className={`saferoute-leaflet ${driver ? "saferoute-leaflet--driver" : ""}`} style={tileUnavailable ? { background: "#dce5ed" } : undefined} aria-label={driver ? "Driver route map" : "Dispatch route map"} />
    {tileUnavailable && <p className={driver ? "drv-tile-warning" : "tile-warning"} role="status">Không tải được nền bản đồ. Tuyến và điểm dừng vẫn hiển thị. <button type="button" onClick={()=>{setTileUnavailable(false);onTileUnavailableRef.current?.(false);retryBasemapRef.current();}}>Thử tải lại bản đồ</button></p>}
    {[...scene.accepted, ...scene.proposed].some((segment) => segment.geometrySource !== "MEMBER2_SUPPLIED") &&
      <p className={`route-geometry-notice ${driver ? "route-geometry-notice--driver" : ""}`} role="note">
        Schematic demo routes — do not follow roads.
      </p>}
  </>;
}
