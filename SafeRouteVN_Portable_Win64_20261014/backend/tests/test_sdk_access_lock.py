"""Serialize real OS ownership around mocked SDK processes, with one deadline."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from backend.api.errors import ApiError
from backend.services.runtime_gateway import RuntimeGateway
from backend.services.sdk_access_lock import SdkAccessBusy, SdkAccessLock
from backend.tests.test_runtime_gateway import gateway
from backend.tests.test_runtime_read_timeout import SESSION, JOB, basis, execution, job_view, sdk_response


def authority(gateway):
    return Path(gateway.settings.installation()["authority_store_parent"]) / "backend_authority.sqlite"


def fields(operation):
    value = {"session_id": SESSION, "command_id": "sdk-coordination-test"}
    if operation in ("job_view", "cancel", "accept", "compute"):
        value["job_id"] = JOB
    if operation == "read_notifications_batch":
        value = {"session_ids": [SESSION], "command_id": "batch-test"}
    return value


def value_for(operation):
    if operation == "read_notifications_batch":
        return {SESSION: []}
    if operation in ("compute", "job_view"):
        return job_view()
    if operation == "accept":
        return {"status": "ACCEPTED", "job_id": JOB, "basis": basis()}
    return execution()


@pytest.mark.parametrize("different_metadata", [False, True])
def test_batch_and_foreground_use_same_authority_mutex_even_with_different_m3_metadata(gateway, monkeypatch, different_metadata):
    other = RuntimeGateway(replace(gateway.settings, metadata_path=gateway.settings.metadata_path.with_name("other-metadata"))) if different_metadata else gateway
    assert SdkAccessLock(authority(gateway)).path == SdkAccessLock(authority(other)).path
    started, release, foreground = Event(), Event(), Event()
    calls = []

    def run(command, **kwargs):
        operation = command[command.index("--operation") + 1]
        calls.append(operation)
        if operation == "read_notifications_batch":
            started.set()
            assert release.wait(3)
        else:
            foreground.set()
        return sdk_response(value_for(operation))

    monkeypatch.setattr(subprocess, "run", run)
    with ThreadPoolExecutor(max_workers=2) as executor:
        batch = executor.submit(gateway._call, "read_notifications_batch", fields("read_notifications_batch"))
        try:
            assert started.wait(3)
            read = executor.submit(other._call, "resolve", fields("resolve"))
            assert not foreground.wait(0.05) and not read.done()
            release.set()
            assert batch.result(timeout=3) == {SESSION: []}
            assert read.result(timeout=3) == execution()
        finally:
            release.set()
    assert calls == ["read_notifications_batch", "resolve"]


def test_compute_remains_concurrent_with_foreground_access_owner(gateway, monkeypatch):
    owner = SdkAccessLock(authority(gateway))
    owner.acquire(monotonic() + 1)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        return sdk_response(job_view())

    monkeypatch.setattr(subprocess, "run", run)
    try:
        assert gateway._call("compute", fields("compute"), compute_timeout=105) == job_view()
    finally:
        owner.release()
    assert len(calls) == 1 and calls[0][1] == 105


@pytest.mark.parametrize("operation", ["resolve", "submit", "cancel", "recover", "read_notifications_batch"])
def test_contended_deadline_exhaustion_is_busy_without_sdk_launch_retry_or_corruption_gate(gateway, monkeypatch, operation):
    limited = RuntimeGateway(replace(gateway.settings, runtime_timeout_seconds=0.06, runtime_replay_timeout_seconds=0.06))
    owner = SdkAccessLock(authority(gateway))
    owner.acquire(monotonic() + 1)
    calls = []

    def unexpected(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Busy gateway launched an SDK process")

    monkeypatch.setattr(subprocess, "run", unexpected)
    started = monotonic()
    try:
        with pytest.raises(ApiError) as caught:
            limited._call(operation, fields(operation))
    finally:
        owner.release()
    assert caught.value.status_code == 503 and caught.value.code == "RUNTIME_BUSY"
    assert monotonic() - started < 1
    assert calls == [] and not limited.settings.metadata_path.exists()


@pytest.mark.parametrize("operation", ["resolve", "accept", "read_notifications_batch"])
def test_lock_wait_and_sdk_process_share_one_configured_budget(gateway, monkeypatch, operation):
    limited = RuntimeGateway(replace(gateway.settings, runtime_timeout_seconds=0.25,
        runtime_accept_timeout_seconds=0.25, runtime_replay_timeout_seconds=0.25))
    owner = SdkAccessLock(authority(gateway))
    owner.acquire(monotonic() + 1)
    release = Event()

    def delayed_release():
        release.wait(3)
        sleep(0.08)
        owner.release()

    thread = Thread(target=delayed_release)
    thread.start()
    allocated = []

    def run(*args, **kwargs):
        allocated.append(kwargs["timeout"])
        return sdk_response(value_for(operation))

    monkeypatch.setattr(subprocess, "run", run)
    try:
        release.set()
        assert limited._call(operation, fields(operation)) == value_for(operation)
    finally:
        release.set()
        thread.join(timeout=3)
        owner.release()
    assert len(allocated) == 1 and 0 < allocated[0] < 0.2
    assert not thread.is_alive()


@pytest.mark.parametrize("failure,expected", [
    ("timeout", "RUNTIME_TIMEOUT"), ("projection", "RUNTIME_UNAVAILABLE"),
    ("rejection", "STALE_HEAD"), ("transport", "RUNTIME_UNAVAILABLE"),
])
def test_access_owner_releases_after_reaped_timeout_transport_or_sdk_error(gateway, monkeypatch, failure, expected):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if failure == "transport":
            raise OSError("Injected subprocess launch failure")
        if failure == "rejection":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"schema_version": "task02-m2-runtime-response/1",
                "status": "FAIL", "diagnostics": [{"code": "STALE_HEAD"}]}))
        return sdk_response({**execution(), "schema_version": "unverified"})

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ApiError) as caught:
        gateway._call("resolve", fields("resolve"))
    assert caught.value.code == expected and len(calls) == 1
    next_owner = SdkAccessLock(authority(gateway))
    next_owner.acquire(monotonic() + 0.2)
    next_owner.release()


def test_real_other_process_holds_same_resolved_authority_lock_until_release(gateway):
    code = """import sys,time
