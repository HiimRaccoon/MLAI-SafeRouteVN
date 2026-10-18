"""SDK mutex contention postpones worker work without inventing corruption."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import time

from fastapi.testclient import TestClient
import pytest

from backend.api.errors import ApiError
from backend.api.main import create_app
from backend.services.compute_worker import ComputeWorker
from backend.tests.test_outbox import notification
from backend.tests.test_recovery import recovery_audit, recovery_row
from backend.tests.test_sessions import code, headers, setup
from backend.tests.test_worker import heartbeat, repository, seed_job, worker_setup
from backend.tests.test_worker_outbox_polling import polling_setup, seed_sessions


def fail_then_resume(gateway, method, errors):
    original = getattr(gateway, method)
    attempts = []

    async def operation(*args):
        attempts.append(deepcopy(args))
        diagnostic = errors.pop(0) if errors else None
        if diagnostic:
            raise ApiError(503, diagnostic, "runtime", "Injected bounded SDK coordination failure")
        return await original(*args)

    setattr(gateway, method, operation)
    return attempts


def add_replay_and_accept(gateway):
    async def accept(sid, jid, command, basis):
        assert gateway.views[sid]["basis"] == basis
        current = gateway.views[sid]
        current["basis"]["generation"] = str(int(basis["generation"]) + 1)
        current["active_job_id"] = jid
        current["planned_served_suffix"] = deepcopy(gateway.jobs[(sid, jid)]["served_orders"])
        return {"status": "ACCEPTED", "job_id": jid, "basis": deepcopy(current["basis"])}

    async def advance(sid, command, basis, target):
        assert gateway.views[sid]["basis"] == basis
        current = gateway.views[sid]
        current["current_time"] = target
        current["basis"]["head_version"] = str(int(basis["head_version"]) + 1)
        current["basis"]["head_sha256"] = hashlib.sha256(target.encode()).hexdigest()
        return {"status": "ADVANCED", "basis": deepcopy(current["basis"])}

    gateway.accept, gateway.advance = accept, advance


def reserve_pending(worker, operation, sid, jid):
    basis = deepcopy(worker.gateway.views[sid]["basis"])
    common = ("alice", sid)
    tail = ("busy-pending", "d" * 64, worker.settings.installation_identity())
    if operation == "accept":
        repo = worker.plans
        row = repo.reserve_request(*common, *tail, job_id=jid, basis=basis)
    elif operation == "advance":
        repo = worker.replays
        current = worker.gateway.views[sid]["current_time"]
        target = (datetime.fromisoformat(current) + timedelta(seconds=60)).isoformat()
        row = repo.reserve_request(*common, operation, *tail, basis=basis,
                                  payload={"current_time": current, "target_time": target})
    else:
        repo = worker.jobs
        extra = {"job_id": jid} if operation == "cancel" else {
            "basis": basis, "profile": "BALANCED", "budget_seconds": worker.settings.compute_budget_seconds}
        row = repo.reserve_request(*common, operation, *tail, **extra)
    repo.release_request(row)
    return repo, next(item for item in repo.pending_requests() if item["request_id"] == "busy-pending")


def assert_open(worker, settings, sessions):
    assert worker.status == "READY" and worker.last_error is None
    assert heartbeat(settings)["status"] == "READY"
    for sid in sessions:
        assert recovery_row(settings, sid)["status"] == "VERIFIED"
        worker.session_service.mutation_allowed(sid)
        assert not any(row["operation"] in ("OUTBOX_FAILED", "COMPUTE_QUARANTINED")
                       for row in recovery_audit(settings, sid))


@pytest.mark.parametrize("phase", ["read", "ack"])
def test_periodic_busy_keeps_same_due_batch_gates_and_pending_ack_then_resumes(polling_setup, phase):
    settings, gateway = polling_setup
    sessions = seed_sessions(settings, gateway, 4)
    before = deepcopy(gateway.views)

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            gateway.events = {sid: [notification(sid)] for sid in sessions}
            setattr(gateway, phase + "_error", "RUNTIME_BUSY")
            worker.next_outbox_poll = 0
            batch = sorted(sessions)[:1]
            assert await worker.run_once() is False
            assert worker.next_outbox_poll == 0 and worker.outbox_poll_order == sorted(sessions)
            assert gateway.read_batches == [batch]
            assert_open(worker, settings, sessions)
            pending = {sid: worker.outbox.pending(sid) for sid in batch}
            assert all(len(rows) == (1 if phase == "ack" else 0) for rows in pending.values())
            old_ack = deepcopy(gateway.ack_batches)
            setattr(gateway, phase + "_error", None)
            assert await worker.run_once() is False
            assert gateway.read_batches == [batch, batch]
            assert worker.outbox_poll_order == sorted(sessions)[1:] + batch
            assert worker.next_outbox_poll > time.monotonic()
            assert_open(worker, settings, sessions)
            assert all(worker.outbox.pending(sid) == [] for sid in batch)
            if phase == "ack":
                assert gateway.ack_batches == old_ack + old_ack
                for sid in batch:
                    row = worker.outbox.list_notifications(sid)["notifications"][0]
                    assert row["created_at"] == pending[sid][0]["created_at"] and row["status"] == "ACKNOWLEDGED"
            for _ in range(len(sessions) - 1):
                worker.next_outbox_poll = 0
                assert await worker.run_once() is False
            assert all(worker.outbox.list_notifications(sid)["notifications"][0]["status"] == "ACKNOWLEDGED" for sid in sessions)
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.views == before and gateway.computes == []


@pytest.mark.parametrize("operation,method", [("optimize", "submit"), ("cancel", "cancel"), ("accept", "accept"), ("advance", "advance")])
def test_pending_busy_preserves_original_payload_skips_dispatch_and_reconciles_once(polling_setup, operation, method):
    settings, gateway = polling_setup
    sid, data = seed_job(settings, gateway)
    add_replay_and_accept(gateway)

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            # Already accounted-for historical job; only a later optimize may enqueue compute.
            worker.jobs.finish_job(worker.jobs.claim_job(worker.worker_id))
            gateway.completed(gateway.jobs[(sid, data["job_id"])])
            repo, original = reserve_pending(worker, operation, sid, data["job_id"])
            attempts = fail_then_resume(gateway, method, ["RUNTIME_BUSY"])
            reads = len(gateway.reads)
            assert await worker.run_once() is False
            assert repo.pending_requests() == [original] and gateway.computes == []
            assert_open(worker, settings, [sid])
            assert len(attempts) == 1 and len(gateway.reads) == reads
            assert await worker.run_once() is (operation == "optimize")
            assert repo.pending_requests() == [] and attempts[0] == attempts[1]
            assert attempts[0][1 if operation in ("optimize", "advance") else 2] == original["command_id"]
            if operation != "cancel":
                assert original["basis"] in attempts[0]
            if operation == "advance":
                assert attempts[0][-1] == original["payload"]["target_time"]
                assert len(worker.replays.history(sid)) == 1
            elif operation == "accept":
                assert len(worker.plans.audit(sid)) == 1
            assert_open(worker, settings, [sid])
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.views[sid]["delivered_prefix"] == []


def test_startup_pending_busy_never_opens_http_gate_and_next_worker_reuses_exact_command(polling_setup):
    settings, gateway = polling_setup
    sid, data = seed_job(settings, gateway)
    first = ComputeWorker(settings, gateway=gateway)
    repo, original = reserve_pending(first, "optimize", sid, data["job_id"])
    attempts = fail_then_resume(gateway, "submit", ["RUNTIME_BUSY"])

    async def scenario():
        with pytest.raises(ApiError) as caught:
            await first.startup()
        assert caught.value.code == "RUNTIME_BUSY" and not first.started and first.lock.handle is None
        assert first.last_error == "RUNTIME_BUSY" and repo.pending_requests() == [original]
        assert recovery_row(settings, sid)["status"] == "BLOCKED"
        assert recovery_row(settings, sid)["diagnostic_code"] == "RUNTIME_BUSY"
        assert any(row["operation"] == "STARTUP_FAILED" and row["diagnostic_code"] == "RUNTIME_BUSY"
                   for row in recovery_audit(settings, sid))
        with TestClient(create_app(settings, gateway=gateway)) as client:
            code(client.post(f"/api/sessions/{sid}/optimize", headers=headers("alice"), json={"request_id": "new-while-busy"}), 503, "RUNTIME_BUSY")
        assert len(attempts) == 1 and gateway.computes == []
        resumed = ComputeWorker(settings, gateway=gateway)
        await resumed.startup()
        try:
            assert repo.pending_requests() == [] and attempts[0] == attempts[1]
            assert attempts[1][1] == original["command_id"] and attempts[1][2] == original["basis"]
            assert resumed.status == "READY" and recovery_row(settings, sid)["status"] == "VERIFIED"
            resumed.session_service.mutation_allowed(sid)
        finally:
            await resumed.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["initial_poll", "poll_after_orphan_recovery", "terminal_failure_poll", "failure_recovery", "stale_cancel"])
def test_dispatch_busy_requeues_without_quarantine_and_resumes_stable_commands(worker_setup, phase):
    settings, gateway = worker_setup
    sid, data = seed_job(settings, gateway)
    original = repository(settings).job(sid, data["job_id"])
    before = deepcopy(gateway.views[sid])

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            initial_recoveries = len(gateway.recoveries)
            if phase == "initial_poll":
                fail_then_resume(gateway, "job_view", ["RUNTIME_BUSY"])
            elif phase == "poll_after_orphan_recovery":
                gateway.jobs[(sid, data["job_id"])]["job_status"] = "RUNNING"
                fail_then_resume(gateway, "job_view", [None, "RUNTIME_BUSY"])
            elif phase == "terminal_failure_poll":
                gateway.compute_mode = "BUDGET_EXHAUSTED"
                fail_then_resume(gateway, "job_view", [None, "RUNTIME_BUSY"])
            elif phase == "failure_recovery":
                gateway.compute_mode = "RUNTIME_TIMEOUT"
                gateway.recover_error = "RUNTIME_BUSY"
            else:
                gateway.compute_mode, gateway.cancel_error = "stale", "RUNTIME_BUSY"
            assert await worker.run_once() is False
            assert worker.jobs.job(sid, data["job_id"]) == original
            assert worker.jobs.quarantined_jobs() == [] and worker.current_job is None
            assert_open(worker, settings, [sid])
            if phase == "initial_poll":
                assert gateway.computes == [] and len(gateway.recoveries) == initial_recoveries
            gateway.recover_error = gateway.cancel_error = None
            assert await worker.run_once() is True
            final = worker.jobs.job(sid, data["job_id"])
            assert final["queue_state"] == "DONE" and final["compute_command_id"] == original["compute_command_id"]
            assert final["basis"] == original["basis"] and final["claim_token"] is None
            assert_open(worker, settings, [sid])
            assert all(call[2] == original["compute_command_id"] for call in gateway.computes)
            if phase == "stale_cancel":
                assert gateway.cancellations == [(sid, data["job_id"], original["stale_cancel_command_id"])] * 2
            elif phase in ("poll_after_orphan_recovery", "terminal_failure_poll", "failure_recovery"):
                assert len(gateway.computes) == (0 if phase == "poll_after_orphan_recovery" else 1)
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.views[sid] == before


@pytest.mark.parametrize("phase", ["periodic_outbox", "dispatch"])
def test_actual_store_integrity_failure_still_blocks_gate_and_dispatch(polling_setup, phase):
    settings, gateway = polling_setup
    sid, data = seed_job(settings, gateway)

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            if phase == "periodic_outbox":
                worker.next_outbox_poll, gateway.read_error = 0, "STORE_INVALID"
                assert await worker.run_once() is False
                assert gateway.computes == []
            else:
                gateway.compute_mode = "STORE_INVALID"
                assert await worker.run_once() is True
                assert worker.jobs.job(sid, data["job_id"])["queue_state"] == "QUARANTINED"
            assert worker.status == "DEGRADED" and worker.last_error == "STORE_INVALID"
            assert recovery_row(settings, sid)["status"] == "BLOCKED"
            with pytest.raises(ApiError) as caught:
                worker.session_service.mutation_allowed(sid)
            assert caught.value.code == "STORE_INVALID"
            assert await worker.run_once() is False
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
