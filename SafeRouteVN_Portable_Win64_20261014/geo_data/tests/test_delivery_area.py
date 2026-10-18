"""Polygon delivery selection, full-graph detours and historical boundary provenance."""

import copy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock

import requests
from shapely.geometry import Polygon, box, mapping

from geo_data.common import read_json, write_json
from geo_data.delivery_area import (DEFAULT_AREA_CONFIG, DeliveryArea, area_query, fetch_delivery_area,
                                   normalize_area, validate_area_membership)
from geo_data.depot_selection import select_depot
from geo_data.osm.extract_download import file_hash
from geo_data.osm.routing import RoutingGraph
from geo_data.pipeline import run_pipeline
from geo_data.qa import check_pipeline
from geo_data.scenario_catalog import ScenarioCatalog, export_scenarios
from geo_data.scenario_generator import generate_scenarios
from test_handoff import HandoffFixture
from test_pipeline import QUIET


def area_document(polygons):
    return {"type": "FeatureCollection", "properties": {"areaId": "synthetic", "source": "SYNTHETIC"},
            "features": [{"type": "Feature", "geometry": mapping(p), "properties": {"regionId": rid, "name": rid}}
                         for rid, p in polygons.items()]}


def boundary_payload():
    regions = [(101, "Thành phố Thủ Đức", box(106.73, 10.82, 106.81, 10.88)),
               (102, "Quận Bình Thạnh", box(106.69, 10.79, 106.72, 10.83))]
    return {"elements": [{"type": "relation", "id": rid, "version": 1,
        "tags": {"name": name, "boundary": "administrative", "admin_level": "6"},
        "members": [{"type": "way", "role": "outer", "ref": rid,
                     "geometry": [{"lon": x, "lat": y} for x, y in p.exterior.coords]}]}
        for rid, name, p in regions] + [{"type": "count", "tags": {"total": "2"}}]}


class AreaSourceTests(unittest.TestCase):
    def test_holes_and_boundary_points_are_respected(self):
        polygon = Polygon(box(106, 10, 107, 11).exterior.coords, [box(106.2, 10.2, 106.4, 10.4).exterior.coords])
        area = DeliveryArea(area_document({"a": polygon}))
        self.assertTrue(area.covers(106, 10.5))
        self.assertFalse(area.covers(106.3, 10.3))
        self.assertFalse(area.covers(108, 10.5))
        doc = area_document({"a": polygon})
        doc["features"].append(doc["features"][0])
        with self.assertRaisesRegex(ValueError, "unique"):
            DeliveryArea(doc)

    def test_historical_query_cache_and_provenance(self):
        session = Mock()
        session.post.return_value.json.return_value = boundary_payload()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "area.geojson"
            first = fetch_delivery_area(path, user_agent="test", session=session, progress=QUIET)
            query = session.post.call_args.kwargs["data"]["data"]
            self.assertIn('[date:"2025-01-01T00:00:00Z"]', query)
            self.assertIn("out meta geom;out count;", query)
            self.assertEqual(first["properties"]["verification"], "candidate-needs-boundary-review")
            self.assertEqual(fetch_delivery_area(path, user_agent="test", session=session, progress=QUIET), first)
            session.post.assert_called_once()
            first["features"][0]["geometry"] = mapping(box(106.5, 10.5, 107, 11))
            write_json(path, first)
            with self.assertRaisesRegex(ValueError, "cache geometry"):
                fetch_delivery_area(path, user_agent="test", session=session, progress=QUIET)

    def test_incomplete_ambiguous_or_wrong_region_fails(self):
        config = read_json(DEFAULT_AREA_CONFIG)
        for mode in ("partial", "missing", "duplicate", "wrong-probe"):
            payload = boundary_payload()
            if mode == "partial":
                payload["remark"] = "timeout"
            elif mode == "missing":
                payload["elements"].pop(0)
            elif mode == "duplicate":
                payload["elements"][1] = copy.deepcopy(payload["elements"][0])
            else:
                payload["elements"][0]["members"][0]["geometry"] = [{"lon": x, "lat": y} for x, y in box(105, 9, 105.1, 9.1).exterior.coords]
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                normalize_area(payload, config, endpoint="https://test", query=area_query(config))

    def test_network_retry_does_not_publish_failed_area(self):
        session = Mock()
        response = Mock()
        response.json.return_value = boundary_payload()
        session.post.side_effect = [requests.Timeout("test"), response]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "area.geojson"
            result = fetch_delivery_area(path, user_agent="test", session=session, sleep=QUIET, progress=QUIET)
            self.assertEqual(len(result["features"]), 2)
            self.assertEqual(session.post.call_count, 2)


