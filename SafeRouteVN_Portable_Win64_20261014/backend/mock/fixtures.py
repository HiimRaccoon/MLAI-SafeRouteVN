"""Byte-pinned public examples, without loading a runtime or authority store."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from backend.services.artifact_repository import validate_execution


SOURCE_BUILD = "80694f511dc735d0b6a1a0a830edd7f6267df395e87dad0ccf180914b49d5a41"
MANIFEST_SHA256 = "dcd4902daa8f5391fa07be550375794d65851cea557c6e4024358b8217c9714f"
SCENARIOS = ("S2", "S3", "S4")
DEFAULT_ROOT = Path(__file__).with_name("fixtures")


class FixtureStore:
    def __init__(self, root=None):
        supplied = Path(root) if root is not None else DEFAULT_ROOT
        if supplied.is_symlink():
            raise ValueError("Fixture directory must not be a symbolic link")
        self.root = supplied.resolve(strict=True)
        manifest_raw = self._read("manifest.json")
        if hashlib.sha256(manifest_raw).hexdigest() != MANIFEST_SHA256:
            raise ValueError("Fixture manifest hash differs")
        self.manifest = json.loads(manifest_raw)
        if (set(self.manifest) != {"schema_version", "source_package_build_sha256", "source", "fixtures"}
                or self.manifest["schema_version"] != "saferoute-m3-mock-fixtures/1"
                or self.manifest["source_package_build_sha256"] != SOURCE_BUILD
                or self.manifest["source"] != "M2_PINNED_SHARED_EXECUTION_EXAMPLES"
                or set(self.manifest["fixtures"]) != set(SCENARIOS)):
            raise ValueError("Exact mock fixture manifest required")
        for scenario_id in SCENARIOS:
            self.view(scenario_id)

    def _read(self, name):
        path = self.root / name
        if path.is_symlink() or not path.is_file() or path.resolve(strict=True).parent != self.root:
            raise ValueError("Regular contained fixture file required")
        return path.read_bytes()

    def record(self, scenario_id):
        if scenario_id not in SCENARIOS:
            raise KeyError("Unknown mock scenario")
        record = self.manifest["fixtures"][scenario_id]
        if set(record) != {"file", "bytes", "sha256", "fixture_build_sha256", "fixture_session_id"}:
            raise ValueError("Exact fixture binding required")
        if record["file"] != scenario_id + "_execution_view.json":
            raise ValueError("Canonical scenario fixture path required")
        return deepcopy(record)

    def raw(self, scenario_id):
        record = self.record(scenario_id)
        raw = self._read(record["file"])
        if len(raw) != record["bytes"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ValueError("Fixture bytes differ from the frozen source")
        return raw

    def view(self, scenario_id):
        record = self.record(scenario_id)
        value = json.loads(self.raw(scenario_id))
        # Historical public examples keep their original execution build. The
        # package that supplies an example is not the build that produced it.
        validate_execution(value, record["fixture_session_id"], record["fixture_build_sha256"])
        return value

    def catalog(self):
        return [{"scenario_id": sid, **self.record(sid), "links": {
            "execution_view": f"/api/mock/scenarios/{sid}/execution-view",
            "exact_source": f"/api/mock/scenarios/{sid}/source"}} for sid in SCENARIOS]
