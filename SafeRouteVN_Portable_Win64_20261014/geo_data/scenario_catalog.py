"""Publish existing QA-approved scenarios into the team's fixtures/manifests layout."""

from pathlib import Path
import re

from geo_data.bundle import verify
from geo_data.common import digest, exclusive_lock, now_vn, read_json, write_json
from geo_data.delivery_area import scenario_area, validate_area_membership
from geo_data.handoff import copy_verified, verify_run
from geo_data.osm.extract_download import file_hash
from geo_data.osm.routing import RoutingGraph
from geo_data.scenario_generator import SCENARIO_SCHEMA, validate_scenario

CATALOG_SCHEMA = "member1-scenario-catalog/1"
SCENARIO_IDS = tuple(f"S{i}" for i in range(9))


def suite_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", value):
        raise ValueError("suite-id must contain 1..80 letters, digits, underscores or hyphens")
    return value


def inside(root, relative, prefix):
    """Portable POSIX paths contained in the required scenarios subtree."""
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise ValueError("Expected a relative scenarios path")
    parts = relative.split("/")
    if len(parts) < 2 or parts[0] != prefix or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"Path must stay inside scenarios/{prefix}")
    path = (root / relative).resolve()
    if not path.is_relative_to(root / prefix):
        raise ValueError("Scenario path escapes its subtree")
    return path


def checked_scenario(path, scenario_id, manifests):
    scenario = read_json(path)
    source, features = manifests["scenarios"], manifests["features"]
    if (scenario.get("schemaVersion") != "member1-scenario-draft/1"
            or scenario.get("scenarioId") != scenario_id or scenario.get("sourceType") != "SYNTHETIC"
            or scenario.get("seed") != source["seed"]
            or scenario.get("routingVersion") != manifests["routing"]["version"]
            or scenario.get("featuresVersion") != features["version"]
            or scenario.get("contextVersion") != features["contextVersion"]
            or scenario["initialState"]["currentTime"] != features["at"]):
        raise ValueError(f"Scenario {scenario_id} has inconsistent identity/versions/epoch")
    validate_scenario(scenario)
    validate_area_membership(scenario, scenario_area(path.parent, source))
    if scenario_id == "S0" and (len(scenario["initialState"]["orders"]) != 3
                               or len(scenario["initialState"]["vehicles"]) != 2):
        raise ValueError("S0 requires 3 orders and 2 vehicles")
    return scenario


def catalog_identity(root, run, suite_id, manifests):
    fixture_directory = f"fixtures/{suite_id}"
    entries = {}
    for sid in SCENARIO_IDS:
        name = sid + ".json"
        meta = manifests["scenarios"]["files"].get(name)
        if not meta:
            raise ValueError(f"Source bundle is missing {name}")
        scenario = checked_scenario(run / "scenarios" / name, sid, manifests)
        entries[sid] = {"file": f"{fixture_directory}/{name}", "sha256": meta["sha256"],
                        "orders": len(scenario["initialState"]["orders"]),
                        "vehicles": len(scenario["initialState"]["vehicles"]),
                        "events": len(scenario["events"]), "sourceType": "SYNTHETIC"}
    return {"schemaVersion": CATALOG_SCHEMA, "suiteId": suite_id,
            "sourceRun": run.relative_to(root).as_posix(), "fixtureDirectory": fixture_directory,
            "sourceManifestHashes": {k: digest(v) for k, v in manifests.items()},
            "versions": {k: v["version"] for k, v in manifests.items()},
            "contextVersion": manifests["features"]["contextVersion"],
            "at": manifests["features"]["at"], "seed": manifests["scenarios"]["seed"],
            "scenarios": entries, "suiteReady": True, "integrated": False,
            "units": {"distance": "km", "speed": "km/h", "duration": "h", "money": "VND",
                      "mass": "kg", "width": "m", "timezone": "Asia/Ho_Chi_Minh"}}


