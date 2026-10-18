"""Singleton ownership and replay control integration for automatic playback."""
import asyncio
from copy import deepcopy
import sqlite3
from types import SimpleNamespace

from backend.api.errors import ApiError
from backend.models.http import ReplayControlRequest
from backend.models.playback import StartPlaybackRequest
from backend.services.compute_worker import ComputeWorker
from backend.tests.test_jobs import optimize
from backend.tests.test_playback import activated, control, description, due, playback_client, start
from backend.tests.test_plans import revision
from backend.tests.test_replay import EPOCH, FIRST_STEP, replay_setup, step
from backend.tests.test_sessions import headers, setup
import pytest


def test_queued_compute_has_priority_over_due_playback_tick(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    async def compute(session_id, job_id, command_id, budget):
        gateway.computes.append((session_id, job_id, command_id, budget))
        gateway.certify(session_id, job_id)
        return deepcopy(gateway.jobs[(session_id, job_id)])
    gateway.compute = compute
    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert await worker.run_once() is True  # Drain original certified job queue item.
            assert start(playback_client, gateway, sid).status_code == 200
            due(settings, sid)
            response = optimize(playback_client, sid, request_id="queued-before-playback", profile="SAFER")
            assert response.status_code == 202
            assert await worker.run_once() is True
            assert len(gateway.computes) == 1 and gateway.advances == []
            assert worker.jobs.job(sid, response.json()["data"]["job_id"])["queue_state"] == "DONE"
            assert await worker.run_once() is True
            assert len(gateway.advances) == 1 and worker.active_playback is None
        finally:
            await worker.shutdown()
    asyncio.run(scenario())


def test_cancelled_owned_playback_drains_before_singleton_release(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    original = gateway.advance
    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(*args):
            entered.set()
            await release.wait()
            assert worker.lock.handle is not None
            return await original(*args)
        gateway.advance = blocked
        try:
            assert await worker.run_once() is True
            assert start(playback_client, gateway, sid).status_code == 200
            due(settings, sid)
            task = asyncio.create_task(worker.run_once())
            await asyncio.wait_for(entered.wait(), 3)
            assert worker.active_playback is not None
            task.cancel()
            await asyncio.sleep(0.02)
            task.cancel()
            assert not task.done() and worker.lock.handle is not None
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert worker.active_playback is None and len(gateway.advances) == 1
            assert worker.playback.public(worker.playback.get(sid))["in_flight"] is False
        finally:
            release.set()
            await worker.shutdown()
        assert worker.lock.handle is None
        assert worker.playback.public(worker.playback.get(sid))["fully_paused"] is True
    asyncio.run(scenario())


def test_startup_fences_controller_before_reconciling_uncertain_tick(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    assert start(playback_client, gateway, sid).status_code == 200
    service = playback_client.app.state.playback_service
    due(settings, sid)
    state = service.repository.get(sid)
    row = service.repository.reserve_tick(state, state["basis"], FIRST_STEP, EPOCH)
    original = gateway.advance
    async def checked(*args):
        assert service.repository.get(sid)["reason"] == "PAUSED_WORKER_RESTART"
        return await original(*args)
    gateway.advance = checked
    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.status == "READY" and len(gateway.advances) == 1
            assert worker.replays.pending_requests() == []
            assert worker.playback.get(sid)["status"] == "PAUSED"
            assert worker.playback.get(sid)["reason"] == "PAUSED_WORKER_RESTART"
            # Finalize scheduling pointer after the generic startup reconciler.
            assert await worker.run_once() is True  # Original queue item's terminal view.
            assert await worker.run_once() is True  # Settles pointer, no new SDK command.
            assert worker.playback.public(worker.playback.get(sid))["fully_paused"] is True
            due(settings, sid)
            assert await worker.run_once() is False and len(gateway.advances) == 1
        finally:
            await worker.shutdown()
    asyncio.run(scenario())
    assert gateway.advances[0][1] == row["command_id"] and gateway.advances[0][2] == row["basis"]


@pytest.mark.parametrize("operation", ["pause", "reset", "advance"])
def test_new_manual_mutation_stops_playback_but_retry_does_not_stop_a_new_start(playback_client, replay_setup, operation):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    assert start(playback_client, gateway, sid).status_code == 200
    old_revision = revision(gateway, sid)
    if operation == "advance":
        response = step(playback_client, sid, old_revision, target_time=FIRST_STEP)
    else:
        response = playback_client.post(f"/api/sessions/{sid}/replay/{operation}", headers=headers(), json={"request_id": "manual-1"})
    assert response.status_code == 200, response.text
    current = description(playback_client, sid).json()["data"]
    assert current["fully_paused"] is True and current["reason"] == "MANUAL_" + operation.upper()
    assert start(playback_client, gateway, sid, request_id="play-2").status_code == 200
    if operation == "advance":
        retry = step(playback_client, sid, old_revision, target_time=FIRST_STEP)
    else:
        retry = playback_client.post(f"/api/sessions/{sid}/replay/{operation}", headers=headers(), json={"request_id": "manual-1"})
    assert retry.status_code == 200 and retry.json()["data"]["receipt"] == response.json()["data"]["receipt"]
    assert description(playback_client, sid).json()["data"]["paused"] is False
    if operation == "reset":
        new_sid = response.json()["data"]["session"]["session_id"]
        assert description(playback_client, new_sid).json()["data"]["reason"] == "NOT_STARTED"


def test_worker_critical_playback_error_degrades_and_fences_controller(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert await worker.run_once() is True
            assert start(playback_client, gateway, sid).status_code == 200
            due(settings, sid)
            gateway.corrupt_replay_result = "head_sha256", {}
            assert await worker.run_once() is False
            assert worker.status == "DEGRADED" and worker.last_error == "REPLAY_BINDING_CHANGED"
            assert worker.playback.get(sid)["status"] == "PAUSED"
            assert worker.playback.public(worker.playback.get(sid))["in_flight"] is True
            assert await worker.run_once() is False and len(gateway.advances) == 1
        finally:
            await worker.shutdown()
    asyncio.run(scenario())


def test_shutdown_before_initialization_keeps_lock_and_task_cleanup_safe(replay_setup):
    settings, gateway = replay_setup
    worker = ComputeWorker(settings, gateway=gateway)
    asyncio.run(worker.shutdown())
    assert worker.lock.handle is None and worker.heartbeat_task is None


def test_shutdown_metadata_failure_still_cancels_heartbeat_and_releases_lock(playback_client, replay_setup, monkeypatch):
    settings, gateway = replay_setup
    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        heartbeat_task = worker.heartbeat_task
        def broken_fence():
            raise sqlite3.OperationalError("Injected playback metadata failure during shutdown")
        monkeypatch.setattr(worker.playback_service, "fence_restart", broken_fence)
        await worker.shutdown()
        assert worker.heartbeat_task is None and heartbeat_task.done()
        assert worker.lock.handle is None and worker.started is False
    asyncio.run(scenario())
