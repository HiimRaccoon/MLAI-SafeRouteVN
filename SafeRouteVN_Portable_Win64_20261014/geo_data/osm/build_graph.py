"""Build a directed multigraph preview from one verified PBF snapshot, offline.

SQLite is the canonical graph; CSV exports are streamed. Turn restrictions are
preserved for the next routing integration, never silently treated as enforced.
"""

from collections import Counter
import csv
import json
import math
import os
from pathlib import Path
import sqlite3
import time

import osmium
from pyproj import Geod

from geo_data.common import digest, exclusive_lock, now_vn, read_json, write_json
from geo_data.osm.access_policy import (DEFAULT_POLICY, load_policy, node_block_reason,
                                      way_decision, width_metres)
from geo_data.osm.extract_download import file_hash
from geo_data.osm.extract_filter import validate_pbf

BUILDER_VERSION = "motorcycle-graph-preview/1"
GEOD = Geod(ellps="WGS84")
ARTIFACTS = ("graph.sqlite", "nodes.csv", "edges.csv", "components.csv", "policy_audit.csv",
             "restrictions.json", "qa_report.json", "policy.json")
NODE_COLUMNS = ("nodeId", "longitude", "latitude", "componentId", "tagsJson")
EDGE_COLUMNS = ("edgeId", "fromNodeId", "toNodeId", "edgeKey", "osmWayId", "startIndex",
                "endIndex", "direction", "lengthKm", "widthM", "highway", "geometryJson",
                "osmNodeIdsJson", "missingFlagsJson")


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def partial(output, name):
    path = Path(name)
    return output / f"{path.stem}.partial{path.suffix}"


def restriction_record(relation):
    tags = dict(relation.tags)
    members = [{"type": m.type, "ref": m.ref, "role": m.role} for m in relation.members]
    keys = ("restriction:motorcycle", "restriction:motor_vehicle", "restriction:vehicle", "restriction")
    exceptions = {v.strip() for v in tags.get("except", "").split(";")}
    modes = {"motorcycle", "motor_vehicle", "vehicle"}
    conditional = any(key + ":conditional" in tags for key in keys)
    key = next((key for key in keys if key in tags), None)
    value = tags.get(key, "")
    if exceptions & modes:
        status = "except-motorcycle"
    elif not key and not conditional and any(k.startswith("restriction:") for k in tags):
        status = "other-mode-only"
    elif conditional or "@" in value:
        status = "pending-conditional"
    elif any(m["role"] == "via" and m["type"] == "w" for m in members):
        status = "pending-via-way"
    elif (sorted((m["type"], m["role"]) for m in members) ==
          [("n", "via"), ("w", "from"), ("w", "to")] and
          value in {"no_left_turn", "no_right_turn", "no_straight_on", "no_u_turn",
                    "only_left_turn", "only_right_turn", "only_straight_on", "only_u_turn"}):
        status = "pending-via-node"
    else:
        status = "pending-unrecognized"
    return {"relationId": relation.id, "osmVersion": relation.version, "tags": tags,
            "members": members, "status": status, "enforced": False}


def export_table(db, output, name, columns, query):
    with partial(output, name).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(db.execute(query))


def component_report(db):
    """Weak connectivity only: no inference about one-way/turn reachability."""
    parent, size, minimum = {}, {}, {}

    def find(node):
        if node not in parent:
            parent[node] = minimum[node] = node
            size[node] = 1
        root = node
        while root != parent[root]:
            root = parent[root]
        while node != root:
            following = parent[node]
            parent[node] = root
            node = following
        return root

    for u, v in db.execute("SELECT fromNodeId,toNodeId FROM edges"):
        a, b = find(u), find(v)
        if a != b:
            if size[a] < size[b]:
                a, b = b, a
            parent[b] = a
            size[a] += size[b]
            minimum[a] = min(minimum[a], minimum[b])
    db.executemany("UPDATE nodes SET componentId=? WHERE nodeId=?",
                   ((minimum[find(node)], node) for node in parent))
    db.execute("CREATE INDEX nodes_component ON nodes(componentId)")
    return list(db.execute("""
        SELECT componentId,COUNT(*),MIN(longitude),MIN(latitude),MAX(longitude),MAX(latitude)
        FROM nodes GROUP BY componentId ORDER BY COUNT(*) DESC,componentId
    """))


