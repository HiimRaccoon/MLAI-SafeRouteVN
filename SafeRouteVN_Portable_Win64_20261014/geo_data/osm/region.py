"""Validate WGS84 boundaries without silently repairing or dropping islands."""

import math
from pathlib import Path

import requests
from shapely.geometry import Point, mapping, shape
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import polygonize_full
from shapely.validation import explain_validity

from geo_data.common import digest, now_vn, read_json, write_json

# Coverage probes only, not a substitute for an authoritative boundary survey.
HCMC_PROBES = {
    "hcmc_core": (106.700, 10.776),
    "thu_dau_mot": (106.652, 10.980),
    "dau_tieng": (106.360, 11.275),
    "vung_tau": (107.085, 10.350),
    "xuyen_moc": (107.420, 10.535),
    "con_son": (106.607, 8.684),
}


def load_boundary(document):
    if "crs" in document:
        raise ValueError("Supply RFC 7946 WGS84 GeoJSON without a legacy crs member")
    if document.get("type") == "FeatureCollection":
        features = document.get("features", [])
        if len(features) != 1:
            raise ValueError("Boundary must contain exactly one Polygon/MultiPolygon feature")
        document = features[0]
    geometry = document.get("geometry") if document.get("type") == "Feature" else document
    if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("Boundary must be a Polygon or MultiPolygon")
    def check_coordinates(coords):
        if not isinstance(coords, (list, tuple)) or not coords:
            raise ValueError("Empty or invalid coordinates")
        if isinstance(coords[0], (int, float)):
            if len(coords) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float))
                                       or not math.isfinite(v) for v in coords):
                raise ValueError("Boundary coordinates must be finite 2D lon,lat pairs")
            if not (-180 <= coords[0] <= 180 and -90 <= coords[1] <= 90):
                raise ValueError("Coordinate outside WGS84 range")
        else:
            for part in coords:
                check_coordinates(part)
    check_coordinates(geometry.get("coordinates"))
    rings = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    for polygon in rings:
        for ring in polygon:
            if len(ring) < 4 or ring[0] != ring[-1]:
                raise ValueError("Polygon rings must be explicitly closed with at least four positions")
    result = shape(geometry)
    if result.is_empty or not result.is_valid or result.area <= 0:
        raise ValueError(f"Invalid boundary: {explain_validity(result)}")
    if result.bounds[2] - result.bounds[0] > 180:
        raise ValueError("Antimeridian-crossing regions are not supported")
    return result


def hcmc_coverage_checks(geometry):
    return {name: geometry.covers(Point(*point)) for name, point in HCMC_PROBES.items()}


def relation_geometry(relation):
    """Assemble boundary member lines; reject incomplete rings and nested boundary relations."""
    lines = {"outer": [], "inner": []}
    for member in relation.get("members", []):
        role = member.get("role", "")
        if role not in lines:
            if member.get("type") == "way" and role == "":
                raise ValueError("Boundary way has no outer/inner role")
            continue  # label/admin_centre/subarea do not define boundary rings.
        if member.get("type") != "way":
            raise ValueError("Nested boundary relation is not supported; supply verified GeoJSON")
        coords = member.get("geometry")
        if not coords or any(not p or "lon" not in p or "lat" not in p for p in coords):
            raise ValueError("Boundary has incomplete way geometry")
        line = LineString([(p["lon"], p["lat"]) for p in coords])
        if line.is_empty or not line.is_valid:
            raise ValueError("Invalid boundary member geometry")
        lines[role].append(line)
    if not lines["outer"]:
        raise ValueError("Boundary has no outer ways")
    rings = {}
    for role, paths in lines.items():
        polygons, cuts, dangles, invalid = polygonize_full(paths)
        if not cuts.is_empty or not dangles.is_empty or not invalid.is_empty:
            raise ValueError(f"Boundary {role} rings are incomplete or invalid")
        rings[role] = list(polygons.geoms)
        if any(len(p.interiors) for p in rings[role]):
            raise ValueError("Nested same-role boundary rings need explicit review")
    holes = [[] for _ in rings["outer"]]
    for inner in rings["inner"]:
        containers = [i for i, outer in enumerate(rings["outer"]) if outer.contains(inner)]
        if len(containers) != 1:
            raise ValueError("Boundary hole does not have exactly one outer ring")
        holes[containers[0]].append(list(inner.exterior.coords))
    polygons = [Polygon(outer.exterior.coords, holes[i]) for i, outer in enumerate(rings["outer"])]
    geometry = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
    return load_boundary(mapping(geometry))


