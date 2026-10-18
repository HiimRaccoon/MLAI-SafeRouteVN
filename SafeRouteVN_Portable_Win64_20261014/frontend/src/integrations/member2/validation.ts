import type { Wgs84Point } from "./types";

export function record(value: unknown, path: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`Invalid ${path}`);
  return value as Record<string, unknown>;
}

export function string(value: unknown, path: string): string {
  if (typeof value !== "string" || value.length === 0) throw new Error(`Invalid ${path}`);
  return value;
}

export function finite(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`Invalid ${path}`);
  return value;
}

export function list(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`Invalid ${path}`);
  return value;
}

export function stringList(value: unknown, path: string): string[] {
  return list(value, path).map((item, index) => string(item, `${path}[${index}]`));
}

export function point(value: unknown, path: string): Wgs84Point {
  if (!Array.isArray(value) || value.length !== 2) throw new Error(`Invalid ${path} geometry`);
  const [longitude, latitude] = value;
  if (typeof longitude !== "number" || !Number.isFinite(longitude) || Math.abs(longitude) > 180 ||
      typeof latitude !== "number" || !Number.isFinite(latitude) || Math.abs(latitude) > 90) {
    throw new Error(`Invalid ${path} geometry`);
  }
  return [longitude, latitude];
}

export function line(value: unknown, path: string): Wgs84Point[] {
  const coordinates = list(value, `${path} geometry`).map((item, index) => point(item, `${path}[${index}]`));
  if (coordinates.length < 2) throw new Error(`Invalid ${path} geometry`);
  return coordinates;
}
