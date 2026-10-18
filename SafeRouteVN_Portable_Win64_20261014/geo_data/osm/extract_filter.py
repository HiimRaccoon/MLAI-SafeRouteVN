"""Filter a country PBF locally while retaining full ways, tags and restrictions."""

import os
from pathlib import Path
import time

import osmium
from pyproj import Transformer
from shapely import from_wkb
from shapely.geometry import mapping
from shapely.ops import transform
from shapely.prepared import prep

from geo_data.common import digest, exclusive_lock, now_vn, read_json, write_json
from geo_data.osm.extract_download import file_hash
from geo_data.osm.region import hcmc_coverage_checks, load_boundary
from geo_data.units import nonnegative, timestamp_vn


def validate_pbf(path):
    """Check referential integrity independently of the back-reference writer."""
    ids = {"n": set(), "w": set(), "r": set()}
    needed_relations = set()
    needed_ways = set()
    needed_nodes = set()
    counts = {"node": 0, "way": 0, "relation": 0}
    for obj in osmium.FileProcessor(path):
        kind = obj.type_str()
        if kind not in ids:
            continue
        if obj.id in ids[kind]:
            raise ValueError(f"Duplicate OSM object {kind}{obj.id}")
        ids[kind].add(obj.id)
        if obj.is_node():
            counts["node"] += 1
            if not obj.location.valid():
                raise ValueError(f"Invalid node location: {obj.id}")
        elif obj.is_way():
            counts["way"] += 1
            if len(obj.nodes) < 2:
                raise ValueError(f"Way has fewer than two nodes: {obj.id}")
            needed_nodes.update(n.ref for n in obj.nodes)
        elif obj.is_relation():
            counts["relation"] += 1
            for member in obj.members:
                {"n": needed_nodes, "w": needed_ways, "r": needed_relations}[member.type].add(member.ref)
    missing = {"node": len(needed_nodes - ids["n"]), "way": len(needed_ways - ids["w"]),
               "relation": len(needed_relations - ids["r"])}
    if any(missing.values()):
        raise ValueError(f"PBF has missing references: {missing}")
    return counts


