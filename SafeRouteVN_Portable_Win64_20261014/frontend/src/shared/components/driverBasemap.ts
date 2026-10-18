import type L from 'leaflet';
import { maplibreGL } from '@maplibre/maplibre-gl-leaflet';
import { setWorkerUrl } from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import 'maplibre-gl/dist/maplibre-gl.css';

setWorkerUrl(workerUrl);
const style = 'https://tiles.openfreemap.org/styles/liberty';
const attribution = '<a href="https://openfreemap.org">OpenFreeMap</a> · © <a href="https://openmaptiles.org">OpenMapTiles</a> · © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';

export function attachDriverBasemap(map: L.Map, status: (unavailable: boolean) => void) {
  const layer = maplibreGL({style, attributionControl: false, interactive: false});
  // Leaflet registers the layer before the plugin constructs WebGL. Make
  // removal safe even when construction throws halfway through onAdd.
  const remove = layer.onRemove;
  layer.onRemove = function (owner) {
    if (this.getMaplibreMap()) return remove.call(this, owner);
    this.getContainer()?.remove();
    return this;
  };
  try { layer.addTo(map); }
  catch (error) { map.removeLayer(layer); throw error; }
  const gl = layer.getMaplibreMap();
  map.attributionControl.addAttribution(attribution);
  const failed = () => status(true);
  const loaded = () => {
    if (gl.isStyleLoaded() && gl.areTilesLoaded()) {
      const features = gl.queryRenderedFeatures().length;
      layer.getCanvas().dataset.basemapFeatures = String(features);
      if (features > 0) status(false);
    }
  };
  gl.on('error', failed); gl.on('idle', loaded);
  return { remove() { gl.off('error', failed); gl.off('idle', loaded); map.removeLayer(layer); map.attributionControl.removeAttribution(attribution); } };
}
