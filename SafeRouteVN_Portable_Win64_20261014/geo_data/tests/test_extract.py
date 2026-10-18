import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import osmium
import requests

from geo_data.common import read_json, write_json
from geo_data.osm.extract_download import download_extract
from geo_data.osm.extract_filter import filter_extract, validate_pbf

FIXTURE = Path(__file__).parent / "fixtures" / "boundary.synthetic.geojson"
URL = "https://example.org/vietnam.osm.pbf"
BODY = b"example-pbf-content"


def response(status=200, headers=None, text="", chunks=None):
    result = Mock(status_code=status, headers=headers or {}, text=text, url=URL)
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.iter_content.return_value = iter(chunks or [])
    return result


class BulkDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.session = Mock()
        self.session.head.return_value = response(headers={"Content-Length": str(len(BODY)), "ETag": '"v1"'})

    def run_download(self, **kwargs):
        return download_extract(self.output, url=URL, user_agent="test", session=self.session,
                                sleep=lambda _: None, progress=lambda _: None, **kwargs)

    def md5(self):
        return response(text=hashlib.md5(BODY).hexdigest() + "  vietnam.osm.pbf")

    def test_resume_after_interrupted_stream_and_verify_cached_file(self):
        def interrupted():
            yield BODY[:5]
            raise requests.ConnectionError("connection interrupted")
        first = response()
        first.iter_content.return_value = interrupted()
        second = response(206, {"Content-Range": f"bytes 5-{len(BODY)-1}/{len(BODY)}"}, chunks=[BODY[5:]])
        self.session.get.side_effect = [self.md5(), first, self.md5(), second]
        result = self.run_download(attempts=2)
        self.assertTrue(result["complete"])
        self.assertEqual((self.output / "source.osm.pbf").read_bytes(), BODY)
        self.assertEqual(self.session.get.call_args_list[3].kwargs["headers"]["Range"], "bytes=5-")
        self.assertEqual(self.session.get.call_args_list[3].kwargs["headers"]["If-Range"], '"v1"')
        self.session.reset_mock()
        self.assertEqual(self.run_download(), result)
        self.session.get.assert_not_called()

    def seed_partial(self):
        write_json(self.output / "download-state.json", {"url": URL, "resolvedUrl": URL,
                   "bytes": len(BODY), "md5": hashlib.md5(BODY).hexdigest(), "validator": '"v1"'})
        (self.output / "source.osm.pbf.part").write_bytes(BODY[:5])

    def test_server_ignoring_range_restarts_instead_of_appending(self):
        self.seed_partial()
        self.session.get.side_effect = [self.md5(), response(chunks=[BODY])]
        self.run_download()
        self.assertEqual((self.output / "source.osm.pbf").read_bytes(), BODY)

    def test_wrong_range_does_not_append(self):
        self.seed_partial()
        self.session.get.side_effect = [self.md5(), response(206, {"Content-Range": f"bytes 0-{len(BODY)-1}/{len(BODY)}"}, chunks=[BODY])]
        with self.assertRaisesRegex(ValueError, "Content-Range"):
            self.run_download()
        self.assertEqual((self.output / "source.osm.pbf.part").read_bytes(), BODY[:5])
        self.assertFalse((self.output / "source.osm.pbf").exists())

    def test_md5_mismatch_never_publishes_complete(self):
        self.session.get.side_effect = [self.md5(), response(chunks=[b"x" * len(BODY)])]
        with self.assertRaises(requests.ConnectionError):
            self.run_download(attempts=1)
        self.assertFalse((self.output / "extract-manifest.json").exists())
        self.assertFalse((self.output / "source.osm.pbf").exists())

    def test_source_change_discards_old_partial(self):
        self.seed_partial()
        self.session.head.return_value = response(headers={"Content-Length": str(len(BODY)), "ETag": '"v2"'})
        self.session.get.side_effect = [self.md5(), response(chunks=[BODY])]
        self.run_download()
        self.assertNotIn("Range", self.session.get.call_args_list[1].kwargs["headers"])


XML = '''<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="synthetic-tests">
 <node id="1" version="1" lat="10.775" lon="106.695"><tag k="barrier" v="bollard"/></node>
 <node id="2" version="1" lat="10.78" lon="106.70"/>
 <node id="3" version="1" lat="10.777" lon="106.68"/>
 <node id="4" version="1" lat="10.777" lon="106.72"/>
 <node id="5" version="1" lat="12" lon="108"/>
 <node id="6" version="1" lat="12.1" lon="108.1"/>
 <node id="7" version="1" lat="11" lon="107"/>
 <node id="8" version="1" lat="11.1" lon="107.1"/>
 <way id="10" version="1"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/><tag k="oneway" v="yes"/></way>
 <way id="20" version="1"><nd ref="3"/><nd ref="4"/><tag k="highway" v="primary"/></way>
 <way id="30" version="1"><nd ref="7"/><nd ref="8"/><tag k="name" v="reference-only way"/></way>
 <way id="40" version="1"><nd ref="5"/><nd ref="6"/><tag k="highway" v="residential"/></way>
 <relation id="100" version="1"><member type="way" ref="10" role="from"/><member type="node" ref="2" role="via"/><member type="way" ref="30" role="to"/><tag k="type" v="restriction"/><tag k="restriction" v="no_left_turn"/></relation>
</osm>
'''


class ExtractFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "synthetic.osm"
        self.source.write_text(XML, encoding="utf-8")

    def test_local_filter_preserves_crossing_way_reference_tags_and_restriction(self):
        output = self.root / "filtered"
        result = filter_extract(self.source, FIXTURE, output, scope="test", buffer_km=0, progress=lambda _: None)
        self.assertEqual(result["selectedHighwayWays"], 2)
        self.assertEqual(result["counts"], {"node": 6, "way": 3, "relation": 1})
        self.assertTrue(result["referenceComplete"])
        self.assertFalse(result["routingReady"])
        self.assertIn("sourceTimestamp", result["missingFlags"])
        ways = set()
        for obj in osmium.FileProcessor(output / "roads.osm.pbf"):
            if obj.is_way():
                ways.add(obj.id)
            if obj.is_node() and obj.id == 1:
                self.assertEqual(obj.tags["barrier"], "bollard")
        self.assertEqual(ways, {10, 20, 30})
        cached = filter_extract(self.source, FIXTURE, output, scope="test", buffer_km=0, progress=lambda _: None)
        self.assertEqual(cached, result)

    def test_missing_reference_rejected(self):
        self.source.write_text(XML.replace('<nd ref="1"/>', '<nd ref="999"/>'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing references"):
            validate_pbf(self.source)

    def test_test_boundary_cannot_claim_city_scope(self):
        with self.assertRaisesRegex(ValueError, "coverage probes"):
            filter_extract(self.source, FIXTURE, self.root / "filtered", progress=lambda _: None)


if __name__ == "__main__":
    unittest.main()