class AreaScenarioTests(HandoffFixture):
    def setUp(self):
        super().setUp()
        self.ready_run()
        self.area_path = self.base / "area.geojson"
        # Two strips contain nodes 1/6/7 and 3/5/9. Connector nodes 2/4/8 lie outside.
        write_json(self.area_path, area_document({"west": box(106.6995, 10.7695, 106.7005, 10.7745),
                                                "east": box(106.7095, 10.7695, 106.7105, 10.7745)}))
        config = read_json(self.base / "synthetic-scenarios.json")
        config.update(radiusKm=0.02, anchorLongitude=106.705, anchorLatitude=10.770)
        self.area_profile = self.base / "area-profile.json"
        write_json(self.area_profile, config)
        self.area_output = self.base / "area-scenarios"

    def generate(self, depot=1):
        return generate_scenarios(self.routing, self.features, self.area_output, config_path=self.area_profile,
                                  delivery_area=self.area_path, depot_node=depot, progress=QUIET)

    def test_only_in_area_orders_but_routes_can_leave_polygons_and_radius(self):
        before = file_hash(self.routing / "network.sqlite")
        result = self.generate()
        area = DeliveryArea(read_json(self.area_path))
        self.assertEqual(result["deliveryAreaVersion"], area.version)
        self.assertTrue(result["suiteReady"])
        for i in range(9):
            validate_area_membership(read_json(self.area_output / f"S{i}.json"), area)
        s0 = read_json(self.area_output / "S0.json")
        self.assertEqual({o["deliveryRegionId"] for o in s0["initialState"]["orders"]}, {"west", "east"})
        with RoutingGraph(self.routing, features=self.features) as graph:
            route = graph.path(1, 3)
            self.assertGreater(route["distanceKm"], 0.02)
            nodes = [graph.db.execute("SELECT n.longitude,n.latitude FROM nodes n JOIN edges e ON e.toNodeId=n.nodeId WHERE e.edgeId=?", (eid,)).fetchone() for eid in route["edgeIds"]]
            self.assertTrue(any(not area.covers(*point) for point in nodes))
            depot = select_depot(graph, read_json(self.area_profile), delivery_area=area, progress=QUIET)
            self.assertTrue(area.covers(depot["longitude"], depot["latitude"]))
        self.assertEqual(file_hash(self.routing / "network.sqlite"), before)

    def test_explicit_outside_depot_and_changed_area_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the delivery"):
            self.generate(depot=2)
        # Failed attempt binds depot/config; use its own output for the valid run.
        self.area_output = self.base / "valid-area-scenarios"
        self.generate()
        doc = read_json(self.area_path)
        doc["properties"]["source"] = "SYNTHETIC revision"
        write_json(self.area_path, doc)
        with self.assertRaisesRegex(ValueError, "different inputs"):
            self.generate()

    def test_named_profile_requires_matching_area_before_pipeline_writes(self):
        profile = read_json(self.area_profile)
        profile["requiredDeliveryAreaId"] = "expected-area"
        write_json(self.area_profile, profile)
        with self.assertRaisesRegex(ValueError, "matching --delivery-area"):
            generate_scenarios(self.routing, self.features, self.area_output, config_path=self.area_profile, progress=QUIET)
        target = self.base / "not-started"
        with self.assertRaisesRegex(ValueError, "matching --delivery-area"):
            run_pipeline(self.graph, target, scenario_profile=self.area_profile, delivery_area=self.area_path, progress=QUIET)
        self.assertFalse(target.exists())

    def test_area_bundle_passes_qa_and_portable_catalog(self):
        self.generate()
        qa = self.base / "area-qa"
        check_pipeline(self.routing, self.travel, self.plan, self.weather, self.features, self.area_output, qa, progress=QUIET)
        root = self.base / "export/scenarios"
        run = root / "cached_context/test-area"
        for name in ("routing", "travel", "weather-plan", "weather", "features"):
            shutil.copytree(self.root / name, run / name)
        shutil.copytree(self.area_output, run / "scenarios")
        shutil.copytree(qa, run / "qa")
        export_scenarios(run, root, suite_id="area-v1", progress=QUIET)
        catalog = ScenarioCatalog(root, "area-v1")
        self.assertEqual(catalog.scenario()["deliveryAreaVersion"], DeliveryArea(read_json(self.area_path)).version)
        self.assertTrue((root / "fixtures/area-v1/delivery_area.geojson").is_file())

    def test_qa_checks_urgent_order_against_area(self):
        self.generate()
        case_path = self.area_output / "S2.json"
        case = read_json(case_path)
        case["events"][0]["orderPayload"]["longitude"] = 108
        write_json(case_path, case)
        manifest_path = self.area_output / "manifest.json"
        manifest = read_json(manifest_path)
        manifest["files"]["S2.json"]["sha256"] = file_hash(case_path)
        write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "outside its delivery"):
            check_pipeline(self.routing, self.travel, self.plan, self.weather, self.features,
                           self.area_output, self.base / "bad-area-qa", progress=QUIET)
