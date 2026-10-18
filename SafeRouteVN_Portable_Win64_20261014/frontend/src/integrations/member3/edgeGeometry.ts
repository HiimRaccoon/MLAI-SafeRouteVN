type Point = [number, number];

/** M2 member1_motion_replay.interpolate_directed_geometry: haversine chain lengths,
 * linear longitude/latitude interpolation within each directed source segment.
 * Only the drawable interval changes; the original source array stays intact.
 */
export function drawableEdgeCoordinates(points: Point[], start: number, end: number): Point[] {
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || start > end || end > 1) throw new Error("Invalid drawable EDGE fraction");
  if (start === 0 && end === 1) return points.map(p => [...p]);
  const radians = (n: number) => n * Math.PI / 180;
  const lengths = points.slice(1).map((right, i) => {
    const left = points[i], deltaLon = radians(right[0] - left[0]), deltaLat = radians(right[1] - left[1]);
    const h = Math.sin(deltaLat / 2) ** 2 + Math.cos(radians(left[1])) * Math.cos(radians(right[1])) * Math.sin(deltaLon / 2) ** 2;
    return 6371008.8 * 2 * Math.asin(Math.min(1, Math.sqrt(h)));
  });
  const total = lengths.reduce((sum, n) => sum + n, 0);
  if (!Number.isFinite(total) || total <= 0) throw new Error("Partial EDGE geometry is degenerate");
  const from = start * total, to = end * total;
  const interpolate = (target: number): Point => {
    let cursor = 0;
    for (const [i, length] of lengths.entries()) {
      if (target <= cursor + length || i === lengths.length - 1) {
        const local = length === 0 ? 0 : Math.max(0, Math.min(1, (target - cursor) / length));
        return [points[i][0] + local * (points[i + 1][0] - points[i][0]), points[i][1] + local * (points[i + 1][1] - points[i][1])];
      }
      cursor += length;
    }
    return [...points[points.length - 1]];
  };
  const result = [interpolate(from)]; let cursor = 0;
  for (const [i, length] of lengths.entries()) {
    cursor += length;
    if (cursor > from && cursor < to) result.push([...points[i + 1]]);
  }
  result.push(interpolate(to));
  return result;
}
