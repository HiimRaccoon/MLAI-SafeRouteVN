"""Trusted acceptance boundary for Step-4 replay of frozen M1 witnesses.

This module authenticates an already-produced plan.  It never runs a solver or
road search.  The hard-coded manifest digests are local trust anchors reviewed
in the preceding checkpoints; they are not digital signatures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from optimization.integration.member1_decision_state_adapter import load_pinned_initial_states
from optimization.integration.member1_ortools_static_validation import validate_ortools_static_solution
from optimization.integration.member1_profile_validation import validate_profile_solution
from optimization.integration.member1_s0_graph import Member1RoadGraph
from optimization.integration.member1_s1_runner import _require_no_sqlite_sidecars
from optimization.integration.member1_static_runner import _verified_source
from optimization.models.decision_state import DecisionState

from .member1_motion_replay import (
    ACCEPTANCE_SCHEMA_VERSION, LEADER_POLICY_ID, LEADER_POLICY_SHA256,
    canonical_sha256,
)


TRUSTED_ACCEPTANCE_VERSION = "task02-m1-motion-trusted-plan-registry/2"


@dataclass(frozen=True, slots=True)
class FrozenPlan:
    scenario_id: str
    run_id: str
    relative_directory: str
    manifest_sha256: str
    solution_file: str
    validation_file: str
    selected_profile: str | None
    kind: str
    domain_file: str | None = None


FROZEN_PLANS: dict[str, FrozenPlan] = {
    "S1": FrozenPlan(
        "S1", "S1_PROFILES_20261002T084936626621Z",
        "outputs/member1_profiles_step3_closure_checks/S1_PROFILES_20261002T084936626621Z",
        "0bd0f299fc38fc63ce663b3148def90df723fe28d0d65d1f9ea95f0b347fdd9e",
        "balanced_solution.json", "balanced_validation.json", "BALANCED", "PROFILE",
        "shared_domain.json",
    ),
    "S7": FrozenPlan(
        "S7", "S7_PROFILES_20261002T084937293047Z",
        "outputs/member1_profiles_step3_closure_checks/S7_PROFILES_20261002T084937293047Z",
        "8a9c44689c064e15b6a1969f8f4f7789cbfdabab8c61d56be487913c5304f6ad",
        "safer_solution.json", "safer_validation.json", "SAFER", "PROFILE",
        "shared_domain.json",
    ),
    "S5": FrozenPlan(
        "S5", "S5_ORTOOLS_STATIC_20261001T135424239846Z",
        "outputs/member1_ortools_static_validator_closure/S5_ORTOOLS_STATIC_20261001T135424239846Z",
        "e48a8e461a6fb4968ca6aac27eea9f397bf42771dbd64d390cd35f8a257e5c56",
        "solution.json", "validation.json", None, "STATIC",
    ),
    "S6": FrozenPlan(
        "S6", "S6_ORTOOLS_STATIC_20261001T135518854989Z",
        "outputs/member1_ortools_static_validator_closure/S6_ORTOOLS_STATIC_20261001T135518854989Z",
        "52421d35ee4ffa9986e1039d1de5a75836b75b15efec9c5044a7cbb29d2956b4",
        "solution.json", "validation.json", None, "STATIC",
    ),
}


class AcceptanceError(ValueError):
    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code, self.path = code, path

    def diagnostic(self) -> dict[str, str]:
        return {"severity": "ERROR", "code": self.code, "path": self.path,
                "message": str(self)}


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError("ACCEPTED_ARTIFACT_INVALID", str(path), str(error)) from error
    if not isinstance(value, dict):
        raise AcceptanceError("ACCEPTED_ARTIFACT_INVALID", str(path), "JSON object required")
    return value


def _verify_child(manifest: Mapping[str, Any], directory: Path, name: str) -> Path:
    record = manifest.get("files", {}).get(name) if isinstance(manifest.get("files"), Mapping) else None
    path = directory / name
    if (not isinstance(record, Mapping) or type(record.get("bytes")) is not int
            or not isinstance(record.get("sha256"), str) or not path.is_file()
            or path.stat().st_size != record["bytes"] or _sha(path) != record["sha256"]):
        raise AcceptanceError("ACCEPTED_ARTIFACT_HASH", str(path),
                              "child bytes differ from pinned manifest")
    return path


def load_frozen_plan(repo_root: str | Path, snapshot_root: str | Path,
                     scenario_id: str) -> tuple[DecisionState, dict[str, Any],
                                                 dict[str, Any], dict[str, Path],
                                                 dict[str, str]]:
    """Load, source-bind, and independently revalidate a frozen witness."""

    pin = FROZEN_PLANS.get(scenario_id)
    if pin is None:
        raise AcceptanceError("PRE_EVENT_PLAN_REQUIRED", "scenario_id",
                              "Step 4 has no accepted pre-event plan for this scenario")
    repo = Path(repo_root).resolve()
    directory = repo / pin.relative_directory
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file() or _sha(manifest_path) != pin.manifest_sha256:
        raise AcceptanceError("TRUSTED_MANIFEST_MISMATCH", str(manifest_path),
                              "manifest differs from the reviewed checkpoint pin")
    manifest = _read_json(manifest_path)
    if manifest.get("run_id") != pin.run_id or manifest.get("scenario_id") != scenario_id:
        raise AcceptanceError("TRUSTED_MANIFEST_BINDING", str(manifest_path),
                              "run/scenario identity differs from trust registry")
    solution_path = _verify_child(manifest, directory, pin.solution_file)
    validation_path = _verify_child(manifest, directory, pin.validation_file)
    plan, historical_validation = _read_json(solution_path), _read_json(validation_path)
    if historical_validation.get("valid") is not True:
        raise AcceptanceError("HISTORICAL_VALIDATION", str(validation_path),
                              "historical validation did not pass")

    batch = load_pinned_initial_states(Path(snapshot_root).resolve())
    if (batch.get("status") != "INITIAL_STATE_READY"
            or batch.get("source_gate") != "M1_SOURCE_CONTRACT_GATE_PASS"):
        raise AcceptanceError("SOURCE_GATE", "snapshot_root", str(batch.get("diagnostics")))
    state_raw = next((item for item in batch.get("states", ())
                      if item.get("scenario_id") == scenario_id), None)
    if state_raw is None:
        raise AcceptanceError("SOURCE_STATE_MISSING", "states", scenario_id)
    state = DecisionState.from_dict(state_raw)
    paths, hashes = _verified_source(Path(snapshot_root).resolve(), state)
    _require_no_sqlite_sidecars(paths, phase="before_open")
    try:
        with Member1RoadGraph(
            paths["network_sqlite_sha256"], paths["features_sqlite_sha256"],
            routing_version=state.routing_version, features_version=state.features_version,
            context_version=state.context_version, source_hashes=hashes,
        ) as graph:
            if pin.kind == "PROFILE":
                if pin.domain_file is None:
                    raise AcceptanceError("TRUST_REGISTRY_INVALID", "domain_file",
                                          "profile trust record needs a domain artifact")
                domain_path = _verify_child(manifest, directory, pin.domain_file)
                domain = _read_json(domain_path)
                config = _read_json(repo / "configs/member1_profiles_step3.json")
                current = validate_profile_solution(plan, state, graph, domain, config)
            else:
                current = validate_ortools_static_solution(plan, state, graph)
    finally:
        _require_no_sqlite_sidecars(paths, phase="after_close")
    if current.get("valid") is not True:
        raise AcceptanceError("CURRENT_REVALIDATION_FAILED", pin.solution_file,
                              str(current.get("diagnostics")))
    after = {key: _sha(path) for key, path in paths.items()}
    if after != hashes:
        raise AcceptanceError("SOURCE_CHANGED", "snapshot_root", "source bytes changed")

    now = datetime.now(timezone.utc).isoformat()
    state_sha = canonical_sha256(state.to_dict())
    acceptance: dict[str, Any] = {
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "trust_registry_version": TRUSTED_ACCEPTANCE_VERSION,
        "acceptance_id": f"accept-{scenario_id.lower()}-{pin.run_id.lower()}",
        "acceptance_type": "SYNTHETIC_LEADER_ACCEPTANCE_REVALIDATED",
        "acceptance_sha256": None,
        "accepted_at": state.decision_epoch,
        "record_created_at": now,
        "execution_start": state.decision_epoch,
        "leader_policy_id": LEADER_POLICY_ID,
        "leader_policy_sha256": LEADER_POLICY_SHA256,
        "scenario_id": scenario_id,
        "run_id": pin.run_id,
        "solution_id": plan.get("solution_id"),
        "selected_profile": pin.selected_profile,
        "initial_state_sha256": state_sha,
        "initial_state_schema_version": state.schema_version,
        "plan_sha256": _sha(solution_path),
        "plan_canonical_sha256": canonical_sha256(plan),
        "manifest_sha256": pin.manifest_sha256,
        "validation_sha256": _sha(validation_path),
        "fixture_raw_sha256": state.fixture_raw_sha256,
        "receipt_sha256": state.receipt_sha256,
        "routing_version": state.routing_version,
        "features_version": state.features_version,
        "context_version": state.context_version,
        "historical_validator_version": historical_validation.get("validator_version"),
        "current_validator_version": current.get("validator_version"),
        "source_hashes": hashes,
        "source_mode": "PINNED_M1_READ_ONLY",
        "solver_rerun": False,
    }
    acceptance["acceptance_sha256"] = canonical_sha256(
        {key: value for key, value in acceptance.items() if key != "record_created_at"})
    return state, plan, acceptance, paths, hashes


__all__ = [
    "AcceptanceError", "FROZEN_PLANS", "FrozenPlan", "TRUSTED_ACCEPTANCE_VERSION",
    "load_frozen_plan",
]
