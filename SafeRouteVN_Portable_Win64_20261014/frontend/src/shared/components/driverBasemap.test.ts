import { afterEach, expect, it, vi } from 'vitest';
import L from 'leaflet';
import { attachDriverBasemap } from './driverBasemap';
vi.mock('maplibre-gl', () => ({setWorkerUrl: vi.fn()}));
vi.mock('@maplibre/maplibre-gl-leaflet', () => ({maplibreGL: () => {
  const container = document.createElement('div');
  return new (L.Layer.extend({
    onAdd(map: L.Map) {map.getPane('tilePane')!.appendChild(container);throw new Error('WebGL unavailable');},
    onRemove() {throw new Error('Broken layer cleanup');},
    getEvents() {return {zoom: () => {throw new Error('Abandoned layer event');}};},
    getMaplibreMap() {return undefined;},
    getContainer() {return container;},
  }))();
}}));
afterEach(()=>vi.restoreAllMocks());
it('cleans a partially initialized WebGL layer before retry or unmount', () => {
  const element=document.createElement('div');document.body.appendChild(element);
  const map=L.map(element).setView([10.8,106.7],12);
  try {
    expect(()=>attachDriverBasemap(map,()=>{})).toThrow('WebGL unavailable');
    let count=0;map.eachLayer(()=>count++);expect(count).toBe(0);
    expect(map.getPane('tilePane')!.children).toHaveLength(0);
    expect(()=>map.fire('zoom')).not.toThrow();
  } finally { try {map.remove();} catch {} element.remove(); }
});
