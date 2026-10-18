import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from shapely.geometry import box, shape
from shapely.ops import unary_union

from geo_data.common import exclusive_lock, read_json
from geo_data.osm.download_graph import OverpassClient, build_query, download_tiles, validate_osm
from geo_data.osm.region import fetch_boundary, load_boundary, relation_geometry
from geo_data.osm.tile_index import build_plan, validate_plan
from geo_data.units import distance_km, duration_hours, mass_kg, speed_kph, timestamp_vn, travel_hours

FIXTURE = Path(__file__).parent / "fixtures" / "boundary.synthetic.geojson"


def make_plan():
    return build_plan(read_json(FIXTURE), region_id="synthetic", source="SYNTHETIC",
                      boundary_version="v1", tile_km=2, scope="test")


def osm_payload():
    return {"osm3s": {"timestamp_osm_base": "2026-09-26T01:00:00Z"}, "elements": [
        {"type": "node", "id": 1, "version": 1, "lat": 10.77, "lon": 106.69},
        {"type": "node", "id": 2, "version": 1, "lat": 10.78, "lon": 106.70},
        {"type": "way", "id": 10, "version": 2, "nodes": [1, 2],
         "tags": {"highway": "residential", "motorcycle": "yes", "oneway": "yes"}},
        {"type": "count", "tags": {"nodes": "2", "ways": "1", "relations": "0", "total": "3"}},
    ]}


