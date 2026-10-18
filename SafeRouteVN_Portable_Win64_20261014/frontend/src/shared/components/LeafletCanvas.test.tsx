import { createRef } from "react";
import { act, render, screen } from "@testing-library/react";
import L from "leaflet";
import { describe, expect, it, vi } from "vitest";
import { LeafletCanvas, type LeafletCanvasHandle } from "./LeafletCanvas";
import type { MapScene } from "./mapScene";

describe("supplied route geometry rendering", () => {
  it('shows one accurate rain boundary and clears it without a second bounding overlay', () => {
    const rectangle = vi.spyOn(L, 'rectangle');
    const drawPolygon = vi.spyOn(L, 'polygon');
    const polygon: MapScene['rain'] = {type:'Polygon',coordinates:[[[106.7,10.8],[106.72,10.81],[106.71,10.82],[106.7,10.8]]]};
    const scene: MapScene = {accepted:[],proposed:[],markers:[],rain:polygon};
    const handle = createRef<LeafletCanvasHandle>();
    try {
      const view = render(<LeafletCanvas scene={scene} mapHandle={handle}/>);
      expect(rectangle).not.toHaveBeenCalled();
      expect(drawPolygon).toHaveBeenCalledTimes(1);
      expect(drawPolygon.mock.calls[0][0]).toEqual([[[10.8,106.7],[10.81,106.72],[10.82,106.71],[10.8,106.7]]]);
      expect(view.container.querySelector('.rain-bounds')).toBeInTheDocument();
      expect(screen.getByText('Vùng mưa')).toBeInTheDocument();
      view.rerender(<LeafletCanvas scene={{...scene,rain:null}} mapHandle={handle}/>);
      expect(view.container.querySelector('.rain-bounds')).not.toBeInTheDocument();
    } finally {rectangle.mockRestore();drawPolygon.mockRestore();}
  });
  it("batches by Admin leg color without mixing legs or accepted/proposed styles", () => {
    const original = L.polyline;
    const layers: L.Polyline[] = [];
    const spy = vi.spyOn(L, "polyline").mockImplementation((...args) => {
      const layer = original(...args); layers.push(layer); return layer;
    });
    const scene: MapScene = { accepted: [
      { id: "e1", vehicleId: "V1", completed: false, geometrySource: "MEMBER2_SUPPLIED", legId: "leg1", color: "#2563eb", coordinates: [[106.7, 10.8], [106.701, 10.801]] },
      { id: "e2", vehicleId: "V1", completed: false, geometrySource: "MEMBER2_SUPPLIED", legId: "leg1", color: "#2563eb", coordinates: [[106.71, 10.81], [106.711, 10.811]] },
      { id: "e3", vehicleId: "V1", completed: false, geometrySource: "MEMBER2_SUPPLIED", legId: "leg2", color: "#06b6d4", coordinates: [[106.72, 10.82], [106.721, 10.821]] }
    ], proposed: [{ id: "p1", vehicleId: "V1", completed: false, geometrySource: "MEMBER2_SUPPLIED", legId: "leg1", color: "#2563eb", coordinates: [[106.73, 10.83], [106.731, 10.831]] }], markers: [], rain: null };
    try {
      render(<LeafletCanvas scene={scene} mapHandle={createRef<LeafletCanvasHandle>()} />);
      expect(layers).toHaveLength(3);
      expect(layers[0].toGeoJSON().geometry.coordinates).toEqual([scene.accepted[0].coordinates, scene.accepted[1].coordinates]);
      expect(layers.map((layer) => layer.options.color)).toEqual(["#2563eb", "#06b6d4", "#2563eb"]);
      expect(layers[0].options.dashArray).toBeNull();
      expect(layers[2].options.dashArray).toBe("7 6");
    } finally { spy.mockRestore(); }
  });
  it("batches road edges by vehicle and completion style while preserving independent full polylines", () => {
    const original = L.polyline;
    const layers: L.Polyline[] = [];
    const spy = vi.spyOn(L, "polyline").mockImplementation((...args) => {
      const layer = original(...args);
      layers.push(layer);
      return layer;
    });
    const scene: MapScene = { accepted: [
      { id: "a1", vehicleId: "V1", completed: false, geometrySource: "MEMBER2_SUPPLIED", coordinates: [[106.7, 10.8], [106.701, 10.802], [106.703, 10.803]] },
      // Deliberately disconnected: batching must not create a line between these edges.
      { id: "a2", vehicleId: "V1", completed: false, geometrySource: "MEMBER2_SUPPLIED", coordinates: [[106.72, 10.82], [106.724, 10.825], [106.725, 10.829]] },
      { id: "a3", vehicleId: "V1", completed: true, geometrySource: "MEMBER2_SUPPLIED", coordinates: [[106.73, 10.83], [106.734, 10.839]] },
      { id: "a4", vehicleId: "V2", completed: false, geometrySource: "MEMBER2_SUPPLIED", coordinates: [[106.74, 10.84], [106.744, 10.849]] }
    ], proposed: [
      { id: "p1", vehicleId: "V1", completed: false, geometrySource: "MEMBER2_SUPPLIED", coordinates: [[106.75, 10.85], [106.754, 10.859]] },
      { id: "p2", vehicleId: "V1", completed: false, geometrySource: "MEMBER2_SUPPLIED", coordinates: [[106.76, 10.86], [106.764, 10.869]] }
    ], markers: [], rain: null };
    try {
      render(<LeafletCanvas scene={scene} mapHandle={createRef<LeafletCanvasHandle>()} />);
      expect(layers).toHaveLength(4);
      expect(layers[0].toGeoJSON().geometry).toEqual({ type: "MultiLineString", coordinates: [scene.accepted[0].coordinates, scene.accepted[1].coordinates] });
      expect(layers[1].options.opacity).toBe(0.35);
      expect(layers[2].options.color).not.toBe(layers[0].options.color);
      expect(layers[3].options.dashArray).toBe("7 6");
      expect(layers[3].toGeoJSON().geometry).toEqual({ type: "MultiLineString", coordinates: scene.proposed.map((s) => s.coordinates) });
    } finally {
      spy.mockRestore();
    }
  });
  it("shows coincident depot and vehicle badges without moving their geographic positions", () => {
    const original = L.marker;
    const markers: L.Marker[] = [];
    const spy = vi.spyOn(L, "marker").mockImplementation((...args) => {
      const marker = original(...args);
      markers.push(marker);
      return marker;
    });
    try {
      render(<LeafletCanvas scene={{ accepted: [], proposed: [], rain: null, markers: [
        { id: "DEPOT", kind: "depot", coordinates: [106.7, 10.8], label: "Depot" },
        { id: "V1", kind: "vehicle", coordinates: [106.7, 10.8], label: "V1" },
        { id: "V2", kind: "vehicle", coordinates: [106.7, 10.8], label: "V2" },
        { id: "V3", kind: "vehicle", coordinates: [106.8, 10.9], label: "V3" }
      ] }} mapHandle={createRef<LeafletCanvasHandle>()} />);
      expect(markers.slice(0, 3).map((marker) => marker.getLatLng())).toEqual([
        L.latLng(10.8, 106.7), L.latLng(10.8, 106.7), L.latLng(10.8, 106.7)
      ]);
      const badgeBoxes = markers.slice(0, 3).map((marker) => {
        const options = marker.getIcon().options;
        const anchor = L.point(options.iconAnchor!);
        const size = L.point(options.iconSize!);
        return { left: -anchor.x, right: size.x - anchor.x };
      }).sort((a, b) => a.left - b.left);
      expect(badgeBoxes[0].right).toBeLessThan(badgeBoxes[1].left);
      expect(badgeBoxes[1].right).toBeLessThan(badgeBoxes[2].left);
      expect(markers[3].getIcon().options.iconAnchor).toEqual([17, 11]);
    } finally {
      spy.mockRestore();
    }
  });
  it("keeps Leaflet container classes when switching to tile-failure fallback", () => {
    const original = L.tileLayer;
    let tiles: L.TileLayer | undefined;
    const spy = vi.spyOn(L, "tileLayer").mockImplementation((...args) => {
      tiles = original(...args);
      return tiles;
    });
    try {
      render(<LeafletCanvas scene={{ accepted: [], proposed: [], markers: [], rain: null }} mapHandle={createRef<LeafletCanvasHandle>()} />);
      const map = screen.getByLabelText("Dispatch route map");
      expect(map).toHaveClass("leaflet-container");
      act(() => { tiles!.fire("tileerror"); });
      expect(screen.getByRole("status")).toHaveTextContent("Không tải được nền bản đồ");
      expect(map).toHaveClass("leaflet-container");
    } finally {
      spy.mockRestore();
    }
  });
  it("preserves full accepted and proposed polylines independently in Leaflet", () => {
    const original = L.polyline;
    const layers: L.Polyline[] = [];
    const spy = vi.spyOn(L, "polyline").mockImplementation((...args) => {
      const layer = original(...args);
      layers.push(layer);
      return layer;
    });
    const scene: MapScene = {
      accepted: [{ id: "edge-a", vehicleId: "V1", completed: false, geometrySource: "MEMBER2_SUPPLIED",
        coordinates: [[106.7, 10.8], [106.702, 10.801], [106.705, 10.803], [106.71, 10.81]] }],
      proposed: [{ id: "proposal-b", vehicleId: "V1", completed: false, geometrySource: "MEMBER2_SUPPLIED",
        coordinates: [[106.72, 10.82], [106.721, 10.823], [106.724, 10.825], [106.73, 10.83]] }],
      markers: [], rain: null
    };
    try {
      render(<LeafletCanvas scene={scene} mapHandle={createRef<LeafletCanvasHandle>()} />);
      expect(layers).toHaveLength(2);
      expect(layers[0].toGeoJSON().geometry.coordinates).toEqual([[106.7, 10.8], [106.702, 10.801], [106.705, 10.803], [106.71, 10.81]]);
      expect(layers[1].toGeoJSON().geometry.coordinates).toEqual([[106.72, 10.82], [106.721, 10.823], [106.724, 10.825], [106.73, 10.83]]);
      expect(layers[0].options.dashArray).toBeNull();
      expect(layers[1].options.dashArray).toBe("7 6");
      expect(screen.queryByRole("note")).not.toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });
});


describe('Leaflet playback animation', () => {
  it('moves the marker between snapshots, retains motion on refresh and settles on Pause or a new plan', () => {
    let now = 0;
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => now);
    const frames = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(() => 1);
    const cancel = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {});
    const markers: L.Marker[] = [], original = L.marker;
    const markerSpy = vi.spyOn(L, 'marker').mockImplementation((...args) => {
      const marker = original(...args); markers.push(marker); return marker;
    });
    const road: MapScene['accepted'] = [{ id:'road', vehicleId:'V1', completed:false, geometrySource:'MEMBER2_SUPPLIED', coordinates:[[106.7,10.8],[106.71,10.8],[106.71,10.81]] }];
    const scene = (coordinates: [number,number], playing=true, key='session:plan'): MapScene => ({
      accepted: road, proposed: [], rain: null, viewKey:key,
      markers:[{id:'V1',kind:'vehicle',label:'V1',coordinates}],
      motion:{key,clock: coordinates[0] === 106.7 ? '2026-09-27T21:00:00+07:00' : '2026-09-27T21:01:00+07:00',playing,stoppedVehicleIds:[],segments:road}
    });
    const handle = createRef<LeafletCanvasHandle>();
    try {
      const view = render(<LeafletCanvas scene={scene([106.7,10.8])} mapHandle={handle}/>);
      now = 2500;
      view.rerender(<LeafletCanvas scene={scene([106.71,10.81])} mapHandle={handle}/>);
      expect(markers.at(-1)!.getLatLng().lng).toBeCloseTo(106.7);
      act(() => frames.mock.calls.at(-1)![0](4062.5));
      expect(markers.at(-1)!.getLatLng().lng).toBeGreaterThan(106.7);
      expect(markers.at(-1)!.getLatLng().lat).toBeGreaterThanOrEqual(10.8);
      const animatedPosition = markers.at(-1)!.getLatLng();
      now = 4062.5;
      view.rerender(<LeafletCanvas scene={scene([106.71,10.81])} mapHandle={handle}/>);
      expect(markers.at(-1)!.getLatLng()).toEqual(animatedPosition);
      expect(markerSpy).toHaveBeenCalledTimes(1);
      view.rerender(<LeafletCanvas scene={scene([106.71,10.81],false)} mapHandle={handle}/>);
      expect(markers.at(-1)!.getLatLng().lat).toBeCloseTo(10.81);
      view.rerender(<LeafletCanvas scene={scene([106.7,10.8],true,'session:new-plan')} mapHandle={handle}/>);
      expect(markers.at(-1)!.getLatLng().lng).toBeCloseTo(106.7);
      view.unmount();
      expect(cancel).toHaveBeenCalled();
    } finally { clock.mockRestore(); frames.mockRestore(); cancel.mockRestore(); markerSpy.mockRestore(); }
  });
});
