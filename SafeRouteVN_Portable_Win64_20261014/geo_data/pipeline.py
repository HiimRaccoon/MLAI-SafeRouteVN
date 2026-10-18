"""Convenience runner. The user launches city jobs; development tests use fixtures."""

import argparse
from pathlib import Path
import sys
import sqlite3

import requests

from geo_data.bundle import instant, positive
from geo_data.common import exclusive_lock, now_vn, read_json, write_json
from geo_data.delivery_area import DeliveryArea
from geo_data.features.edge_features import build_features
from geo_data.features.risk_proxy import DEFAULT_RISK
from geo_data.features.travel_time import DEFAULT_PROFILE, build_travel
from geo_data.osm.process_graph import prepare_routing
from geo_data.qa import check_pipeline
from geo_data.scenario_generator import DEFAULT_SCENARIO, generate_scenarios
from geo_data.weather.context_builder import weather_plan
from geo_data.weather.open_meteo_client import fetch_context

STEPS = ("routing", "travel", "weather-plan", "weather", "features", "scenarios", "qa")


def run_pipeline(graph, output, *, at=None, grid_km=20, seed=42, user_agent="SafeRouteVN/0.1 Member1 development",
                 offline_weather=False, stop_after="qa", travel_profile=DEFAULT_PROFILE,
                 risk_profile=DEFAULT_RISK, scenario_profile=DEFAULT_SCENARIO, depot_node=None, delivery_area=None, progress=print):
    if stop_after not in STEPS:
        raise ValueError("Unknown stop-after stage")
    positive(grid_km, "gridKm")
    required_area = read_json(scenario_profile).get("requiredDeliveryAreaId")
    if required_area and (not delivery_area or DeliveryArea(read_json(delivery_area)).summary["areaId"] != required_area):
        raise ValueError("Scenario profile requires its matching --delivery-area")
    graph, output = Path(graph), Path(output)
    with exclusive_lock(output):
        settings_path = output / "run-settings.json"
        old = read_json(settings_path) if settings_path.exists() else None
        epoch = instant(at or (old["at"] if old else now_vn()))
        if at is None and old is None:
            epoch = epoch.replace(minute=0, second=0, microsecond=0)
        settings = {"at": epoch.isoformat(), "graph": str(graph.resolve()), "gridKm": grid_km, "seed": seed,
                    "travelProfile": str(Path(travel_profile).resolve()), "riskProfile": str(Path(risk_profile).resolve()),
                    "scenarioProfile": str(Path(scenario_profile).resolve()), "depotNode": depot_node}
        if delivery_area:
            settings.update(deliveryArea=str(Path(delivery_area).resolve()),
                            deliveryAreaVersion=DeliveryArea(read_json(delivery_area)).version)
        if old and old != settings:
            raise ValueError("Run settings changed; use a new --output directory for a new snapshot")
        write_json(settings_path, settings)
        routing, travel, plan, weather, features, scenarios, qa = [output / name for name in STEPS]
        actions = (
            lambda: prepare_routing(graph, routing, progress=progress),
            lambda: build_travel(routing, travel, at=settings["at"], profile_path=travel_profile, progress=progress),
            lambda: weather_plan(routing, plan, grid_km=grid_km, progress=progress),
            lambda: fetch_context(plan, weather, at=settings["at"], user_agent=user_agent, offline=offline_weather, progress=progress),
            lambda: build_features(routing, travel, plan, weather, features, risk_path=risk_profile, progress=progress),
            lambda: generate_scenarios(routing, features, scenarios, seed=seed, config_path=scenario_profile, depot_node=depot_node, delivery_area=delivery_area, progress=progress),
            lambda: check_pipeline(routing, travel, plan, weather, features, scenarios, qa, progress=progress),
        )
        progress(f"Frozen decision epoch: {settings['at']} | output={output}")
        for index, (name, action) in enumerate(zip(STEPS, actions), 1):
            progress(f"Pipeline [{index}/{len(STEPS)}] {name}")
            result = action()
            if name == "scenarios" and not result["suiteReady"]:
                progress("S7/S8 tradeoff evidence is incomplete; see scenarios/qa_report.json. Suite is not accepted yet.")
            if name == stop_after:
                return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run Member 1 stages after build-graph; resume verified stages")
    parser.add_argument("--graph", type=Path, default=Path("scenarios/cached_context/hcmc/graph-v1"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--at", help="Timezone-aware decision epoch; first default is current Vietnam hour, then frozen in run-settings.json")
    parser.add_argument("--grid-km", type=float, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--user-agent", default="SafeRouteVN/0.1 Member1 development")
    parser.add_argument("--offline-weather", action="store_true")
    parser.add_argument("--stop-after", choices=STEPS, default="qa")
    parser.add_argument("--travel-profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--risk-profile", type=Path, default=DEFAULT_RISK)
    parser.add_argument("--scenario-profile", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--depot-node", type=int)
    parser.add_argument("--delivery-area", type=Path, help="Versioned GeoJSON delivery polygons; routing graph remains unchanged")
    args = vars(parser.parse_args(argv))
    try:
        run_pipeline(**args, progress=lambda text: print(text, flush=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, sqlite3.Error, requests.RequestException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
