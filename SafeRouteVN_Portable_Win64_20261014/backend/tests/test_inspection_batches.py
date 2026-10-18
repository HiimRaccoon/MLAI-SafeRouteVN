"""Bound verified recovery batching without dropping any public validation."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from backend.api.errors import ApiError
from backend.tests.test_runtime_gateway import gateway
from backend.tests.test_runtime_read_timeout import execution


def row(sid):
    view = execution()
    view["basis"]["session_id"] = sid
    return {"session_id": sid, "recovery": {"status": "RECOVERED", "fenced_jobs": [], "basis": deepcopy(view["basis"])},
        "validation": {"validator_version": "task02-m2-bound-session-validator/2", "valid": True,
            "checked_physical_mutations": 5, "historical_execution_builds_preserved": True,
            "current_checker_build_sha256": "8" * 64,
            "scope": "TRUSTED_LOG_RAW_PREFIX_EVENT_SUFFIX; SIMULATED_REPLAY; NOT_OPTIMALITY"},
        "execution_view": view, "notifications": []}


def response(values):
    return SimpleNamespace(returncode=0, stdout=json.dumps({"schema_version": "task02-m2-runtime-response/1", "status": "OK", "value": values}))


@pytest.mark.parametrize("count", [1, 2, 3, 5])
def test_batches_reuse_verified_bridge_and_bound_each_public_session(gateway, monkeypatch, count):
    ids, seen = ["session-" + str(i) for i in range(count)], []
    def run(command, **kwargs):
        fields = json.loads(kwargs["input"])
        batch = fields["session_ids"]
        seen.append(batch)
        assert command[1:3] == ["-I", "-B"]
        assert command[-2:] == ["--operation", "inspect_sessions"]
        assert 120 * len(batch) - 1 <= kwargs["timeout"] <= 120 * len(batch)
        assert fields["command_id"] == "stable-recovery-command"
        return response([row(sid) for sid in batch])
    monkeypatch.setattr(subprocess, "run", run)
    result = asyncio.run(gateway.inspect_sessions(ids, "stable-recovery-command"))
    assert [r["session_id"] for r in result] == ids
    assert seen == [ids[i:i + 2] for i in range(0, count, 2)]


@pytest.mark.parametrize("change", [lambda rows: rows.reverse(), lambda rows: rows.pop(),
    lambda rows: rows.append(deepcopy(rows[0])), lambda rows: rows[1].update(session_id="foreign"),
    lambda rows: rows[1]["execution_view"]["basis"].update(session_id="foreign"),
    lambda rows: rows[1]["validation"].update(valid=False),
    lambda rows: rows[1]["recovery"]["basis"].update(head_version="999")])
def test_batch_rejects_reordering_omission_duplicates_foreign_basis_or_unvalidated_row(gateway, monkeypatch, change):
    values = [row("s0"), row("s1")]
    change(values)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: response(values))
    with pytest.raises(ApiError) as caught:
        asyncio.run(gateway.inspect_sessions(["s0", "s1"], "inspect"))
    assert caught.value.code == "RUNTIME_UNAVAILABLE"


def test_cancellation_during_first_batch_never_dispatches_later_batches(gateway, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def invoke(operation, fields, *, operation_timeout):
        calls.append(fields["session_ids"])
        entered.set()
        assert release.wait(5)
        return [{"session_id": sid} for sid in fields["session_ids"]]
    monkeypatch.setattr(gateway, "_call", invoke)
    async def scenario():
        task = asyncio.create_task(gateway.inspect_sessions(["s0", "s1", "s2", "s3"], "inspect"))
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert calls == [["s0", "s1"]]
        finally:
            release.set()
    asyncio.run(scenario())
    assert calls == [["s0", "s1"]]


@pytest.mark.parametrize("ids,seconds", [(["s0", "s1", "s2"], 360), (["s0"], 240), ([], 0)])
def test_arbitrary_wide_or_unbound_timeout_override_is_rejected(gateway, monkeypatch, ids, seconds):
    def never(*args, **kwargs):
        raise AssertionError("Invalid timeout must not launch a bridge")
    monkeypatch.setattr(subprocess, "run", never)
    with pytest.raises(ApiError) as caught:
        gateway._call("inspect_sessions", {"session_ids": ids, "command_id": "inspect"}, operation_timeout=seconds)
    assert caught.value.code == "RUNTIME_UNAVAILABLE"
