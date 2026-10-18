"""Portable API v1 acceptance suite.

This file intentionally imports only the contract package and dependencies in
requirements-test.txt.  It does not import TASK-02 optimization code, access
SQLite, or require frozen S0/S1/Gate 1 runs.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from shared.contracts.task02_api_v1 import (
    SCHEMA,
    validate_bound_job,
    validate_decision,
    validate_job,
    validate_request,
)


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = Path(__file__).resolve().parent
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
        elif isinstance(parent, list):
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


def test_portable_schema_and_declared_wire_versions():
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$defs"]["request"]["properties"]["schema_version"]["const"] == (
        "task02-m2-m3m4-request/1")
    assert SCHEMA["$defs"]["job"]["properties"]["schema_version"]["const"] == (
        "task02-m2-m3m4-job/1")
    assert SCHEMA["$defs"]["decision"]["properties"]["schema_version"]["const"] == (
        "task02-m2-m3m4-decision/1")


@pytest.mark.parametrize("vector", VECTORS, ids=lambda item: item["id"])
def test_portable_neutral_vector(vector):
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


def test_all_eleven_projection_payloads_are_locally_consistent():
    assert set(SUMMARIES) == {"S0", "S1", "S2", "S3", "S4"}
    for scenario in ("s0", "s1", "s2", "s3", "s4"):
        summary = SUMMARIES[scenario.upper()]
        request = example(f"{scenario}_request.json")
        job = example(f"{scenario}_job.json")
        assert validate_request(request, summary, request) == []
        assert validate_job(job, summary) == []
        assert validate_bound_job(job, request, summary, trusted_job_record(job)) == []


@pytest.mark.parametrize("bad_status", [[], {}], ids=["array_status", "object_status"])
def test_status_wrong_container_is_a_schema_diagnostic_not_an_exception(bad_status):
    request = example("s1_request.json")
    pristine = example("s1_job.json")
    trusted = trusted_job_record(pristine)
    job = deepcopy(pristine)
    job["decision"]["status"] = bad_status
    checks = (
        (validate_decision(job["decision"], SUMMARIES["S1"]), "status"),
        (validate_job(job, SUMMARIES["S1"]), "decision.status"),
        (validate_bound_job(job, request, SUMMARIES["S1"], trusted), "decision.status"),
    )
    for issues, path in checks:
        assert ("SCHEMA_INVALID", path) in {
            (issue["code"], issue["path"]) for issue in issues}


def test_bound_mode_requires_server_state_and_offline_mode_remains_offline():
    request = example("s1_request.json")
    pristine = example("s1_job.json")
    trusted = trusted_job_record(pristine)
    job = deepcopy(pristine)
    job["decision"]["source"]["fixture_sha256"] = "f" * 64
    for payload in (pristine, job):
        issues = validate_bound_job(payload, request, None, trusted)
        assert ("TRUSTED_STATE_REQUIRED", "resolved_state") in {
            (issue["code"], issue["path"]) for issue in issues}
    assert validate_job(job, None) == []
    issues = validate_bound_job(job, request, SUMMARIES["S1"], trusted)
    assert ("SOURCE_MISMATCH", "source.fixture_sha256") in {
        (issue["code"], issue["path"]) for issue in issues}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_portable_python_nonfinite_numbers_fail_closed(value):
    job = example("s1_job.json")
    job["decision"]["plan"]["metrics"]["total_distance_m"] = value
    issues = validate_job(job, SUMMARIES["S1"])
    assert ("NUMBER_NOT_FINITE", "decision.plan.metrics.total_distance_m") in {
        (issue["code"], issue["path"]) for issue in issues}


def test_package_manifest_matches_every_portable_file():
    manifest_path = CONTRACT / "task02_api_v1_package_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    required = {
        "shared/contracts/task02_api_v1.py",
        "shared/contracts/task02_api_v1.schema.json",
        "shared/contracts/task02_api_v1_vectors.json",
        "shared/contracts/test_task02_api_v1_portable.py",
        "shared/contracts/requirements-test.txt",
    }
    assert required.issubset(manifest["files"])
    for relative, expected in manifest["files"].items():
        path = (ROOT / relative).resolve()
        assert path.is_relative_to(ROOT) and path.is_file()
        assert path.stat().st_size == expected["bytes"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected["sha256"]
