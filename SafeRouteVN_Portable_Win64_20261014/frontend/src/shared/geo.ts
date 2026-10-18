export type Point = { latitude: number; longitude: number };
export function distanceKm(a: Point, b: Point): number {
  const r = Math.PI / 180, x = (b.latitude - a.latitude) * r, y = (b.longitude - a.longitude) * r;
  return 12742 * Math.asin(Math.min(1, Math.sqrt(Math.sin(x / 2) ** 2 + Math.cos(a.latitude * r) * Math.cos(b.latitude * r) * Math.sin(y / 2) ** 2)));
}
export const atStop = (a: Point, b: Point) => distanceKm(a, b) <= 0.025;
export function alongLine(coordinates: [number, number][], fraction: number): Point {
  const points = coordinates.map(([longitude, latitude]) => ({ longitude, latitude }));
  const lengths = points.slice(1).map((p, i) => distanceKm(points[i], p));
  let remaining = lengths.reduce((a, b) => a + b, 0) * Math.max(0, Math.min(1, fraction));
  for (let i = 0; i < lengths.length; i++) {
    if (remaining <= lengths[i] && lengths[i] > 0) {
      const t = remaining / lengths[i];
      return { latitude: points[i].latitude + t * (points[i + 1].latitude - points[i].latitude), longitude: points[i].longitude + t * (points[i + 1].longitude - points[i].longitude) };
    }
    remaining -= lengths[i];
  }
  return points.at(-1)!;
}
