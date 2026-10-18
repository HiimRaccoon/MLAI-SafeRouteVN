"""Graph topology/access tests with small, explicitly synthetic OSM snapshots."""

import csv
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from xml.sax.saxutils import quoteattr

from pyproj import Geod

from geo_data.common import read_json, write_json
from geo_data.osm.access_policy import load_policy, node_block_reason, way_decision, width_metres
from geo_data.osm.build_graph import build_graph


def tags_xml(tags):
    return "".join(f'<tag k={quoteattr(k)} v={quoteattr(v)}/>' for k, v in tags.items())


def osm_xml(nodes, ways, relations=""):
    parts = ['<osm version="0.6" generator="synthetic-graph-tests">']
    for node, (lon, lat, tags) in sorted(nodes.items()):
        parts.append(f'<node id="{node}" version="1" lon="{lon}" lat="{lat}">{tags_xml(tags)}</node>')
    for way, refs, tags in ways:
        parts.append(f'<way id="{way}" version="1">' + "".join(f'<nd ref="{n}"/>' for n in refs) + tags_xml(tags) + '</way>')
    return "".join(parts) + relations + "</osm>"


class AccessPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy()

    def directions(self, **tags):
        return way_decision({"highway": "residential", **tags}, self.policy)[0]

    def test_mode_specific_access_and_direction_override(self):
        self.assertEqual(self.directions(access="no", motorcycle="yes"), ("forward", "backward"))
        self.assertEqual(self.directions(motorcycle="no", access="yes"), ())
        self.assertEqual(self.directions(**{"motorcycle:backward": "no"}), ("forward",))
        self.assertEqual(self.directions(**{"motor_vehicle:forward": "no", "motorcycle": "yes"}), ("forward", "backward"))

    def test_oneway_reverse_roundabout_and_mode_exception(self):
        self.assertEqual(self.directions(oneway="-1"), ("backward",))
        self.assertEqual(self.directions(junction="roundabout"), ("forward",))
        self.assertEqual(self.directions(junction="roundabout", oneway="no"), ("forward", "backward"))
        self.assertEqual(self.directions(**{"oneway": "yes", "oneway:motorcycle": "no"}), ("forward", "backward"))
        self.assertEqual(self.directions(oneway="alternating"), ())

    def test_restricted_unknown_and_conditional_are_not_unrestricted_roads(self):
        for value in ("private", "destination", "delivery", "customers", "unknown", "yes|yes"):
            self.assertEqual(self.directions(motorcycle=value), ())
        self.assertEqual(self.directions(**{"motorcycle:conditional": "yes @ (06:00-09:00)"}), ())
        self.assertEqual(self.directions(**{"oneway:conditional": "yes @ (06:00-09:00)"}), ())
        self.assertEqual(self.directions(**{"hgv:conditional": "no @ (06:00-09:00)"}), ("forward", "backward"))

    def test_highway_defaults_and_area(self):
        self.assertEqual(self.directions(highway="motorway"), ())
        self.assertEqual(self.directions(highway="motorway", motorcycle="yes"), ("forward",))
        self.assertEqual(self.directions(highway="footway", access="yes"), ())
        self.assertEqual(self.directions(highway="footway", motorcycle="yes"), ("forward", "backward"))
        self.assertEqual(self.directions(highway="construction", motorcycle="yes"), ())
        self.assertEqual(self.directions(area="yes"), ())

    def test_barriers_and_width_missing(self):
        self.assertIsNotNone(node_block_reason({"barrier": "gate"}, self.policy))
        self.assertIsNone(node_block_reason({"barrier": "gate", "motorcycle": "yes"}, self.policy))
        self.assertIsNotNone(node_block_reason({"barrier": "bollard", "motorcycle": "yes"}, self.policy))
        self.assertIsNotNone(node_block_reason({"access": "no"}, self.policy))
        self.assertEqual(width_metres("3.5 m"), (3.5, None))
        for raw in (None, "0", "NaN", "3;4", "10 ft"):
            self.assertIsNone(width_metres(raw)[0])


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "synthetic.osm"
        self.output = self.root / "graph"
        self.nodes = {1: (106.7, 10.7, {}), 2: (106.701, 10.701, {}),
                      3: (106.702, 10.7, {}), 4: (106.702, 10.702, {}),
                      5: (106.701, 10.699, {}), 6: (106.701, 10.703, {}),
                      7: (106.8, 10.8, {}), 8: (106.801, 10.8, {})}

    def run_build(self, ways, relations="", output=None):
        self.source.write_text(osm_xml(self.nodes, ways, relations), encoding="utf-8")
        return build_graph(self.source, output or self.output, scope="test", progress=lambda _: None)

    def rows(self, query):
        with closing(sqlite3.connect(self.output / "graph.sqlite")) as db:
            return list(db.execute(query))

    def test_polyline_length_reverse_parallel_grade_crossing_and_island(self):
        ways = [(10, [1, 2, 3], {"highway": "residential", "width": "3.5"}),
                (20, [1, 3], {"highway": "residential", "oneway": "-1"}),
                (30, [5, 6], {"highway": "primary", "bridge": "yes", "layer": "1"}),
                (40, [7, 8], {"highway": "service"})]
        result = self.run_build(ways)
        self.assertEqual(result["counts"], {"nodes": 6, "edges": 7, "weakComponents": 3, "restrictions": 0})
        self.assertTrue(result["complete"])
        self.assertFalse(result["routingReady"])
        curve = self.rows("SELECT lengthKm,geometryJson,osmNodeIdsJson,widthM FROM edges WHERE edgeId='w10:0-2:backward'")[0]
        expected = Geod(ellps="WGS84").line_length([106.7, 106.701, 106.702], [10.7, 10.701, 10.7]) / 1000
        self.assertAlmostEqual(curve[0], expected, places=10)
        self.assertEqual(json.loads(curve[1])["coordinates"][0], [106.702, 10.7])
        self.assertEqual(json.loads(curve[2]), [3, 2, 1])
        self.assertEqual(curve[3], 3.5)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM edges WHERE fromNodeId=3 AND toNodeId=1")[0][0], 2)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM edges WHERE osmWayId=20 AND fromNodeId=1")[0][0], 0)
        with (self.output / "edges.csv").open(encoding="utf-8", newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 7)

    def test_shared_osm_node_splits_way_and_blocked_internal_node_cannot_be_crossed(self):
        self.nodes[3] = (106.702, 10.7, {"barrier": "gate"})
        self.run_build([(10, [1, 2, 3, 4], {"highway": "residential"}),
                        (20, [2, 5], {"highway": "residential"})])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM edges WHERE fromNodeId=3 OR toNodeId=3")[0][0], 0)
        self.assertEqual(self.rows("SELECT fromNodeId,toNodeId FROM edges WHERE osmWayId=10 ORDER BY fromNodeId"), [(1, 2), (2, 1)])
        qa = read_json(self.output / "qa_report.json")
        self.assertEqual(qa["blockedSegments"], 2)
        self.assertEqual(qa["blockedNodes"], 1)

    def test_restrictions_preserved_and_never_claimed_enforced(self):
        relation = '''<relation id="100" version="1"><member type="way" ref="10" role="from"/>
          <member type="node" ref="2" role="via"/><member type="way" ref="20" role="to"/>
          <tag k="type" v="restriction"/><tag k="restriction" v="no_left_turn"/></relation>
          <relation id="101" version="1"><member type="way" ref="10" role="from"/>
          <member type="way" ref="20" role="via"/><member type="way" ref="30" role="to"/>
          <tag k="type" v="restriction"/><tag k="restriction:conditional" v="no_right_turn @ (06:00-09:00)"/></relation>'''
        result = self.run_build([(10, [1, 2], {"highway": "primary"}),
                                 (20, [2, 3], {"highway": "primary"}),
                                 (30, [3, 4], {"highway": "primary"})], relation)
        records = read_json(self.output / "restrictions.json")["records"]
        self.assertEqual([r["status"] for r in records], ["pending-via-node", "pending-conditional"])
        self.assertTrue(all(not r["enforced"] for r in records))
        self.assertFalse(result["routingReady"])

    def test_cache_deterministic_exports_policy_changes_and_tamper(self):
        ways = [(10, [1, 2, 3], {"highway": "residential"})]
        first = self.run_build(ways)
        second = build_graph(self.source, self.output, scope="test", progress=lambda _: None)
        self.assertEqual(first, second)
        other = build_graph(self.source, self.root / "other", scope="test", progress=lambda _: None)
        self.assertEqual(first["graphVersion"], other["graphVersion"])
        self.assertEqual(first["files"], other["files"])
        policy = load_policy()
        policy["defaultHighways"].remove("service")
        policy_path = self.root / "policy.json"
        write_json(policy_path, policy)
        with self.assertRaisesRegex(ValueError, "another source/policy"):
            build_graph(self.source, self.output, scope="test", policy_path=policy_path)
        (self.output / "nodes.csv").write_text("bad", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cache failed verification"):
            build_graph(self.source, self.output, scope="test")

    def test_unverified_scope_and_missing_references_rejected(self):
        self.source.write_text(osm_xml(self.nodes, [(10, [1, 999], {"highway": "residential"})]), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "verified extract-city manifest"):
            build_graph(self.source, self.output)
        with self.assertRaisesRegex(ValueError, "missing references"):
            build_graph(self.source, self.output, scope="test", progress=lambda _: None)
        self.assertFalse((self.output / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
