import hashlib
import json
from pathlib import Path

from backend.api.errors import ApiError


class ScenarioCatalog:
    """Cache catalog/fixtures once, using the M2 receipt pinned to the server build."""
    def __init__(self, settings):
        self.settings = settings
        self.loaded = False

    def initialize(self):
        try:
            config = self.settings.installation()
            g0 = json.loads(Path(config["latest_preflight_receipt"]).read_text(encoding="utf-8"))
            if (g0["status"] != "G0_TECHNICAL_PASS" or g0["step1_input_completeness"] != "COMPLETE_VERIFIED"
                or g0["build_sha256"] != config["expected_build_sha256"]
                or Path(g0["snapshot_root"]).resolve() != self.settings.project_root.resolve()
                or Path(config["snapshot_root"]).resolve() != self.settings.project_root.resolve()):
                raise ValueError("G0 source/build binding")
            root = Path(config["runtime_root"])
            inventory = json.loads((root / "production_inventory.json").read_bytes())
            body = {key: value for key, value in inventory.items() if key != "build_sha256"}
            sha = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            if inventory["build_sha256"] != config["expected_build_sha256"] or sha != config["expected_build_sha256"]:
                raise ValueError("Inventory binding")
            receipt_name = "optimization/integration/member1_trusted_receipt.json"
            raw = (root / receipt_name).read_bytes()
            pin = inventory["files"][receipt_name]
            if len(raw) != pin["bytes"] or hashlib.sha256(raw).hexdigest() != pin["sha256"]:
                raise ValueError("Unpinned trusted receipt")
            trusted = json.loads(raw)
            self.receipt_sha256 = hashlib.sha256(raw).hexdigest()
            self.suite_id = trusted["suite_id"]
            if self.suite_id != "thu-duc-binh-thanh-v1":
                raise ValueError("Unsupported pinned suite")
            self.catalog_path = self.settings.project_root / "scenarios/manifests" / (self.suite_id + ".json")
            self.catalog_sha256 = trusted["catalog_raw_sha256"]
            self.catalog = json.loads(self._verified_bytes(self.catalog_path, self.catalog_sha256))
            if (self.catalog["schemaVersion"] != "member1-scenario-catalog/1"
                or self.catalog["version"] != trusted["catalog_version"] or self.catalog["suiteReady"] is not True
                or set(self.catalog["scenarios"]) != {f"S{i}" for i in range(9)}):
                raise ValueError("Catalog version/domain")
            self.fixture_sha256 = trusted["fixture_raw_sha256"]
            self.fixtures, self.fixture_paths = {}, {}
            for scenario_id in self.catalog["scenarios"]:
                path = self.settings.project_root / "scenarios/fixtures" / self.suite_id / (scenario_id + ".json")
                fixture = json.loads(self._verified_bytes(path, self.fixture_sha256[scenario_id]))
                entry = self.catalog["scenarios"][scenario_id]
                if (fixture["scenarioId"] != scenario_id or entry["sha256"] != self.fixture_sha256[scenario_id]
                    or len(fixture["initialState"]["orders"]) != entry["orders"]
                    or len(fixture["initialState"]["vehicles"]) != entry["vehicles"]):
                    raise ValueError("Fixture identity/counts")
                self.fixtures[scenario_id], self.fixture_paths[scenario_id] = fixture, path
            self.loaded = True
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.loaded = False
            raise ApiError(503, "CATALOG_NOT_VERIFIED", "scenarios", "Pinned scenario catalog is unavailable") from error

    @staticmethod
    def _verified_bytes(path, expected):
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Source bytes changed")
        return raw

    def ensure_current(self, scenario_id=None):
        if not self.loaded:
            raise ApiError(503, "CATALOG_NOT_VERIFIED", "scenarios", "Pinned scenario catalog is unavailable")
        try:
            self._verified_bytes(self.catalog_path, self.catalog_sha256)
            for key in ([scenario_id] if scenario_id else self.fixtures):
                self._verified_bytes(self.fixture_paths[key], self.fixture_sha256[key])
        except (OSError, ValueError, KeyError) as error:
            raise ApiError(503, "SOURCE_CHANGED", "scenarios", "Pinned scenario bytes changed; restore the verified snapshot") from error

    def healthy(self):
        try:
            self.ensure_current()
            return True
        except ApiError:
            return False

    def fixture(self, scenario_id):
        self.ensure_current(scenario_id)
        return self.fixtures[scenario_id]

    def public(self):
        self.ensure_current()
        return {"schema_version": "saferoute-m3-scenario-catalog/1", "suite_id": self.suite_id,
            "catalog_version": self.catalog["version"], "catalog_sha256": self.catalog_sha256,
            "source_run": self.catalog["sourceRun"], "versions": self.catalog["versions"], "seed": self.catalog["seed"],
            "source_units": self.catalog["units"], "projection_units": {"distance": "m", "duration": "s", "mass": "kg", "money": "VND"},
            "execution_mode": "SIMULATED_REPLAY", "real_world_observation": False,
            "scenarios": [self.detail(key) for key in sorted(self.fixtures)]}

    def detail(self, scenario_id):
        fixture = self.fixture(scenario_id)
        return {"scenario_id": scenario_id, "fixture_sha256": self.fixture_sha256[scenario_id],
            "initial_time": fixture["initialState"]["currentTime"], "order_count": len(fixture["initialState"]["orders"]),
            "vehicle_count": len(fixture["initialState"]["vehicles"]), "source_type": fixture["sourceType"],
            "events": [{"event_id": event["eventId"], "event_type": event["type"], "timestamp": event["timestamp"]}
                       for event in fixture["events"]]}