def fetch_boundary(osm_id, output, *, user_agent, refresh=False, provider="nominatim",
                   endpoint="https://nominatim.openstreetmap.org/lookup"):
    """Single cached lookup. Output remains a candidate pending coverage checks."""
    if not osm_id.startswith("R") or not osm_id[1:].isdigit():
        raise ValueError("Expected a relation ID such as R1973756")
    if not user_agent.strip():
        raise ValueError("A descriptive User-Agent is required")
    if provider not in ("nominatim", "overpass"):
        raise ValueError("Unknown boundary provider")
    output = Path(output)
    if output.exists() and not refresh:
        cached = read_json(output)
        if cached.get("properties", {}).get("osmId") != osm_id:
            raise ValueError("Cached boundary has a different OSM ID")
        if cached.get("properties", {}).get("provider", "nominatim") != provider:
            raise ValueError("Cached boundary has a different provider; choose another output")
        load_boundary(cached)
        return cached
    if provider == "overpass":
        from geo_data.osm.download_graph import DEFAULT_ENDPOINT
        from geo_data.units import timestamp_vn

        query = f'[out:json][timeout:90];relation({int(osm_id[1:])});out meta geom;out count;'
        response = requests.post(DEFAULT_ENDPOINT, data={"data": query},
                                 headers={"User-Agent": user_agent}, timeout=(10, 105))
        response.raise_for_status()
        payload = response.json()
        elements = payload.get("elements", [])
        if payload.get("remark") or len(elements) != 2 or elements[-1].get("type") != "count":
            raise ValueError("Incomplete/ambiguous Overpass boundary response")
        relation = elements[0]
        counts = elements[-1].get("tags", {})
        if relation.get("type") != "relation" or relation.get("id") != int(osm_id[1:]) or counts.get("total") != "1":
            raise ValueError("Overpass returned an unexpected boundary")
        if relation.get("tags", {}).get("boundary") != "administrative":
            raise ValueError("OSM relation is not an administrative boundary")
        geometry = relation_geometry(relation)
        feature = {"type": "Feature", "geometry": mapping(geometry), "properties": {
            "osmId": osm_id, "provider": provider,
            "source": f"https://www.openstreetmap.org/relation/{osm_id[1:]}",
            "endpoint": DEFAULT_ENDPOINT, "query": query,
            "fetchedAt": now_vn(), "sourceTimestamp": timestamp_vn(payload["osm3s"]["timestamp_osm_base"]),
            "osmVersion": relation.get("version"),
            "osmEditedAt": timestamp_vn(relation["timestamp"]) if relation.get("timestamp") else None,
            "displayName": relation.get("tags", {}).get("name"), "tags": relation.get("tags", {}),
            "license": "© OpenStreetMap contributors, ODbL 1.0",
            "boundaryVersion": "osm-" + digest(mapping(geometry))[:16],
            "verification": "candidate", "coverageChecks": hcmc_coverage_checks(geometry),
        }}
        write_json(output, feature)
        return read_json(output)
    response = requests.get(endpoint, params={
        "osm_ids": osm_id, "format": "jsonv2", "polygon_geojson": 1,
        "extratags": 1, "namedetails": 1, "addressdetails": 1,
    }, headers={"User-Agent": user_agent}, timeout=(10, 60))
    response.raise_for_status()
    results = response.json()
    if not isinstance(results, list) or len(results) != 1:
        raise ValueError("Boundary lookup did not return exactly one result")
    result = results[0]
    if result.get("osm_type") != "relation" or str(result.get("osm_id")) != osm_id[1:]:
        raise ValueError("Lookup returned a different OSM object")
    geometry = load_boundary(result.get("geojson", {}))
    feature = {"type": "Feature", "geometry": mapping(geometry), "properties": {
        "osmId": osm_id, "provider": provider, "source": response.url, "fetchedAt": now_vn(),
        "displayName": result.get("display_name"), "tags": result.get("extratags", {}),
        "address": result.get("address", {}), "license": result.get("licence"),
        "boundaryVersion": "osm-" + digest(mapping(geometry))[:16],
        "verification": "candidate", "coverageChecks": hcmc_coverage_checks(geometry),
    }}
    write_json(output, feature)
    return read_json(output)
