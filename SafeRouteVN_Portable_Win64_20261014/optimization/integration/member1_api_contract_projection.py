"""Generate transport-neutral API v1 examples from pinned M1 artifacts.

No solver is run. This script refuses unverified M1 source and altered frozen
solution/validation bytes. Its outputs are examples, not new solver runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from optimization.integration.member1_decision_state_adapter import (
    apply_pending_event, load_pinned_initial_states,
)
from optimization.models.decision_state import DecisionState
from shared.contracts.task02_api_v1 import validate_bound_job, validate_request


ROOT = Path(__file__).resolve().parents[2]
S0_RUN = "S0_20260929T174046567846Z"
S1_RUN = "S1_20260930T043601841406Z"
STATE_RUN = "M1_STATE_20260929T205037621762Z"
TRUSTED_MANIFEST_SHA256 = {
    "S0": "d7a259215003272fde0b3b0a778998a3a2c46088b7f2469860f43aa9ff01e55b",
    "S1": "4ba0e8bdb419164f5df8d43148e8e75a04d4d22956ad9b36698168f48c169357",
    "GATE1": "ecea384ab8867d6465c7248cc5a70578050d4f2253fef2c3d8be5b3852926b31",
}
UNITS = {"mass": "kg", "distance": "meter", "duration": "second", "cost_rate": "VND/km",
         "cost": "VND", "geometry_crs": "WGS84", "geometry_order": "longitude_latitude",
         "exposure_semantic": "relative_exposure_proxy"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_run(directory: Path, expected_gate: str,
              trusted_manifest_sha256: str | None = None
              ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest_path = directory / "manifest.json"
    if trusted_manifest_sha256 is None:
        trusted_manifest_sha256 = TRUSTED_MANIFEST_SHA256[
            "S0" if expected_gate == "PASS" else "S1"
        ]
    if _sha(manifest_path) != trusted_manifest_sha256:
        raise ValueError(f"trusted manifest digest mismatch: {manifest_path}")
    manifest = json.loads(manifest_path.read_bytes())
    solution = json.loads((directory / "solution.json").read_bytes())
    validation = json.loads((directory / "validation.json").read_bytes())
    gate = manifest.get("s0_gate", manifest.get("gate"))
    if (gate != expected_gate or not manifest.get("snapshot_verified")
        or not manifest.get("validation_valid") or not validation.get("valid")
        or solution.get("status") != "FEASIBLE" or solution.get("unserved_orders")
        or manifest.get("run_id") != directory.name):
        raise ValueError(f"frozen run gate mismatch: {directory}")
    for name in ("solution.json", "validation.json"):
        item = manifest["files"][name]
        path = directory / name
        if item["sha256"] != _sha(path) or item["bytes"] != path.stat().st_size:
            raise ValueError(f"frozen run artifact bytes mismatch: {path}")
    return solution, validation, manifest


def _state_summary(state: DecisionState) -> dict[str, Any]:
    return {"state_id": f"m1/{state.suite_id}/{state.scenario_id}/initial-v{state.state_version}",
            "scenario_id": state.scenario_id, "state_version": state.state_version,
            "context_version": state.context_version, "decision_epoch": state.decision_epoch,
            "order_ids": [order.order_id for order in state.orders],
            "pending_event_ids": [event.event_id for event in state.pending_events],
            "pending_event_epochs": {event.event_id: event.timestamp
                                     for event in state.pending_events},
            "suite_id": state.suite_id, "source_run": state.source_run,
            "fixture_sha256": state.fixture_raw_sha256,
            "catalog_sha256": state.catalog_raw_sha256,
            "receipt_sha256": state.receipt_sha256,
            "routing_version": state.routing_version, "features_version": state.features_version,
            "delivery_area_version": state.delivery_area_version}


def _source(state: DecisionState) -> dict[str, Any]:
    summary = _state_summary(state)
    keys = ("suite_id", "source_run", "fixture_sha256", "catalog_sha256", "receipt_sha256",
            "routing_version", "features_version", "delivery_area_version")
    return {**{key: summary[key] for key in keys}, "source_gate": "M1_SOURCE_CONTRACT_GATE_PASS"}


def _request(state: DecisionState, event_id: str | None = None) -> dict[str, Any]:
    summary = _state_summary(state)
    epoch = summary["decision_epoch"] if event_id is None else summary["pending_event_epochs"][event_id]
    return {"schema_version": "task02-m2-m3m4-request/1", "kind": "DECISION_REQUEST",
            "request_id": f"example-{state.scenario_id.lower()}" + (f"-{event_id.lower()}" if event_id else "-initial"),
            "scenario_id": state.scenario_id,
            "state_ref": {"kind": "SERVER_MANAGED", "state_id": summary["state_id"]},
            "decision_epoch": epoch, "expected_state_version": state.state_version,
            "expected_context_version": state.context_version, "event_id": event_id}


def _trusted_job_record(job: dict[str, Any]) -> dict[str, Any]:
    decision = job["decision"]
    return {"request_id": job["request_id"], "run_id": job["run_id"],
            "validation_gate": decision["validation"]["gate"],
            "derived_from": decision["derived_from"]}


def _plan(solution: dict[str, Any]) -> dict[str, Any]:
    routes = []
    for source_route in solution["vehicle_routes"]:
        routes.append({"vehicle_id": source_route["vehicle_id"],
                       "order_sequence": source_route["order_sequence"],
                       "node_sequence": source_route["node_sequence"],
                       "start_node": source_route["start_node"], "end_node": source_route["end_node"],
                       "load_after_depot_pickup_kg": source_route["load_after_depot_pickup_kg"],
                       "distance_m": source_route["total_distance_m"],
                       "travel_time_s": source_route["total_travel_time_s"],
                       "exposure_proxy": source_route["total_exposure"],
                       "cost_vnd": source_route.get("total_cost_vnd"),
                       "stops": [{"order_id": stop["order_id"], "node_id": stop["node_id"],
                                  "arrival_s": stop["arrival_s"], "completion_s": stop["completion_s"],
                                  "load_after_delivery_kg": stop["load_after_delivery_kg"]}
                                 for stop in source_route["stops"]],
                       "legs": [{"path_id": leg["path_id"], "from_node": leg["from_node"],
                                 "to_node": leg["to_node"], "edge_ids": leg["edge_ids"],
                                 "geometry": {"type": "LineString", "coordinates": leg["geometry"]},
                                 "distance_m": leg["distance_m"], "travel_time_s": leg["travel_time_s"],
                                 "exposure_proxy": leg["exposure"]}
                                for leg in source_route["legs"]]})
    metrics = solution["metrics"]
    cost = metrics.get("total_cost_vnd")
    return {"vehicle_routes": routes,
            "metrics": {"total_distance_m": metrics["total_distance_m"],
                        "total_travel_time_s": metrics["total_travel_time_s"],
                        "total_exposure_proxy": metrics["total_exposure"],
                        "total_cost_vnd": cost, "cost_available": cost is not None}}


def _search(solution: dict[str, Any]) -> dict[str, Any]:
    raw = solution["search"]
    return {key: raw[key] for key in ("optimality_proven", "search_complete", "truncated",
                                       "truncation_reasons", "elapsed_seconds", "limits")}


def _witness_job(state: DecisionState, directory: Path, gate: str) -> dict[str, Any]:
    solution, validation, manifest = _read_run(
        directory, gate, TRUSTED_MANIFEST_SHA256[state.scenario_id]
    )
    expected_hashes = manifest.get("source_hashes_before", manifest.get("source_hashes"))
    if (solution["scenario_id"] != state.scenario_id
        or solution["source_versions"]["state_version"] != state.state_version
        or solution["source_versions"]["context_version"] != state.context_version
        or expected_hashes[f"fixture_{state.scenario_id.lower()}_sha256"] != state.fixture_raw_sha256
        or expected_hashes["catalog_sha256"] != state.catalog_raw_sha256):
        raise ValueError("frozen run/state provenance mismatch")
    if state.scenario_id == "S1" and (expected_hashes != manifest["source_hashes_after"]
                                      or expected_hashes["receipt_sha256"] != state.receipt_sha256
                                      or manifest["validator_version"] != "member1-s1-independent-validator/2"):
        raise ValueError("S1 source/validator version mismatch")
    if not (solution["search"]["optimality_proven"] is False
            and solution["search"]["search_complete"] is False):
        raise ValueError("bounded solver falsely claims proof")
    decision = {"schema_version": "task02-m2-m3m4-decision/1", "scenario_id": state.scenario_id,
                "run_id": directory.name, "status": "FEASIBLE", "state_version": state.state_version,
                "context_version": state.context_version, "decision_epoch": state.decision_epoch,
                "event_id": None, "order_ids": [o.order_id for o in state.orders],
                "served_orders": solution["served_orders"], "unserved_orders": [],
                "coverage_evaluated": True, "post_event_state": None, "plan": _plan(solution),
                "validation": {"status": "PASSED", "valid": True,
                               "validator_version": validation["validator_version"],
                               "gate": "S0_GATE_PASS" if state.scenario_id == "S0" else gate},
                "search": _search(solution), "diagnostics": [], "units": UNITS.copy(),
                "source": _source(state),
                "derived_from": {"kind": "SOLVER_WITNESS_PROJECTION", "run_id": directory.name,
                                 "artifact": str((directory / "solution.json").relative_to(ROOT)).replace("\\", "/"),
                                 "artifact_sha256": _sha(directory / "solution.json"),
                                 "manifest_sha256": _sha(directory / "manifest.json")}}
    return {"schema_version": "task02-m2-m3m4-job/1", "kind": "DECISION_JOB",
            "request_id": _request(state)["request_id"], "run_id": directory.name,
            "job_status": "COMPLETED", "decision": decision, "failure": None}


def _unsupported_job(state: DecisionState, adapter_run: Path) -> dict[str, Any]:
    event = state.pending_events[0]
    rejection = apply_pending_event(state, event.event_id)
    if (rejection["post_event_state"] is not None
        or rejection["status"] not in {"UNSUPPORTED_POLICY", "UNSUPPORTED_FEATURES"}):
        raise ValueError("pending event was unexpectedly applied")
    adapter_artifact = adapter_run / "event_rejections.json"
    adapter_manifest_path = adapter_run / "manifest.json"
    if _sha(adapter_manifest_path) != TRUSTED_MANIFEST_SHA256["GATE1"]:
        raise ValueError(f"trusted manifest digest mismatch: {adapter_manifest_path}")
    adapter_manifest = json.loads(adapter_manifest_path.read_bytes())
    if (_sha(adapter_artifact) != adapter_manifest["files"]["event_rejections.json"]["sha256"]
        or adapter_artifact.stat().st_size != adapter_manifest["files"]["event_rejections.json"]["bytes"]
        or adapter_manifest["fixture_raw_sha256"][state.scenario_id] != state.fixture_raw_sha256):
        raise ValueError("adapter rejection provenance mismatch")
    recorded_payload = json.loads(adapter_artifact.read_bytes())
    recorded = next((item for item in recorded_payload.get("rejections", [])
                     if item.get("scenario_id") == state.scenario_id
                     and item.get("event_id") == event.event_id), None)
    if recorded is None:
        raise ValueError("adapter rejection event is absent from the trusted artifact")
    recorded_diagnostic = recorded.get("diagnostics", [{}])[0]
    computed_diagnostic = rejection["diagnostics"][0]
    if (recorded.get("status") != rejection["status"]
        or recorded.get("post_event_state") is not None
        or any(recorded_diagnostic.get(key) != computed_diagnostic.get(key)
               for key in ("code", "path", "message", "severity"))):
        raise ValueError("adapter rejection differs from trusted receipt-gated evidence")
    code = rejection["diagnostics"][0]["code"]
    decision = {"schema_version": "task02-m2-m3m4-decision/1", "scenario_id": state.scenario_id,
                "run_id": f"CONTRACT_EXAMPLE_{state.scenario_id}_{event.event_id}",
                "status": "UNSUPPORTED", "state_version": state.state_version,
                "context_version": state.context_version, "decision_epoch": event.timestamp,
                "event_id": event.event_id, "order_ids": [o.order_id for o in state.orders],
                "served_orders": [], "unserved_orders": [], "coverage_evaluated": False,
                "post_event_state": None, "plan": None,
                "validation": {"status": "NOT_RUN", "valid": None, "validator_version": None, "gate": None},
                "search": None,
                "diagnostics": [{"severity": "ERROR", "code": code,
                                 "path": rejection["diagnostics"][0]["path"],
                                 "message": rejection["diagnostics"][0]["message"]}],
                "units": UNITS.copy(), "source": _source(state),
                "derived_from": {"kind": "ADAPTER_REJECTION_PROJECTION", "run_id": adapter_run.name,
                                 "artifact": str(adapter_artifact.relative_to(ROOT)).replace("\\", "/"),
                                 "artifact_sha256": _sha(adapter_artifact),
                                 "manifest_sha256": _sha(adapter_run / "manifest.json")}}
    return {"schema_version": "task02-m2-m3m4-job/1", "kind": "DECISION_JOB",
            "request_id": _request(state, event.event_id)["request_id"],
            "run_id": decision["run_id"], "job_status": "COMPLETED",
            "decision": decision, "failure": None}


def build_examples(snapshot_root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    batch = load_pinned_initial_states(snapshot_root)
    if batch.get("status") != "INITIAL_STATE_READY" or batch.get("source_gate") != "M1_SOURCE_CONTRACT_GATE_PASS":
        raise ValueError(f"pinned M1 source gate failed: {batch.get('diagnostics')}")
    states = {item["scenario_id"]: DecisionState.from_dict(item) for item in batch["states"]}
    if set(states) != {f"S{n}" for n in range(9)}:
        raise ValueError("nine pinned initial states are required")
    if any(state.source_authentication != "PINNED_RECEIPT_VERIFIED" for state in states.values()):
        raise ValueError("source authentication was not established by the pinned loader")
    adapter_run = ROOT / "outputs/member1_decision_state" / STATE_RUN
    examples: dict[str, dict[str, Any]] = {}
    for scenario in ("S0", "S1", "S2", "S3", "S4"):
        state = states[scenario]
        event_id = state.pending_events[0].event_id if state.pending_events else None
        examples[f"{scenario.lower()}_request.json"] = _request(state, event_id)
        examples[f"{scenario.lower()}_job.json"] = (
            _witness_job(state, ROOT / "outputs" / f"member1_{scenario.lower()}" /
                         (S0_RUN if scenario == "S0" else S1_RUN),
                         "PASS" if scenario == "S0" else "S1_VALIDATED_FULL_8_OF_8")
            if scenario in {"S0", "S1"} else _unsupported_job(state, adapter_run))
        summary = _state_summary(state)
        request = examples[f"{scenario.lower()}_request.json"]
        job = examples[f"{scenario.lower()}_job.json"]
        issues = validate_request(request, summary)
        issues += validate_bound_job(job, request, summary, _trusted_job_record(job))
        if issues:
            raise ValueError(f"{scenario} projection violates API contract: {issues[:3]}")
    summaries = {name: _state_summary(state) for name, state in states.items() if name in {"S0", "S1", "S2", "S3", "S4"}}
    evidence = {"source_gate": batch["source_gate"], "states_checked": 9,
                "receipt_sha256": batch["receipt_sha256"],
                "catalog_raw_sha256": batch["catalog_raw_sha256"],
                "fixture_raw_sha256": batch["fixture_raw_sha256"]}
    return examples, summaries, evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Project pinned M1 evidence into API v1 examples; no solver")
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        examples, summaries, evidence = build_examples(args.snapshot_root)
        output = args.output_root.resolve()
        output.mkdir(parents=True, exist_ok=False)
        for name, payload in {**examples, "resolved_state_summaries.json": summaries}.items():
            (output / name).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                  indent=2, allow_nan=False) + "\n", encoding="utf-8")
        manifest = {"schema_version": "task02-m2-m3m4-example-manifest/1", **evidence,
                    "source_runs": {"S0": S0_RUN, "S1": S1_RUN, "adapter": STATE_RUN},
                    "trusted_manifest_sha256": TRUSTED_MANIFEST_SHA256,
                    "gates": {"S0_GATE_PASS": True, "S1_VALIDATED_FULL_8_OF_8": True,
                              "GENERAL_M1_NOT_VALIDATED": True, "E4_NOT_RUN": True,
                              "PRODUCTION_CALIBRATION_UNCONFIGURED": True,
                              "S2_S8_SOLVER_NOT_VALIDATED": True},
                    "code_sha256": {"projection": _sha(Path(__file__)),
                                    "schema": _sha(ROOT / "shared/contracts/task02_api_v1.schema.json"),
                                    "semantic_checker": _sha(ROOT / "shared/contracts/task02_api_v1.py")},
                    "files": {path.name: {"bytes": path.stat().st_size, "sha256": _sha(path)}
                              for path in sorted(output.glob("*.json"))}}
        (output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8")
        print(json.dumps({"status": "API_V1_EXAMPLES_PROJECTED", "files": len(examples) + 2,
                          "output_dir": str(output)}, ensure_ascii=False))
        return 0
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({"status": "API_V1_PROJECTION_BLOCKED", "diagnostic": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
