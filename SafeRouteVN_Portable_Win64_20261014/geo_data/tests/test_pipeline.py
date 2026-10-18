"""Synthetic end-to-end pipeline and mocked HTTP tests; no city jobs or live API."""

import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import requests

from geo_data.bundle import database, instant, verify
from geo_data.cli import main as cli_main
from geo_data.common import digest, read_json, write_json
from geo_data.depot_selection import probe_depot, select_depot
from geo_data.features.edge_features import build_features
from geo_data.features.risk_proxy import DEFAULT_RISK, load_risk, risk_values
from geo_data.features.travel_time import DEFAULT_PROFILE, build_travel, load_profile, parse_maxspeed, travel_values
from geo_data.osm.build_graph import build_graph
from geo_data.osm.process_graph import prepare_routing
from geo_data.osm.routing import RoutingGraph, SearchLimitError
from geo_data.qa import check_pipeline
from geo_data.pipeline import run_pipeline
from geo_data.scenario_generator import DEFAULT_SCENARIO, generate_scenarios, validate_scenario
from geo_data.weather.context_builder import weather_plan
from geo_data.weather.open_meteo_client import fetch_context, normalize, request_weather
from test_graph import osm_xml

AT = "2026-09-27T09:00:00+07:00"
QUIET = lambda _: None


def payload(at=AT):
    return {"latitude": 10.77, "longitude": 106.7,
            "hourly_units": {"time": "unixtime", "precipitation": "mm", "wind_speed_10m": "km/h", "visibility": "m", "weather_code": "wmo code"},
            "hourly": {"time": [int(instant(at).timestamp())], "precipitation": [2], "wind_speed_10m": [12], "visibility": [8000], "weather_code": [3]}}


def relation(value="no_left_turn", *, via_way=False, extra="", same_way=False):
    via = '<member type="way" ref="30" role="via"/>' if via_way else '<member type="node" ref="2" role="via"/>'
    to = 10 if same_way else 20
    return f'''<relation id="100" version="1"><member type="way" ref="10" role="from"/>{via}
      <member type="way" ref="{to}" role="to"/><tag k="type" v="restriction"/>
      <tag k="restriction" v="{value}"/>{extra}</relation>'''


class PipelineFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.graph, self.routing, self.travel, self.plan, self.weather, self.features = [self.root / n for n in ("graph", "routing", "travel", "plan", "weather", "features")]
        self.nodes = {1: (106.700, 10.770, {}), 2: (106.705, 10.770, {}), 3: (106.710, 10.770, {}),
                      4: (106.705, 10.772, {}), 5: (106.710, 10.772, {}), 6: (106.700, 10.772, {}),
                      7: (106.700, 10.774, {}), 8: (106.705, 10.774, {}), 9: (106.710, 10.774, {})}
        self.ways = [(10, [1, 2], {"highway": "primary", "surface": "asphalt", "width": "6", "maxspeed": "25"}),
                     (20, [2, 3], {"highway": "primary", "surface": "asphalt"}),
                     (30, [1, 6, 4, 5, 3], {"highway": "residential", "surface": "asphalt"}),
                     (40, [6, 7, 8, 9, 5], {"highway": "service", "surface": "gravel"}),
                     (50, [4, 8], {"highway": "residential"})]
        # Tagged nodes are preserved as graph nodes rather than simplified away.
        for node in self.nodes:
            lon, lat, _ = self.nodes[node]
            self.nodes[node] = (lon, lat, {"name": f"synthetic-{node}"})
        self.session = Mock()
        response = Mock(status_code=200, headers={})
        response.json.return_value = payload()
        self.session.get.return_value = response

    def network(self, restriction=""):
        source = self.root / "source.osm"
        source.write_text(osm_xml(self.nodes, self.ways, restriction), encoding="utf-8")
        build_graph(source, self.graph, scope="test", progress=QUIET)
        return prepare_routing(self.graph, self.routing, progress=QUIET)

    def pipeline(self):
        self.network(relation())
        build_travel(self.routing, self.travel, at=AT, progress=QUIET)
        weather_plan(self.routing, self.plan, grid_km=1, progress=QUIET)
        fetch_context(self.plan, self.weather, at=AT, user_agent="synthetic-tests", session=self.session, sleep=QUIET, progress=QUIET)
        return build_features(self.routing, self.travel, self.plan, self.weather, self.features, progress=QUIET)


