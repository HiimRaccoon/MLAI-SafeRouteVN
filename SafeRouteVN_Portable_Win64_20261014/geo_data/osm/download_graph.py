"""Sequential, resumable acquisition. Output is raw OSM, NOT a routing graph."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
from pathlib import Path
import time

import requests

from geo_data.common import SCHEMA_VERSION, atomic_write, digest, exclusive_lock, now_vn, read_json, write_json
from geo_data.osm.cache import load_response, save_response
from geo_data.osm.tile_index import validate_plan
from geo_data.units import timestamp_vn

DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"


def build_query(bbox, timeout_sec=90, osm_date=None):
    south, west, north, east = bbox
    if not all(isinstance(n, (int, float)) and math.isfinite(n) for n in bbox):
        raise ValueError("Invalid bbox")
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise ValueError("Invalid bbox order: expected south,west,north,east")
    date_clause = ""
    if osm_date:
        normalized = datetime.fromisoformat(timestamp_vn(osm_date)).astimezone(timezone.utc)
        date_clause = f'[date:"{normalized.strftime("%Y-%m-%dT%H:%M:%SZ")}"]'
    bbox_text = ",".join(f"{n:.7f}" for n in bbox)
    return (f"[out:json][timeout:{int(timeout_sec)}]{date_clause};\n"
            f'way["highway"]({bbox_text})->.roads;\n'
            'rel(bw.roads)["type"~"^restriction"]->.restrictions;\n'
            "(.roads;.restrictions;);\n(._;>>;);\nout meta;\nout count;\n")


def validate_osm(payload):
    if not isinstance(payload, dict) or payload.get("remark"):
        raise ValueError("Overpass returned an error/partial response")
    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise ValueError("Missing OSM elements")
    source_timestamp = payload.get("osm3s", {}).get("timestamp_osm_base")
    if not source_timestamp:
        raise ValueError("Missing OSM source timestamp")
    timestamp_vn(source_timestamp)
    objects = {}
    counters = []
    counts = {"node": 0, "way": 0, "relation": 0}
    for element in elements:
        kind = element.get("type")
        if kind == "count":
            counters.append(element.get("tags", {}))
            continue
        if kind not in counts or not isinstance(element.get("id"), int):
            raise ValueError("Invalid OSM element type/id")
        key = (kind, element["id"])
        if key in objects:
            raise ValueError(f"Duplicate OSM object: {key}")
        if not isinstance(element.get("version"), int) or element["version"] < 1:
            raise ValueError(f"Missing OSM version: {key}")
        if kind == "node":
            lat, lon = element.get("lat"), element.get("lon")
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (lat, lon)):
                raise ValueError("Invalid node coordinate")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("Node outside WGS84 bounds")
        objects[key] = element
        counts[kind] += 1
    if len(counters) != 1 or not elements or elements[-1].get("type") != "count":
        raise ValueError("Missing final count marker; response may be truncated")
    expected = {kind + "s": count for kind, count in counts.items()}
    expected["total"] = sum(counts.values())
    if any(str(counters[0].get(name)) != str(value) for name, value in expected.items()):
        raise ValueError("OSM count marker mismatch")
    for key, element in objects.items():
        if key[0] == "way":
            refs = element.get("nodes")
            if not isinstance(refs, list) or len(refs) < 2:
                raise ValueError(f"Way has fewer than two nodes: {key}")
            if any(("node", ref) not in objects for ref in refs):
                raise ValueError(f"Way references missing nodes: {key}")
        if key[0] == "relation":
            members = element.get("members")
            if not isinstance(members, list):
                raise ValueError("Missing relation members")
            if any((member.get("type"), member.get("ref")) not in objects for member in members):
                raise ValueError(f"Relation references missing members: {key}")
    return counts


class OverpassClient:
    def __init__(self, *, endpoint=DEFAULT_ENDPOINT, user_agent, attempts=3,
                 timeout_sec=90, min_interval_sec=2, session=None, sleep=time.sleep):
        if not endpoint.startswith("https://") or not user_agent.strip():
            raise ValueError("HTTPS endpoint and descriptive User-Agent required")
        if attempts < 1 or attempts > 5 or timeout_sec < 1 or not math.isfinite(min_interval_sec) or min_interval_sec < 1:
            raise ValueError("Invalid retries/timeout/interval")
        self.endpoint, self.user_agent = endpoint, user_agent
        self.attempts, self.timeout_sec = attempts, timeout_sec
        self.min_interval_sec, self.session, self.sleep = min_interval_sec, session or requests.Session(), sleep
        self.last_request = None

    def fetch(self, query):
        for attempt in range(self.attempts):
            if self.last_request is not None:
                remaining = self.min_interval_sec - (time.monotonic() - self.last_request)
                if remaining > 0:
                    self.sleep(remaining)
            self.last_request = time.monotonic()
            retry_after = 0
            try:
                response = self.session.post(self.endpoint, data={"data": query},
                                             headers={"User-Agent": self.user_agent},
                                             timeout=(10, self.timeout_sec + 15))
                if response.status_code in (408, 429, 500, 502, 503, 504):
                    retry_after = 30
                    header = response.headers.get("Retry-After")
                    if header:
                        try:
                            retry_after = max(retry_after, float(header))
                        except ValueError:
                            try:
                                retry_after = max(retry_after, (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
                            except (TypeError, ValueError):
                                pass
                    response.raise_for_status()
                response.raise_for_status()
                payload = response.json()
                validate_osm(payload)
                return payload
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
                status = exc.response.status_code if getattr(exc, "response", None) is not None else None
                if status is not None and status not in (408, 429, 500, 502, 503, 504):
                    raise
                if attempt + 1 == self.attempts:
                    raise
                self.sleep(max(retry_after, 2 ** attempt * 5))


def download_tiles(plan, output, *, client=None, endpoint=DEFAULT_ENDPOINT, offline=False,
                   refresh=False, max_tiles=None, tile_ids=None, osm_date=None, progress=print):
    validate_plan(plan)
    if offline and refresh:
        raise ValueError("offline and refresh cannot be combined")
    if max_tiles is not None and max_tiles < 1:
        raise ValueError("max_tiles must be positive")
    if not offline and client is None:
        raise ValueError("Online downloads require a client")
    endpoint = client.endpoint if client else endpoint
    output = Path(output)
    features = plan["features"]
    ids = {f["properties"]["tileId"] for f in features}
    tile_positions = {f["properties"]["tileId"]: index
                      for index, f in enumerate(features, start=1)}
    total_tiles = len(features)
    if tile_ids and not set(tile_ids) <= ids:
        raise ValueError("Unknown tile ID")
    selected = [f for f in features if not tile_ids or f["properties"]["tileId"] in tile_ids]
    # max_tiles counts missing/refresh attempts, not verified cache hits, so resumes advance.
    with exclusive_lock(output):
        manifest_path = output / "manifest.json"
        identity = {"planVersion": plan["planVersion"], "endpoint": endpoint, "osmDate": osm_date}
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            if manifest.get("identity") != identity:
                raise ValueError("Output belongs to a different plan/source/date; use a new directory")
        else:
            manifest = {"schemaVersion": SCHEMA_VERSION, "identity": identity, "createdAt": now_vn(),
                        "regionId": plan["regionId"], "scope": plan["scope"],
                        "sourceType": "REAL-derived", "routingReady": False,
                        "attribution": "© OpenStreetMap contributors, ODbL 1.0",
                        "tiles": {tile_id: {"status": "pending"} for tile_id in sorted(ids)}}

        def completion_text():
            completed = sum(record["status"] == "complete" for record in manifest["tiles"].values())
            return f"completed: {completed}/{total_tiles} ({completed / total_tiles:.1%})"

        def tile_progress(tile_id, message):
            progress(f"[tile {tile_positions[tile_id]}/{total_tiles}] {tile_id}: "
                     f"{message} | {completion_text()}")

        progress(f"Plan: {total_tiles} tiles | selected: {len(selected)} | {completion_text()}")
        attempted = 0
        for feature in selected:
            props = feature["properties"]
            tile_id = props["tileId"]
            query = build_query(props["bboxSWNE"], osm_date=osm_date)
            request_key = digest({"query": query, "endpoint": endpoint})
            directory = output / "raw_tiles" / tile_id
            old_status = manifest["tiles"][tile_id]
            try:
                cached = None if refresh else load_response(directory, request_key)
                if cached:
                    payload, record = cached
                    counts = validate_osm(payload)
                    # A previously failed refresh must not silently fall back to an old snapshot.
                    if old_status.get("refreshFailed"):
                        raise ValueError("Previous refresh failed; retry with --refresh")
                    manifest["tiles"][tile_id] = dict(record, status="complete", counts=counts, cacheHit=True)
                else:
                    if offline:
                        raise ValueError("Offline cache missing")
                    if max_tiles is not None and attempted >= max_tiles:
                        continue
                    attempted += 1
                    manifest["tiles"][tile_id] = {"status": "in_progress", "refreshFailed": refresh}
                    manifest["complete"] = False
                    write_json(manifest_path, manifest)
                    atomic_write(directory / "query.overpassql", query.encode("utf-8"))
                    tile_progress(tile_id, "downloading...")
                    payload = client.fetch(query)
                    counts = validate_osm(payload)
                    record = save_response(directory, payload, {
                        "requestKey": request_key, "fetchedAt": now_vn(), "endpoint": endpoint,
                        "sourceTimestamp": timestamp_vn(payload["osm3s"]["timestamp_osm_base"]),
                        "bboxSWNE": props["bboxSWNE"], "osmDate": osm_date,
                    })
                    manifest["tiles"][tile_id] = dict(record, status="complete", counts=counts, cacheHit=False)
                tile_progress(tile_id, f"complete (cache={manifest['tiles'][tile_id]['cacheHit']})")
            except (ValueError, OSError, requests.RequestException) as exc:
                manifest["tiles"][tile_id] = {"status": "failed", "error": str(exc),
                                               "refreshFailed": refresh or old_status.get("refreshFailed", False)}
                tile_progress(tile_id, f"failed: {exc}")
                # Stop on network failure instead of hammering every remaining tile.
                manifest["complete"] = False
                manifest["updatedAt"] = now_vn()
                write_json(manifest_path, manifest)
                break
            manifest["complete"] = all(v["status"] == "complete" for v in manifest["tiles"].values())
            manifest["updatedAt"] = now_vn()
            write_json(manifest_path, manifest)
        manifest["complete"] = all(v["status"] == "complete" for v in manifest["tiles"].values())
        manifest["summary"] = {state: sum(v["status"] == state for v in manifest["tiles"].values())
                               for state in ("pending", "in_progress", "complete", "failed")}
        manifest["updatedAt"] = now_vn()
        write_json(manifest_path, manifest)
        return manifest
