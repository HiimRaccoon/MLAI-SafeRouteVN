"""Portable handoff, exact QA binding and consumer smoke on synthetic fixtures."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from geo_data.bundle import verify
from geo_data.common import read_json, write_json
from geo_data.consumer import Member1Dataset, consumer_smoke
from geo_data.features.edge_features import build_features
from geo_data.features.risk_proxy import DEFAULT_RISK
from geo_data.features.travel_time import build_travel
from geo_data.handoff import build_handoff, copy_verified, verify_handoff
from geo_data.osm.extract_download import file_hash
from geo_data.qa import check_pipeline
from geo_data.quality_review import quality_review
from geo_data.scenario_generator import DEFAULT_SCENARIO, generate_scenarios
from geo_data.weather.context_builder import weather_plan
from geo_data.weather.open_meteo_client import fetch_context
from test_pipeline import AT, QUIET, PipelineFixture


class HandoffFixture(PipelineFixture):
    def setUp(self):
        super().setUp()
        self.base = self.root
        self.root = self.base / "run"
        self.root.mkdir()
        self.graph = self.base / "graph"
        self.routing, self.travel, self.plan, self.weather, self.features = [self.root / name for name in ("routing", "travel", "weather-plan", "weather", "features")]
        self.package = self.base / "package"

    def ready_run(self):
        self.network()
        build_travel(self.routing, self.travel, at=AT, progress=QUIET)
        weather_plan(self.routing, self.plan, grid_km=1, progress=QUIET)
        fetch_context(self.plan, self.weather, at=AT, user_agent="tests", session=self.session, sleep=QUIET, progress=QUIET)
        risk = read_json(DEFAULT_RISK)
        risk["weights"] = {"roadFactor": 1, "timeFactor": 0, "weatherFactor": 0, "trafficFactor": 0}
        risk["roadWeights"] = {"highway": 1, "width": 0, "surface": 0}
        risk["highwayFactor"].update(primary=1, residential=0, service=0.5)
        risk_path = self.base / "synthetic-risk.json"
        write_json(risk_path, risk)
        build_features(self.routing, self.travel, self.plan, self.weather, self.features, risk_path=risk_path, progress=QUIET)
        config = read_json(DEFAULT_SCENARIO)
        config.update(orders=3, radiusKm=5, minDistanceKm=0.01, candidateLimit=20, searchMaxStates=10000)
        config_path = self.base / "synthetic-scenarios.json"
        write_json(config_path, config)
        result = generate_scenarios(self.routing, self.features, self.root / "scenarios", config_path=config_path, depot_node=1, progress=QUIET)
        self.assertTrue(result["suiteReady"], "Fixture must contain measured single-path tradeoff evidence")
        check_pipeline(self.routing, self.travel, self.plan, self.weather, self.features, self.root / "scenarios", self.root / "qa", progress=QUIET)
        write_json(self.root / "run-settings.json", {"graph": str(self.graph), "at": AT})


class HandoffTests(HandoffFixture):
    def test_portable_package_and_s0_geometry_metrics(self):
        self.ready_run()
        review = self.base / "review"
        quality_review(self.root, review, progress=QUIET)
        built = build_handoff(self.root, self.package, review=review, progress=QUIET)
        self.assertTrue(built["reviewIncluded"])
        self.assertFalse(built["integrated"])
        self.assertIn("docs/member1_runbook.md", built["files"])
        self.assertEqual(build_handoff(self.root, self.package, review=review, progress=QUIET), built)
        moved = self.base / "other-machine" / "snapshot"
        shutil.copytree(self.package, moved)
        self.assertEqual(verify_handoff(moved)["version"], built["version"])
        with Member1Dataset(moved) as dataset:
            s0 = dataset.scenario()
            self.assertEqual(len(s0["initialState"]["orders"]), 3)
            route = dataset.graph.path(1, 3)
            described = dataset.describe_path(1, route["edgeIds"])
            self.assertAlmostEqual(described["travelTimeHours"], route["travelTimeHours"])
            self.assertAlmostEqual(described["distanceKm"], route["distanceKm"])
            self.assertEqual(described["geojson"]["features"][0]["geometry"], dataset.edge(route["edgeIds"][0])["geometry"])
            with self.assertRaises(ValueError):
                dataset.describe_path(9, route["edgeIds"])
        smoke = consumer_smoke(moved, self.base / "smoke", progress=QUIET)
        self.assertTrue(smoke["consumerSmokePassed"])
        self.assertEqual(smoke["legsChecked"], 6)
        self.assertFalse(smoke["integrated"])
        env = {**os.environ, "PYTHONPATH": ""}
        process = subprocess.run([sys.executable, "-m", "geo_data.examples.read_handoff", "--package", ".."],
                                 cwd=moved / "sdk", env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(process.stdout)["orders"], 3)

    def test_tampered_sdk_and_output_inside_source_rejected(self):
        self.ready_run()
        with self.assertRaisesRegex(ValueError, "separate"):
            build_handoff(self.root, self.root / "bad", progress=QUIET)
        build_handoff(self.root, self.package, progress=QUIET)
        with (self.package / "sdk/geo_data/consumer.py").open("a", encoding="utf-8") as stream:
            stream.write("\n# tampered\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            verify_handoff(self.package)

    def test_qa_for_other_snapshot_does_not_authorize_handoff(self):
        self.ready_run()
        qa_path = self.root / "qa/manifest.json"
        qa = read_json(qa_path)
        qa["identity"]["inputs"]["features"]["version"] = "another-snapshot"
        write_json(qa_path, qa)
        with self.assertRaisesRegex(ValueError, "different features"):
            build_handoff(self.root, self.package, progress=QUIET)

    def test_source_policy_must_match_routing_snapshot(self):
        self.ready_run()
        policy_path = self.graph / "policy.json"
        policy = read_json(policy_path)
        policy["policyVersion"] = "changed-after-graph-build"
        write_json(policy_path, policy)
        with self.assertRaisesRegex(ValueError, "provenance checksum"):
            build_handoff(self.root, self.package, progress=QUIET)

    def test_copy_restarts_partial_and_reuses_verified_file(self):
        source, target = self.base / "source.bin", self.base / "copy.bin"
        source.write_bytes(b"synthetic snapshot bytes")
        target.with_name(target.name + ".part").write_bytes(b"interrupted")
        self.assertEqual(copy_verified(source, target, file_hash(source)), "copied")
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertEqual(copy_verified(source, target, file_hash(source)), "cached")
        target.write_bytes(b"bad")
        with self.assertRaisesRegex(ValueError, "different checksum"):
            copy_verified(source, target, file_hash(source))

    def test_quality_review_preserves_missing_values_and_is_not_manual_acceptance(self):
        self.ready_run()
        result = quality_review(self.root, self.base / "review", progress=QUIET)
        self.assertTrue(result["computedReviewReady"])
        self.assertFalse(result["manualReviewComplete"])
        report = read_json(self.base / "review/quality_report.json")
        self.assertEqual(sum(row["directedEdges"] for row in report["byHighway"]), result["directedEdges"])
        self.assertGreater(sum(row["widthMissingEdges"] for row in report["byHighway"]), 0)
        self.assertEqual(len(report["regionalSamples"]), 6)
        verify(self.base / "review", "member1-quality-review/1")
