"""Deterministic metric grid over all parts of a WGS84 boundary."""

import math
import re

from pyproj import Transformer
from shapely.geometry import box, mapping
from shapely.ops import transform

from geo_data.common import SCHEMA_VERSION, digest, now_vn
from geo_data.osm.region import hcmc_coverage_checks, load_boundary
from geo_data.units import UNITS, nonnegative

GRID_CRS = "EPSG:32648"  # UTM 48N: local metric working CRS, never output units.


def plan_hash(plan):
    return digest({k: v for k, v in plan.items() if k not in ("planVersion", "createdAt")})


def build_plan(boundary_document, *, region_id, source, boundary_version,
               tile_km=5, buffer_km=0.25, scope="hcmc-current", max_tiles=20000):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", region_id):
        raise ValueError("region_id must be a safe identifier")
    if not source.strip() or not boundary_version.strip():
        raise ValueError("Boundary source and version are required")
    tile_km = nonnegative(tile_km, "tileKm")
    buffer_km = nonnegative(buffer_km, "bufferKm")
    if not 0.1 <= tile_km <= 20 or buffer_km > tile_km:
        raise ValueError("tileKm must be 0.1..20 and bufferKm <= tileKm")
    if scope not in ("hcmc-current", "test"):
        raise ValueError("Unknown scope")
    boundary = load_boundary(boundary_document)
    west, south, east, north = boundary.bounds
    if not (96 <= west < east <= 114 and 0 < south < north < 30):
        raise ValueError("This grid implementation is scoped to the HCMC region near UTM 48N")
    checks = hcmc_coverage_checks(boundary)
    if scope == "hcmc-current" and not all(checks.values()):
        missing = [name for name, covered in checks.items() if not covered]
        raise ValueError(f"Boundary fails current HCMC coverage probes: {missing}")
    forward = Transformer.from_crs("EPSG:4326", GRID_CRS, always_xy=True)
    inverse = Transformer.from_crs(GRID_CRS, "EPSG:4326", always_xy=True)
    projected = transform(forward.transform, boundary)
    step, margin = tile_km * 1000, buffer_km * 1000
    features = []
    seen = set()
    parts = list(projected.geoms) if projected.geom_type == "MultiPolygon" else [projected]
    # Iterate per component: avoid scanning ocean between mainland and islands.
    for part in parts:
        minx, miny, maxx, maxy = part.bounds
        columns = range(math.floor(minx / step), math.ceil(maxx / step))
        rows = range(math.floor(miny / step), math.ceil(maxy / step))
        if len(columns) * len(rows) > max_tiles * 20:
            raise ValueError("Grid too fine for this boundary; increase tileKm")
        for col in columns:
            for row in rows:
                if (col, row) in seen:
                    continue
                cell = box(col * step, row * step, (col + 1) * step, (row + 1) * step)
                if not cell.intersects(part) or cell.intersection(part).area == 0:
                    continue
                seen.add((col, row))
                if len(seen) > max_tiles:
                    raise ValueError("Too many tiles; increase tileKm")
                # transform_bounds densifies edges; a small padding avoids round-off clipping.
                left, bottom, right, top = inverse.transform_bounds(
                    col * step - margin, row * step - margin,
                    (col + 1) * step + margin, (row + 1) * step + margin,
                    densify_pts=41,
                )
                bbox = [math.floor(bottom * 1e7) / 1e7, math.floor(left * 1e7) / 1e7,
                        math.ceil(top * 1e7) / 1e7, math.ceil(right * 1e7) / 1e7]
                features.append({"type": "Feature", "geometry": mapping(transform(inverse.transform, cell)),
                                 "properties": {"tileId": f"c{col}_r{row}", "bboxSWNE": bbox}})
    features.sort(key=lambda item: item["properties"]["tileId"])
    if not features:
        raise ValueError("Boundary produced no tiles")
    plan = {"type": "FeatureCollection", "schemaVersion": SCHEMA_VERSION,
            "regionId": region_id, "scope": scope, "boundarySource": source,
            "boundaryVersion": boundary_version, "boundary": mapping(boundary),
            "boundarySha256": digest(mapping(boundary)),
            "coverageChecks": checks, "boundaryVerification": "coverage-probes-only",
            "tileKm": tile_km, "bufferKm": buffer_km, "gridCrs": GRID_CRS,
            "units": UNITS, "createdAt": now_vn(), "features": features}
    plan["planVersion"] = plan_hash(plan)
    return plan


def validate_plan(plan):
    if plan.get("schemaVersion") != SCHEMA_VERSION or plan.get("type") != "FeatureCollection":
        raise ValueError("Unsupported tile plan")
    if plan.get("planVersion") != plan_hash(plan):
        raise ValueError("Tile plan checksum mismatch; regenerate the plan")
    features = plan.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("Tile plan is empty")
    ids = set()
    for feature in features:
        props = feature["properties"]
        tile_id = props["tileId"]
        if not re.fullmatch(r"c-?\d+_r-?\d+", tile_id) or tile_id in ids:
            raise ValueError("Unsafe or duplicate tile ID")
        ids.add(tile_id)
        bbox = props["bboxSWNE"]
        if len(bbox) != 4 or not all(isinstance(n, (int, float)) and math.isfinite(n) for n in bbox):
            raise ValueError("Invalid tile bbox")
        south, west, north, east = bbox
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            raise ValueError("Invalid tile bbox bounds")
    return plan