class RoutingTests(PipelineFixture):
    def test_auto_depot_skips_nearest_disconnected_service_road(self):
        self.nodes[101] = (106.70002, 10.77002, {})
        self.nodes[102] = (106.70003, 10.77003, {})
        self.ways.append((60, [101, 102], {"highway": "service"}))
        self.network()
        config = read_json(DEFAULT_SCENARIO)
        config.update(anchorLongitude=106.70002, anchorLatitude=10.77002, orders=3, minDistanceKm=0.01)
        audit = []
        with RoutingGraph(self.routing) as reader:
            node = select_depot(reader, config, audit=audit, progress=QUIET)
            self.assertNotIn(node["nodeId"], (101, 102))
            self.assertEqual(audit[0]["nodeId"], 101)
            self.assertEqual(audit[0]["status"], "insufficient-local-outbound-nodes")
            self.assertTrue(audit[-1]["qualified"])
            limited = probe_depot(reader, node, {**config, "depotProbeMaxStates": 1})
            self.assertEqual(limited["status"], "probe-budget-exceeded")
            reader.forbidden.update((edge["edgeId"], following["edgeId"])
                                    for edge in reader.outgoing(node["nodeId"])
                                    for following in reader.outgoing(edge["toNodeId"]))
            blocked = probe_depot(reader, node, {**config, "orders": 8})
            self.assertFalse(blocked["qualified"])

    def test_no_turn_forces_detour_and_validates_sequences(self):
        result = self.network(relation())
        self.assertTrue(result["routingReady"])
        with RoutingGraph(self.routing) as reader:
            path = reader.path(1, 3, weight="distance")
            self.assertIsNotNone(path)
            self.assertNotEqual(path["edgeIds"], ["w10:0-1:forward", "w20:0-1:forward"])
            self.assertEqual(reader.validate_path(1, path["edgeIds"]), 3)
            with self.assertRaisesRegex(ValueError, "Invalid edge sequence"):
                reader.validate_path(1, ["w10:0-1:forward", "w20:0-1:forward"])
            with self.assertRaises(SearchLimitError):
                reader.path(1, 9, weight="distance", max_states=1)

    def test_no_u_turn_does_not_block_same_way_continuation(self):
        self.ways[0] = (10, [1, 2, 3], {"highway": "primary"})
        self.network(relation("no_u_turn", same_way=True))
        with RoutingGraph(self.routing) as reader:
            self.assertEqual(reader.validate_path(1, ["w10:0-1:forward", "w10:1-2:forward"]), 3)
            with self.assertRaises(ValueError):
                reader.validate_path(1, ["w10:0-1:forward", "w10:0-1:backward"])

    def test_only_turn_bans_other_exit_including_reverse(self):
        self.network(relation("only_straight_on"))
        with RoutingGraph(self.routing) as reader:
            self.assertEqual(reader.validate_path(1, ["w10:0-1:forward", "w20:0-1:forward"]), 3)
            with self.assertRaises(ValueError):
                reader.validate_path(1, ["w10:0-1:forward", "w10:0-1:backward"])

    def test_via_way_is_quarantined_in_both_directions(self):
        self.network(relation(via_way=True))
        with database(self.routing / "network.sqlite") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM edges WHERE osmWayId=10").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM excluded_edges").fetchone()[0], 2)

    def test_conditional_is_quarantined(self):
        self.network(relation(extra='<tag k="restriction:conditional" v="no_left_turn @ (06:00-09:00)"/>'))
        report = read_json(self.routing / "restriction_report.json")
        self.assertEqual(report["records"][0]["reason"], "conditional")
        self.assertEqual(report["excludedEdges"], 2)

    def test_motorcycle_exception_keeps_the_turn(self):
        self.network(relation(extra='<tag k="except" v="bicycle; motorcycle"/>'))
        with RoutingGraph(self.routing) as reader:
            self.assertEqual(reader.validate_path(1, ["w10:0-1:forward", "w20:0-1:forward"]), 3)


