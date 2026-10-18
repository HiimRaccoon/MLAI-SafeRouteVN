"""Versioned delivery polygons; they never clip the graph used for routing."""

from datetime import timezone
import json
from pathlib import Path
import re
import time

import requests
from shapely.geometry import Point, mapping
from shapely.prepared import prep

from geo_data.bundle import instant
from geo_data.common import digest, exclusive_lock, now_vn, read_json, write_json
from geo_data.osm.download_graph import DEFAULT_ENDPOINT
from geo_data.osm.region import load_boundary, relation_geometry

DEFAULT_AREA_CONFIG = Path(__file__).with_name("areas") / "thu_duc_binh_thanh.json"


class DeliveryArea:
    def __init__(self, document):
        if document.get("type") != "FeatureCollection" or "crs" in document:
            raise ValueError("Delivery area must be a WGS84 FeatureCollection")
        properties = document.get("properties", {})
        if not properties.get("areaId") or not properties.get("source"):
            raise ValueError("Delivery area requires areaId and source provenance")
        regions = {}
        for feature in document.get("features", []):
            meta = feature.get("properties", {})
            rid = meta.get("regionId")
            if not isinstance(rid, str) or not rid or rid in regions or not meta.get("name"):
                raise ValueError("Delivery regions require unique regionId and name")
            regions[rid] = (meta["name"], prep(load_boundary(feature)))
        if not regions:
            raise ValueError("Delivery area has no polygons")
        self.document, self.regions = document, dict(sorted(regions.items()))
        self.version = digest(document)
        self.summary = {"areaId": properties["areaId"], "version": self.version,
                        "regions": [{"regionId": rid, "name": value[0]} for rid, value in self.regions.items()],
                        "selection": "polygon-with-minimum-depot-distance", "routingScope": "unmodified-input-graph"}

    def region_id(self, longitude, latitude):
        point = Point(longitude, latitude)
        return next((rid for rid, (_, polygon) in self.regions.items() if polygon.covers(point)), None)

    def covers(self, longitude, latitude):
        return self.region_id(longitude, latitude) is not None


def scenario_area(directory, manifest):
    version = manifest.get("deliveryAreaVersion")
    if version is None:
        return None
    if "delivery_area.geojson" not in manifest["files"]:
        raise ValueError("Scenario bundle is missing its delivery polygon")
    area = DeliveryArea(read_json(Path(directory) / "delivery_area.geojson"))
    if area.version != version:
        raise ValueError("Scenario delivery-area version mismatch")
    return area


def validate_area_membership(scenario, area):
    expected = area.version if area else None
    if scenario.get("deliveryAreaVersion") != expected:
        raise ValueError("Scenario and delivery-area versions differ")
    if area is None:
        return
    state = scenario["initialState"]
    points = [*state["locations"], *state["orders"], *(v["currentPosition"] for v in state["vehicles"])]
    points.extend(event["orderPayload"] for event in scenario["events"] if "orderPayload" in event)
    if scenario.get("tradeoffEvidence"):
        points.append(scenario["tradeoffEvidence"])
    for point in points:
        rid = area.region_id(point["longitude"], point["latitude"])
        if rid is None or ("deliveryRegionId" in point and point["deliveryRegionId"] != rid):
            raise ValueError("Scenario depot/order/vehicle is outside its delivery area or mislabeled")


def area_query(config):
    south, west, north, east = config["searchBboxSWNE"]
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise ValueError("Invalid delivery area search bbox")
    date = instant(config["osmDate"]).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    names = [name for region in config["regions"] for name in region["names"]]
    if not names or any(not isinstance(name, str) or not name for name in names):
        raise ValueError("Delivery area lookup requires explicit names")
    regex = json.dumps("^(" + "|".join(re.escape(name) for name in names) + ")$", ensure_ascii=False)
    level = str(config["adminLevel"])
    if not level.isdigit():
        raise ValueError("adminLevel must be numeric")
    selectors = "".join(f'relation["boundary"="administrative"]["admin_level"="{level}"]["{tag}"~{regex},i]'
                        f'({south},{west},{north},{east});' for tag in ("name", "name:vi", "name:en"))
    return f'[out:json][timeout:120][date:"{date}"];({selectors});out meta geom;out count;'