def filter_extract(source, boundary_path, output, *, buffer_km=0.25, scope="hcmc-current", progress=print):
    source, output = Path(source), Path(output)
    if not source.is_file():
        raise ValueError("Source PBF does not exist")
    if scope not in ("hcmc-current", "test"):
        raise ValueError("Unknown scope")
    buffer_km = nonnegative(buffer_km, "bufferKm")
    if buffer_km > 20:
        raise ValueError("bufferKm must not exceed 20")
    document = read_json(boundary_path)
    boundary = load_boundary(document)
    checks = hcmc_coverage_checks(boundary)
    if scope == "hcmc-current" and not all(checks.values()):
        raise ValueError("Boundary fails current HCMC coverage probes")
    props = document.get("properties", {})
    if not props.get("source") or not props.get("boundaryVersion"):
        raise ValueError("Boundary properties must include source and boundaryVersion")
    forward = Transformer.from_crs("EPSG:4326", "EPSG:32648", always_xy=True)
    inverse = Transformer.from_crs("EPSG:32648", "EPSG:4326", always_xy=True)
    region = transform(inverse.transform, transform(forward.transform, boundary).buffer(buffer_km * 1000)) if buffer_km else boundary
    spatial_filter = prep(region)
    progress("PBF [1/4] verifying source and boundary...")
    source_sha = file_hash(source)
    download_manifest = source.parent / "extract-manifest.json"
    provenance = None
    if download_manifest.exists():
        candidate = read_json(download_manifest)
        if candidate.get("file") == source.name:
            if candidate.get("sha256") != source_sha or candidate.get("bytes") != source.stat().st_size:
                raise ValueError("Source does not match its download manifest")
            provenance = candidate
    identity = {"sourceSha256": source_sha, "boundarySha256": digest(mapping(boundary)),
                "bufferKm": buffer_km, "scope": scope, "extractorVersion": "pbf-highways/1"}
    with exclusive_lock(output):
        final = output / "roads.osm.pbf"
        temporary = output / "roads.partial.osm.pbf"
        manifest_path = output / "manifest.json"
        if manifest_path.exists():
            saved = read_json(manifest_path)
            if saved.get("identity") != identity:
                raise ValueError("Output belongs to another source/boundary/config; use a new directory")
            if final.exists() and file_hash(final) == saved.get("sha256"):
                progress(f"Filtered PBF cache verified: {final}")
                return saved
        if final.exists():
            raise ValueError("Existing filtered PBF failed verification; use a new output directory")
        started = time.monotonic()
        selected_ways = set()
        selected_relations = 0
        factory = osmium.geom.WKBFactory()
        processor = (osmium.FileProcessor(source).with_locations()
                     .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
                     .with_filter(osmium.filter.KeyFilter("highway")))
        source_timestamp = processor.header.get("osmosis_replication_timestamp", "")
        progress("PBF [2/4] scanning highway ways against the city boundary (local, no API)...")
        scanned = 0
        # Retain node tags (barriers/access) and restriction member tags.
        with osmium.BackReferenceWriter(temporary, ref_src=source, overwrite=True,
                                        remove_tags=False, relation_depth=10) as writer:
            for way in processor:
                scanned += 1
                try:
                    line = from_wkb(factory.create_linestring(way))
                except (osmium.InvalidLocationError, RuntimeError) as exc:
                    raise ValueError(f"Cannot construct highway geometry {way.id}: {exc}") from exc
                if spatial_filter.intersects(line):
                    selected_ways.add(way.id)
                    writer.add_way(way)
                if scanned % 100000 == 0:
                    progress(f"PBF [2/4] scanned {scanned:,} highways | selected {len(selected_ways):,}")
            # Release the country node-location cache before the additional passes.
            del processor
            if not selected_ways:
                raise ValueError("No highways intersect the boundary; check source coverage")
            progress(f"PBF [3/4] selected {len(selected_ways):,} highways; collecting restrictions and references...")
            for relation in osmium.FileProcessor(source, osmium.osm.RELATION):
                if relation.tags.get("type", "").startswith("restriction") and any(
                        member.type == "w" and member.ref in selected_ways for member in relation.members):
                    writer.add_relation(relation)
                    selected_relations += 1
            progress(f"PBF [3/4] {selected_relations:,} restrictions; completing node/way/relation references...")
        progress("PBF [4/4] checking references and checksum...")
        counts = validate_pbf(temporary)
        checksum = file_hash(temporary)
        manifest = {"schemaVersion": "member1-filtered-pbf/1", "identity": identity,
                    "datasetVersion": digest(identity), "file": final.name, "sha256": checksum,
                    "bytes": temporary.stat().st_size, "counts": counts, "complete": True,
                    "routingReady": False, "scope": scope, "sourceType": "REAL-derived",
                    "sourcePath": str(source.resolve()), "downloadProvenance": provenance,
                    "sourceTimestamp": timestamp_vn(source_timestamp) if source_timestamp else None,
                    "missingFlags": [] if source_timestamp else ["sourceTimestamp"],
                    "boundarySource": props["source"], "boundaryVersion": props["boundaryVersion"],
                    "coverageChecks": checks, "boundaryVerification": "coverage-probes-only",
                    "selectedHighwayWays": len(selected_ways), "selectedRestrictions": selected_relations,
                    "referenceComplete": True, "createdAt": now_vn(),
                    "processingDurationHours": (time.monotonic() - started) / 3600,
                    "selection": "whole highway ways intersecting buffered boundary, plus restriction references",
                    "attribution": "© OpenStreetMap contributors, ODbL 1.0"}
        write_json(manifest_path, manifest)
        os.replace(temporary, final)
        progress(f"PBF complete: {final} | nodes={counts['node']:,}, ways={counts['way']:,}, relations={counts['relation']:,}")
        return manifest
