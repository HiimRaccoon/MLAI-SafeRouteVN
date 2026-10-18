"""Stream full-city features to SQLite and JSONL, using one frozen context."""

import json
from pathlib import Path

from geo_data.bundle import cached, database, publish, reset_database, temporary, verify
from geo_data.common import digest, exclusive_lock, read_json, write_json
from geo_data.features.risk_proxy import DEFAULT_RISK, load_risk, risk_values
from geo_data.features.travel_time import TRAVEL_SCHEMA
from geo_data.osm.process_graph import ROUTING_SCHEMA
from geo_data.weather.context_builder import PLAN_SCHEMA
from geo_data.weather.open_meteo_client import CONTEXT_SCHEMA

FEATURE_SCHEMA = "member1-edge-features/1"


def build_features(routing, travel, plan, weather, output, *, risk_path=DEFAULT_RISK, progress=print):
    routing, travel, plan, weather, output = map(Path, (routing, travel, plan, weather, output))
    net, times, grid, context = (verify(routing, ROUTING_SCHEMA), verify(travel, TRAVEL_SCHEMA),
                               verify(plan, PLAN_SCHEMA), verify(weather, CONTEXT_SCHEMA))
    if any(m["routingVersion"] != net["version"] for m in (times, grid, context)) or context["planVersion"] != grid["version"]:
        raise ValueError("Routing/travel/weather grid versions do not match")
    if times["at"] != context["at"]:
        raise ValueError("Travel and weather must use the same decision epoch")
    config = load_risk(risk_path)
    identity = {"stage": FEATURE_SCHEMA, "routingVersion": net["version"], "travelVersion": times["version"],
                "planVersion": grid["version"], "contextVersion": context["contextVersion"], "riskHash": digest(config)}
    with exclusive_lock(output):
        if saved := cached(output, identity):
            progress(f"Features cache verified: {output}")
            return saved
        observations = {r["regionId"]: r for r in read_json(weather / "weather_context.json")["regions"]}
        path = reset_database(output, "features.sqlite")
        count = fallback_count = 0
        with database(routing / "network.sqlite") as graph, database(path, readonly=False) as db, temporary(output, "edge_features.jsonl").open("w", encoding="utf-8") as stream:
            graph.execute("ATTACH DATABASE ? AS trip", ((travel / "travel.sqlite").resolve().as_uri() + "?mode=ro",))
            graph.execute("ATTACH DATABASE ? AS weathergrid", ((plan / "mapping.sqlite").resolve().as_uri() + "?mode=ro",))
            db.execute("CREATE TABLE features(edgeId TEXT PRIMARY KEY,travelTimeHours REAL NOT NULL,relativeExposure REAL NOT NULL,edgeProxy REAL NOT NULL,regionId TEXT NOT NULL,payloadJson TEXT NOT NULL)")
            rows = graph.execute("""SELECT e.*,w.tagsJson,t.baseSpeedKph,t.baseTravelTimeHours,t.travelTimeHours,
              t.travelMultiplier,t.timeBucket,t.timeFactor,t.trafficFactor,t.missingFlagsJson AS travelMissing,r.regionId
              FROM edges e JOIN ways w USING(osmWayId) JOIN trip.travel t USING(edgeId)
              JOIN weathergrid.edge_regions r USING(edgeId) ORDER BY e.edgeId""")
            for edge in rows:
                region = observations.get(edge["regionId"])
                if region is None:
                    raise ValueError(f"Weather missing for region {edge['regionId']}")
                risk = risk_values(edge, json.loads(edge["tagsJson"]), region, edge, config)
                flags = sorted(set(json.loads(edge["missingFlagsJson"]) + json.loads(edge["travelMissing"]) + risk["missingFlags"]))
                duration = edge["travelTimeHours"] * risk["weatherTravelMultiplier"]
                payload = {"edgeId": edge["edgeId"], "fromNodeId": edge["fromNodeId"], "toNodeId": edge["toNodeId"],
                           "lengthKm": edge["lengthKm"], "widthM": edge["widthM"], "baseSpeedKph": edge["baseSpeedKph"],
                           "baseTravelTimeHours": edge["baseTravelTimeHours"], "travelTimeHours": duration,
                           "travelMultiplier": edge["travelMultiplier"] * risk["weatherTravelMultiplier"],
                           "timeBucket": edge["timeBucket"], "weatherRegionId": edge["regionId"],
                           "weatherValidAt": region["validAt"], "weatherFallbackUsed": region["fallbackUsed"],
                           **risk, "missingFlags": flags, "fallbackUsed": bool(flags),
                           "travelSourceType": "ESTIMATED", "graphVersion": net["graphVersion"],
                           "contextVersion": context["contextVersion"], "riskModelVersion": config["riskModelVersion"]}
                encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                db.execute("INSERT INTO features VALUES (?,?,?,?,?,?)", (edge["edgeId"], duration, risk["relativeExposure"], risk["edgeProxy"], edge["regionId"], encoded))
                stream.write(encoded + "\n")
                count += 1
                fallback_count += bool(flags)
                if count % 50000 == 0:
                    progress(f"Features {count:,}/{net['edgeCount']:,} edges")
            if count != net["edgeCount"]:
                raise ValueError("Feature coverage incomplete: missing travel or region mapping")
        write_json(temporary(output, "risk_model.json"), config)
        return publish(output, identity, FEATURE_SCHEMA, ["features.sqlite", "edge_features.jsonl", "risk_model.json"],
                       routingVersion=net["version"], graphVersion=net["graphVersion"], travelVersion=times["version"],
                       contextVersion=context["contextVersion"], at=times["at"], edgeCount=count,
                       fallbackEdges=fallback_count, riskModelVersion=config["riskModelVersion"], sourceType="PROXY",
                       units={"length": "km", "speed": "km/h", "duration": "h", "exposure": "km*proxy-score"})