def build_graph(source, output, *, policy_path=DEFAULT_POLICY, scope="hcmc-current", progress=print):
    source, output = Path(source), Path(output)
    if scope not in ("hcmc-current", "test"):
        raise ValueError("Unknown graph scope")
    policy = load_policy(policy_path)
    source_sha = file_hash(source)
    provenance_path = source.parent / "manifest.json"
    provenance = read_json(provenance_path) if provenance_path.exists() else None
    if provenance:
        if (provenance.get("file") != source.name or provenance.get("sha256") != source_sha or
                provenance.get("complete") is not True or provenance.get("referenceComplete") is not True or
                provenance.get("scope") != scope):
            raise ValueError("Source does not match a complete filtered-PBF manifest for this scope")
    elif scope != "test":
        raise ValueError("City graph requires the verified extract-city manifest next to the source")
    identity = {"sourceSha256": source_sha, "policySha256": digest(policy), "scope": scope,
                "builderVersion": BUILDER_VERSION, "provenanceSha256": digest(provenance)}
    with exclusive_lock(output):
        manifest_path = output / "manifest.json"
        if manifest_path.exists():
            saved = read_json(manifest_path)
            if saved.get("identity") != identity:
                raise ValueError("Graph output has another source/policy/version; use a new directory")
            if (saved.get("complete") is True and set(saved.get("files", {})) == set(ARTIFACTS) and
                    all((output / name).is_file() and file_hash(output / name) == saved["files"][name]["sha256"]
                        for name in ARTIFACTS)):
                progress(f"Graph cache verified: {output}")
                return saved
            raise ValueError("Graph cache failed verification; use a new output directory")
        if any((output / name).exists() for name in ARTIFACTS):
            raise ValueError("Output contains unpublished artifacts; choose a new directory")
        started = time.monotonic()
        progress("Graph [1/5] validating source references and indexing access (offline)...")
        source_counts = validate_pbf(source)
        refs, accepted, node_tags, blocked = Counter(), {}, {}, {}
        restrictions, protected = [], set()
        reasons, counts = Counter(), Counter()
        with partial(output, "policy_audit.csv").open("w", newline="", encoding="utf-8") as stream:
            audit = csv.writer(stream)
            audit.writerow(("osmType", "osmId", "action", "reasonsJson"))
            for obj in osmium.FileProcessor(source):
                if obj.is_node():
                    tags = dict(obj.tags)
                    if tags:
                        node_tags[obj.id] = tags
                        protected.add(obj.id)
                    reason = node_block_reason(tags, policy)
                    if reason:
                        blocked[obj.id] = reason
                        reasons[reason] += 1
                        audit.writerow(("node", obj.id, "exclude-incident-segments", compact([reason])))
                elif obj.is_way():
                    directions, why = way_decision(dict(obj.tags), policy)
                    if why:
                        reasons.update(why)
                        audit.writerow(("way", obj.id, "partially-accepted" if directions else "excluded", compact(why)))
                    if directions:
                        accepted[obj.id] = directions
                        refs.update(n.ref for n in obj.nodes)
                    else:
                        counts["excludedWays"] += 1
                elif obj.is_relation() and obj.tags.get("type", "").startswith("restriction"):
                    record = restriction_record(obj)
                    restrictions.append(record)
                    protected.update(m["ref"] for m in record["members"] if m["type"] == "n")
        progress(f"Graph [2/5] accepted {len(accepted):,}/{source_counts['way']:,} ways; building directed edges...")
        db_path = partial(output, "graph.sqlite")
        # Restart only our incomplete database; completed artifacts are never overwritten.
        db_path.unlink(missing_ok=True)
        db = sqlite3.connect(db_path)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE nodes (nodeId INTEGER PRIMARY KEY,longitude REAL NOT NULL,latitude REAL NOT NULL,
                                    componentId INTEGER,tagsJson TEXT NOT NULL);
                CREATE TABLE ways (osmWayId INTEGER PRIMARY KEY,osmVersion INTEGER NOT NULL,tagsJson TEXT NOT NULL);
                CREATE TABLE edges (
                    edgeId TEXT PRIMARY KEY,fromNodeId INTEGER NOT NULL REFERENCES nodes(nodeId),
                    toNodeId INTEGER NOT NULL REFERENCES nodes(nodeId),edgeKey TEXT NOT NULL UNIQUE,
                    osmWayId INTEGER NOT NULL REFERENCES ways(osmWayId),startIndex INTEGER NOT NULL,
                    endIndex INTEGER NOT NULL,direction TEXT NOT NULL,lengthKm REAL NOT NULL CHECK(lengthKm>0),
                    widthM REAL,highway TEXT NOT NULL,geometryJson TEXT NOT NULL,osmNodeIdsJson TEXT NOT NULL,
                    missingFlagsJson TEXT NOT NULL);
            """)
            db.execute("INSERT INTO metadata VALUES (?,?)", ("identity", compact(identity)))
            db.execute("INSERT INTO metadata VALUES (?,?)", ("routingReady", "false"))
            processor = (osmium.FileProcessor(source).with_locations()
                         .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY)))
            for processed, way in enumerate(processor, 1):
                if way.id not in accepted:
                    continue
                tags = dict(way.tags)
                ids = [n.ref for n in way.nodes]
                coords = [(n.lon, n.lat) for n in way.nodes]
                if not all(math.isfinite(x) and math.isfinite(y) for x, y in coords):
                    raise ValueError(f"Non-finite geometry in way {way.id}")
                cuts = [i for i, node in enumerate(ids) if i in (0, len(ids)-1) or refs[node] > 1 or node in protected]
                width, flag = width_metres(tags.get("width"))
                db.execute("INSERT INTO ways VALUES (?,?,?)", (way.id, way.version, compact(tags)))
                for start, end in zip(cuts, cuts[1:]):
                    if ids[start] in blocked or ids[end] in blocked:
                        counts["blockedSegments"] += 1
                        continue
                    shape = coords[start:end+1]
                    length = GEOD.line_length([c[0] for c in shape], [c[1] for c in shape]) / 1000
                    if not math.isfinite(length) or length <= 0:
                        counts["zeroLengthSegments"] += 1
                        continue
                    counts["physicalSegments"] += 1
                    counts["segmentsWidthMissing"] += width is None
                    for i in (start, end):
                        db.execute("INSERT OR IGNORE INTO nodes VALUES (?,?,?,?,?)",
                                   (ids[i], *coords[i], None, compact(node_tags.get(ids[i], {}))))
                    for direction in accepted[way.id]:
                        reverse = direction == "backward"
                        u, v = (ids[end], ids[start]) if reverse else (ids[start], ids[end])
                        edge_id = f"w{way.id}:{start}-{end}:{direction}"
                        geometry = {"type": "LineString", "coordinates": shape[::-1] if reverse else shape}
                        ordered_ids = ids[start:end+1]
                        db.execute("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                            edge_id, u, v, edge_id, way.id, start, end, direction, length, width,
                            tags["highway"], compact(geometry), compact(ordered_ids[::-1] if reverse else ordered_ids),
                            compact([flag] if flag else [])))
                        counts["directedEdges"] += 1
                if processed % 25000 == 0:
                    db.commit()
                    progress(f"Graph [2/5] ways {processed:,}/{source_counts['way']:,} | edges {counts['directedEdges']:,}")
            del processor
            if not counts["directedEdges"]:
                raise ValueError("No usable graph edges after access policy")
            db.executescript("CREATE INDEX edges_from ON edges(fromNodeId); CREATE INDEX edges_to ON edges(toNodeId); CREATE INDEX edges_way ON edges(osmWayId);")
            progress("Graph [3/5] checking connectivity and database integrity...")
            components = component_report(db)
            node_count = db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Graph database failed integrity/reference checks")
            db.commit()
            progress("Graph [4/5] exporting nodes, edges and restriction diagnostics...")
            export_table(db, output, "nodes.csv", NODE_COLUMNS, "SELECT * FROM nodes ORDER BY nodeId")
            export_table(db, output, "edges.csv", EDGE_COLUMNS, "SELECT * FROM edges ORDER BY osmWayId,startIndex,endIndex,direction")
        finally:
            db.close()
        with partial(output, "components.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("componentId", "nodeCount", "west", "south", "east", "north"))
            writer.writerows(components)
        restriction_counts = dict(Counter(r["status"] for r in restrictions))
        qa = {"scope": scope, "sourceCounts": source_counts, "acceptedWays": len(accepted),
              "graphNodes": node_count, **dict(counts), "blockedNodes": len(blocked),
              "policyReasons": dict(sorted(reasons.items())), "restrictionStatuses": restriction_counts,
              "weakComponentCount": len(components), "largestWeakComponentNodes": components[0][1],
              "largestWeakComponentFraction": components[0][1] / node_count,
              "checks": {"sourceReferences": True, "sqliteIntegrity": True, "graphReferences": True,
                         "positiveLengthKm": True},
              "connectivityMeaning": "weak only; not a directed or turn-aware reachability guarantee",
              "limitations": ["Turn restrictions are preserved, not enforced; do not route using edges alone.",
                              "Access defaults are engineering assumptions; conditional ways/nodes are excluded.",
                              "Blocked nodes remove adjacent simplified segments, including endpoint access.",
                              "No ferries, travel-time profile, weather or risk features at this stage.",
                              "All components retained; no synthetic bridges between mainland and islands."]}
        write_json(partial(output, "qa_report.json"), qa)
        write_json(partial(output, "restrictions.json"), {"schemaVersion": "member1-restriction-audit/1",
                   "enforced": False, "records": sorted(restrictions, key=lambda r: r["relationId"])})
        write_json(partial(output, "policy.json"), policy)
        progress("Graph [5/5] verifying source stability and publishing artifact checksums...")
        if file_hash(source) != source_sha:
            raise ValueError("Source changed during build; graph not published")
        files = {name: {"sha256": file_hash(partial(output, name)), "bytes": partial(output, name).stat().st_size}
                 for name in ARTIFACTS}
        manifest = {"schemaVersion": "member1-road-graph-preview/1", "identity": identity,
                    "graphVersion": digest(identity), "policyVersion": policy["policyVersion"],
                    "createdAt": now_vn(), "processingDurationHours": (time.monotonic() - started) / 3600,
                    "complete": True, "graphReady": True, "routingReady": False,
                    "readinessBlockers": ["turn-restrictions-not-enforced", "travel-profile-not-built", "policy-review-required"],
                    "scope": scope, "sourceType": "REAL-derived" if provenance else "UNVERIFIED",
                    "sourcePath": str(source.resolve()), "sourceProvenance": provenance,
                    "files": files, "counts": {"nodes": node_count, "edges": counts["directedEdges"],
                                               "weakComponents": len(components), "restrictions": len(restrictions)},
                    "units": {"lengthKm": "km", "widthM": "m", "coordinates": "WGS84 lon,lat degrees",
                              "processingDurationHours": "h", "timezone": "Asia/Ho_Chi_Minh"},
                    "attribution": "© OpenStreetMap contributors, ODbL 1.0"}
        for name in ARTIFACTS:
            os.replace(partial(output, name), output / name)
        write_json(manifest_path, manifest)
        progress(f"Graph preview complete: {node_count:,} nodes | {counts['directedEdges']:,} edges | {len(components):,} weak components | routingReady=False")
        return manifest
