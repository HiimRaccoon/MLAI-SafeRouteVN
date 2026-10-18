"""Bound shared verified bridge work while preserving typed public projections."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from backend.api.errors import ApiError
from backend.services.runtime_gateway import RuntimeGateway
from backend.tests.test_runtime_gateway import gateway


SESSION, JOB = "session-read-timeout", "job-read-timeout"


def basis():
    return {"session_id": SESSION, "root_sha256": "a" * 64, "head_sha256": "b" * 64,
        "head_version": "17", "generation": "4", "source_sha256": "c" * 64,
        "context_version": "TEST_ONLY-context", "overlay_sha256": None, "build_sha256": "8" * 64}


def execution():
    return {"schema_version": "task02-m2-execution-view/2", "basis": basis(),
        "execution_mode": "SIMULATED_REPLAY", "real_world_observation": False,
        "current_time": "2026-09-27T21:00:00+07:00", "order_ids": [], "delivered_prefix": [],
        "planned_served_suffix": [], "unserved": [], "metric_scope": "OBSERVED_PREFIX_ONLY",
        "observed_metrics": None, "vehicles": [], "active_job_id": None, "pending_event_ids": [],
        "planned_suffix_metrics": None, "projected_whole_metrics": None, "accepted_trajectory": None}


def job_view():
    return {"schema_version": "task02-m2-runtime-job-view/1", "job_id": JOB,
        "job_status": "COMPLETED", "input_basis": basis(), "business_status": "SEARCH_LIMIT",
        "internal_status": "SEARCH_LIMIT", "diagnostics": [], "validation": {"status": "NOT_RUN", "valid": None},
        "coverage_evaluated": False, "served_orders": [], "unserved_orders": [], "plan_available": False,
        "execution_view_required": True, "public_api_v1_dynamic_plan_available": False}


def sdk_response(value):
    return SimpleNamespace(returncode=0, stdout=json.dumps({"schema_version": "task02-m2-runtime-response/1",
        "status": "OK", "value": value}))


def invoke(gateway, operation):
    if operation == "resolve":
        pending = gateway.resolve(SESSION)
    elif operation == "job_view":
        pending = gateway.job_view(SESSION, JOB)
    elif operation == "submit":
        pending = gateway.submit(SESSION, "ordinary-submit", basis(), "BALANCED", 60)
    elif operation == "cancel":
        pending = gateway.cancel(SESSION, JOB, "ordinary-cancel")
    else:
        pending = gateway.recover(SESSION, "ordinary-recover")
    return asyncio.run(pending)


@pytest.mark.parametrize("operation", ["resolve", "job_view"])
def test_public_read_has_finite_ordinary_timeout_and_retains_verified_bridge_and_full_binding(gateway, monkeypatch, operation):
    assert gateway.settings.runtime_timeout_seconds == 60
    expected = execution() if operation == "resolve" else job_view()
    original = deepcopy(expected)
    calls = []
    monkeypatch.setenv("PYTHONPATH", "untrusted-team-tree")
    monkeypatch.setenv("PYTHONHOME", "untrusted-home")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        config = gateway.settings.installation()
        assert command == [config["runtime_python"], "-I", "-B",
            str(Path(__file__).resolve().parents[1] / "services/runtime_bridge.py"),
            "--installation", str(gateway.settings.installation_path), "--operation", operation]
        assert 0 < kwargs["timeout"] <= 60 and kwargs["cwd"] == config["runtime_root"]
        assert "PYTHONPATH" not in kwargs["env"] and "PYTHONHOME" not in kwargs["env"]
        assert kwargs["capture_output"] is True and kwargs["text"] is True and kwargs["encoding"] == "utf-8"
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        request = json.loads(kwargs["input"])
        assert request["session_id"] == SESSION
        assert request["command_id"].startswith("m3-read-" if operation == "resolve" else "m3-poll-")
        assert set(request) == ({"session_id", "command_id"} if operation == "resolve" else {"session_id", "command_id", "job_id"})
        if operation == "job_view":
            assert request["job_id"] == JOB
        return sdk_response(expected)

    monkeypatch.setattr(subprocess, "run", run)
    assert invoke(gateway, operation) == original
    assert len(calls) == 1 and expected == original


@pytest.mark.parametrize("operation", ["resolve", "job_view", "submit", "cancel", "recover"])
def test_ordinary_operation_timeout_is_one_bounded_attempt_and_typed_503_without_fallback(gateway, monkeypatch, operation):
    calls = []

    def timeout(command, **kwargs):
        calls.append((command, kwargs))
        assert 0 < kwargs["timeout"] <= 60
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ApiError) as caught:
        invoke(gateway, operation)
    assert caught.value.status_code == 503 and caught.value.code == "RUNTIME_TIMEOUT"
    assert len(calls) == 1
    assert not gateway.settings.metadata_path.exists()


@pytest.mark.parametrize("operation", ["resolve", "job_view"])
@pytest.mark.parametrize("bad_binding", ["session", "build", "schema"])
def test_longer_read_budget_still_rejects_unverified_projection(gateway, monkeypatch, operation, bad_binding):
    value = execution() if operation == "resolve" else job_view()
    if bad_binding == "schema":
        value["schema_version"] = "unverified-private-view/1"
    else:
        bound = value["basis"] if operation == "resolve" else value["input_basis"]
        bound["session_id" if bad_binding == "session" else "build_sha256"] = "another-session" if bad_binding == "session" else "9" * 64
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs["timeout"])
        return sdk_response(value)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ApiError) as caught:
        invoke(gateway, operation)
    assert caught.value.code == "RUNTIME_UNAVAILABLE" and len(calls) == 1 and 0 < calls[0] <= 60


@pytest.mark.parametrize("operation", ["resolve", "job_view"])
def test_configured_ordinary_timeout_controls_reads_and_preserves_explicit_overrides(gateway, monkeypatch, operation):
    configured = RuntimeGateway(replace(gateway.settings, runtime_timeout_seconds=23))
    value = execution() if operation == "resolve" else job_view()
    captured = []

    def run(*args, **kwargs):
        captured.append(kwargs["timeout"])
        return sdk_response(value)

    monkeypatch.setattr(subprocess, "run", run)
    assert invoke(configured, operation) == value and len(captured) == 1 and 0 < captured[0] <= 23
    assert configured.settings.runtime_timeout_seconds == 23
    assert configured.settings.runtime_capabilities_timeout_seconds == configured.settings.runtime_accept_timeout_seconds == 60
    assert configured.settings.runtime_replay_timeout_seconds == 120


@pytest.mark.parametrize("operation,expected_timeout", [
    ("submit", 60), ("cancel", 60), ("recover", 60), ("capabilities", 60), ("bootstrap", 60),
    ("accept", 60), ("advance", 120), ("apply_event", 120), ("inspect_sessions", 120),
    ("read_notifications_batch", 120), ("acknowledge_batch", 120), ("compute", 105),
])
def test_ordinary_timeout_preserves_explicit_other_operation_budgets(gateway, monkeypatch, operation, expected_timeout):
    captured = []

    def timeout(command, **kwargs):
        captured.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ApiError) as caught:
        gateway._call(operation, {}, compute_timeout=105 if operation == "compute" else None)
    assert caught.value.code == "RUNTIME_TIMEOUT" and len(captured) == 1
    assert 0 < captured[0] <= expected_timeout
    if operation == "compute":
        assert captured[0] == expected_timeout
