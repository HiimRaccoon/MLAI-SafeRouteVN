"""Static estimated travel features for a frozen decision epoch."""

import csv
import json
import math
from pathlib import Path
import re

from geo_data.bundle import cached, database, instant, positive, publish, reset_database, temporary, verify
from geo_data.common import digest, exclusive_lock, read_json, write_json
from geo_data.osm.process_graph import ROUTING_SCHEMA

DEFAULT_PROFILE = Path(__file__).with_name("profiles") / "travel_v1.json"
TRAVEL_SCHEMA = "member1-travel/1"


def load_profile(path):
    profile = read_json(path)
    if not profile.get("profileVersion"):
        raise ValueError("Missing profileVersion")
    positive(profile["fallbackSpeedKph"], "fallback speed")
    for speed in profile["speedKphByHighway"].values():
        positive(speed, "highway speed")
    for multiplier in [profile["unknownSurfaceMultiplier"], *profile["surfaceSpeedMultiplier"].values()]:
        if not 0 < positive(multiplier, "surface multiplier") <= 1:
            raise ValueError("Surface multiplier must be <=1")
    end = 0
    for bucket in profile["timeBuckets"]:
        if bucket["startHour"] != end or not end < bucket["endHour"] <= 24:
            raise ValueError("Time buckets must cover [0,24) without gaps/overlaps")
        end = bucket["endHour"]
        if positive(bucket["travelMultiplier"], "time multiplier") < 1:
            raise ValueError("Time multiplier must be >=1")
        for key in ("timeFactor", "trafficFactor"):
            if not 0 <= positive(bucket[key], key, zero=True) <= 1:
                raise ValueError("Factors must be in [0,1]")
    if end != 24:
        raise ValueError("Time buckets must end at 24")
    return profile


def parse_maxspeed(raw):
    if raw is None:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(km/h|kmh|kph|mph)?\s*", raw)
    if not match or float(match[1]) <= 0:
        return None
    value = float(match[1]) * (1.609344 if match[2] == "mph" else 1)
    return value if math.isfinite(value) else None


def travel_values(edge, tags, profile, bucket):
    flags = []
    speed = profile["speedKphByHighway"].get(edge["highway"], profile["fallbackSpeedKph"])
    if edge["highway"] not in profile["speedKphByHighway"]:
        flags.append("highway:fallback-speed")
    surface = tags.get("surface")
    speed *= profile["surfaceSpeedMultiplier"].get(surface, profile["unknownSurfaceMultiplier"])
    if surface not in profile["surfaceSpeedMultiplier"]:
        flags.append("surface:missing-or-unknown")
    direction = edge["direction"]
    keys = (f"maxspeed:motorcycle:{direction}", "maxspeed:motorcycle", f"maxspeed:{direction}", "maxspeed")
    raw = next((tags[k] for k in keys if k in tags), None)
    limit = parse_maxspeed(raw)
    if limit is not None:
        speed = min(speed, limit)
    else:
        flags.append("maxspeed:missing-or-unparsed")
    if any(k.startswith("maxspeed") and "conditional" in k for k in tags):
        flags.append("maxspeed:conditional-not-evaluated")
    positive(speed, "estimated speed")
    length = positive(edge["lengthKm"], "lengthKm")
    base = length / speed
    return speed, base, base * bucket["travelMultiplier"], flags


def build_travel(routing, output, *, at, profile_path=DEFAULT_PROFILE, progress=print):
    routing, output = Path(routing), Path(output)
    source = verify(routing, ROUTING_SCHEMA)
    epoch, profile = instant(at), load_profile(profile_path)
    hour = epoch.hour + epoch.minute / 60 + epoch.second / 3600
    bucket = next(b for b in profile["timeBuckets"] if b["startHour"] <= hour < b["endHour"])
    identity = {"stage": TRAVEL_SCHEMA, "routingVersion": source["version"], "at": epoch.isoformat(), "profileHash": digest(profile)}
    with exclusive_lock(output):
        if saved := cached(output, identity):
            progress(f"Travel cache verified: {output}")
            return saved
        path = reset_database(output, "travel.sqlite")
        count = fallback_count = 0
        with database(routing / "network.sqlite") as graph, database(path, readonly=False) as db, temporary(output, "travel_edges.csv").open("w", encoding="utf-8", newline="") as stream:
            db.execute("CREATE TABLE travel(edgeId TEXT PRIMARY KEY,baseSpeedKph REAL NOT NULL,baseTravelTimeHours REAL NOT NULL,travelTimeHours REAL NOT NULL,travelMultiplier REAL NOT NULL,timeBucket TEXT NOT NULL,timeFactor REAL NOT NULL,trafficFactor REAL NOT NULL,sourceType TEXT NOT NULL,missingFlagsJson TEXT NOT NULL)")
            writer = csv.writer(stream)
            writer.writerow([row[1] for row in db.execute("PRAGMA table_info(travel)")])
            for edge in graph.execute("SELECT e.*,w.tagsJson FROM edges e JOIN ways w USING(osmWayId) ORDER BY e.edgeId"):
                speed, base, adjusted, flags = travel_values(edge, json.loads(edge["tagsJson"]), profile, bucket)
                row = (edge["edgeId"], speed, base, adjusted, bucket["travelMultiplier"], bucket["id"], bucket["timeFactor"], bucket["trafficFactor"], "ESTIMATED", json.dumps(flags))
                db.execute("INSERT INTO travel VALUES (?,?,?,?,?,?,?,?,?,?)", row)
                writer.writerow(row)
                count += 1
                fallback_count += bool(flags)
                if count % 50000 == 0:
                    progress(f"Travel {count:,}/{source['edgeCount']:,} edges")
            if count != source["edgeCount"]:
                raise ValueError("Travel edge coverage mismatch")
        write_json(temporary(output, "profile.json"), profile)
        return publish(output, identity, TRAVEL_SCHEMA, ["travel.sqlite", "travel_edges.csv", "profile.json"],
                       routingVersion=source["version"], graphVersion=source["graphVersion"], at=epoch.isoformat(),
                       travelProfileVersion=profile["profileVersion"], edgeCount=count, fallbackEdges=fallback_count,
                       sourceType="ESTIMATED", temporalModel="frozen-decision-epoch", units={"speed": "km/h", "duration": "h"})
