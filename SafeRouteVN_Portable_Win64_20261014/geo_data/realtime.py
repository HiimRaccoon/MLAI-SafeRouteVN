"""Poll live forecast API; atomically expose complete, versioned routing costs."""

from datetime import timedelta
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

import requests

from geo_data.bundle import database, instant, positive, verify
from geo_data.common import digest, exclusive_lock, now_vn, read_json, write_json
from geo_data.features.edge_features import FEATURE_SCHEMA, build_features
from geo_data.features.risk_proxy import DEFAULT_RISK, load_risk
from geo_data.features.travel_time import DEFAULT_PROFILE, TRAVEL_SCHEMA, build_travel, load_profile
from geo_data.osm.process_graph import ROUTING_SCHEMA
from geo_data.weather.context_builder import PLAN_SCHEMA
from geo_data.weather.open_meteo_client import CONTEXT_SCHEMA, fetch_context

SCHEMA = "member1-live-snapshot/1"
ERRORS = (ValueError, OSError, KeyError, TypeError, RuntimeError, sqlite3.Error, requests.RequestException)


def separate(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError(f"Live output must be separate from input: {source}")


def freshness(snapshot, now):
    current = instant(now)
    valid_age = (current - instant(snapshot["oldestValidAt"])).total_seconds() / 3600
    fetch_age = (current - instant(snapshot["oldestFetchedAt"])).total_seconds() / 3600
    fresh = (0 <= valid_age <= snapshot["maxAgeHours"]
             and 0 <= fetch_age <= snapshot["maxAgeHours"]
             and current >= instant(snapshot["newestFetchedAt"]))
    return {"fresh": fresh, "checkedAt": current.isoformat(),
            "weatherAgeHours": valid_age, "fetchAgeHours": fetch_age,
            "expiresAt": (min(instant(snapshot["oldestValidAt"]), instant(snapshot["oldestFetchedAt"]))
                          + timedelta(hours=snapshot["maxAgeHours"])).isoformat()}


def validate_stages(routing, plan, run):
    """Verify immutable artifacts and bindings before publishing or accepting them."""
    net = verify(routing, ROUTING_SCHEMA)
    grid = verify(plan, PLAN_SCHEMA)
    manifests = {name: verify(run / name, schema) for name, schema in
                 (("weather", CONTEXT_SCHEMA), ("travel", TRAVEL_SCHEMA), ("features", FEATURE_SCHEMA))}
    weather, travel, features = (manifests[name] for name in ("weather", "travel", "features"))
    if any(m["routingVersion"] != net["version"] for m in (grid, weather, travel, features)):
        raise ValueError("Live routing versions differ")
    if (weather["planVersion"] != grid["version"] or features["contextVersion"] != weather["contextVersion"]
            or features["travelVersion"] != travel["version"]
            or features["identity"]["planVersion"] != grid["version"]
            or len({m["at"] for m in manifests.values()}) != 1):
        raise ValueError("Live stage lineage/epochs differ")
    if weather["fallbackRegions"] != 0:
        raise ValueError("Live publication requires a fresh API capture for every region")
    with database(run / "features" / "features.sqlite") as db:
        count, invalid = db.execute("""SELECT COUNT(*), SUM(CASE WHEN
          travelTimeHours IS NULL OR NOT(travelTimeHours > 0 AND travelTimeHours < 1e999)
          OR relativeExposure IS NULL OR NOT(relativeExposure >= 0 AND relativeExposure < 1e999)
          OR edgeProxy IS NULL OR NOT(edgeProxy BETWEEN 0 AND 1) THEN 1 ELSE 0 END) FROM features""").fetchone()
        if count != net["edgeCount"] or count != features["edgeCount"] or invalid:
            raise ValueError("Live feature coverage/cost validation failed")
    manifests.update(routing=net, plan=grid)
    return manifests


class LiveUpdater:
    def __init__(self, routing, plan, output, *, user_agent, max_age_hours=2,
                 travel_profile=DEFAULT_PROFILE, risk_profile=DEFAULT_RISK,
                 attempts=3, progress=print, clock=now_vn, session=requests, http_sleep=time.sleep):
        positive(max_age_hours, "maxAgeHours")
        if not user_agent.strip() or not isinstance(attempts, int) or not 1 <= attempts <= 10:
            raise ValueError("User-Agent and 1..10 attempts required")
        self.routing, self.plan, self.output = (Path(p).resolve() for p in (routing, plan, output))
        self.travel_profile, self.risk_profile = Path(travel_profile).resolve(), Path(risk_profile).resolve()
        for source in (self.routing, self.plan, self.travel_profile, self.risk_profile):
            separate(source, self.output)
        net, grid = verify(self.routing, ROUTING_SCHEMA), verify(self.plan, PLAN_SCHEMA)
        if grid["routingVersion"] != net["version"]:
            raise ValueError("Weather plan belongs to another routing version")
        self.settings = {"schemaVersion": "member1-live-settings/1", "routing": str(self.routing),
                         "plan": str(self.plan), "routingManifestHash": digest(net), "planManifestHash": digest(grid),
                         "travelProfile": str(self.travel_profile), "riskProfile": str(self.risk_profile),
                         "travelProfileHash": digest(load_profile(self.travel_profile)),
                         "riskProfileHash": digest(load_risk(self.risk_profile)), "maxAgeHours": max_age_hours}
        self.user_agent, self.attempts = user_agent, attempts
        self.progress, self.clock, self.session, self.http_sleep = progress, clock, session, http_sleep
        with exclusive_lock(self.output):
            path = self.output / "live-settings.json"
            if path.exists() and read_json(path) != self.settings:
                raise ValueError("Live settings changed; choose a new --output directory")
            write_json(path, self.settings)

    def refresh(self):
        with exclusive_lock(self.output):
            if read_json(self.output / "live-settings.json") != self.settings:
                raise ValueError("Live settings changed during this process")
            started = instant(self.clock())
            epoch = started.replace(minute=0, second=0, microsecond=0).isoformat()
            # Always a new capture, including two requests in the same hour.
            name = started.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:12]
            run = self.output / "snapshots" / name
            run.mkdir(parents=True)
            status = {"schemaVersion": "member1-live-status/1", "state": "updating",
                      "attemptStartedAt": started.isoformat(), "snapshot": f"snapshots/{name}"}
            write_json(self.output / "status.json", status)
            try:
                if (digest(read_json(self.routing / "manifest.json")) != self.settings["routingManifestHash"]
                        or digest(read_json(self.plan / "manifest.json")) != self.settings["planManifestHash"]
                        or digest(load_profile(self.travel_profile)) != self.settings["travelProfileHash"]
                        or digest(load_risk(self.risk_profile)) != self.settings["riskProfileHash"]):
                    raise ValueError("Live source/config changed; use a new output directory")
                self.progress(f"Live capture {name} | weather hour={epoch}")
                self.progress("Live [1/4] Fetching fresh API weather for every region")
                fetch_context(self.plan, run / "weather", at=epoch, user_agent=self.user_agent,
                              attempts=self.attempts, session=self.session, sleep=self.http_sleep, progress=self.progress)
                self.progress("Live [2/4] Rebuilding estimated travel for this hour")
                build_travel(self.routing, run / "travel", at=epoch, profile_path=self.travel_profile, progress=self.progress)
                self.progress("Live [3/4] Rebuilding edge costs with new weather")
                build_features(self.routing, run / "travel", self.plan, run / "weather", run / "features",
                               risk_path=self.risk_profile, progress=self.progress)
                self.progress("Live [4/4] Validating before publication")
                manifests = validate_stages(self.routing, self.plan, run)
                regions = read_json(run / "weather" / "weather_context.json")["regions"]
                expected = {r["regionId"] for r in read_json(self.plan / "regions.json")["regions"]}
                if len(regions) != len(expected) or {r["regionId"] for r in regions} != expected or not regions:
                    raise ValueError("Incomplete live weather coverage")
                if any(r["fallbackUsed"] or instant(r["fetchedAt"]) < started or r["validAt"] != epoch for r in regions):
                    raise ValueError("Weather was not freshly captured for this attempt")
                snapshot = {"schemaVersion": SCHEMA, "at": epoch, "publishedAt": self.clock(),
                            "settingsHash": digest(self.settings), "versions": {k: v["version"] for k, v in manifests.items()},
                            "manifestHashes": {k: digest(v) for k, v in manifests.items()},
                            "oldestValidAt": min(r["validAt"] for r in regions),
                            "oldestFetchedAt": min(r["fetchedAt"] for r in regions),
                            "newestFetchedAt": max(r["fetchedAt"] for r in regions),
                            "maxAgeHours": self.settings["maxAgeHours"], "regionCount": len(regions),
                            "edgeCount": manifests["features"]["edgeCount"], "checksPassed": True,
                            "integrated": False, "weatherSource": "Open-Meteo hourly model forecast",
                            "trafficSource": "ESTIMATED", "requiresTurnAwareReader": True}
                if not freshness(snapshot, self.clock())["fresh"]:
                    raise ValueError("Live capture is stale at publication; latest remains unchanged")
                write_json(run / "snapshot.json", snapshot)
                pointer = {"schemaVersion": "member1-live-pointer/1", "snapshot": f"snapshots/{name}",
                           "snapshotHash": digest(snapshot), "featureVersion": snapshot["versions"]["features"]}
                # The single commit point: readers see either the old or the fully built new set.
                write_json(self.output / "latest.json", pointer)
            except BaseException as exc:
                write_json(self.output / "status.json", {**status, "state": "interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                                                        "finishedAt": self.clock(), "error": str(exc)})
                raise
            write_json(self.output / "status.json", {**status, "state": "ready", "finishedAt": self.clock(),
                                                    "featureVersion": pointer["featureVersion"]})
            self.progress(f"Live published: {run} | features={pointer['featureVersion']}")
            return snapshot


def load_latest(output, *, now=None, allow_stale=False):
    """Pin and verify one complete version. Call again before each optimization job."""
    output = Path(output).resolve()
    pointer = read_json(output / "latest.json")
    run = (output / pointer["snapshot"]).resolve()
    if pointer.get("schemaVersion") != "member1-live-pointer/1" or run.parent != output / "snapshots":
        raise ValueError("Invalid live snapshot pointer")
    snapshot = read_json(run / "snapshot.json")
    settings = read_json(output / "live-settings.json")
    if (snapshot.get("schemaVersion") != SCHEMA or digest(snapshot) != pointer["snapshotHash"]
            or digest(settings) != snapshot["settingsHash"] or not snapshot["checksPassed"]
            or pointer["featureVersion"] != snapshot["versions"]["features"]):
        raise ValueError("Live snapshot/settings checksum failed")
    manifests = validate_stages(Path(settings["routing"]), Path(settings["plan"]), run)
    if any(digest(m) != snapshot["manifestHashes"][k] or m["version"] != snapshot["versions"][k]
           for k, m in manifests.items()):
        raise ValueError("Live snapshot manifest binding failed")
    health = freshness(snapshot, now or now_vn())
    if not health["fresh"] and not allow_stale:
        raise ValueError(f"Latest weather is stale or clock is inconsistent: {health}")
    return {**snapshot, **health, "routing": settings["routing"], "plan": settings["plan"],
            "weather": str(run / "weather"), "travel": str(run / "travel"), "features": str(run / "features"),
            "snapshot": str(run)}


def watch(updater, *, interval_hours=0.5, cycles=None, sleep=time.sleep):
    """Serial attempts; interval is measured after completion/failure, no overlap."""
    positive(interval_hours, "intervalHours")
    if interval_hours < 1 / 12:
        raise ValueError("Polling interval must be at least 1/12 h (5 minutes)")
    if cycles is not None and (isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 1):
        raise ValueError("cycles must be a positive integer")
    count, failed = 0, False
    while cycles is None or count < cycles:
        count += 1
        updater.progress(f"Live cycle {count}")
        try:
            updater.refresh()
            failed = False
        except ERRORS as exc:
            updater.progress(f"Live cycle failed; previous latest preserved: {exc}")
            failed = True
        if cycles is not None and count >= cycles:
            break
        updater.progress(f"Next attempt in {interval_hours:g} h. Ctrl+C to stop.")
        remaining = interval_hours * 3600
        while remaining > 0:
            step = min(30, remaining)
            sleep(step)
            remaining -= step
    return 1 if failed else 0
