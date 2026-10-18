"""Portable fixtures/catalog exports from an actual synthetic QA pipeline."""

import shutil
from unittest.mock import patch

from geo_data.common import digest, read_json, write_json
from geo_data.handoff import copy_verified
from geo_data.osm.extract_download import file_hash
from geo_data.scenario_catalog import ScenarioCatalog, export_scenarios
from test_handoff import HandoffFixture
from test_pipeline import QUIET


class ScenarioCatalogTests(HandoffFixture):
    def setUp(self):
        super().setUp()
        self.scenarios_root = self.base / "scenarios"
        self.root = self.scenarios_root / "cached_context/hcmc/run-v1"
        self.root.mkdir(parents=True)
        self.routing, self.travel, self.plan, self.weather, self.features = [self.root / n for n in
            ("routing", "travel", "weather-plan", "weather", "features")]
        self.ready_run()

    def export(self, suite="hcmc-v1"):
        return export_scenarios(self.root, self.scenarios_root, suite_id=suite, progress=QUIET)

    def test_exports_exact_bytes_and_loads_after_moving_project(self):
        before = file_hash(self.root / "scenarios/manifest.json")
        result = self.export()
        self.assertEqual(self.export(), result)
        self.assertEqual(result["sourceRun"], "cached_context/hcmc/run-v1")
        self.assertEqual(len(result["scenarios"]), 9)
        self.assertFalse(result["integrated"])
        self.assertEqual(file_hash(self.root / "scenarios/manifest.json"), before)
        for sid in result["scenarios"]:
            source = self.root / "scenarios" / (sid + ".json")
            exported = self.scenarios_root / result["scenarios"][sid]["file"]
            self.assertEqual(source.read_bytes(), exported.read_bytes())
        moved = self.base / "new-machine/project/scenarios"
        shutil.copytree(self.scenarios_root, moved)
        catalog = ScenarioCatalog(moved, "hcmc-v1")
        self.assertEqual(len(catalog.scenario("S0")["initialState"]["orders"]), 3)
        self.assertTrue(catalog.paths["features"].is_relative_to(moved))
        with catalog.open_graph() as graph:
            self.assertIsNotNone(graph.path(1, 3))

    def test_interrupted_copy_is_not_published_and_can_resume(self):
        calls = 0
        def interrupted(*args):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("simulated disk interruption")
            return copy_verified(*args)
        with patch("geo_data.scenario_catalog.copy_verified", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.export()
        self.assertFalse((self.scenarios_root / "manifests/hcmc-v1.json").exists())
        with self.assertRaises(FileNotFoundError):
            ScenarioCatalog(self.scenarios_root, "hcmc-v1")
        self.export()
        self.assertEqual(ScenarioCatalog(self.scenarios_root, "hcmc-v1").scenario()["scenarioId"], "S0")

    def test_fixture_tampering_is_rejected_even_by_open_reader(self):
        self.export()
        catalog = ScenarioCatalog(self.scenarios_root, "hcmc-v1")
        path = self.scenarios_root / "fixtures/hcmc-v1/S0.json"
        scenario = read_json(path)
        scenario["initialState"]["orders"][0]["demandKg"] += 1
        write_json(path, scenario)
        with self.assertRaisesRegex(ValueError, "checksum"):
            catalog.scenario("S0")
        with self.assertRaisesRegex(ValueError, "checksum"):
            ScenarioCatalog(self.scenarios_root, "hcmc-v1")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.export()

    def test_catalog_tampering_and_path_escape_are_rejected(self):
        self.export()
        path = self.scenarios_root / "manifests/hcmc-v1.json"
        saved = read_json(path)
        write_json(path, {**saved, "seed": -99})
        with self.assertRaisesRegex(ValueError, "checksum/identity"):
            ScenarioCatalog(self.scenarios_root, "hcmc-v1")
        escaped = {**saved, "sourceRun": "cached_context/../../outside"}
        escaped["version"] = digest({k: v for k, v in escaped.items() if k not in ("version", "createdAt", "complete")})
        write_json(path, escaped)
        with self.assertRaisesRegex(ValueError, "inside scenarios"):
            ScenarioCatalog(self.scenarios_root, "hcmc-v1")
        with self.assertRaises(ValueError):
            self.export("../unsafe")

    def test_wrong_source_qa_and_partial_suite_identity_are_rejected(self):
        output = self.scenarios_root / "fixtures/hcmc-v1/export-state.json"
        write_json(output, {"wrong": "inputs"})
        with self.assertRaisesRegex(ValueError, "Partial suite"):
            self.export()
        qa_path = self.root / "qa/manifest.json"
        qa = read_json(qa_path)
        qa["identity"]["inputs"]["features"]["version"] = "wrong-snapshot"
        write_json(qa_path, qa)
        with self.assertRaisesRegex(ValueError, "different features"):
            self.export("hcmc-v2")

    def test_missing_context_rejected_and_external_run_cannot_be_exported(self):
        self.export()
        with self.assertRaisesRegex(ValueError, "inside scenarios-root"):
            export_scenarios(self.root, self.base / "unrelated", suite_id="v1", progress=QUIET)
        (self.root / "weather/weather_context.json").unlink()
        with self.assertRaisesRegex(ValueError, "checksum"):
            ScenarioCatalog(self.scenarios_root, "hcmc-v1")
