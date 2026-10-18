"""Portable v1 API vectors: no SQLite, solver, HTTP server or M1 path needed."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from shared.contracts.task02_api_v1 import (
    SCHEMA, validate_bound_job, validate_decision, validate_job, validate_request,
)


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "shared/contracts"
EXAMPLES = CONTRACT / "examples_v1"
VECTORS = json.loads((CONTRACT / "task02_api_v1_vectors.json").read_bytes())["vectors"]
SUMMARIES = json.loads((EXAMPLES / "resolved_state_summaries.json").read_bytes())


def example(name: str):
    return json.loads((EXAMPLES / name).read_bytes())


def pointer(root, path: str):
    parts = [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]
    current = root
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current, parts[-1]


def patched(vector):
    value = example(vector["base"])
    for operation in vector.get("patch", []):
        parent, key = pointer(value, operation["path"])
        if operation["op"] == "copy_from_example":
            source_parent, source_key = pointer(example(operation["from_example"]),
                                                operation["from_path"])
            replacement = deepcopy(source_parent[source_key])
        else:
            replacement = operation.get("value")
        if operation["op"] == "remove":
            if isinstance(parent, list):
                parent.pop(int(key))
            else:
                del parent[key]
        elif operation["op"] == "append":
            target = parent[int(key)] if isinstance(parent, list) else parent[key]
            target.append(replacement)
        else:
            if isinstance(parent, list):
                parent[int(key)] = replacement
            else:
                parent[key] = replacement
    return value


def trusted_job_record(job):
    decision = job.get("decision")
    return {
        "request_id": job["request_id"],
        "run_id": job["run_id"],
        "validation_gate": decision["validation"]["gate"] if decision else None,
        "derived_from": deepcopy(decision["derived_from"]) if decision else None,
    }


def test_machine_readable_schema_is_valid():
    Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("vector", VECTORS, ids=lambda item: item["id"])
def test_portable_contract_vector(vector):
    payload = patched(vector)
    scenario = payload.get("scenario_id") or payload["decision"]["scenario_id"]
    summary = SUMMARIES[scenario]
    if payload["kind"] == "DECISION_REQUEST":
        existing = example(vector["existing_example"]) if "existing_example" in vector else None
        issues = validate_request(payload, summary, existing)
    elif vector.get("validation_mode") == "BOUND":
        accepted = example(vector["accepted_request"])
        trusted_source = example(vector.get("trusted_job_example", vector["base"]))
        resolved = None if vector.get("resolved_state") == "NONE" else summary
        issues = validate_bound_job(payload, accepted, resolved, trusted_job_record(trusted_source))
    else:
        issues = validate_job(payload, summary)
    if vector.get("expected") == "PASS":
        assert issues == []
    else:
        assert (vector["expected_code"], vector["expected_path"]) in {
            (issue["code"], issue["path"]) for issue in issues}


def test_every_request_and_response_example_and_idempotent_retry():
    for scenario in ("s0", "s1", "s2", "s3", "s4"):
        summary = SUMMARIES[scenario.upper()]
        request = example(f"{scenario}_request.json")
        job = example(f"{scenario}_job.json")
        assert not validate_request(request, summary, request)
        assert not validate_job(job, summary)
        assert request["request_id"] == job["request_id"]
        assert job["decision"]["post_event_state"] is None


def test_transport_lifecycle_is_independent_of_business_status():
    lifecycle = example("lifecycle_jobs.json")
    for status in ("queued", "running", "failed"):
        assert not validate_job(lifecycle[status])
    invalid = deepcopy(lifecycle["failed"])
    invalid["decision"] = example("s1_job.json")["decision"]
    assert validate_job(invalid)


def test_projections_trace_to_unmodified_frozen_artifacts():
    for scenario in ("s0", "s1", "s2", "s3", "s4"):
        decision = example(f"{scenario}_job.json")["decision"]
        derived = decision["derived_from"]
        artifact = ROOT / derived["artifact"]
        assert artifact.is_file()
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == derived["artifact_sha256"]
        if scenario in ("s0", "s1"):
            manifest_path = artifact.with_name("manifest.json")
            manifest = json.loads(manifest_path.read_bytes())
            assert manifest["files"]["solution.json"]["sha256"] == derived["artifact_sha256"]
            assert manifest["snapshot_verified"] and manifest["validation_valid"]
            assert manifest.get("s0_gate", manifest.get("gate")) == (
                "PASS" if scenario == "s0" else "S1_VALIDATED_FULL_8_OF_8")
        else:
            manifest_path = artifact.with_name("manifest.json")
        assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == derived["manifest_sha256"]


def test_s0_cost_unavailable_and_s1_loads_are_source_exact():
    s0 = example("s0_job.json")["decision"]
    s1 = example("s1_job.json")["decision"]
    assert s0["plan"]["metrics"]["total_cost_vnd"] is None
    assert not s0["plan"]["metrics"]["cost_available"]
    loads = {route["vehicle_id"]: route["load_after_depot_pickup_kg"]
             for route in s1["plan"]["vehicle_routes"]}
    assert loads == pytest.approx({"V1": 12.008, "V2": 13.426})
    assert s1["search"]["optimality_proven"] is False
    assert s1["search"]["search_complete"] is False
    for scenario, code in (("s2", "DEPOT_RELOAD_POLICY_MISSING"),
                           ("s3", "CUSTODY_POLICY_MISSING"),
                           ("s4", "POST_RAIN_FEATURES_MISSING")):
        decision = example(f"{scenario}_job.json")["decision"]
        assert decision["status"] == "UNSUPPORTED"
        assert decision["plan"] is None and decision["post_event_state"] is None
        assert decision["diagnostics"][0]["code"] == code


def test_projection_preserves_selected_route_and_adapter_rejection_data():
    for scenario in ("s0", "s1"):
        decision = example(f"{scenario}_job.json")["decision"]
        source = json.loads((ROOT / decision["derived_from"]["artifact"]).read_bytes())
        assert decision["served_orders"] == source["served_orders"]
        assert "alternatives" not in decision
        assert len(decision["plan"]["vehicle_routes"]) == len(source["vehicle_routes"])
        for route, original in zip(decision["plan"]["vehicle_routes"], source["vehicle_routes"]):
            assert route["order_sequence"] == original["order_sequence"]
            assert route["node_sequence"] == original["node_sequence"]
            assert route["distance_m"] == original["total_distance_m"]
            assert route["travel_time_s"] == original["total_travel_time_s"]
            assert route["exposure_proxy"] == original["total_exposure"]
            for leg, source_leg in zip(route["legs"], original["legs"]):
                assert leg["edge_ids"] == source_leg["edge_ids"]
                assert leg["geometry"]["coordinates"] == source_leg["geometry"]
                assert leg["distance_m"] == source_leg["distance_m"]
                assert leg["travel_time_s"] == source_leg["travel_time_s"]
                assert leg["exposure_proxy"] == source_leg["exposure"]
    rejections = json.loads((ROOT / example("s2_job.json")["decision"]["derived_from"]["artifact"]).read_bytes())
    by_scenario = {item["scenario_id"]: item for item in rejections["rejections"]}
    for scenario in ("s2", "s3", "s4"):
        decision = example(f"{scenario}_job.json")["decision"]
        source_rejection = by_scenario[scenario.upper()]
        assert decision["event_id"] == source_rejection["event_id"]
        assert decision["diagnostics"][0]["code"] == source_rejection["diagnostics"][0]["code"]
        assert source_rejection["post_event_state"] is None


def test_handoff_manifest_hashes_match_every_bundled_file():
    manifest = json.loads((CONTRACT / "task02_api_v1_package_manifest.json").read_bytes())
    assert manifest["schema_version"] == "task02-m2-m3m4-package-manifest/1"
    assert manifest["evidence"]["S0_GATE_PASS"]
    assert manifest["evidence"]["S1_VALIDATED_FULL_8_OF_8"]
    assert manifest["evidence"]["S2_S8_SOLVER_NOT_VALIDATED"]
    for relative, expected in manifest["files"].items():
        path = (ROOT / relative).resolve()
        assert path.is_relative_to(ROOT) and path.is_file()
        assert path.stat().st_size == expected["bytes"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected["sha256"]


def test_projector_rejects_self_consistent_but_untrusted_s1_manifest(tmp_path):
    from optimization.integration.member1_api_contract_projection import _read_run

    source = ROOT / "outputs/member1_s1/S1_20260930T043601841406Z"
    copied = tmp_path / source.name
    shutil.copytree(source, copied)
    solution_path = copied / "solution.json"
    solution = json.loads(solution_path.read_bytes())
    solution["metrics"]["total_distance_m"] += 1
    solution_path.write_text(json.dumps(solution, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                             encoding="utf-8")
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"]["solution.json"] = {
        "bytes": solution_path.stat().st_size,
        "sha256": hashlib.sha256(solution_path.read_bytes()).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                             encoding="utf-8")
    with pytest.raises(ValueError, match="trusted manifest digest"):
        _read_run(copied, "S1_VALIDATED_FULL_8_OF_8")


def test_projector_rejects_self_consistent_but_untrusted_gate1_manifest(tmp_path):
    from optimization.integration.member1_api_contract_projection import _unsupported_job
    from optimization.models.decision_state import DecisionState

    source = ROOT / "outputs/member1_decision_state/M1_STATE_20260929T205037621762Z"
    copied = tmp_path / source.name
    shutil.copytree(source, copied)
    rejection_path = copied / "event_rejections.json"
    rejection_payload = json.loads(rejection_path.read_bytes())
    rejection_payload["rejections"][0]["diagnostics"][0]["message"] = "tampered"
    rejection_path.write_text(
        json.dumps(rejection_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"]["event_rejections.json"] = {
        "bytes": rejection_path.stat().st_size,
        "sha256": hashlib.sha256(rejection_path.read_bytes()).hexdigest(),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    initial_states = json.loads((source / "initial_states.json").read_bytes())
    state = DecisionState.from_dict(next(item for item in initial_states["states"]
                                         if item["scenario_id"] == "S2"))
    with pytest.raises(ValueError, match="trusted manifest digest"):
        _unsupported_job(state, copied)


@pytest.mark.parametrize("status", ["UNSUPPORTED", "INVALID_DATA", "SEARCH_LIMIT", "TIME_LIMIT"])
def test_no_witness_status_requires_actionable_error_diagnostic(status):
    job = example("s2_job.json")
    job["decision"]["status"] = status
    job["decision"]["diagnostics"] = []
    issues = validate_job(job, SUMMARIES["S2"])
    assert ("DIAGNOSTIC_REQUIRED", "decision.diagnostics") in {
        (issue["code"], issue["path"]) for issue in issues}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_checker_rejects_nonfinite_python_numbers(value):
    job = example("s1_job.json")
    job["decision"]["plan"]["metrics"]["total_distance_m"] = value
    issues = validate_job(job, SUMMARIES["S1"])
    assert ("NUMBER_NOT_FINITE", "decision.plan.metrics.total_distance_m") in {
        (issue["code"], issue["path"]) for issue in issues}


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_checker_rejects_nonfinite_json_decoder_values(token):
    raw = json.dumps(example("s1_job.json"), allow_nan=False)
    marker = '"total_distance_m": 52634.05780604088'
    assert marker in raw
    job = json.loads(raw.replace(marker, f'"total_distance_m": {token}', 1))
    issues = validate_job(job, SUMMARIES["S1"])
    assert ("NUMBER_NOT_FINITE", "decision.plan.metrics.total_distance_m") in {
        (issue["code"], issue["path"]) for issue in issues}


def test_offline_validate_job_does_not_bind_request_but_acceptance_must():
    request = example("s1_request.json")
    pristine = example("s1_job.json")
    trusted = trusted_job_record(pristine)
    job = deepcopy(pristine)
    job["request_id"] = "different-request-id"
    assert validate_job(job, SUMMARIES["S1"]) == []
    issues = validate_bound_job(job, request, SUMMARIES["S1"], trusted)
    assert ("REQUEST_MISMATCH", "request_id") in {
        (issue["code"], issue["path"]) for issue in issues}


@pytest.mark.parametrize("field", ["leg_to_node", "stop_node_id"])
def test_route_structure_rejects_node_sequence_discontinuity(field):
    job = example("s1_job.json")
    route = job["decision"]["plan"]["vehicle_routes"][0]
    if field == "leg_to_node":
        route["legs"][0]["to_node"] += 1
    else:
        route["stops"][0]["node_id"] += 1
    issues = validate_job(job, SUMMARIES["S1"])
    assert ("ROUTE_CONTINUITY_INVALID", "plan.vehicle_routes.0") in {
        (issue["code"], issue["path"]) for issue in issues}


@pytest.mark.parametrize("field", ["gate", "derived_from"])
def test_offline_claims_require_trusted_binding(field):
    request = example("s1_request.json")
    pristine = example("s1_job.json")
    trusted = trusted_job_record(pristine)
    job = deepcopy(pristine)
    if field == "gate":
        job["decision"]["validation"]["gate"] = "SELF_DECLARED_GATE"
        expected_path = "decision.validation.gate"
    else:
        job["decision"]["derived_from"]["manifest_sha256"] = "f" * 64
        expected_path = "decision.derived_from"
    assert validate_job(job, SUMMARIES["S1"]) == []
    issues = validate_bound_job(job, request, SUMMARIES["S1"], trusted)
    assert ("TRUSTED_EVIDENCE_MISMATCH", expected_path) in {
        (issue["code"], issue["path"]) for issue in issues}


def test_bound_validation_accepts_all_projected_completed_examples():
    for scenario in ("s0", "s1", "s2", "s3", "s4"):
        request = example(f"{scenario}_request.json")
        job = example(f"{scenario}_job.json")
        assert validate_bound_job(job, request, SUMMARIES[scenario.upper()],
                                  trusted_job_record(job)) == []


def test_bound_lifecycle_uses_server_persistence_ids_and_keeps_failure_separate():
    accepted = example("s1_request.json")
    accepted["request_id"] = "example-lifecycle"
    lifecycle = example("lifecycle_jobs.json")
    trusted = {"request_id": "example-lifecycle", "run_id": "CONTRACT_EXAMPLE_JOB_1",
               "validation_gate": None, "derived_from": None}
    for status in ("queued", "running", "failed"):
        assert validate_bound_job(lifecycle[status], accepted, SUMMARIES["S1"], trusted) == []
    tampered = deepcopy(lifecycle["queued"])
    tampered["run_id"] = "different-run"
    issues = validate_bound_job(tampered, accepted, SUMMARIES["S1"], trusted)
    assert ("TRUSTED_RECORD_MISMATCH", "run_id") in {
        (issue["code"], issue["path"]) for issue in issues}


@pytest.mark.parametrize("bad_status", [[], {}], ids=["array_status", "object_status"])
def test_status_wrong_container_type_fails_closed_at_stable_path(bad_status):
    request = example("s1_request.json")
    pristine = example("s1_job.json")
    trusted = trusted_job_record(pristine)
    job = deepcopy(pristine)
    job["decision"]["status"] = bad_status

    decision_issues = validate_decision(job["decision"], SUMMARIES["S1"])
    assert ("SCHEMA_INVALID", "status") in {
        (issue["code"], issue["path"]) for issue in decision_issues}
    job_issues = validate_job(job, SUMMARIES["S1"])
    assert ("SCHEMA_INVALID", "decision.status") in {
        (issue["code"], issue["path"]) for issue in job_issues}
    bound_issues = validate_bound_job(job, request, SUMMARIES["S1"], trusted)
    assert ("SCHEMA_INVALID", "decision.status") in {
        (issue["code"], issue["path"]) for issue in bound_issues}


def test_bound_validation_requires_server_resolved_state_before_any_trust_claim():
    request = example("s1_request.json")
    job = example("s1_job.json")
    trusted = trusted_job_record(job)
    issues = validate_bound_job(job, request, None, trusted)
    assert ("TRUSTED_STATE_REQUIRED", "resolved_state") in {
        (issue["code"], issue["path"]) for issue in issues}


def test_bound_validation_does_not_trust_tampered_source_when_state_is_missing():
    request = example("s1_request.json")
    pristine = example("s1_job.json")
    trusted = trusted_job_record(pristine)
    job = deepcopy(pristine)
    job["decision"]["source"]["fixture_sha256"] = "f" * 64

    missing_state_issues = validate_bound_job(job, request, None, trusted)
    assert ("TRUSTED_STATE_REQUIRED", "resolved_state") in {
        (issue["code"], issue["path"]) for issue in missing_state_issues}
    assert validate_job(job, None) == []
    bound_issues = validate_bound_job(job, request, SUMMARIES["S1"], trusted)
    assert ("SOURCE_MISMATCH", "source.fixture_sha256") in {
        (issue["code"], issue["path"]) for issue in bound_issues}