class TravelAndRiskTests(unittest.TestCase):
    def test_speed_cap_mph_surface_and_hour_units(self):
        profile = load_profile(DEFAULT_PROFILE)
        edge = {"highway": "primary", "direction": "forward", "lengthKm": 2}
        speed, base, total, flags = travel_values(edge, {"surface": "asphalt", "maxspeed:forward": "10 mph"}, profile, profile["timeBuckets"][2])
        self.assertAlmostEqual(speed, 16.09344)
        self.assertAlmostEqual(base, 2 / speed)
        self.assertAlmostEqual(total, base * 1.5)
        self.assertEqual(flags, [])
        self.assertIsNone(parse_maxspeed("VN:urban"))
        self.assertIsNone(parse_maxspeed("0"))
        self.assertIsNone(parse_maxspeed("NaN"))

    def test_invalid_profile_buckets_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = read_json(DEFAULT_PROFILE)
            profile["timeBuckets"][0]["endHour"] = 7
            path = Path(temp) / "profile.json"
            write_json(path, profile)
            with self.assertRaises(ValueError):
                load_profile(path)

    def test_missing_width_and_weather_have_explicit_proxy_fallback(self):
        config = load_risk(DEFAULT_RISK)
        result = risk_values({"highway": "primary", "widthM": None, "lengthKm": 2}, {},
                             {"precipitationMm": None, "windSpeedKph": 120, "visibilityM": None,
                              "precipitationIntervalHours": 1, "fallbackUsed": True},
                             {"timeFactor": 0.2, "trafficFactor": 0.8}, config)
        self.assertEqual(result["weatherFactor"], 1)
        self.assertAlmostEqual(result["relativeExposure"], 2 * result["edgeProxy"])
        self.assertIn("risk:width-fallback", result["missingFlags"])
        self.assertIn("weather:stale-fallback", result["missingFlags"])
        self.assertEqual(result["sourceType"], "PROXY")