def normalize_area(payload, config, *, endpoint, query):
    elements = payload.get("elements", [])
    if payload.get("remark") or not elements or elements[-1].get("type") != "count":
        raise ValueError("Incomplete Overpass delivery-area response")
    relations = elements[:-1]
    if (len(relations) != len(config["regions"]) or
            elements[-1].get("tags", {}).get("total") != str(len(relations))):
        raise ValueError("Expected exactly one historical boundary per delivery region; inspect source or supply GeoJSON")
    features, used = [], set()
    for region in config["regions"]:
        aliases = {name.casefold() for name in region["names"]}
        matches = [r for r in relations if r.get("type") == "relation"
                   and r.get("tags", {}).get("boundary") == "administrative"
                   and r.get("tags", {}).get("admin_level") == str(config["adminLevel"])
                   and any(r.get("tags", {}).get(tag, "").casefold() in aliases for tag in ("name", "name:vi", "name:en"))]
        if len(matches) != 1 or matches[0]["id"] in used:
            raise ValueError(f"Missing or ambiguous boundary: {region['regionId']}")
        relation = matches[0]
        used.add(relation["id"])
        geometry = relation_geometry(relation)
        if not geometry.covers(Point(*region["probeLonLat"])):
            raise ValueError(f"Boundary fails reference-point check: {region['regionId']}")
        features.append({"type": "Feature", "geometry": mapping(geometry), "properties": {
            "regionId": region["regionId"], "name": region["label"], "osmId": f"R{relation['id']}",
            "osmVersion": relation.get("version"), "osmEditedAt": relation.get("timestamp"),
            "osmTags": relation["tags"], "source": f"https://www.openstreetmap.org/relation/{relation['id']}",
            "probeLonLat": region["probeLonLat"], "probePassed": True}})
    document = {"type": "FeatureCollection", "properties": {"areaId": config["areaId"],
        "source": "OpenStreetMap historical administrative boundary via Overpass", "sourceType": "REAL-derived",
        "osmDate": instant(config["osmDate"]).isoformat(), "fetchedAt": now_vn(),
        "endpoint": endpoint, "query": query, "lookupConfigHash": digest(config),
        "rawPayloadSha256": digest(payload), "verification": "candidate-needs-boundary-review",
        "scope": "Historical delivery planning areas, not current administrative certification",
        "license": "© OpenStreetMap contributors, ODbL 1.0"}, "features": features}
    DeliveryArea(document)
    return document


def fetch_delivery_area(output, *, user_agent, config_path=DEFAULT_AREA_CONFIG,
                        endpoint=DEFAULT_ENDPOINT, attempts=3, session=requests, sleep=time.sleep, progress=print):
    if not user_agent.strip() or not 1 <= attempts <= 5 or not endpoint.startswith("https://"):
        raise ValueError("User-Agent, HTTPS endpoint and 1..5 attempts required")
    output = Path(output)
    if output.suffix.lower() != ".geojson":
        raise ValueError("Delivery area output must use the .geojson extension")
    config = read_json(config_path)
    query = area_query(config)
    with exclusive_lock(output.parent):
        if output.exists():
            document = read_json(output)
            area = DeliveryArea(document)
            props = document["properties"]
            raw = read_json(output.with_suffix(".raw.json"))
            if (props.get("lookupConfigHash") != digest(config) or props.get("endpoint") != endpoint
                    or props.get("query") != query or props.get("rawPayloadSha256") != digest(raw)):
                raise ValueError("Delivery area cache/config mismatch; choose a new output")
            reconstructed = normalize_area(raw, config, endpoint=endpoint, query=query)
            reconstructed["properties"]["fetchedAt"] = props["fetchedAt"]
            if digest(reconstructed) != area.version:
                raise ValueError("Delivery area cache geometry/provenance mismatch")
            progress(f"Delivery area cache verified: {output}")
            return document
        payload = None
        for attempt in range(attempts):
            progress(f"Delivery boundary HTTP attempt {attempt + 1}/{attempts} | OSM date={config['osmDate']}")
            try:
                response = session.post(endpoint, data={"data": query}, headers={"User-Agent": user_agent}, timeout=(15, 150))
                response.raise_for_status()
                payload = response.json()
                if payload.get("remark"):
                    raise requests.ConnectionError(f"Incomplete Overpass response: {payload['remark']}")
                break
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if attempt + 1 == attempts or (code is not None and code < 500 and code != 429):
                    raise
                sleep(2 ** (attempt + 1))
        document = normalize_area(payload, config, endpoint=endpoint, query=query)
        write_json(output.with_suffix(".raw.json"), payload)
        write_json(output, document)
        progress(f"Delivery area saved: {output}; inspect the two polygons before accepting the boundary")
        return read_json(output)
