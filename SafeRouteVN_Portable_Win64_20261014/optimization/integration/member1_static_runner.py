"""Read-only pinned-M1 runner for static S1/S5/S6 witnesses and D/T/R/G."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from optimization.models.decision_state import DecisionState

from .member1_decision_state_adapter import load_pinned_initial_states
from .member1_path_foundation import (
    CandidateFoundationLimits,
    FOUNDATION_SCHEMA_VERSION,
    FOUNDATION_VERSION,
    build_source_dtrg_foundation,
)
from .member1_s0_graph import Member1RoadGraph
from .member1_s0_runner import PINNED_SOURCE_HASHES
from .member1_s1 import S1SearchLimits
from .member1_s1_runner import (
    SIDECAR_SUFFIXES,
    S1SidecarError,
    _require_no_sqlite_sidecars,
)
from .member1_static import (
    SCHEMA_VERSION,
    SOLVER_VERSION,
    SUPPORTED_SCENARIOS,
    solve_static_initial_state,
)
from .member1_static_validation import VALIDATOR_VERSION, validate_static_solution


MANIFEST_VERSION = "member1-static-s1-s5-s6-run-manifest/2"
ACCEPTED_FOUNDATION_GATES = frozenset({"PASS", "PASS_WITH_DECLARED_TRUNCATION"})


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
    return {
        f"fixture_{state.scenario_id.lower()}_sha256":
            scenarios / "fixtures" / state.suite_id / f"{state.scenario_id}.json",
        "catalog_sha256": scenarios / "manifests" / f"{state.suite_id}.json",
        "receipt_sha256": Path(__file__).with_name("member1_trusted_receipt.json"),
        "network_sqlite_sha256": run / "routing" / "network.sqlite",
        "features_sqlite_sha256": run / "features" / "features.sqlite",
        "routing_manifest_sha256": run / "routing" / "manifest.json",
        "features_manifest_sha256": run / "features" / "manifest.json",
    }


def _verified_source(root: Path, state: DecisionState) -> tuple[dict[str, Path], dict[str, str]]:
    if state.source_authentication == "RUN_MANIFEST_VERIFIED":
        from optimization.runtime.run_source import verified_source
        return verified_source(root, state)
    paths = _source_paths(root, state)
    hashes = {key: _sha(path) for key, path in paths.items()}
    fixture_key = f"fixture_{state.scenario_id.lower()}_sha256"
    expected = {
        fixture_key: state.fixture_raw_sha256,
        "catalog_sha256": state.catalog_raw_sha256,
        "receipt_sha256": state.receipt_sha256,
        "network_sqlite_sha256": PINNED_SOURCE_HASHES["network_sqlite_sha256"],
        "features_sqlite_sha256": PINNED_SOURCE_HASHES["features_sqlite_sha256"],
    }
    for key, value in expected.items():
        if hashes[key] != value:
            raise ValueError(f"pinned source byte mismatch: {key}")
    routing = json.loads(paths["routing_manifest_sha256"].read_bytes())
    features = json.loads(paths["features_manifest_sha256"].read_bytes())
    if (
        routing.get("version") != state.routing_version
        or features.get("version") != state.features_version
        or features.get("contextVersion") != state.context_version
        or routing.get("files", {}).get("network.sqlite", {}).get("sha256")
            != hashes["network_sqlite_sha256"]
        or features.get("files", {}).get("features.sqlite", {}).get("sha256")
            != hashes["features_sqlite_sha256"]
    ):
        raise ValueError("routing/features manifest identity differs from DecisionState")
    return paths, hashes


def run_member1_static(
    snapshot_root: str | Path,
    output_root: str | Path,
    *,
    scenario_id: str,
    solver_limits: S1SearchLimits | None = None,
    foundation_limits: CandidateFoundationLimits | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    if scenario_id not in SUPPORTED_SCENARIOS:
        raise ValueError("scenario_id must be S1, S5, or S6")
    root = Path(snapshot_root).resolve()
    batch = load_pinned_initial_states(root)
    if (batch.get("status") != "INITIAL_STATE_READY"
        or batch.get("source_gate") != "M1_SOURCE_CONTRACT_GATE_PASS"):
        raise ValueError(f"M1 receipt/semantic source gate failed: {batch.get('diagnostics')}")
    raw_state = next((item for item in batch.get("states", ())
                      if item.get("scenario_id") == scenario_id), None)
    if raw_state is None:
        raise ValueError(f"pinned state is missing: {scenario_id}")
    state = DecisionState.from_dict(raw_state)
    if (
        state.source_authentication != "PINNED_RECEIPT_VERIFIED"
        or len(state.orders) != 8 or len(state.vehicles) != 2
        or state.pending_events or state.current_plans or state.execution_updates
    ):
        raise ValueError(f"pinned {scenario_id} static initial-state contract is not satisfied")
    paths, hashes = _verified_source(root, state)
    _require_no_sqlite_sidecars(paths, phase="before_open")
    run_id = f"{scenario_id}_STATIC_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        with Member1RoadGraph(
            paths["network_sqlite_sha256"], paths["features_sqlite_sha256"],
            routing_version=state.routing_version,
            features_version=state.features_version,
            context_version=state.context_version,
            source_hashes=hashes,
        ) as graph:
            foundation = build_source_dtrg_foundation(
                state, graph, limits=foundation_limits,
            )
            solution = solve_static_initial_state(state, graph, limits=solver_limits)
            solution["run_id"] = run_id
            validation = validate_static_solution(solution, state, graph)
    finally:
        _require_no_sqlite_sidecars(paths, phase="after_close")

    after = {key: _sha(path) for key, path in paths.items()}
    snapshot_verified = after == hashes
    served_count = len(solution.get("served_orders", ()))
    witness_valid = validation.get("valid") is True
    if (
        snapshot_verified and foundation.get("gate") in ACCEPTED_FOUNDATION_GATES and witness_valid
        and solution.get("status") == "FEASIBLE"
        and served_count == len(state.orders) and not solution.get("unserved_orders")
    ):
        gate = f"{scenario_id}_STATIC_VALIDATED_FULL_{served_count}_OF_{len(state.orders)}"
    elif (
        snapshot_verified and foundation.get("gate") in ACCEPTED_FOUNDATION_GATES and witness_valid
        and solution.get("status") == "PARTIAL"
        and 0 < served_count < len(state.orders)
    ):
        gate = f"{scenario_id}_STATIC_VALIDATED_PARTIAL_{served_count}_OF_{len(state.orders)}"
    else:
        gate = f"{scenario_id}_STATIC_BLOCKED"

    destination = Path(output_root).resolve() / run_id
    destination.mkdir(parents=True, exist_ok=False)
    documents = {
        "foundation.json": foundation,
        "solution.json": solution,
        "validation.json": validation,
    }
    for name, document in documents.items():
        (destination / name).write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n",
            encoding="utf-8",
        )
    code_files = [
        Path(__file__), Path(__file__).with_name("member1_static.py"),
        Path(__file__).with_name("member1_static_validation.py"),
        Path(__file__).with_name("member1_path_foundation.py"),
        Path(__file__).with_name("member1_s1.py"),
        Path(__file__).with_name("member1_s0_graph.py"),
        Path(__file__).with_name("member1_s0_runner.py"),
        Path(__file__).with_name("member1_s1_runner.py"),
        Path(__file__).with_name("member1_decision_state_adapter.py"),
        Path(__file__).with_name("member1_mapping_audit.py"),
        Path(__file__).with_name("member1_trusted_receipt.json"),
        Path(__file__).resolve().parents[1] / "matrix" / "matrix_builder.py",
        Path(__file__).resolve().parents[1] / "models" / "candidate_path.py",
        Path(__file__).resolve().parents[1] / "models" / "matrix_bundle.py",
        Path(__file__).resolve().parents[1] / "models" / "decision_state.py",
    ]
    code_names = [path.name for path in code_files]
    if len(code_names) != len(set(code_names)):
        raise ValueError("duplicate code provenance filename")
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "run_id": run_id,
        "scenario_id": scenario_id,
        "suite_id": state.suite_id,
        "source_run": state.source_run,
        "gate": gate,
        "foundation_gate": foundation.get("gate"),
        "foundation_schema_version": FOUNDATION_SCHEMA_VERSION,
        "foundation_version": FOUNDATION_VERSION,
        "solution_status": solution.get("status"),
        "solution_schema_version": SCHEMA_VERSION,
        "solver_version": SOLVER_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "state_schema_version": state.schema_version,
        "snapshot_verified": snapshot_verified,
        "validation_valid": validation.get("valid"),
        "database_hashes_checked_by_this_runner": True,
        "sqlite_sidecar_guard": {
            "version": "m1-static-sqlite-sidecar-guard/1",
            "suffixes": list(SIDECAR_SUFFIXES),
            "before_open_absent": True,
            "after_close_absent": True,
        },
        "served_count": served_count,
        "order_count": len(state.orders),
        "pair_count": len(foundation.get("pair_coverage", ())),
        "truncated_pairs": foundation.get("truncated_pairs", ()),
        "missing_pairs": foundation.get("missing_pairs", ()),
        "source_hashes_before": hashes,
        "source_hashes_after": after,
        "source_versions": solution.get("source_versions"),
        "search_limits": solution.get("search", {}).get("limits"),
        "foundation_limits": foundation.get("limits"),
        "code_provenance_scope": (
            "direct static solver/validator/foundation modules plus receipt, source-audit, "
            "pinned-source and SQLite-sidecar trust-gate dependencies"
        ),
        "code_sha256": {path.name: _sha(path) for path in code_files},
        "files": {
            name: {"bytes": (destination / name).stat().st_size,
                   "sha256": _sha(destination / name)}
            for name in documents
        },
        "integrated_solver_validated": witness_valid,
        "optimality_proven": False,
        "general_m1_validated": False,
        "e4_run": False,
        "production_calibration_configured": False,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2,
                   allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return solution, validation, foundation, destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run pinned M1 static S1/S5/S6 path foundation and witness validation"
    )
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--scenario-id", required=True, choices=sorted(SUPPORTED_SCENARIOS))
    parser.add_argument("--time-limit-seconds", type=float, default=600.0)
    parser.add_argument("--foundation-time-limit-seconds", type=float, default=240.0)
    args = parser.parse_args()
    try:
        solution, validation, foundation, destination = run_member1_static(
            args.snapshot_root, args.output_root, scenario_id=args.scenario_id,
            solver_limits=S1SearchLimits(time_limit_seconds=args.time_limit_seconds),
            foundation_limits=CandidateFoundationLimits(
                time_limit_seconds=args.foundation_time_limit_seconds,
                k_per_objective=1,
            ),
        )
        manifest = json.loads((destination / "manifest.json").read_bytes())
        print(json.dumps({
            "run_id": manifest["run_id"], "gate": manifest["gate"],
            "foundation_gate": foundation["gate"], "status": solution["status"],
            "served": solution["served_orders"], "unserved": solution["unserved_orders"],
            "validation_valid": validation["valid"], "search": solution["search"],
            "output_dir": str(destination),
        }, ensure_ascii=False))
        return 0 if manifest["gate"].startswith(f"{args.scenario_id}_STATIC_VALIDATED_") else 2
    except S1SidecarError as error:
        print(json.dumps({"gate": f"{args.scenario_id}_STATIC_BLOCKED",
                          "diagnostics": [error.to_diagnostic()]}, ensure_ascii=False))
        return 2
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({"gate": f"{args.scenario_id}_STATIC_BLOCKED",
                          "diagnostic": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MANIFEST_VERSION", "run_member1_static"]
