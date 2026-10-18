"""Read-only pinned-M1 S1 run: receipt gate, SQLite witness, independent audit."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from optimization.models.decision_state import DecisionState
from .member1_decision_state_adapter import load_pinned_initial_states
from .member1_s0_graph import Member1RoadGraph
from .member1_s0_runner import PINNED_SOURCE_HASHES
from .member1_s1 import SCHEMA_VERSION, SOLVER_VERSION, S1SearchLimits, solve_s1
from .member1_s1_validation import VALIDATOR_VERSION, validate_s1_solution


MANIFEST_VERSION = "member1-s1-run-manifest/2"
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class S1SidecarError(ValueError):
    """A SQLite sidecar can make the read view differ from hashed base bytes."""

    def __init__(self, code: str, path: Path, phase: str, detail: str) -> None:
        super().__init__(f"{code} at {path} ({phase}): {detail}")
        self.code = code
        self.path = path
        self.phase = phase
        self.detail = detail

    def to_diagnostic(self) -> dict[str, str]:
        return {"severity": "ERROR", "code": self.code, "path": str(self.path),
                "phase": self.phase, "message": self.detail}


def _require_no_sqlite_sidecars(paths: dict[str, Path], *, phase: str) -> None:
    """Fail closed even for empty sidecars or symlinks, before/after graph use.

    A read-only SQLite connection can still consult an existing WAL. Hashing
    only the base .sqlite bytes therefore cannot authenticate its read view.
    This check never opens or changes the M1 files or their journal mode.
    """
    if phase not in {"before_open", "after_close"}:
        raise ValueError("unknown S1 SQLite sidecar-check phase")
    for key in ("network_sqlite_sha256", "features_sqlite_sha256"):
        database = paths[key]
        for suffix in SIDECAR_SUFFIXES:
            sidecar = database.with_name(database.name + suffix)
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise S1SidecarError("SQLITE_SIDECAR_CHECK_FAILED", sidecar, phase,
                                     f"cannot establish sidecar absence: {error}") from error
            raise S1SidecarError("SQLITE_SIDECAR_PRESENT", sidecar, phase,
                                 "SQLite sidecar exists; hashed base file may not represent the read view")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_paths(root: Path, state: DecisionState) -> dict[str, Path]:
    if state.source_run != "cached_context/hcmc/member1-tdbt-v1":
        raise ValueError("source run is not the pinned M1 run")
    scenarios = root / "scenarios"
    run = scenarios / state.source_run
    return {"fixture_s1_sha256": scenarios / "fixtures" / state.suite_id / "S1.json",
            "catalog_sha256": scenarios / "manifests" / f"{state.suite_id}.json",
            "receipt_sha256": Path(__file__).with_name("member1_trusted_receipt.json"),
            "network_sqlite_sha256": run / "routing" / "network.sqlite",
            "features_sqlite_sha256": run / "features" / "features.sqlite",
            "routing_manifest_sha256": run / "routing" / "manifest.json",
            "features_manifest_sha256": run / "features" / "manifest.json"}


def _verified_source(root: Path, state: DecisionState) -> tuple[dict[str, Path], dict[str, str]]:
    paths = _source_paths(root, state)
    hashes = {key: _sha(path) for key, path in paths.items()}
    expected = {"fixture_s1_sha256": state.fixture_raw_sha256,
                "catalog_sha256": state.catalog_raw_sha256,
                "receipt_sha256": state.receipt_sha256,
                "network_sqlite_sha256": PINNED_SOURCE_HASHES["network_sqlite_sha256"],
                "features_sqlite_sha256": PINNED_SOURCE_HASHES["features_sqlite_sha256"]}
    for key, value in expected.items():
        if hashes[key] != value:
            raise ValueError(f"pinned source byte mismatch: {key}")
    routing = json.loads(paths["routing_manifest_sha256"].read_bytes())
    features = json.loads(paths["features_manifest_sha256"].read_bytes())
    if (routing.get("version") != state.routing_version
        or features.get("version") != state.features_version
        or features.get("contextVersion") != state.context_version
        or routing.get("files", {}).get("network.sqlite", {}).get("sha256") != hashes["network_sqlite_sha256"]
        or features.get("files", {}).get("features.sqlite", {}).get("sha256") != hashes["features_sqlite_sha256"]):
        raise ValueError("routing/features manifest identity differs from S1 DecisionState")
    return paths, hashes


def run_member1_s1(snapshot_root: str | Path, output_root: str | Path, *,
                   limits: S1SearchLimits | None = None) -> tuple[dict[str, Any], dict[str, Any], Path]:
    root = Path(snapshot_root).resolve()
    batch = load_pinned_initial_states(root)
    if batch.get("status") != "INITIAL_STATE_READY" or batch.get("source_gate") != "M1_SOURCE_CONTRACT_GATE_PASS":
        raise ValueError(f"M1 receipt/semantic source gate failed: {batch.get('diagnostics')}")
    state = DecisionState.from_dict(batch["states"][1])
    if (state.scenario_id != "S1" or state.source_authentication != "PINNED_RECEIPT_VERIFIED"
        or len(state.orders) != 8 or len(state.vehicles) != 2
        or state.pending_events or state.current_plans or state.execution_updates):
        raise ValueError("pinned S1 initial state contract is not satisfied")
    paths, hashes = _verified_source(root, state)
    _require_no_sqlite_sidecars(paths, phase="before_open")
    run_id = "S1_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        with Member1RoadGraph(paths["network_sqlite_sha256"], paths["features_sqlite_sha256"],
                              routing_version=state.routing_version,
                              features_version=state.features_version,
                              context_version=state.context_version,
                              source_hashes=hashes) as graph:
            solution = solve_s1(state, graph, limits=limits)
            solution["run_id"] = run_id
            validation = validate_s1_solution(solution, state, graph)
    finally:
        # Also runs when opening, solving, validation, or graph cleanup fails.
        # A sidecar finding takes precedence: no run may be certified from an
        # unauthenticated SQLite read view.
        _require_no_sqlite_sidecars(paths, phase="after_close")
    after = {key: _sha(path) for key, path in paths.items()}
    snapshot_verified = after == hashes
    covered = len(solution.get("served_orders", []))
    gate = ("S1_VALIDATED_FULL_8_OF_8" if snapshot_verified and validation["valid"]
            and solution["status"] == "FEASIBLE" and covered == 8
            and not solution["unserved_orders"]
            else f"S1_VALIDATED_PARTIAL_{covered}_OF_8" if snapshot_verified and validation["valid"]
            and solution["status"] == "PARTIAL" and 0 < covered < 8
            else "S1_BLOCKED")
    destination = Path(output_root).resolve() / run_id
    destination.mkdir(parents=True, exist_ok=False)
    documents = {"solution.json": solution, "validation.json": validation}
    for name, document in documents.items():
        (destination / name).write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8")
    code_files = [Path(__file__), Path(__file__).with_name("member1_s1.py"),
                  Path(__file__).with_name("member1_s1_validation.py"),
                  Path(__file__).with_name("member1_s0_graph.py"),
                  Path(__file__).with_name("member1_decision_state_adapter.py"),
                  Path(__file__).resolve().parents[1] / "models" / "decision_state.py"]
    manifest = {"schema_version": MANIFEST_VERSION, "run_id": run_id,
                "suite_id": state.suite_id, "source_run": state.source_run,
                "gate": gate, "solution_status": solution["status"],
                "solution_schema_version": SCHEMA_VERSION,
                "solver_version": SOLVER_VERSION, "validator_version": VALIDATOR_VERSION,
                "state_schema_version": state.schema_version,
                "snapshot_verified": snapshot_verified, "validation_valid": validation["valid"],
                "database_hashes_checked_by_this_runner": True,
                "sqlite_sidecar_guard": {"version": "s1-sqlite-sidecar-guard/1",
                                         "suffixes": list(SIDECAR_SUFFIXES),
                                         "before_open_absent": True, "after_close_absent": True},
                "served_count": covered, "order_count": len(state.orders),
                "source_hashes_before": hashes, "source_hashes_after": after,
                "source_versions": solution["source_versions"],
                "code_sha256": {path.name: _sha(path) for path in code_files},
                "files": {name: {"bytes": (destination / name).stat().st_size,
                                 "sha256": _sha(destination / name)} for name in documents},
                "integrated_solver_validated": gate == "S1_VALIDATED_FULL_8_OF_8",
                "general_m1_validated": False, "e4_run": False}
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")
    return solution, validation, destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Run only pinned M1 S1 static eight-order integration")
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--time-limit-seconds", type=float, default=600.0)
    parser.add_argument("--max-route-states", type=int, default=60_000)
    parser.add_argument("--max-road-states", type=int, default=250_000)
    parser.add_argument("--max-road-labels", type=int, default=500_000)
    parser.add_argument("--max-arrival-options", type=int, default=4)
    parser.add_argument("--max-assignments", type=int, default=128)
    parser.add_argument("--max-stop-sequences", type=int, default=12)
    args = parser.parse_args()
    try:
        limits = S1SearchLimits(time_limit_seconds=args.time_limit_seconds,
                                max_route_states=args.max_route_states,
                                max_road_states=args.max_road_states,
                                max_road_labels=args.max_road_labels,
                                max_arrival_options=args.max_arrival_options,
                                max_assignments=args.max_assignments,
                                max_stop_sequences=args.max_stop_sequences)
        solution, validation, destination = run_member1_s1(args.snapshot_root, args.output_root,
                                                            limits=limits)
        manifest = json.loads((destination / "manifest.json").read_bytes())
        print(json.dumps({"run_id": manifest["run_id"], "gate": manifest["gate"],
                          "status": solution["status"], "served": solution["served_orders"],
                          "unserved": solution["unserved_orders"],
                          "validation_valid": validation["valid"],
                          "search": solution["search"], "output_dir": str(destination)},
                         ensure_ascii=False))
        return 0 if manifest["gate"] == "S1_VALIDATED_FULL_8_OF_8" else 2
    except S1SidecarError as error:
        print(json.dumps({"gate": "S1_BLOCKED", "diagnostics": [error.to_diagnostic()]},
                         ensure_ascii=False))
        return 2
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({"gate": "S1_BLOCKED", "diagnostic": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