class UnitsTests(unittest.TestCase):
    def test_distance_speed_duration_mass(self):
        self.assertEqual(distance_km(500, "m"), 0.5)
        self.assertEqual(speed_kph(10, "m/s"), 36)
        self.assertEqual(mass_kg(1500, "g"), 1.5)
        self.assertAlmostEqual(duration_hours(72, "s"), 0.02)
        self.assertAlmostEqual(travel_hours(0.5, 25), 0.02)

    def test_invalid_values_and_units(self):
        for value in [-1, float("nan"), float("inf"), True, "12"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                distance_km(value, "m")
        with self.assertRaises(ValueError):
            mass_kg(2, "packages")
        with self.assertRaises(ValueError):
            travel_hours(1, 0)

    def test_timezone_preserves_instant_and_date_rollover(self):
        self.assertEqual(timestamp_vn("2026-09-26T20:00:00Z"), "2026-09-27T03:00:00+07:00")
        with self.assertRaises(ValueError):
            timestamp_vn("2026-09-26T09:00:00")


class BoundaryTests(unittest.TestCase):
    @patch("geo_data.osm.region.requests.post")
    def test_overpass_boundary_provider_and_cache(self, post):
        members = []
        for polygon in read_json(FIXTURE)["geometry"]["coordinates"]:
            members.append({"type": "way", "role": "outer", "ref": len(members) + 1,
                            "geometry": [{"lon": x, "lat": y} for x, y in polygon[0]]})
        post.return_value.json.return_value = {
            "osm3s": {"timestamp_osm_base": "2026-09-26T01:00:00Z"},
            "elements": [
                {"type": "relation", "id": 123, "version": 5, "timestamp": "2026-09-25T00:00:00Z",
                 "tags": {"boundary": "administrative", "name": "Synthetic"}, "members": members},
                {"type": "count", "tags": {"total": "1"}},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boundary.json"
            result = fetch_boundary("R123", path, user_agent="test", provider="overpass")
            self.assertEqual(result["properties"]["sourceTimestamp"], "2026-09-26T08:00:00+07:00")
            self.assertEqual(result["properties"]["osmEditedAt"], "2026-09-25T07:00:00+07:00")
            self.assertEqual(result["geometry"]["type"], "MultiPolygon")
            self.assertEqual(result, fetch_boundary("R123", path, user_agent="test", provider="overpass"))
            post.assert_called_once()
            with self.assertRaisesRegex(ValueError, "different provider"):
                fetch_boundary("R123", path, user_agent="test", provider="nominatim")

    def test_relation_assembly_preserves_holes_and_islands(self):
        def member(points, role):
            return {"type": "way", "role": role,
                    "geometry": [{"lon": x, "lat": y} for x, y in points]}
        outer_a = [(106, 10), (107, 10), (107, 11)]
        outer_b = [(107, 11), (106, 11), (106, 10)]
        hole = [(106.2, 10.2), (106.4, 10.2), (106.4, 10.4), (106.2, 10.4), (106.2, 10.2)]
        island = [(106, 8), (106.1, 8), (106.1, 8.1), (106, 8.1), (106, 8)]
        relation = {"members": [member(outer_a, "outer"), member(outer_b, "outer"),
                                 member(hole, "inner"), member(island, "outer")]}
        geometry = relation_geometry(relation)
        self.assertEqual(len(geometry.geoms), 2)
        self.assertEqual(sum(len(p.interiors) for p in geometry.geoms), 1)
        relation["members"].pop(1)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            relation_geometry(relation)

    def test_grid_covers_mainland_and_island(self):
        plan = make_plan()
        bboxes = [f["properties"]["bboxSWNE"] for f in plan["features"]]
        coverage = unary_union([box(w, s, e, n) for s, w, n, e in bboxes])
        self.assertTrue(coverage.covers(shape(plan["boundary"])))
        self.assertTrue(any(s < 9 for s, w, n, e in bboxes))
        self.assertEqual(plan["planVersion"], make_plan()["planVersion"])
        validate_plan(plan)

    def test_old_or_test_boundary_rejected_for_city_scope(self):
        with self.assertRaisesRegex(ValueError, "coverage probes"):
            build_plan(read_json(FIXTURE), region_id="hcmc", source="test", boundary_version="v1")

    def test_holes_do_not_get_interior_tiles(self):
        doc = {"type": "Polygon", "coordinates": [
            [[106.6, 10.7], [106.8, 10.7], [106.8, 10.9], [106.6, 10.9], [106.6, 10.7]],
            [[106.65, 10.75], [106.65, 10.85], [106.75, 10.85], [106.75, 10.75], [106.65, 10.75]],
        ]}
        plan = build_plan(doc, region_id="hole", source="test", boundary_version="v1", scope="test", tile_km=1)
        inner = box(106.67, 10.77, 106.73, 10.83)
        self.assertFalse(any(inner.covers(shape(f["geometry"])) for f in plan["features"]))

    def test_invalid_boundary_and_tampered_plan(self):
        with self.assertRaises(ValueError):
            load_boundary({"type": "Point", "coordinates": [106, 10]})
        with self.assertRaises(ValueError):
            load_boundary({"type": "Polygon", "coordinates": [[[106, 10], [107, 11], [107, 10], [106, 11], [106, 10]]]})
        plan = make_plan()
        plan["features"][0]["properties"]["bboxSWNE"][0] += 0.1
        with self.assertRaisesRegex(ValueError, "checksum"):
            validate_plan(plan)

    @patch("geo_data.osm.region.requests.get")
    def test_boundary_lookup_cache(self, get):
        get.return_value.json.return_value = [{"osm_type": "relation", "osm_id": 123,
                                              "geojson": read_json(FIXTURE)["geometry"]}]
        get.return_value.url = "https://example.org/lookup?osm_ids=R123"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boundary.json"
            first = fetch_boundary("R123", path, user_agent="SafeRouteVN test")
            second = fetch_boundary("R123", path, user_agent="SafeRouteVN test")
            self.assertEqual(first, second)
            get.assert_called_once()


class OSMTests(unittest.TestCase):
    def test_query_preserves_restrictions_and_count_marker(self):
        query = build_query([10.7, 106.6, 10.8, 106.7], osm_date="2026-09-26T08:00:00+07:00")
        self.assertIn('rel(bw.roads)', query)
        self.assertIn('>>', query)
        self.assertIn('2026-09-26T01:00:00Z', query)
        self.assertTrue(query.endswith("out count;\n"))

    def test_valid_and_empty_payload(self):
        self.assertEqual(validate_osm(osm_payload())["way"], 1)
        payload = osm_payload()
        payload["elements"] = [{"type": "count", "tags": {"nodes": "0", "ways": "0", "relations": "0", "total": "0"}}]
        self.assertEqual(validate_osm(payload)["node"], 0)

    def test_partial_error_and_missing_node_rejected(self):
        for mode in ("remark", "count", "missing_node", "source"):
            payload = osm_payload()
            if mode == "remark":
                payload["remark"] = "runtime error: timeout"
            elif mode == "count":
                payload["elements"].pop()
            elif mode == "missing_node":
                payload["elements"][2]["nodes"].append(999)
            else:
                payload["osm3s"] = {}
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_osm(payload)

    def test_missing_relation_member_rejected(self):
        payload = osm_payload()
        payload["elements"].insert(-1, {"type": "relation", "id": 20, "version": 1,
                                         "members": [{"type": "way", "ref": 999, "role": "from"}]})
        payload["elements"][-1]["tags"].update(relations="1", total="4")
        with self.assertRaisesRegex(ValueError, "missing members"):
            validate_osm(payload)

    def test_retry_after_and_no_retry_permanent_error(self):
        busy = Mock(status_code=429, headers={"Retry-After": "45"})
        busy.raise_for_status.side_effect = requests.HTTPError(response=busy)
        success = Mock(status_code=200)
        success.json.return_value = osm_payload()
        session, sleep = Mock(), Mock()
        session.post.side_effect = [busy, success]
        client = OverpassClient(user_agent="test", session=session, sleep=sleep)
        self.assertEqual(client.fetch("query"), osm_payload())
        self.assertIn(unittest.mock.call(45.0), sleep.call_args_list)
        bad = Mock(status_code=400, headers={})
        bad.raise_for_status.side_effect = requests.HTTPError(response=bad)
        session.post.reset_mock()
        session.post.side_effect = None
        session.post.return_value = bad
        with self.assertRaises(requests.HTTPError):
            client.fetch("bad")
        session.post.assert_called_once()


class CacheAndResumeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.plan = make_plan()
        self.client = Mock(endpoint="https://example.org/interpreter")
        self.client.fetch.side_effect = lambda query: osm_payload()

    def download(self, **kwargs):
        return download_tiles(self.plan, self.output, client=self.client, progress=lambda _: None, **kwargs)

    def test_resume_advances_past_complete_cache(self):
        first = self.download(max_tiles=1)
        self.assertFalse(first["complete"])
        self.assertEqual(first["summary"]["complete"], 1)
        second = self.download(max_tiles=1)
        self.assertEqual(second["summary"]["complete"], 2)
        self.assertEqual(self.client.fetch.call_count, 2)
        final = self.download()
        self.assertTrue(final["complete"])
        calls = self.client.fetch.call_count
        with patch("requests.Session.post", side_effect=AssertionError("Network in offline mode")):
            offline = download_tiles(self.plan, self.output, offline=True,
                                     endpoint=self.client.endpoint, progress=lambda _: None)
        self.assertTrue(offline["complete"])
        self.assertEqual(calls, self.client.fetch.call_count)
        self.assertFalse(offline["routingReady"])

    def test_failed_fetch_stops_and_can_resume(self):
        self.client.fetch.side_effect = requests.Timeout("test outage")
        manifest = self.download()
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["summary"]["failed"], 1)
        self.client.fetch.assert_called_once()
        self.client.fetch.side_effect = lambda q: osm_payload()
        self.assertTrue(self.download()["complete"])

    def test_corruption_detected_and_explicit_refresh_repairs(self):
        manifest = self.download()
        tile_id, record = next(iter(manifest["tiles"].items()))
        (self.output / "raw_tiles" / tile_id / record["rawFile"]).write_text("corrupt")
        result = self.download(offline=True)
        self.assertFalse(result["complete"])
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertTrue(self.download(refresh=True)["complete"])

    def test_refresh_failure_does_not_fall_back_to_old_data(self):
        self.download()
        self.client.fetch.side_effect = requests.Timeout("refresh outage")
        self.assertFalse(self.download(refresh=True)["complete"])
        self.assertFalse(self.download(offline=True)["complete"])

    def test_plan_mismatch_and_lock(self):
        self.download(max_tiles=1)
        other = copy.deepcopy(self.plan)
        other["planVersion"] = "changed"
        with self.assertRaises(ValueError):
            download_tiles(other, self.output, client=self.client)
        with exclusive_lock(self.output), self.assertRaises(ValueError):
            self.download()

    def test_missing_cache_offline(self):
        result = self.download(offline=True)
        self.assertFalse(result["complete"])
        self.client.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
