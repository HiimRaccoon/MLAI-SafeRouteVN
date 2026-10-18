"""Windows/source-backed Step-4 replay runner (never invokes a solver)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
from typing import Any, Mapping

from optimization.integration.member1_s0_graph import Member1RoadGraph
from optimization.integration.member1_s1_runner import _require_no_sqlite_sidecars
from optimization.models.motion_state import MotionState

from .member1_motion_acceptance import AcceptanceError, load_frozen_plan
from .member1_motion_replay import MotionReplayError, replay_motion, validate_acceptance_binding
from .member1_motion_validation import MOTION_VALIDATOR_VERSION, validate_motion_state


RUN_MANIFEST_VERSION = "task02-m1-motion-replay-run-manifest/2"
RUNNER_VERSION = "task02-m1-motion-replay-runner/2"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MotionReplayError("INVALID_DATA", "REPLAY_INPUT", str(path), str(error)) from error


def _acceptance_equivalent(supplied: Mapping[str, Any], trusted: Mapping[str, Any]) -> bool:
    ignored = {"record_created_at", "acceptance_sha256"}
    return all(supplied.get(key) == value for key, value in trusted.items() if key not in ignored)


def run_motion_replay(repo_root: str | Path, snapshot_root: str | Path,
                      output_root: str | Path, *, scenario_id: str,
                      target_times: list[str], accepted_plan_receipt: str | Path | None = None,
                      parent_state: str | Path | None = None,
                      parent_ancestors: list[str | Path] | None = None) -> tuple[dict[str, Any], Path]:
    repo = Path(repo_root).resolve()
    state, plan, trusted, paths, source_before = load_frozen_plan(repo, snapshot_root, scenario_id)
    acceptance = trusted
    if accepted_plan_receipt is not None:
        supplied = _json(Path(accepted_plan_receipt).resolve())
        if not isinstance(supplied, Mapping) or not _acceptance_equivalent(supplied, trusted):
            raise MotionReplayError("INVALID_DATA", "ACCEPTANCE_BINDING",
                                    "accepted_plan_receipt", "receipt differs from trusted artifacts")
        acceptance = dict(supplied)
    validate_acceptance_binding(state, plan, acceptance)
    parent = MotionState.from_dict(_json(Path(parent_state).resolve())) if parent_state else None
    ancestors = [MotionState.from_dict(_json(Path(item).resolve()))
                 for item in (parent_ancestors or [])]
    if ancestors and parent is None:
        raise MotionReplayError("INVALID_DATA", "TRUSTED_PARENT_REQUIRED", "parent_state",
                                "ancestors require a parent state")
    _require_no_sqlite_sidecars(paths, phase="before_open")
    try:
        with Member1RoadGraph(
            paths["network_sqlite_sha256"], paths["features_sqlite_sha256"],
            routing_version=state.routing_version, features_version=state.features_version,
            context_version=state.context_version, source_hashes=source_before,
        ) as graph:
            states, validations = [], []
            for target in target_times:
                candidate = replay_motion(state, plan, acceptance, graph, target,
                                          parent=parent, parent_chain=ancestors)
                validation = validate_motion_state(state, plan, acceptance, graph, candidate,
                                                   trusted_parent=parent,
                                                   trusted_parent_chain=ancestors)
                states.append(candidate); validations.append(validation)
                if validation.get("valid") is not True:
                    raise MotionReplayError("INVALID_DATA", "REPLAY_VALIDATION", target,
                                            str(validation.get("diagnostics")))
    finally:
        _require_no_sqlite_sidecars(paths, phase="after_close")
    source_after = {key: _sha(path) for key, path in paths.items()}
    if source_after != source_before:
        raise MotionReplayError("INVALID_DATA", "SOURCE_CHANGED", "snapshot_root",
                                "pinned source bytes changed during replay")

    run_id = f"{scenario_id}_MOTION_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = Path(output_root).resolve() / run_id
    directory.mkdir(parents=True, exist_ok=False)
    payloads = {
        "accepted_plan_receipt.json": acceptance,
        "motion_states.json": {"run_id": run_id, "scenario_id": scenario_id, "states": states},
        "validations.json": {"run_id": run_id, "scenario_id": scenario_id,
                             "validator_version": MOTION_VALIDATOR_VERSION,
                             "validations": validations},
    }
    records: dict[str, dict[str, Any]] = {}
    for name, value in payloads.items():
        path = directory / name
        data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                           allow_nan=False) + "\n").encode("utf-8")
        path.write_bytes(data)
        records[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    code_paths = [
        "optimization/models/motion_state.py",
        "optimization/rolling_horizon/member1_motion_acceptance.py",
        "optimization/rolling_horizon/member1_motion_replay.py",
        "optimization/rolling_horizon/member1_motion_validation.py",
        "optimization/rolling_horizon/member1_motion_runner.py",
        "optimization/integration/member1_s0_graph.py",
        "optimization/integration/member1_s1_runner.py",
        "optimization/integration/member1_static_runner.py",
        "optimization/integration/member1_decision_state_adapter.py",
        "optimization/models/decision_state.py",
        "optimization/models/common.py",
        "optimization/integration/member1_mapping_audit.py",
        "optimization/integration/member1_profile_validation.py",
        "optimization/integration/member1_ortools_static_validation.py",
        "optimization/integration/member1_profile_checkpoint.py",
    ]
    manifest = {
        "schema_version": RUN_MANIFEST_VERSION, "runner_version": RUNNER_VERSION,
        "run_id": run_id, "scenario_id": scenario_id,
        "gate": "M1_MOTION_REPLAY_VALIDATED", "snapshot_verified": True,
        "solver_rerun": False, "road_search_run": False,
        "accepted_plan_run_id": acceptance["run_id"],
        "accepted_plan_acceptance_sha256": acceptance["acceptance_sha256"],
        "accepted_plan_status": plan.get("status"),
        "accepted_plan_solution_id": plan.get("solution_id"),
        "accepted_plan_profile": acceptance.get("selected_profile"),
        "accepted_plan_served_orders": plan.get("served_orders"),
        "accepted_plan_unserved_orders": plan.get("unserved_orders"),
        "initial_state_sha256": acceptance.get("initial_state_sha256"),
        "historical_validator_version": acceptance.get("historical_validator_version"),
        "current_validator_version": acceptance.get("current_validator_version"),
        "target_times": target_times, "state_count": len(states),
        "all_valid": all(item["valid"] is True for item in validations),
        "source_hashes_before": source_before, "source_hashes_after": source_after,
        "code_sha256": {item: _sha(repo / item) for item in code_paths},
        "current_acceptance_config_sha256": _sha(repo / "configs/member1_profiles_step3.json"),
        "current_trusted_receipt_sha256": _sha(repo / "optimization/integration/member1_trusted_receipt.json"),
        "historical_solver_execution": "FROZEN_IN_ACCEPTED_PLAN_MANIFEST; not rerun",
        "files": records,
        "platform": {"system": platform.system(), "python": platform.python_version(),
                     "interpreter": sys.executable, "optimized_mode": not __debug__},
        "scope": {"event_application": False, "dynamic_solver": False,
                  "rain_overlay": False, "e4_run": False,
                  "general_m1_validated": False},
    }
    manifest_path = directory / "manifest.json"
    data = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")
    manifest_path.write_bytes(data)
    return manifest, directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-root", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--scenario-id", required=True, choices=("S1", "S5", "S6", "S7"))
    parser.add_argument("--target-time", required=True, action="append")
    parser.add_argument("--accepted-plan-receipt")
    parser.add_argument("--parent-state")
    parser.add_argument("--parent-ancestor", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        manifest, directory = run_motion_replay(
            args.repo_root, args.snapshot_root, args.output_root,
            scenario_id=args.scenario_id, target_times=args.target_time,
            accepted_plan_receipt=args.accepted_plan_receipt, parent_state=args.parent_state,
            parent_ancestors=args.parent_ancestor,
        )
        print(json.dumps({"status": "READY", "gate": manifest["gate"],
                          "run_id": manifest["run_id"], "output": str(directory)}))
        return 0
    except (AcceptanceError, MotionReplayError, ValueError, OSError) as error:
        diagnostic = error.diagnostic() if hasattr(error, "diagnostic") else {
            "severity": "ERROR", "code": "MOTION_RUNNER", "path": "$", "message": str(error)}
        print(json.dumps({"status": "FAIL", "diagnostic": diagnostic}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

