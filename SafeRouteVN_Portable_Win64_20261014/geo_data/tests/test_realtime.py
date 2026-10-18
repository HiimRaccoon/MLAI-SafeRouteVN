"""Live API polling on a small graph with mocked HTTP, clock and waits."""

from unittest.mock import Mock, patch

import requests

from geo_data.cli import main as cli_main
from geo_data.common import read_json, write_json
from geo_data.osm.routing import RoutingGraph
from geo_data.realtime import LiveUpdater, load_latest, watch
from geo_data.weather.context_builder import weather_plan
from test_pipeline import AT, QUIET, PipelineFixture, payload


class RealtimeTests(PipelineFixture):
    def setUp(self):
        super().setUp()
        self.network()
        weather_plan(self.routing, self.plan, grid_km=1, progress=QUIET)
        self.output = self.root / "live"
        self.weather_clock = patch("geo_data.weather.open_meteo_client.now_vn", return_value=AT)
        self.weather_clock.start()
        self.addCleanup(self.weather_clock.stop)
        self.updater = LiveUpdater(self.routing, self.plan, self.output, user_agent="synthetic-tests",
                                   progress=QUIET, clock=lambda: AT, session=self.session, http_sleep=QUIET)

    def test_same_hour_polls_http_again_and_publishes_new_costs(self):
        first = self.updater.refresh()
        previous = read_json(self.output / "latest.json")
        count = self.session.get.call_count
        changed = payload()
        changed["hourly"]["precipitation"] = [20]
        self.session.get.return_value.json.return_value = changed
        second = self.updater.refresh()
        self.assertEqual(self.session.get.call_count, count * 2)
        self.assertNotEqual(first["versions"]["features"], second["versions"]["features"])
        self.assertNotEqual(previous["snapshot"], read_json(self.output / "latest.json")["snapshot"])
        self.assertTrue((self.output / previous["snapshot"] / "snapshot.json").exists())
        latest = load_latest(self.output, now=AT)
        self.assertTrue(latest["fresh"])
        self.assertFalse(latest["integrated"])
        with RoutingGraph(latest["routing"], features=latest["features"]) as graph:
            self.assertIsNotNone(graph.path(1, 3))

    def test_api_failure_preserves_previous_pointer_and_recovers(self):
        self.updater.refresh()
        previous = read_json(self.output / "latest.json")
        self.session.get.side_effect = requests.Timeout("synthetic outage")
        with self.assertRaisesRegex(ValueError, "no valid explicit fallback"):
            self.updater.refresh()
        self.assertEqual(read_json(self.output / "latest.json"), previous)
        self.assertEqual(read_json(self.output / "status.json")["state"], "error")
        self.assertTrue(load_latest(self.output, now=AT)["fresh"])
        self.session.get.side_effect = None
        self.updater.refresh()
        self.assertEqual(read_json(self.output / "status.json")["state"], "ready")

    def test_next_day_uses_new_api_date_and_consistent_epoch(self):
        self.updater.refresh()
        next_day = "2026-09-28T00:00:00+07:00"
        self.updater.clock = lambda: next_day
        self.session.get.return_value.json.return_value = payload(next_day)
        with patch("geo_data.weather.open_meteo_client.now_vn", return_value=next_day):
            snapshot = self.updater.refresh()
        self.assertEqual(snapshot["at"], next_day)
        self.assertEqual(self.session.get.call_args.kwargs["params"]["start_date"], "2026-09-28")
        self.assertTrue(load_latest(self.output, now=next_day)["fresh"])

    def test_failed_build_never_publishes_partial_capture(self):
        with patch("geo_data.realtime.build_features", side_effect=RuntimeError("interrupted build")):
            with self.assertRaisesRegex(RuntimeError, "interrupted build"):
                self.updater.refresh()
        self.assertFalse((self.output / "latest.json").exists())
        self.assertEqual(cli_main(["live-status", "--output", str(self.output)]), 2)
        self.updater.refresh()
        self.assertTrue(load_latest(self.output, now=AT)["fresh"])

    def test_stale_and_clock_reversal_rejected_on_read(self):
        self.updater.refresh()
        for now in ("2026-09-27T11:01:00+07:00", "2026-09-27T08:59:00+07:00"):
            with self.assertRaisesRegex(ValueError, "stale or clock"):
                load_latest(self.output, now=now)
        result = load_latest(self.output, now="2026-09-27T11:01:00+07:00", allow_stale=True)
        self.assertFalse(result["fresh"])
        with patch("geo_data.realtime.now_vn", return_value="2026-09-27T11:01:00+07:00"):
            self.assertEqual(cli_main(["live-status", "--output", str(self.output)]), 2)

    def test_slow_build_is_not_published_as_fresh(self):
        self.updater.clock = Mock(side_effect=[AT, "2026-09-27T12:00:00+07:00", "2026-09-27T12:00:00+07:00", AT])
        with self.assertRaisesRegex(ValueError, "stale at publication"):
            self.updater.refresh()
        self.assertFalse((self.output / "latest.json").exists())

    def test_tampered_artifact_and_escaping_pointer_rejected(self):
        self.updater.refresh()
        pointer = read_json(self.output / "latest.json")
        write_json(self.output / "latest.json", {**pointer, "snapshot": "../routing"})
        with self.assertRaisesRegex(ValueError, "pointer"):
            load_latest(self.output, now=AT)
        write_json(self.output / "latest.json", pointer)
        (self.output / pointer["snapshot"] / "features" / "edge_features.jsonl").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum"):
            load_latest(self.output, now=AT)

    def test_output_and_settings_cannot_overwrite_inputs(self):
        with self.assertRaisesRegex(ValueError, "separate"):
            LiveUpdater(self.routing, self.plan, self.routing / "live", user_agent="tests")
        with self.assertRaisesRegex(ValueError, "settings changed"):
            LiveUpdater(self.routing, self.plan, self.output, user_agent="tests", max_age_hours=3)
        grid = read_json(self.plan / "manifest.json")
        write_json(self.plan / "manifest.json", {**grid, "routingVersion": "another-network"})
        with self.assertRaisesRegex(ValueError, "another routing"):
            LiveUpdater(self.routing, self.plan, self.root / "other-live", user_agent="tests")

    def test_watch_retries_without_overlapping_and_validates_interval(self):
        updater = Mock()
        updater.refresh.side_effect = [requests.Timeout("outage"), {}]
        sleep = Mock()
        self.assertEqual(watch(updater, interval_hours=0.5, cycles=2, sleep=sleep), 0)
        self.assertEqual(updater.refresh.call_count, 2)
        self.assertEqual(sum(c.args[0] for c in sleep.call_args_list), 1800)
        updater.refresh.side_effect = ValueError("still unavailable")
        self.assertEqual(watch(updater, cycles=1, sleep=sleep), 1)
        for interval in (0, -1, 0.01, float("nan")):
            with self.assertRaises(ValueError):
                watch(updater, interval_hours=interval, cycles=1, sleep=sleep)

    def test_profile_mutation_is_rejected_before_fetch(self):
        with patch("geo_data.realtime.load_profile", return_value={"changed": True}):
            with self.assertRaisesRegex(ValueError, "source/config changed"):
                self.updater.refresh()
        self.session.get.assert_not_called()
