"""Measured data-quality summary and regional map samples for manual review."""

from collections import Counter
import json
from pathlib import Path

from pyproj import Geod

from geo_data.bundle import cached, database, positive, publish, temporary
from geo_data.common import digest, exclusive_lock, read_json, write_json
from geo_data.handoff import disjoint, verify_run
from geo_data.osm.region import HCMC_PROBES


def quality_review(run, output, *, sample_radius_km=5, progress=print):
    run, output = Path(run), Path(output)
    disjoint(run, output)
    positive(sample_radius_km, "sampleRadiusKm")
    manifests = verify_run(run)
    identity = {"stage": "member1-quality-review/1", "inputs": {k: digest(v) for k, v in manifests.items()},
                "probes": HCMC_PROBES, "sampleRadiusKm": sample_radius_km}
    with exclusive_lock(output):
        if saved := cached(output, identity):
            progress(f"Quality review cache verified: {output}")
            return saved
        progress("Review [1/3] summarizing directed edges and feature fallbacks...")
        with database(run / "routing/network.sqlite") as db:
            highway_rows = [dict(r) for r in db.execute("""SELECT highway,COUNT(*) AS directedEdges,
              SUM(lengthKm) AS directedLengthKm,SUM(widthM IS NULL) AS widthMissingEdges
              FROM edges GROUP BY highway ORDER BY highway""")]
            restrictions = {"excludedDirectedEdges": db.execute("SELECT COUNT(DISTINCT edgeId) FROM excluded_edges").fetchone()[0],
                            "forbiddenPairs": db.execute("SELECT COUNT(*) FROM (SELECT DISTINCT inEdgeId,outEdgeId FROM forbidden_turns)").fetchone()[0]}
            geod, nearest = Geod(ellps="WGS84"), {}
            progress("Review [2/3] locating road samples around six geographic probes...")
            for node in db.execute("SELECT nodeId,longitude,latitude FROM nodes ORDER BY nodeId"):
                for name, (lon, lat) in HCMC_PROBES.items():
                    distance = geod.inv(lon, lat, node["longitude"], node["latitude"])[2] / 1000
                    if distance <= sample_radius_km and (name not in nearest or distance < nearest[name][0]):
                        nearest[name] = (distance, dict(node))
            geometries, probes = [], []
            for name, coords in HCMC_PROBES.items():
                row = {"probe": name, "longitude": coords[0], "latitude": coords[1], "status": "no-node-within-radius", "sampleRadiusKm": sample_radius_km}
                geometries.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": list(coords)},
                                   "properties": {"probe": name, "kind": "reference-point"}})
                if name in nearest:
                    distance, node = nearest[name]
                    row.update(status="sample-found-needs-review", nodeId=node["nodeId"], snapDistanceKm=distance)
                    edges = list(db.execute("SELECT edgeId,highway,lengthKm,widthM,geometryJson FROM edges WHERE fromNodeId=? OR toNodeId=? ORDER BY edgeId LIMIT 12", (node["nodeId"], node["nodeId"])))
                    row["sampleEdgeIds"] = [edge["edgeId"] for edge in edges]
                    for edge in edges:
                        geometries.append({"type": "Feature", "geometry": json.loads(edge["geometryJson"]),
                                           "properties": {"probe": name, **{k: edge[k] for k in ("edgeId", "highway", "lengthKm", "widthM")}}})
                probes.append(row)
        missing, proxy_min, proxy_max, count = Counter(), None, None, 0
        with database(run / "features/features.sqlite") as db:
            for row in db.execute("SELECT edgeProxy,payloadJson FROM features ORDER BY edgeId"):
                feature = json.loads(row["payloadJson"])
                missing.update(set(feature["missingFlags"]))
                proxy_min = row["edgeProxy"] if proxy_min is None else min(proxy_min, row["edgeProxy"])
                proxy_max = row["edgeProxy"] if proxy_max is None else max(proxy_max, row["edgeProxy"])
                count += 1
                if count % 100000 == 0:
                    progress(f"Review feature flags {count:,}/{manifests['features']['edgeCount']:,}")
        report = {"directedEdges": count, "byHighway": highway_rows, "missingFlagEdgeCounts": dict(sorted(missing.items())),
                  "proxyRange": {"min": proxy_min, "max": proxy_max}, "restrictions": restrictions, "regionalSamples": probes,
                  "weather": {k: manifests["weather"][k] for k in ("at", "regionCount", "fallbackRegions")},
                  "manualReviewComplete": False, "integrated": False,
                  "interpretation": ["Lengths and counts are directed; two directions count twice.",
                                     "Nearby sample geometry does not prove area coverage or valid motorcycle access.",
                                     "Missing flags can overlap; do not sum flag counts as a count of distinct edges.",
                                     "Proxy range and passing QA do not calibrate accident risk."]}
        write_json(temporary(output, "quality_report.json"), report)
        write_json(temporary(output, "regional_samples.geojson"), {"type": "FeatureCollection", "features": geometries})
        lines = ["# Member 1 — data quality review", "", f"Directed edges checked: **{count:,}**.",
                 "Manual boundary/access review and profile calibration: **pending**.", "",
                 "| Highway | Directed edges | Directed km | Width missing |", "| --- | ---: | ---: | ---: |"]
        lines += [f"| {r['highway']} | {r['directedEdges']:,} | {r['directedLengthKm']:.3f} | {r['widthMissingEdges']:,} |" for r in highway_rows]
        lines += ["", "Counts/lengths include both directions separately; this is not physical road length.", "",
                  "| Missing/fallback flag | Edges |", "| --- | ---: |"]
        lines += [f"| {flag} | {number:,} |" for flag, number in sorted(missing.items())]
        lines += ["", "Flag counts overlap. Inspect `regional_samples.geojson` against a map/field evidence; samples alone do not verify coverage.",
                  "Record review evidence outside this immutable bundle. No automatic policy or width replacement was applied."]
        temporary(output, "quality_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        progress("Review [3/3] writing report and geographic samples; manual review remains pending")
        return publish(output, identity, "member1-quality-review/1", ["quality_report.json", "quality_report.md", "regional_samples.geojson"],
                       computedReviewReady=True, manualReviewComplete=False, integrated=False, directedEdges=count)
