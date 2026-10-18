"""Offline handoff checks for Member 1 artifacts; never claims solver integration."""

import json
import math
from pathlib import Path

from geo_data.bundle import cached, database, instant, publish, temporary, verify
from geo_data.common import exclusive_lock, read_json, write_json
from geo_data.delivery_area import scenario_area, validate_area_membership
from geo_data.features.edge_features import FEATURE_SCHEMA
from geo_data.features.travel_time import TRAVEL_SCHEMA
from geo_data.osm.process_graph import ROUTING_SCHEMA
from geo_data.osm.routing import RoutingGraph
from geo_data.scenario_generator import SCENARIO_SCHEMA, validate_scenario
from geo_data.weather.context_builder import PLAN_SCHEMA
from geo_data.weather.open_meteo_client import CONTEXT_SCHEMA


def require(value, message):
    if not value:
        raise ValueError(message)


def check_pipeline(routing, travel, plan, weather, features, scenarios, output, *, progress=print):
    directories = dict(zip(("routing", "travel", "plan", "weather", "features", "scenarios"),
                           map(Path, (routing, travel, plan, weather, features, scenarios))))
    schemas = (ROUTING_SCHEMA, TRAVEL_SCHEMA, PLAN_SCHEMA, CONTEXT_SCHEMA, FEATURE_SCHEMA, SCENARIO_SCHEMA)
    manifests = {name: verify(path, schema) for (name, path), schema in zip(directories.items(), schemas)}
    net, trips, grid, context, feats, cases = (manifests[name] for name in directories)
    require(all(m["routingVersion"] == net["version"] for m in (trips, grid, context, feats, cases)), "QA: routing versions differ")
    require(context["planVersion"] == grid["version"], "QA: weather plan mismatch")
    require(feats["travelVersion"] == trips["version"] and cases["featuresVersion"] == feats["version"], "QA: feature/scenario version mismatch")
    require(all(m["contextVersion"] == context["contextVersion"] for m in (feats, cases)), "QA: context mismatch")
    require(trips["at"] == context["at"] == feats["at"] == cases["at"], "QA: decision epoch mismatch")
    identity = {"stage": "member1-qa/1", "inputs": {k: {"version": v["version"], "files": v["files"]} for k, v in manifests.items()}}
    with exclusive_lock(output):
        if saved := cached(output, identity):
            progress(f"QA cache verified: {output}")
            return saved
        progress("QA [1/3] checking edge coverage, units, turn references and proxy bounds...")
        with database(directories["routing"] / "network.sqlite") as db:
            for alias, path in (("trip", directories["travel"] / "travel.sqlite"), ("feat", directories["features"] / "features.sqlite"), ("grid", directories["plan"] / "mapping.sqlite")):
                db.execute(f"ATTACH DATABASE ? AS {alias}", (path.resolve().as_uri() + "?mode=ro",))
            require(db.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == net["edgeCount"], "QA: edge count mismatch")
            for table in ("trip.travel", "feat.features", "grid.edge_regions"):
                count = db.execute(f"SELECT COUNT(*) FROM edges e JOIN {table} x USING(edgeId)").fetchone()[0]
                require(count == net["edgeCount"], f"QA: incomplete {table}")
                require(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count, f"QA: extra edges in {table}")
            bad = db.execute("""SELECT COUNT(*) FROM forbidden_turns t LEFT JOIN edges a ON a.edgeId=t.inEdgeId
              LEFT JOIN edges b ON b.edgeId=t.outEdgeId WHERE a.edgeId IS NULL OR b.edgeId IS NULL OR a.toNodeId!=b.fromNodeId""").fetchone()[0]
            require(bad == 0, "QA: invalid turn references")
            require(db.execute("SELECT COUNT(*) FROM edges JOIN excluded_edges USING(edgeId)").fetchone()[0] == 0, "QA: quarantined edge remains traversable")
            count = 0
            for row in db.execute("SELECT e.lengthKm,t.*,f.payloadJson,f.travelTimeHours AS adjustedHours,f.relativeExposure,f.edgeProxy FROM edges e JOIN trip.travel t USING(edgeId) JOIN feat.features f USING(edgeId)"):
                require(all(math.isfinite(row[k]) and row[k] > 0 for k in ("lengthKm", "baseSpeedKph", "baseTravelTimeHours", "travelTimeHours", "adjustedHours")), "QA: invalid travel values")
                require(math.isclose(row["baseTravelTimeHours"], row["lengthKm"] / row["baseSpeedKph"], rel_tol=1e-10), "QA: base duration formula")
                require(math.isclose(row["travelTimeHours"], row["baseTravelTimeHours"] * row["travelMultiplier"], rel_tol=1e-10), "QA: travel multiplier formula")
                payload = json.loads(row["payloadJson"])
                require(all(isinstance(payload[k], (int, float)) and math.isfinite(payload[k]) and 0 <= payload[k] <= 1 for k in ("roadFactor", "timeFactor", "weatherFactor", "trafficFactor", "edgeProxy")), "QA: proxy factor outside [0,1]")
                require(math.isclose(row["relativeExposure"], row["lengthKm"] * row["edgeProxy"], rel_tol=1e-10, abs_tol=1e-12), "QA: exposure formula")
                require(math.isclose(row["adjustedHours"], row["baseTravelTimeHours"] * payload["travelMultiplier"], rel_tol=1e-10), "QA: final duration formula")
                count += 1
                if count % 100000 == 0:
                    progress(f"QA features {count:,}/{net['edgeCount']:,}")
        observations = read_json(directories["weather"] / "weather_context.json")["regions"]
        expected_regions = {r["regionId"] for r in read_json(directories["plan"] / "regions.json")["regions"]}
        require(len(observations) == len(expected_regions) and {r["regionId"] for r in observations} == expected_regions, "QA: weather region coverage")
        for record in observations:
            age = (instant(context["at"]) - instant(record["validAt"])).total_seconds() / 3600
            bound = context["identity"]["maxStaleHours"] if record["fallbackUsed"] else 1
            require(0 <= age <= bound, "QA: weather outside allowed validity window")
            require(record["precipitationIntervalHours"] == 1, "QA: precipitation interval mismatch")
        progress("QA [2/3] checking scenario state invariants and recorded directed paths...")
        area = scenario_area(directories["scenarios"], cases)
        for index in range(9):
            scenario = read_json(directories["scenarios"] / f"S{index}.json")
            validate_scenario(scenario)
            validate_area_membership(scenario, area)
        s0 = read_json(directories["scenarios"] / "S0.json")
        require(len(s0["initialState"]["orders"]) == 3 and len(s0["initialState"]["vehicles"]) == 2, "QA: S0 must have 3 orders and 2 vehicles")
        report = read_json(directories["scenarios"] / "qa_report.json")
        with RoutingGraph(routing, features=features) as reader:
            for node in report["reachableCandidates"]:
                forward, back = node["fastest"], node["return"]
                require(reader.validate_path(forward["fromNodeId"], forward["edgeIds"]) == forward["toNodeId"], "QA: outbound path endpoint")
                require(reader.validate_path(back["fromNodeId"], back["edgeIds"], incoming_edge=forward["edgeIds"][-1]) == back["toNodeId"], "QA: return path endpoint")
            for sid in ("S7", "S8"):
                case = read_json(directories["scenarios"] / f"{sid}.json")
                if evidence := case.get("tradeoffEvidence"):
                    for key in ("fastest", "safer"):
                        route = evidence[key]
                        require(reader.validate_path(route["fromNodeId"], route["edgeIds"]) == route["toNodeId"], "QA: tradeoff path endpoint")
        qa = {"checksPassed": True, "edgesChecked": count, "scenariosChecked": 9,
              "suiteReady": cases["suiteReady"], "s7Verified": cases["s7Verified"], "s8Verified": cases["s8Verified"],
              "integrated": False, "remaining": ["Member 2/3 contract and consumer validation", "Policy/profile calibration against local conditions"],
              "limits": ["No proof of joint VRP feasibility", "Estimated traffic and uncalibrated safety proxy", "Conservative exclusions reduce routing coverage"]}
        write_json(temporary(output, "qa_report.json"), qa)
        progress(f"QA [3/3] checks passed; scenario suiteReady={cases['suiteReady']}; integrated=False")
        return publish(output, identity, "member1-qa/1", ["qa_report.json"], **qa)