from backend.services.sdk_access_lock import SdkAccessLock
lock=SdkAccessLock(sys.argv[1]);lock.acquire(time.monotonic()+2)
print('LOCKED',flush=True)
sys.stdin.readline();lock.release()
"""
    child = subprocess.Popen([sys.executable, "-B", "-c", code, str(authority(gateway))],
        cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    contender = SdkAccessLock(authority(gateway))
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(child.stdout.readline).result(timeout=3).strip() == "LOCKED"
        with pytest.raises(SdkAccessBusy):
            contender.acquire(monotonic() + 0.06)
        child.stdin.write("release\n")
        child.stdin.flush()
        assert child.wait(timeout=3) == 0
        contender.acquire(monotonic() + 0.2)
        contender.release()
    finally:
        contender.release()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()


def test_different_authorities_do_not_serialize_unrelated_sdk_work(gateway, monkeypatch):
    config = gateway.settings.installation()
    config["authority_store_parent"] = str(gateway.settings.project_root / "separate-authority")
    installation = gateway.settings.installation_path.with_name("other-installation.json")
    installation.write_text(json.dumps(config), encoding="utf-8")
    other = RuntimeGateway(replace(gateway.settings, installation_path=installation))
    assert SdkAccessLock(authority(gateway)).path != SdkAccessLock(authority(other)).path
    rendezvous = Barrier(2)

    def run(*args, **kwargs):
        rendezvous.wait(timeout=3)
        return sdk_response(execution())

    monkeypatch.setattr(subprocess, "run", run)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(current._call, "resolve", fields("resolve")) for current in (gateway, other)]
        assert all(result.result(timeout=3) == execution() for result in results)


def test_expired_access_deadline_creates_no_lock_file(gateway):
    access = SdkAccessLock(authority(gateway))
    with pytest.raises(SdkAccessBusy):
        access.acquire(monotonic() - 1)
    assert not access.path.exists()
    access.release()


@pytest.mark.parametrize("failed_operation", ["seek", "write"])
def test_unowned_initialization_io_failure_closes_handle_and_preserves_typed_error(gateway, monkeypatch, failed_operation):
    original_open = Path.open
    streams = []

    class FailingInitialization:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def seek(self, *args):
            if failed_operation == "seek":
                raise OSError("Injected lock-file seek failure")
            return self.stream.seek(*args)

        def write(self, value):
            if failed_operation == "write":
                raise OSError("Injected lock-file write failure")
            return self.stream.write(value)

    def open_path(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path.name.endswith(".m3-sdk-access.lock"):
            streams.append(stream)
            return FailingInitialization(stream)
        return stream

    monkeypatch.setattr(Path, "open", open_path)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ApiError) as caught:
        gateway._call("resolve", fields("resolve"))
    assert caught.value.code == "RUNTIME_UNAVAILABLE"
    assert calls == [] and len(streams) == 1 and streams[0].closed
