"""Auditable read-only Member 1 S0 snapshot load and new-run export."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .member1_s0 import SCHEMA_VERSION, SOLVER_VERSION, SearchLimits, solve_s0
from .member1_s0_graph import Member1RoadGraph
from .member1_s0_validation import VALIDATOR_VERSION, validate_s0_solution


SUITE_ID = "thu-duc-binh-thanh-v1"
RUN_ID = "member1-tdbt-v1"
EPOCH = "2026-09-27T21:00:00+07:00"
PINNED_SOURCE_HASHES = {
    "fixture_s0_sha256": "e981e7d8f120fb75ae4390e99757583690548256deb288821f74eee6e35f04f7",
    "network_sqlite_sha256": "d4f2412884d7809cba79f86b65f6677c025ebf3e0ead357d3fc3f9cea553a204",
    "features_sqlite_sha256": "8870d537c4f3eb3dca07a177951c66abfe204fe61039852109b92f4751981469",
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(document: dict[str, Any]) -> str:
    # Matches Member 1 geo_data.common.encode_json, without importing or editing M1.
    data = (json.dumps(document, ensure_ascii=False, sort_keys=True,
                       indent=2, allow_nan=False) + "\n").encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _verified_snapshot(snapshot_root: Path) -> tuple[dict[str, Any], Path, Path, dict[str, str]]:
    scenarios = snapshot_root.resolve() / "scenarios"
    catalog_path = scenarios / "manifests" / f"{SUITE_ID}.json"
    catalog = _read_json(catalog_path)
    if catalog.get("suiteId") != SUITE_ID or catalog.get("at") != EPOCH or catalog.get("schemaVersion") != "member1-scenario-catalog/1":
        raise ValueError("Member 1 suite/epoch/catalog schema mismatch")
    source_run = catalog.get("sourceRun")
    if not isinstance(source_run, str) or "\\" in source_run or ":" in source_run or ".." in Path(source_run).parts:
        raise ValueError("catalog sourceRun is not a safe relative path")
    run = (scenarios / source_run).resolve()
    if not run.is_relative_to(scenarios / "cached_context") or run.name != RUN_ID:
        raise ValueError("catalog does not reference the approved M1 run")
    routing_dir = run / "routing"
    features_dir = run / "features"
    manifests = {name: _read_json(run / name / "manifest.json") for name in ("routing", "features", "scenarios")}
    for name, manifest in manifests.items():
        if not manifest.get("complete") or _canonical_digest(manifest) != catalog["sourceManifestHashes"].get(name):
            raise ValueError(f"{name} manifest is incomplete or differs from catalog identity")
    routing, features, scenarios_manifest = (manifests[name] for name in ("routing", "features", "scenarios"))
    if (routing.get("version") != catalog["versions"].get("routing")
        or features.get("version") != catalog["versions"].get("features")
        or scenarios_manifest.get("version") != catalog["versions"].get("scenarios")
        or features.get("routingVersion") != routing.get("version")
        or scenarios_manifest.get("routingVersion") != routing.get("version")
        or scenarios_manifest.get("featuresVersion") != features.get("version")
        or features.get("contextVersion") != catalog.get("contextVersion")
        or scenarios_manifest.get("contextVersion") != catalog.get("contextVersion")
        or features.get("at") != EPOCH or scenarios_manifest.get("at") != EPOCH
        or not routing.get("routingReady") or not routing.get("requiresTurnAwareReader")):
        raise ValueError("routing/features/scenario versions or decision epoch are incompatible")
    s0_relative = catalog["scenarios"]["S0"]["file"]
    if s0_relative != f"fixtures/{SUITE_ID}/S0.json":
        raise ValueError("catalog S0 fixture path is not the approved suite")
    s0_path = (scenarios / s0_relative).resolve()
    network = routing_dir / "network.sqlite"
    feature_db = features_dir / "features.sqlite"
    expected = {
        "fixture_s0_sha256": catalog["scenarios"]["S0"]["sha256"],
        "network_sqlite_sha256": routing["files"]["network.sqlite"]["sha256"],
        "features_sqlite_sha256": features["files"]["features.sqlite"]["sha256"],
    }
    actual = {"fixture_s0_sha256": _sha(s0_path),
              "network_sqlite_sha256": _sha(network),
              "features_sqlite_sha256": _sha(feature_db)}
    if actual != expected or actual["fixture_s0_sha256"] != scenarios_manifest["files"]["S0.json"]["sha256"]:
        raise ValueError("S0/routing/features byte hashes disagree with frozen manifests")
    if any(actual[key] != value for key, value in PINNED_SOURCE_HASHES.items()):
        raise ValueError("S0/routing/features bytes are not the pinned integration snapshot")
    fixture = _read_json(s0_path)
    if (fixture.get("scenarioId") != "S0" or fixture.get("routingVersion") != routing["version"]
        or fixture.get("featuresVersion") != features["version"]
        or fixture.get("contextVersion") != features["contextVersion"]
        or fixture.get("deliveryAreaVersion") != scenarios_manifest.get("deliveryAreaVersion")
        or fixture.get("deliveryAreaVersion") != fixture.get("deliveryArea", {}).get("version")
        or fixture.get("initialState", {}).get("currentTime") != EPOCH):
        raise ValueError("S0 fixture identities disagree with the verified run")
    actual.update({"catalog_sha256": _sha(catalog_path),
                   "routing_manifest_sha256": _sha(routing_dir / "manifest.json"),
                   "features_manifest_sha256": _sha(features_dir / "manifest.json")})
    return fixture, network, feature_db, actual


def _snapshot_still_verified(snapshot_root: Path, fixture: dict[str, Any],
                             trusted_fixture: dict[str, Any], network: Path,
                             features: Path, hashes: dict[str, str]) -> bool:
    """Recheck original file bytes and immutable parsed content after solving.

    JSON reserialization is deliberately not used as a substitute for the
    SHA-256 of Member 1's original fixture file.
    """
    fixture_path = snapshot_root.resolve() / "scenarios" / "fixtures" / SUITE_ID / "S0.json"
    try:
        return (fixture == trusted_fixture
                and all(hashes.get(key) == value for key, value in PINNED_SOURCE_HASHES.items())
                and _sha(fixture_path) == hashes["fixture_s0_sha256"]
                and _read_json(fixture_path) == trusted_fixture
                and _sha(network) == hashes["network_sqlite_sha256"]
                and _sha(features) == hashes["features_sqlite_sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def run_member1_s0(snapshot_root: str | Path, output_root: str | Path, *,
                   limits: SearchLimits | None = None) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """Verify M1 inputs, solve and independently validate, then export a new run."""

    fixture, network, features, hashes = _verified_snapshot(Path(snapshot_root))
    trusted_fixture = deepcopy(fixture)
    with Member1RoadGraph(
        network, features, routing_version=fixture["routingVersion"],
        features_version=fixture["featuresVersion"],
        context_version=fixture["contextVersion"], source_hashes=hashes,
    ) as graph:
        solution = solve_s0(fixture, graph, limits=limits)
        validation = validate_s0_solution(solution, trusted_fixture, graph)
    snapshot_stable = _snapshot_still_verified(Path(snapshot_root), fixture, trusted_fixture,
                                               network, features, hashes)
    if solution["status"] == "FEASIBLE" and not validation["valid"]:
        solution["status"] = "INVALID_DATA"
        solution["diagnostics"].append({"severity": "FATAL", "code": "INDEPENDENT_VALIDATION_FAILED",
                                        "message": "Solver witness failed independent validation",
                                        "context": {"codes": [item["code"] for item in validation["diagnostics"]]}})
    gate_pass = (solution["status"] == "FEASIBLE" and validation["valid"]
                 and snapshot_stable
                 and len(trusted_fixture["initialState"]["orders"]) == 3
                 and sorted(solution["served_orders"]) ==
                 sorted(order["id"] for order in trusted_fixture["initialState"]["orders"])
                 and not solution["unserved_orders"])
    output_root = Path(output_root).resolve()
    run_id = "S0_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = output_root / run_id
    destination.mkdir(parents=True, exist_ok=False)
    artifacts = {"solution.json": solution, "validation.json": validation}
    files = {}
    for name, document in artifacts.items():
        path = destination / name
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        files[name] = {"bytes": path.stat().st_size, "sha256": _sha(path)}
    code_files = (Path(__file__), Path(__file__).with_name("member1_s0.py"),
                  Path(__file__).with_name("member1_s0_graph.py"),
                  Path(__file__).with_name("member1_s0_validation.py"))
    manifest = {
        "schema_version": "member1-s0-run-manifest/3", "run_id": run_id,
        "solver_version": SOLVER_VERSION, "solution_schema_version": SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION, "snapshot_verified": snapshot_stable,
        "status": solution["status"], "validation_valid": validation["valid"],
        "s0_gate": "PASS" if gate_pass else "FAIL",
        "snapshot_diagnostics": ([] if snapshot_stable else [{
            "severity": "ERROR", "code": "SNAPSHOT_IDENTITY_INVALID",
            "message": "Pinned snapshot identity or fixture content changed after preflight",
            "context": {},
        }]),
        "source_versions": solution["source_versions"], "source_hashes": hashes,
        "code_sha256": {path.name: _sha(path) for path in code_files},
        "files": files,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return solution, validation, destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Run only TASK-02 × Member 1 S0 integration")
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--max-road-states", type=int, default=250_000)
    parser.add_argument("--max-arrival-options", type=int, default=4)
    parser.add_argument("--max-route-states", type=int, default=50_000)
    parser.add_argument("--time-limit-seconds", type=float, default=120.0)
    args = parser.parse_args()
    limits = SearchLimits(args.max_road_states, args.max_arrival_options,
                          args.max_route_states, args.time_limit_seconds)
    solution, validation, path = run_member1_s0(args.snapshot_root, args.output_root, limits=limits)
    manifest = _read_json(path / "manifest.json")
    print(json.dumps({"status": solution["status"], "s0_gate": manifest["s0_gate"],
                      "served_orders": solution["served_orders"],
                      "unserved_orders": solution["unserved_orders"],
                      "validation_valid": validation["valid"],
                      "run_dir": str(path), "search": solution["search"]}, ensure_ascii=False))
    return 0 if manifest["s0_gate"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