class WeatherTests(PipelineFixture):
    def test_interrupted_region_fetch_resumes_without_refetching_valid_raw(self):
        self.network()
        weather_plan(self.routing, self.plan, grid_km=0.5, progress=QUIET)
        regions = read_json(self.plan / "regions.json")["regions"]
        self.assertGreater(len(regions), 1)
        good = self.session.get.return_value
        self.session.get.side_effect = [good, requests.ConnectionError("synthetic interruption")]
        with self.assertRaisesRegex(ValueError, "unavailable"):
            fetch_context(self.plan, self.weather, at=AT, user_agent="test", attempts=1, session=self.session, sleep=QUIET, progress=QUIET)
        self.assertFalse((self.weather / "manifest.json").exists())
        self.session.reset_mock()
        self.session.get.side_effect = None
        fetch_context(self.plan, self.weather, at=AT, user_agent="test", session=self.session, sleep=QUIET, progress=QUIET)
        self.assertEqual(self.session.get.call_count, len(regions)-1)

    def test_new_snapshot_payload_changes_context_version(self):
        self.pipeline()
        old = read_json(self.weather / "manifest.json")
        changed = payload()
        changed["hourly"]["precipitation"] = [8]
        self.session.get.return_value.json.return_value = changed
        new = fetch_context(self.plan, self.root / "weather-new", at=AT, user_agent="test", session=self.session, sleep=QUIET, progress=QUIET)
        self.assertNotEqual(old["contextVersion"], new["contextVersion"])
        self.assertNotEqual(old["version"], new["version"])

    def test_normalization_timezone_null_and_unit_rejection(self):
        data = payload()
        data["hourly"]["visibility"] = [None]
        region = {"regionId": "test", "latitude": 10, "longitude": 106}
        record = normalize(data, at=AT, fetched_at=AT, region=region)
        self.assertEqual(record["validAt"], AT)
        self.assertIsNone(record["visibilityM"])
        self.assertIn("visibilityM", record["missingFlags"])
        self.assertEqual(record["precipitationIntervalHours"], 1)
        data["hourly_units"]["wind_speed_10m"] = "m/s"
        with self.assertRaisesRegex(ValueError, "Unexpected weather unit"):
            normalize(data, at=AT, fetched_at=AT, region=region)

    def test_retry_timeout_and_retryable_http(self):
        ok = Mock(status_code=200, headers={})
        ok.json.return_value = payload()
        session = Mock()
        session.get.side_effect = [requests.Timeout("test"), Mock(status_code=429, headers={"Retry-After": "2"}), ok]
        self.assertEqual(request_weather({}, "test", session=session, sleep=QUIET), payload())
        self.assertEqual(session.get.call_count, 3)

    def test_cached_offline_and_explicit_fallback_preserves_timestamp(self):
        self.pipeline()
        self.session.reset_mock()
        fetch_context(self.plan, self.weather, at=AT, offline=True, session=self.session, progress=QUIET)
        self.session.get.assert_not_called()
        later = "2026-09-27T10:00:00+07:00"
        result = fetch_context(self.plan, self.root / "fallback", at=later, offline=True, fallback_context=self.weather, progress=QUIET)
        self.assertGreater(result["fallbackRegions"], 0)
        record = read_json(self.root / "fallback/weather_context.json")["regions"][0]
        self.assertTrue(record["fallbackUsed"])
        self.assertEqual(record["validAt"], AT)
        self.assertEqual(record["staleAgeHours"], 1)
        with self.assertRaisesRegex(ValueError, "no valid explicit fallback"):
            fetch_context(self.plan, self.root / "too-stale", at=later, offline=True, fallback_context=self.weather, max_stale_hours=0.5, progress=QUIET)

    def test_missing_offline_context_fails_instead_of_inventing_weather(self):
        self.network()
        weather_plan(self.routing, self.plan, grid_km=1, progress=QUIET)
        with self.assertRaisesRegex(ValueError, "no valid explicit fallback"):
            fetch_context(self.plan, self.weather, at=AT, offline=True, progress=QUIET)
        self.assertFalse((self.weather / "manifest.json").exists())


