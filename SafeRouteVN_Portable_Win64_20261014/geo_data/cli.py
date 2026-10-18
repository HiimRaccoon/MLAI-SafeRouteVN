"""Run with python -m geo_data.cli from SafeRouteVN/."""

import argparse
from functools import partial
import json
from pathlib import Path
import sys
import sqlite3

import requests

from geo_data.common import read_json, write_json
from geo_data.osm.download_graph import DEFAULT_ENDPOINT, OverpassClient, download_tiles
from geo_data.osm.region import fetch_boundary
from geo_data.osm.tile_index import build_plan, validate_plan


def parser():
    root = argparse.ArgumentParser(description="SafeRoute VN — Member 1 data ingestion")
    commands = root.add_subparsers(dest="command", required=True)
    boundary = commands.add_parser("boundary-fetch", help="Fetch/cache a candidate OSM administrative boundary")
    boundary.add_argument("--osm-id", required=True, help="Explicit OSM relation ID, e.g. R1973756; verify its coverage")
    boundary.add_argument("--output", type=Path, required=True)
    boundary.add_argument("--user-agent", required=True)
    boundary.add_argument("--provider", choices=("nominatim", "overpass"), default="nominatim")
    boundary.add_argument("--refresh", action="store_true")
    area = commands.add_parser("delivery-area-fetch", help="Fetch historical Thu Duc / Binh Thanh polygons for delivery selection")
    area.add_argument("--output", type=Path, required=True)
    area.add_argument("--user-agent", required=True)
    area.add_argument("--config", type=Path, help="Default geo_data/areas/thu_duc_binh_thanh.json")
    area.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    area.add_argument("--attempts", type=int, default=3)
    plan = commands.add_parser("plan", help="Validate GeoJSON and generate a deterministic tile index (no network)")
    plan.add_argument("--boundary", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--region-id", default="hcmc-current")
    plan.add_argument("--scope", choices=("hcmc-current", "test"), default="hcmc-current")
    plan.add_argument("--source", help="Required unless boundary metadata includes source")
    plan.add_argument("--boundary-version", help="Required unless boundary metadata includes boundaryVersion")
    plan.add_argument("--tile-km", type=float, default=5)
    plan.add_argument("--buffer-km", type=float, default=0.25)
    fetch = commands.add_parser("download", help="Resume raw OSM tiles; use --offline to check cache without network")
    fetch.add_argument("--plan", type=Path, required=True)
    fetch.add_argument("--output", type=Path, required=True)
    fetch.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    fetch.add_argument("--user-agent")
    fetch.add_argument("--offline", action="store_true")
    fetch.add_argument("--refresh", action="store_true")
    fetch.add_argument("--max-tiles", type=int, help="Limit new fetch attempts, not cache hits")
    fetch.add_argument("--tile-id", action="append", help="Repeat to select specific tile IDs")
    fetch.add_argument("--osm-date", help="Optional historical OSM snapshot timestamp with timezone")
    fetch.add_argument("--attempts", type=int, default=3)
    fetch.add_argument("--interval-seconds", type=float, default=2,
                       help="HTTP pacing only; business durations remain hours")
    inspect = commands.add_parser("inspect-plan", help="Verify plan checksum and show scope/counts")
    inspect.add_argument("--plan", type=Path, required=True)
    bulk = commands.add_parser("bulk-download", help="Download one country PBF with checksum and HTTP resume")
    bulk.add_argument("--output", type=Path, required=True)
    bulk.add_argument("--url", default="https://download.geofabrik.de/asia/vietnam-latest.osm.pbf")
    bulk.add_argument("--user-agent", required=True)
    bulk.add_argument("--attempts", type=int, default=5)
    extract = commands.add_parser("extract-city", help="Filter highways and restrictions locally from a PBF")
    extract.add_argument("--source", type=Path, required=True)
    extract.add_argument("--boundary", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True)
    extract.add_argument("--buffer-km", type=float, default=0.25)
    extract.add_argument("--scope", choices=("hcmc-current", "test"), default="hcmc-current")
    graph = commands.add_parser("build-graph", help="Build an offline motorcycle graph preview and CSV/QA exports")
    graph.add_argument("--source", type=Path, required=True)
    graph.add_argument("--output", type=Path, required=True)
    graph.add_argument("--policy", type=Path, help="Versioned motorcycle policy JSON; default geo_data/osm/policies/motorcycle_v1.json")
    graph.add_argument("--scope", choices=("hcmc-current", "test"), default="hcmc-current")
    routing = commands.add_parser("prepare-routing", help="Compile via-node turns and quarantine unsupported restrictions")
    routing.add_argument("--graph", type=Path, required=True)
    routing.add_argument("--output", type=Path, required=True)
    travel = commands.add_parser("build-travel", help="Build estimated km/h and hours for a frozen epoch")
    travel.add_argument("--routing", type=Path, required=True)
    travel.add_argument("--output", type=Path, required=True)
    travel.add_argument("--at", required=True, help="ISO timestamp with timezone, e.g. +07:00")
    travel.add_argument("--profile", type=Path)
    grid = commands.add_parser("weather-plan", help="Map all routing edges to regional weather samples")
    grid.add_argument("--routing", type=Path, required=True)
    grid.add_argument("--output", type=Path, required=True)
    grid.add_argument("--grid-km", type=float, default=20)
    weather = commands.add_parser("fetch-weather", help="Fetch/cache hourly model weather, optionally use explicit fallback")
    weather.add_argument("--plan", type=Path, required=True)
    weather.add_argument("--output", type=Path, required=True)
    weather.add_argument("--at", required=True)
    weather.add_argument("--user-agent")
    weather.add_argument("--offline", action="store_true")
    weather.add_argument("--fallback-context", type=Path)
    weather.add_argument("--max-stale-hours", type=float, default=6)
    weather.add_argument("--attempts", type=int, default=3)
    features = commands.add_parser("build-features", help="Combine travel/weather with a versioned safety proxy")
    for name in ("routing", "travel", "plan", "weather", "output"):
        features.add_argument("--" + name, type=Path, required=True)
    features.add_argument("--risk-profile", type=Path)
    scenario = commands.add_parser("generate-scenarios", help="Generate deterministic S0-S8 and reachability diagnostics")
    for name in ("routing", "features", "output"):
        scenario.add_argument("--" + name, type=Path, required=True)
    scenario.add_argument("--seed", type=int, default=42)
    scenario.add_argument("--profile", type=Path)
    scenario.add_argument("--depot-node", type=int)
    scenario.add_argument("--delivery-area", type=Path, help="Filter depots/orders by polygons, keeping all routing edges")
    export = commands.add_parser("export-scenarios", help="Publish QA-approved S0-S8 into scenarios/fixtures and manifests")
    export.add_argument("--run", type=Path, required=True)
    export.add_argument("--scenarios-root", type=Path, default=Path("scenarios"))
    export.add_argument("--suite-id", required=True)
    catalog = commands.add_parser("verify-scenarios", help="Verify exported fixtures, catalog and cached context offline")
    catalog.add_argument("--scenarios-root", type=Path, default=Path("scenarios"))
    catalog.add_argument("--suite-id", required=True)
    route = commands.add_parser("route-check", help="Reference turn-aware route check, not a delivery optimizer")
    route.add_argument("--routing", type=Path, required=True)
    route.add_argument("--travel", type=Path)
    route.add_argument("--features", type=Path)
    route.add_argument("--from-node", type=int, required=True)
    route.add_argument("--to-node", type=int, required=True)
    route.add_argument("--weight", choices=("time", "distance", "exposure"), default="time")
    route.add_argument("--max-states", type=int, default=250000)
    route.add_argument("--incoming-edge", help="Incoming edge ending at --from-node; preserves turn state between legs")
    qa = commands.add_parser("qa", help="Verify complete Member 1 stage lineage, units and scenario invariants offline")
    for name in ("routing", "travel", "plan", "weather", "features", "scenarios", "output"):
        qa.add_argument("--" + name, type=Path, required=True)
    review = commands.add_parser("quality-review", help="Summarize missing data and export regional road samples for review")
    review.add_argument("--run", type=Path, required=True)
    review.add_argument("--output", type=Path, required=True)
    review.add_argument("--sample-radius-km", type=float, default=5)
    handoff = commands.add_parser("build-handoff", help="Copy verified run, source policy and pinned SDK into a portable folder")
    handoff.add_argument("--run", type=Path, required=True)
    handoff.add_argument("--output", type=Path, required=True)
    handoff.add_argument("--review", type=Path)
    handoff.add_argument("--graph", type=Path, help="Defaults to graph path in run-settings.json")
    verify_package = commands.add_parser("verify-handoff", help="Verify a portable package and its QA lineage offline")
    verify_package.add_argument("--package", type=Path, required=True)
    smoke = commands.add_parser("consumer-smoke", help="Read packaged S0, validate saved paths/metrics and export their GeoJSON")
    smoke.add_argument("--package", type=Path, required=True)
    smoke.add_argument("--output", type=Path, required=True)
    for command in ("refresh-live", "watch-live"):
        live = commands.add_parser(command, help="Fetch fresh API weather and atomically publish updated routing costs")
        live.add_argument("--routing", type=Path, required=True)
        live.add_argument("--plan", type=Path, required=True)
        live.add_argument("--output", type=Path, required=True)
        live.add_argument("--user-agent", required=True)
        live.add_argument("--max-age-hours", type=float, default=2)
        live.add_argument("--attempts", type=int, default=3)
        live.add_argument("--travel-profile", type=Path)
        live.add_argument("--risk-profile", type=Path)
        if command == "watch-live":
            live.add_argument("--interval-hours", type=float, default=0.5,
                              help="Wait after each cycle; default 0.5 h, minimum 1/12 h")
            live.add_argument("--cycles", type=int, help="Optional bounded run; default runs until Ctrl+C")
    live_status = commands.add_parser("live-status", help="Verify latest live snapshot and check its age now")
    live_status.add_argument("--output", type=Path, required=True)
    return root


def main(argv=None):
    # Windows redirected streams may default to a code page without Vietnamese.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        progress = partial(print, flush=True)
        if args.command == "delivery-area-fetch":
            from geo_data.delivery_area import DEFAULT_AREA_CONFIG, DeliveryArea, fetch_delivery_area
            document = fetch_delivery_area(args.output, user_agent=args.user_agent,
                                           config_path=args.config or DEFAULT_AREA_CONFIG,
                                           endpoint=args.endpoint, attempts=args.attempts, progress=progress)
            print(json.dumps(DeliveryArea(document).summary, ensure_ascii=False, indent=2))
        elif args.command == "export-scenarios":
            from geo_data.scenario_catalog import export_scenarios
            result = export_scenarios(args.run, args.scenarios_root, suite_id=args.suite_id, progress=progress)
            print(json.dumps({"suiteId": result["suiteId"], "version": result["version"], "complete": True,
                              "scenarios": len(result["scenarios"]), "integrated": False}, indent=2))
        elif args.command == "verify-scenarios":
            from geo_data.scenario_catalog import ScenarioCatalog
            result = ScenarioCatalog(args.scenarios_root, args.suite_id).manifest
            print(json.dumps({"verified": True, "suiteId": result["suiteId"], "version": result["version"],
                              "scenarios": len(result["scenarios"]), "integrated": False}, indent=2))
        elif args.command in ("refresh-live", "watch-live"):
            from geo_data.realtime import LiveUpdater, watch
            from geo_data.features.travel_time import DEFAULT_PROFILE
            from geo_data.features.risk_proxy import DEFAULT_RISK
            updater = LiveUpdater(args.routing, args.plan, args.output, user_agent=args.user_agent,
                                  max_age_hours=args.max_age_hours, attempts=args.attempts,
                                  travel_profile=args.travel_profile or DEFAULT_PROFILE,
                                  risk_profile=args.risk_profile or DEFAULT_RISK, progress=progress)
            if args.command == "watch-live":
                return watch(updater, interval_hours=args.interval_hours, cycles=args.cycles)
            updater.refresh()
        elif args.command == "live-status":
            from geo_data.realtime import load_latest
            if not (args.output / "latest.json").exists():
                status_path = args.output / "status.json"
                print(json.dumps({"ready": False, "lastAttempt": read_json(status_path) if status_path.exists() else None}, indent=2))
                return 2
            result = load_latest(args.output, allow_stale=True)
            status_path = args.output / "status.json"
            print(json.dumps({"ready": result["fresh"], "fresh": result["fresh"], "at": result["at"],
                              "checkedAt": result["checkedAt"], "expiresAt": result["expiresAt"],
                              "weatherAgeHours": result["weatherAgeHours"], "fetchAgeHours": result["fetchAgeHours"],
                              "features": result["features"], "featureVersion": result["versions"]["features"],
                              "lastAttempt": read_json(status_path) if status_path.exists() else None,
                              "integrated": False}, indent=2))
            return 0 if result["fresh"] else 2
        elif args.command == "quality-review":
            from geo_data.quality_review import quality_review
            quality_review(args.run, args.output, sample_radius_km=args.sample_radius_km, progress=progress)
        elif args.command == "build-handoff":
            from geo_data.handoff import build_handoff
            build_handoff(args.run, args.output, review=args.review, graph=args.graph, progress=progress)
        elif args.command == "verify-handoff":
            from geo_data.handoff import verify_handoff
            result = verify_handoff(args.package)
            print(json.dumps({"verified": True, "packageVersion": result["version"], "integrated": False}, indent=2))
        elif args.command == "consumer-smoke":
            from geo_data.consumer import consumer_smoke
            consumer_smoke(args.package, args.output, progress=progress)
        elif args.command == "prepare-routing":
            from geo_data.osm.process_graph import prepare_routing
            prepare_routing(args.graph, args.output, progress=progress)
        elif args.command == "build-travel":
            from geo_data.features.travel_time import build_travel, DEFAULT_PROFILE
            build_travel(args.routing, args.output, at=args.at, profile_path=args.profile or DEFAULT_PROFILE, progress=progress)
        elif args.command == "weather-plan":
            from geo_data.weather.context_builder import weather_plan
            weather_plan(args.routing, args.output, grid_km=args.grid_km, progress=progress)
        elif args.command == "fetch-weather":
            from geo_data.weather.open_meteo_client import fetch_context
            fetch_context(args.plan, args.output, at=args.at, user_agent=args.user_agent, offline=args.offline,
                          fallback_context=args.fallback_context, max_stale_hours=args.max_stale_hours,
                          attempts=args.attempts, progress=progress)
        elif args.command == "build-features":
            from geo_data.features.edge_features import build_features
            from geo_data.features.risk_proxy import DEFAULT_RISK
            build_features(args.routing, args.travel, args.plan, args.weather, args.output,
                           risk_path=args.risk_profile or DEFAULT_RISK, progress=progress)
        elif args.command == "generate-scenarios":
            from geo_data.scenario_generator import generate_scenarios, DEFAULT_SCENARIO
            generate_scenarios(args.routing, args.features, args.output, seed=args.seed,
                               config_path=args.profile or DEFAULT_SCENARIO, depot_node=args.depot_node,
                               delivery_area=args.delivery_area, progress=progress)
        elif args.command == "route-check":
            from geo_data.osm.routing import RoutingGraph
            with RoutingGraph(args.routing, travel=args.travel, features=args.features) as reader:
                result = reader.path(args.from_node, args.to_node, weight=args.weight, max_states=args.max_states, incoming_edge=args.incoming_edge)
                print(json.dumps(result or {"reachable": False, "meaning": "No path under the current conservative policy"}, ensure_ascii=False, indent=2))
                if result is None:
                    return 2
        elif args.command == "qa":
            from geo_data.qa import check_pipeline
            check_pipeline(args.routing, args.travel, args.plan, args.weather, args.features, args.scenarios, args.output, progress=progress)
        elif args.command == "build-graph":
            from geo_data.osm.access_policy import DEFAULT_POLICY
            from geo_data.osm.build_graph import build_graph
            build_graph(args.source, args.output, policy_path=args.policy or DEFAULT_POLICY,
                        scope=args.scope, progress=partial(print, flush=True))
        elif args.command == "bulk-download":
            from geo_data.osm.extract_download import download_extract
            download_extract(args.output, url=args.url, user_agent=args.user_agent,
                             attempts=args.attempts, progress=partial(print, flush=True))
        elif args.command == "extract-city":
            from geo_data.osm.extract_filter import filter_extract
            filter_extract(args.source, args.boundary, args.output, buffer_km=args.buffer_km,
                           scope=args.scope, progress=partial(print, flush=True))
        elif args.command == "boundary-fetch":
            boundary = fetch_boundary(args.osm_id, args.output, user_agent=args.user_agent,
                                      refresh=args.refresh, provider=args.provider)
            print(json.dumps(boundary["properties"], ensure_ascii=False, indent=2))
        elif args.command == "plan":
            boundary = read_json(args.boundary)
            props = boundary.get("properties", {})
            result = build_plan(boundary, region_id=args.region_id,
                                source=args.source or props.get("source", ""),
                                boundary_version=args.boundary_version or props.get("boundaryVersion", ""),
                                tile_km=args.tile_km, buffer_km=args.buffer_km, scope=args.scope)
            if args.output.resolve() == args.boundary.resolve():
                raise ValueError("Plan output must not overwrite boundary input")
            if args.output.exists() and read_json(args.output).get("planVersion") != result["planVersion"]:
                raise ValueError("Existing output has a different plan; choose a new path")
            write_json(args.output, result)
            print(f"{len(result['features'])} tiles; planVersion={result['planVersion']}; output={args.output}")
        elif args.command == "inspect-plan":
            plan = validate_plan(read_json(args.plan))
            print(json.dumps({"scope": plan["scope"], "regionId": plan["regionId"],
                              "planVersion": plan["planVersion"], "tileCount": len(plan["features"]),
                              "coverageChecks": plan["coverageChecks"],
                              "boundaryVerification": plan["boundaryVerification"]}, indent=2))
        elif args.command == "download":
            if not args.offline and not args.user_agent:
                raise ValueError("Online download requires --user-agent")
            client = None if args.offline else OverpassClient(
                endpoint=args.endpoint, user_agent=args.user_agent, attempts=args.attempts,
                min_interval_sec=args.interval_seconds,
            )
            manifest = download_tiles(read_json(args.plan), args.output, client=client,
                                      endpoint=args.endpoint, offline=args.offline, refresh=args.refresh,
                                      max_tiles=args.max_tiles, tile_ids=args.tile_id, osm_date=args.osm_date,
                                      progress=partial(print, flush=True))
            print(json.dumps({"complete": manifest["complete"], "summary": manifest["summary"],
                              "routingReady": manifest["routingReady"]}, indent=2))
            if manifest["summary"]["failed"]:
                return 1
            # A deliberate batch limit/selection can succeed with partial coverage.
            if args.offline and not args.tile_id and not manifest["complete"]:
                return 1
        return 0
    except KeyboardInterrupt:
        print("Stopped. The last published snapshot remains available; check live-status for freshness.", file=sys.stderr)
        return 130
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, sqlite3.Error, requests.RequestException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