def export_scenarios(run, scenarios_root, *, suite_id, progress=print):
    """Copy the small scenario bundle; publish its catalog last, without moving cache."""
    suite_name(suite_id)
    root, run = Path(scenarios_root).resolve(), Path(run).resolve()
    try:
        relative = run.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("Run must be inside scenarios-root/cached_context") from exc
    inside(root, relative, "cached_context")
    fixtures = inside(root, f"fixtures/{suite_id}", "fixtures")
    manifest_path = inside(root, f"manifests/{suite_id}.json", "manifests")
    progress("Scenario export [1/3] verifying source bundles and QA; large files are read, not copied...")
    manifests = verify_run(run)
    identity = catalog_identity(root, run, suite_id, manifests)
    with exclusive_lock(manifest_path.parent), exclusive_lock(fixtures):
        if manifest_path.exists():
            saved = ScenarioCatalog(root, suite_id)
            if saved.manifest["version"] != digest(identity):
                raise ValueError("Suite belongs to different inputs; use a new --suite-id")
            progress(f"Scenario catalog verified: {manifest_path}")
            return saved.manifest
        state = fixtures / "export-state.json"
        if state.exists() and read_json(state) != identity:
            raise ValueError("Partial suite belongs to different inputs; use a new --suite-id")
        write_json(state, identity)
        progress("Scenario export [2/3] copying S0-S8, profile, diagnostics and QA evidence...")
        source = run / "scenarios"
        for name, meta in manifests["scenarios"]["files"].items():
            # verify_run already checks containment; do the same for the destination.
            target = inside(root, f"fixtures/{suite_id}/{name}", "fixtures")
            copy_verified(source / name, target, meta["sha256"])
            progress(f"  {name}")
        copy_verified(source / "manifest.json", fixtures / "manifest.json", file_hash(source / "manifest.json"))
        if digest(verify(fixtures, SCENARIO_SCHEMA)) != identity["sourceManifestHashes"]["scenarios"]:
            raise ValueError("Scenario source changed during export")
        for sid in SCENARIO_IDS:
            checked_scenario(fixtures / (sid + ".json"), sid, manifests)
        manifest = {**identity, "version": digest(identity), "createdAt": now_vn(), "complete": True}
        progress("Scenario export [3/3] publishing portable catalog...")
        write_json(manifest_path, manifest)
        return manifest


class ScenarioCatalog:
    """Member 2/3 read adapter, rooted at the local scenarios directory after transfer."""

    def __init__(self, scenarios_root, suite_id):
        suite_name(suite_id)
        self.root = Path(scenarios_root).resolve()
        path = inside(self.root, f"manifests/{suite_id}.json", "manifests")
        self.manifest = read_json(path)
        manifest = self.manifest
        identity = {k: v for k, v in manifest.items() if k not in ("version", "createdAt", "complete")}
        if (manifest.get("schemaVersion") != CATALOG_SCHEMA or manifest.get("complete") is not True
                or manifest.get("suiteId") != suite_id or manifest.get("version") != digest(identity)):
            raise ValueError("Scenario catalog is incomplete or its checksum/identity is invalid")
        if manifest["fixtureDirectory"] != f"fixtures/{suite_id}":
            raise ValueError("Unexpected fixture directory")
        self.run = inside(self.root, manifest["sourceRun"], "cached_context")
        self.fixtures = inside(self.root, manifest["fixtureDirectory"], "fixtures")
        self.sources = verify_run(self.run)
        expected = catalog_identity(self.root, self.run, suite_id, self.sources)
        if identity != expected:
            raise ValueError("Catalog does not match its source bundles/QA")
        fixture_manifest = verify(self.fixtures, SCENARIO_SCHEMA)
        if digest(fixture_manifest) != manifest["sourceManifestHashes"]["scenarios"]:
            raise ValueError("Fixture bundle differs from the original scenario bundle")
        self.paths = {stage: self.run / stage for stage in self.sources}
        for sid in SCENARIO_IDS:
            self.scenario(sid)

    def scenario(self, scenario_id="S0"):
        if scenario_id not in SCENARIO_IDS:
            raise ValueError("Scenario must be S0 through S8")
        entry = self.manifest["scenarios"][scenario_id]
        path = inside(self.root, entry["file"], "fixtures")
        if file_hash(path) != entry["sha256"]:
            raise ValueError(f"Fixture checksum failed: {scenario_id}")
        return checked_scenario(path, scenario_id, self.sources)

    def open_graph(self):
        return RoutingGraph(self.paths["routing"], features=self.paths["features"])