class EndToEndTests(PipelineFixture):
    def test_legacy_failed_scenario_attempt_can_resume_without_rebuilding_inputs(self):
        self.pipeline()
        config = read_json(DEFAULT_SCENARIO)
        config.update(orders=3, radiusKm=5, minDistanceKm=0.01, candidateLimit=20, searchMaxStates=10000)
        config_path = self.root / "scenario-profile.json"
        write_json(config_path, config)
        output = self.root / "failed-attempt"
        net, features = read_json(self.routing / "manifest.json"), read_json(self.features / "manifest.json")
        legacy = {"stage": "member1-scenarios/1", "routingVersion": net["version"], "featuresVersion": features["version"],
                  "seed": 42, "profileHash": digest(config), "depotNode": 1}
        write_json(output / "build-state.json", legacy)
        result = generate_scenarios(self.routing, self.features, output, config_path=config_path, depot_node=1, progress=QUIET)
        self.assertTrue(result["complete"])
        self.assertEqual(result["identity"]["generatorVersion"], "connected-depot/2")
        self.assertTrue(read_json(output / "diagnostics.json")["complete"])
        self.assertEqual(read_json(self.routing / "manifest.json"), net)

    def test_explicit_isolated_depot_is_not_moved_and_failure_details_are_saved(self):
        self.nodes[101] = (106.70002, 10.77002, {})
        self.nodes[102] = (106.70003, 10.77003, {})
        self.ways.append((60, [101, 102], {"highway": "service"}))
        self.pipeline()
        config = read_json(DEFAULT_SCENARIO)
        config.update(orders=3, radiusKm=5, minDistanceKm=0.01, candidateLimit=20)
        config_path = self.root / "scenario-profile.json"
        write_json(config_path, config)
        output = self.root / "isolated"
        with self.assertRaisesRegex(ValueError, "depot=101"):
            generate_scenarios(self.routing, self.features, output, config_path=config_path, depot_node=101, progress=QUIET)
        report = read_json(output / "diagnostics.json")
        self.assertEqual(report["depotNodeId"], 101)
        self.assertEqual(report["reachableCount"], 0)
        self.assertTrue(all(r["status"] == "outbound-unreachable-under-policy" for r in report["diagnostics"]))
        self.assertFalse((output / "manifest.json").exists())

    def test_runner_freezes_epoch_and_resumes_only_requested_stages(self):
        self.network()
        output = self.root / "run"
        first = run_pipeline(self.graph, output, at=AT, stop_after="travel", progress=QUIET)
        second = run_pipeline(self.graph, output, stop_after="travel", progress=QUIET)
        self.assertEqual(first, second)
        self.assertEqual(read_json(output / "run-settings.json")["at"], AT)
        self.assertFalse((output / "weather").exists())
        with self.assertRaisesRegex(ValueError, "settings changed"):
            run_pipeline(self.graph, output, at="2026-09-27T10:00:00+07:00", stop_after="travel", progress=QUIET)

    def test_pipeline_scenarios_determinism_and_qa(self):
        result = self.pipeline()
        self.assertGreater(result["edgeCount"], 0)
        config = read_json(DEFAULT_SCENARIO)
        config.update(orders=3, radiusKm=5, minDistanceKm=0.01, candidateLimit=20, searchMaxStates=10000)
        config_path = self.root / "scenario-profile.json"
        write_json(config_path, config)
        one, two = self.root / "scenarios-one", self.root / "scenarios-two"
        first = generate_scenarios(self.routing, self.features, one, config_path=config_path, depot_node=1, progress=QUIET)
        second = generate_scenarios(self.routing, self.features, two, config_path=config_path, depot_node=1, progress=QUIET)
        self.assertEqual(first["files"], second["files"])
        self.assertEqual(first["version"], second["version"])
        s0 = read_json(one / "S0.json")
        self.assertEqual(len(s0["initialState"]["orders"]), 3)
        self.assertEqual(len(s0["initialState"]["vehicles"]), 2)
        s3 = read_json(one / "S3.json")
        self.assertEqual(s3["initialState"]["orders"][0]["status"], "ONBOARD")
        s3["initialState"]["vehicles"][0]["currentLoadKg"] = 0
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            validate_scenario(s3)
        qa = check_pipeline(self.routing, self.travel, self.plan, self.weather, self.features, one, self.root / "qa", progress=QUIET)
        self.assertTrue(qa["checksPassed"])
        self.assertFalse(qa["integrated"])
        with RoutingGraph(self.routing, features=self.features) as reader:
            route = reader.path(1, 3)
            self.assertGreater(route["travelTimeHours"], 0)
            self.assertGreater(route["relativeExposure"], 0)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli_main(["route-check", "--routing", str(self.routing), "--features", str(self.features),
                             "--from-node", "1", "--to-node", "3", "--weight", "time"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["toNodeId"], 3)

    def test_snapshot_mismatch_and_tampering_rejected(self):
        self.pipeline()
        late = self.root / "late-travel"
        build_travel(self.routing, late, at="2026-09-27T10:00:00+07:00", progress=QUIET)
        with self.assertRaisesRegex(ValueError, "same decision epoch"):
            build_features(self.routing, late, self.plan, self.weather, self.root / "bad-features", progress=QUIET)
        (self.features / "edge_features.jsonl").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum"):
            verify(self.features)


if __name__ == "__main__":
    unittest.main()
