"""Read-only Windows runner for the pinned M1 OR-Tools static bridge."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import platform
from pathlib import Path
import sys
from time import monotonic
from typing import Any

from optimization.models.decision_state import DecisionState
from optimization.solver.member1_static_cp_sat import (
    MODEL_CONFIG_VERSION,
    OBJECTIVE_POLICY_VERSION,
    SCALING_VERSION,
    M1OrToolsStaticConfig,
)

from .member1_decision_state_adapter import load_pinned_initial_states
from .member1_ortools_static import (
    MODEL_INPUT_VERSION,
    ModelInputContractError,
    SCHEMA_VERSION,
    SOLVER_VERSION,
    SUPPORTED_SCENARIOS,
    solve_member1_ortools_static,
)
from .member1_ortools_static_validation import (
    VALIDATOR_VERSION,
    validate_ortools_static_solution,
)
from .member1_s0_graph import Member1RoadGraph
from .member1_s1_runner import (
    SIDECAR_SUFFIXES,
    S1SidecarError,
    _require_no_sqlite_sidecars,
)
from .member1_static_runner import _verified_source


MANIFEST_VERSION = "member1-ortools-static-run-manifest/4"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_member1_ortools_static(
    snapshot_root: str | Path,
    output_root: str | Path,
    *,
    scenario_id: str,
    config: M1OrToolsStaticConfig | None = None,
    model_input_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    runner_started = monotonic()
    if scenario_id not in SUPPORTED_SCENARIOS:
        raise ValueError("scenario_id must be S1, S5, or S6")
    config = config or M1OrToolsStaticConfig()
    root = Path(snapshot_root).resolve()
    batch = load_pinned_initial_states(root)
    if (batch.get("status") != "INITIAL_STATE_READY"
        or batch.get("source_gate") != "M1_SOURCE_CONTRACT_GATE_PASS"):
        raise ValueError(f"M1 receipt/semantic source gate failed: {batch.get('diagnostics')}")
    raw_state = next((item for item in batch.get("states", ())
                      if item.get("scenario_id") == scenario_id), None)
    if raw_state is None:
        raise ValueError(f"pinned DecisionState is missing: {scenario_id}")
    state = DecisionState.from_dict(raw_state)
    if (state.source_authentication != "PINNED_RECEIPT_VERIFIED"
        or len(state.orders) != 8 or len(state.vehicles) != 2
        or state.pending_events or state.current_plans or state.execution_updates):
        raise ValueError(f"pinned {scenario_id} static initial-state contract is not satisfied")
    paths, hashes = _verified_source(root, state)
    cached_model_input = None
    cached_model_input_identity = None
    if model_input_path is not None:
        cache_path = Path(model_input_path).resolve()
        cached_model_input = json.loads(cache_path.read_bytes())
        cached_model_input_identity = {
            "path": str(cache_path), "bytes": cache_path.stat().st_size,
            "sha256": _sha(cache_path),
        }
    _require_no_sqlite_sidecars(paths, phase="before_open")
    source_acceptance_elapsed = monotonic() - runner_started
    run_id = f"{scenario_id}_ORTOOLS_STATIC_" + datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    try:
        with Member1RoadGraph(
            paths["network_sqlite_sha256"], paths["features_sqlite_sha256"],
            routing_version=state.routing_version,
            features_version=state.features_version,
            context_version=state.context_version,
            source_hashes=hashes,
        ) as graph:
            solve_started = monotonic()
            solution, model_input = solve_member1_ortools_static(
                state, graph, config=config, model_input=cached_model_input,
            )
            solve_elapsed = monotonic() - solve_started
            solution["run_id"] = run_id
            validation_started = monotonic()
            validation = validate_ortools_static_solution(solution, state, graph)
            validation_elapsed = monotonic() - validation_started
    finally:
        _require_no_sqlite_sidecars(paths, phase="after_close")
    after = {key: _sha(path) for key, path in paths.items()}
    snapshot_verified = after == hashes
    served_count = len(solution.get("served_orders", ()))
    witness_valid = validation.get("valid") is True
    if (snapshot_verified and model_input.get("gate") == "READY_WITH_FINITE_PATH_DOMAIN"
        and witness_valid and solution.get("status") == "FEASIBLE"
        and served_count == len(state.orders) and not solution.get("unserved_orders")):
        gate = f"{scenario_id}_ORTOOLS_VALIDATED_FULL_{served_count}_OF_{len(state.orders)}"
    elif (snapshot_verified and model_input.get("gate") == "READY_WITH_FINITE_PATH_DOMAIN"
          and witness_valid and solution.get("status") == "PARTIAL"
          and 0 < served_count < len(state.orders)):
        gate = f"{scenario_id}_ORTOOLS_VALIDATED_PARTIAL_{served_count}_OF_{len(state.orders)}"
    else:
        gate = f"{scenario_id}_ORTOOLS_BLOCKED"

    destination = Path(output_root).resolve() / run_id
    destination.mkdir(parents=True, exist_ok=False)
    documents = {
        "model_input.json": model_input,
        "solution.json": solution,
        "validation.json": validation,
    }
    publication_started = monotonic()
    for name, document in documents.items():
        (destination / name).write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n",
            encoding="utf-8",
        )
    payload_publication_elapsed = monotonic() - publication_started
    code_files = [
        Path(__file__), Path(__file__).with_name("member1_ortools_static.py"),
        Path(__file__).with_name("member1_ortools_static_validation.py"),
        Path(__file__).with_name("member1_path_foundation.py"),
        Path(__file__).with_name("member1_s0_graph.py"),
        Path(__file__).with_name("member1_s0_runner.py"),
        Path(__file__).with_name("member1_s1_runner.py"),
        Path(__file__).with_name("member1_static_runner.py"),
        Path(__file__).with_name("member1_static_validation.py"),
        Path(__file__).with_name("member1_decision_state_adapter.py"),
        Path(__file__).with_name("member1_mapping_audit.py"),
        Path(__file__).with_name("member1_trusted_receipt.json"),
        Path(__file__).resolve().parents[1] / "solver" / "member1_static_cp_sat.py",
        Path(__file__).resolve().parents[1] / "matrix" / "matrix_builder.py",
        Path(__file__).resolve().parents[1] / "models" / "candidate_path.py",
        Path(__file__).resolve().parents[1] / "models" / "matrix_bundle.py",
        Path(__file__).resolve().parents[1] / "models" / "decision_state.py",
        Path(__file__).resolve().parents[2] / "configs" / "member1_ortools_static.yaml",
    ]
    relative_names = [str(path.relative_to(Path(__file__).resolve().parents[2])).replace("\\", "/")
                      for path in code_files]
    if len(relative_names) != len(set(relative_names)):
        raise ValueError("duplicate code-provenance path")
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "run_id": run_id, "scenario_id": scenario_id,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "interpreter": str(Path(sys.executable).resolve()),
            "optimized_mode": not __debug__,
        },
        "suite_id": state.suite_id, "source_run": state.source_run,
        "decision_epoch": state.decision_epoch,
        "gate": gate, "solution_status": solution.get("status"),
        "solution_schema_version": SCHEMA_VERSION,
        "solver_version": SOLVER_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "model_input_version": MODEL_INPUT_VERSION,
        "model_config_version": MODEL_CONFIG_VERSION,
        "objective_policy_version": OBJECTIVE_POLICY_VERSION,
        "scaling_version": SCALING_VERSION,
        "state_schema_version": state.schema_version,
        "snapshot_verified": snapshot_verified,
        "validation_valid": validation.get("valid"),
        "database_hashes_checked_by_this_runner": True,
        "sqlite_sidecar_guard": {
            "version": "m1-ortools-static-sqlite-sidecar-guard/1",
            "suffixes": list(SIDECAR_SUFFIXES),
            "before_open_absent": True, "after_close_absent": True,
        },
        "engine": solution.get("engine"),
        "engine_status": solution.get("search", {}).get("engine_status"),
        "phase_telemetry": solution.get("search", {}).get("phases"),
        "proposal_telemetry": solution.get("search", {}).get("proposal_runs"),
        "witness_score": solution.get("search", {}).get("witness_score"),
        "incumbent": solution.get("search", {}).get("incumbent"),
        "termination": solution.get("search", {}).get("termination"),
        "budget_scope": solution.get("search", {}).get("budget_scope"),
        "physical_domain": {
            "route_columns_generated": solution.get("search", {}).get(
                "route_columns_generated", 0
            ),
            "refinement_trace_sha256": hashlib.sha256(json.dumps(
                solution.get("search", {}).get("refinement_trace", []),
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")).hexdigest(),
            "selection_scope": "finite_authenticated_physical_route_columns",
        },
        "served_count": served_count, "order_count": len(state.orders),
        "model_pair_count": model_input.get("pair_count"),
        "model_required_pair_count": model_input.get("required_pair_count"),
        "finite_path_domain": True,
        "source_hashes_before": hashes, "source_hashes_after": after,
        "source_versions": solution.get("source_versions"),
        "model_config": config.to_dict(),
        "stage_elapsed_seconds": {
            "source_acceptance_before_open": source_acceptance_elapsed,
            "solve_total": solve_elapsed,
            "solve_reported_search_budget_scope": solution.get("search", {}).get(
                "elapsed_seconds"
            ),
            "validation": validation_elapsed,
            "payload_publication": payload_publication_elapsed,
            "runner_before_manifest": monotonic() - runner_started,
        },
        "model_input_cache": cached_model_input_identity,
        "code_provenance_scope": (
            "OR-Tools bridge/validator/runner and direct source, sidecar, receipt, "
            "state, matrix and independent-validation trust dependencies"
        ),
        "code_sha256": {
            relative: _sha(path) for relative, path in zip(relative_names, code_files)
        },
        "files": {
            name: {"bytes": (destination / name).stat().st_size,
                   "sha256": _sha(destination / name)}
            for name in documents
        },
        "integrated_solver_validated": witness_valid,
        "optimality_proven": False,
        "search_complete": False,
        "general_m1_validated": False,
        "e4_run": False,
        "production_calibration_configured": False,
        "s2_s4_event_replay_validated": False,
        "profiles_m1_validated": False,
        "s7_s8_m1_validated": False,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2,
                   allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return solution, validation, model_input, destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the pinned M1 OR-Tools static S1/S5/S6 bridge"
    )
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--scenario-id", required=True,
                        choices=sorted(SUPPORTED_SCENARIOS))
    parser.add_argument("--time-limit-seconds", type=float, default=240.0)
    parser.add_argument("--max-model-proposals", type=int, default=64)
    parser.add_argument("--max-road-states", type=int, default=250_000)
    parser.add_argument("--max-road-labels", type=int, default=500_000)
    parser.add_argument("--max-arrival-options", type=int, default=4)
    parser.add_argument("--model-input-path", type=Path)
    args = parser.parse_args()
    try:
        solution, validation, model_input, destination = run_member1_ortools_static(
            args.snapshot_root, args.output_root, scenario_id=args.scenario_id,
            config=M1OrToolsStaticConfig(
                time_limit_seconds=args.time_limit_seconds,
                max_model_proposals=args.max_model_proposals,
                max_road_states=args.max_road_states,
                max_road_labels=args.max_road_labels,
                max_arrival_options=args.max_arrival_options,
            ),
            model_input_path=args.model_input_path,
        )
        manifest = json.loads((destination / "manifest.json").read_bytes())
        print(json.dumps({
            "run_id": manifest["run_id"], "gate": manifest["gate"],
            "status": solution["status"], "served": solution["served_orders"],
            "unserved": solution["unserved_orders"],
            "validation_valid": validation["valid"],
            "model_input_gate": model_input["gate"],
            "engine_status": manifest["engine_status"],
            "output_dir": str(destination),
        }, ensure_ascii=False))
        return 0 if "_ORTOOLS_VALIDATED_" in manifest["gate"] else 2
    except S1SidecarError as error:
        print(json.dumps({"gate": f"{args.scenario_id}_ORTOOLS_BLOCKED",
                          "diagnostics": [error.to_diagnostic()]}, ensure_ascii=False))
        return 2
    except ModelInputContractError as error:
        print(json.dumps({"gate": f"{args.scenario_id}_ORTOOLS_BLOCKED",
                          "diagnostics": [error.diagnostic]}, ensure_ascii=False))
        return 2
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({"gate": f"{args.scenario_id}_ORTOOLS_BLOCKED",
                          "diagnostic": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MANIFEST_VERSION", "run_member1_ortools_static"]
