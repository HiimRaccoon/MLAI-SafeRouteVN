import asyncio
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from backend.api.errors import ApiError
from backend.services.runtime_gateway import RuntimeGateway
from backend.services.settings import Settings


@pytest.fixture
def gateway(tmp_path):
    path = tmp_path / "installation.json"
    path.write_text(json.dumps({"schema_version": "saferoute-m3-server-installation/1", "expected_build_sha256": "8" * 64,
        **{field: str(tmp_path / field) for field in ("runtime_root", "runtime_python", "snapshot_root", "latest_preflight_receipt", "authority_store_parent")}}))
    return RuntimeGateway(Settings(tmp_path, path, tmp_path / "auth", tmp_path / "metadata", tmp_path / "worker"))


def response(**changes):
    value = {"schema_version": "task02-m2-runtime-capabilities/1", "build_sha256": "8" * 64,
             "execution_mode": "SIMULATED_REPLAY", "real_world_observation": False, "online_target_is_sla": False, **changes}
    return SimpleNamespace(returncode=0, stdout=json.dumps({"schema_version": "task02-m2-runtime-response/1", "status": "OK", "value": value}))


def test_bridge_isolates_runtime_interpreter(gateway, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "untrusted-team-tree")
    monkeypatch.setenv("PYTHONHOME", "untrusted-home")
    def run(command, **kwargs):
        assert command[1:3] == ["-I", "-B"]
        assert Path(command[3]).name == "runtime_bridge.py"
        assert command[4:] == ["--installation", str(gateway.settings.installation_path)]
        assert "PYTHONPATH" not in kwargs["env"] and "PYTHONHOME" not in kwargs["env"]
        assert 0 < kwargs["timeout"] <= 60
        return response()
    monkeypatch.setattr(subprocess, "run", run)
    assert asyncio.run(gateway.capabilities())["build_sha256"] == "8" * 64


@pytest.mark.parametrize("changes", [{"build_sha256": "9" * 64}, {"real_world_observation": True},
    {"online_target_is_sla": True}, {"schema_version": "wrong"}])
def test_unverified_projection_rejected(gateway, monkeypatch, changes):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: response(**changes))
    with pytest.raises(ApiError) as caught:
        asyncio.run(gateway.capabilities())
    assert caught.value.code == "RUNTIME_UNAVAILABLE"


def test_timeout_is_not_mocked(gateway, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("runtime", 15)
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ApiError) as caught:
        asyncio.run(gateway.capabilities())
    assert caught.value.status_code == 503 and caught.value.code == "RUNTIME_TIMEOUT"


@pytest.mark.parametrize("count", [0, 3, 8])
def test_startup_inspection_bounds_each_session_and_preserves_full_order(gateway, monkeypatch, count):
    calls = []
    session_ids = ["session-" + str(i) for i in range(count)]
    def inspect(operation, fields, *, operation_timeout):
        assert operation == "inspect_sessions" and fields["command_id"] == "old-recovery-command"
        assert 1 <= len(fields["session_ids"]) <= 2
        assert operation_timeout == 120 * len(fields["session_ids"])
        calls.append(fields["session_ids"])
        return [{"session_id": sid} for sid in fields["session_ids"]]
    monkeypatch.setattr(gateway, "_call", inspect)
    result = asyncio.run(gateway.inspect_sessions(session_ids, "old-recovery-command"))
    assert [v["session_id"] for v in result] == session_ids
    assert calls == [session_ids[i:i + 2] for i in range(0, len(session_ids), 2)]


def test_inspection_session_timeout_propagates_without_dispatching_later_sessions(gateway, monkeypatch):
    calls = []
    def inspect(operation, fields, *, operation_timeout):
        assert operation_timeout == 120 * len(fields["session_ids"])
        calls.append(fields["session_ids"])
        if len(calls) == 2:
            raise ApiError(503, "RUNTIME_TIMEOUT", "runtime", "Injected SDK timeout")
        return [{"session_id": sid} for sid in fields["session_ids"]]
    monkeypatch.setattr(gateway, "_call", inspect)
    with pytest.raises(ApiError) as caught:
        asyncio.run(gateway.inspect_sessions(["s" + str(i) for i in range(10)], "recovery"))
    assert caught.value.code == "RUNTIME_TIMEOUT" and calls == [["s0", "s1"], ["s2", "s3"]]
