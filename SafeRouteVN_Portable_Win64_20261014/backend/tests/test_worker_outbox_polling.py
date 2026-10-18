"""Bound periodic public SDK polling and retain singleton through mutations."""
import asyncio
from copy import deepcopy
import sqlite3

from fastapi.testclient import TestClient
import pytest

from backend.api.errors import ApiError
from backend.api.main import create_app
from backend.services.compute_worker import ComputeWorker
from backend.tests.test_jobs import optimize
from backend.tests.test_outbox import notification
from backend.tests.test_recovery import recovery_audit, recovery_row
from backend.tests.test_sessions import code, load, setup
from backend.tests.test_worker import WorkerGateway, repository, worker_setup


class PollGateway(WorkerGateway):
    def __init__(self, build):
        super().__init__(build)
        self.events, self.acked, self.ack_receipts = {}, set(), {}
        self.read_batches, self.ack_batches = [], []
        self.read_error = self.ack_error = None
        self.fail_ack_after_commit = False
        self.block_read = self.block_ack = self.block_submit = self.block_cancel = False
        self.read_started, self.release_read = asyncio.Event(), asyncio.Event()
        self.ack_started, self.release_ack = asyncio.Event(), asyncio.Event()
        self.submit_started, self.release_submit = asyncio.Event(), asyncio.Event()
        self.cancel_started, self.release_cancel = asyncio.Event(), asyncio.Event()

    async def read_notifications(self, sid):
        return deepcopy([event for event in self.events.get(sid, []) if (sid, event["event_id"]) not in self.acked])

    async def read_notifications_batch(self, session_ids):
        self.read_batches.append(list(session_ids))
        self.read_started.set()
        if self.block_read:
            await self.release_read.wait()
        if self.read_error:
            raise ApiError(503, self.read_error, "runtime", "Injected public polling failure")
        return {sid: await self.read_notifications(sid) for sid in session_ids}

    async def acknowledge_batch(self, rows):
        self.ack_batches.append([(row["session_id"], row["command_id"], row["event_id"]) for row in rows])
        self.ack_started.set()
        if self.block_ack:
            await self.release_ack.wait()
        if self.ack_error:
            raise ApiError(503, self.ack_error, "runtime", "Injected public acknowledgment failure")
        receipts = []
        for row in rows:
            key = (row["session_id"], row["command_id"])
            self.ack_receipts.setdefault(key, {"status": "ACKNOWLEDGED", "event_id": row["event_id"]})
            self.acked.add((row["session_id"], row["event_id"]))
            receipts.append(deepcopy(self.ack_receipts[key]))
        if self.fail_ack_after_commit:
            self.fail_ack_after_commit = False
            raise ApiError(503, "RUNTIME_TIMEOUT", "runtime", "SDK acknowledgment committed before interrupted response")
        return receipts

    async def submit(self, sid, command_id, basis, profile, budget_seconds):
        self.submit_started.set()
        if self.block_submit:
            await self.release_submit.wait()
        return await super().submit(sid, command_id, basis, profile, budget_seconds)

    async def cancel(self, sid, job_id, command_id):
        self.cancel_started.set()
        if self.block_cancel:
            await self.release_cancel.wait()
        return await super().cancel(sid, job_id, command_id)


@pytest.fixture
def polling_setup(worker_setup):
    settings, original = worker_setup
    return settings, PollGateway(original.build)


def seed_sessions(settings, gateway, count, *, initial_events=False):
    with TestClient(create_app(settings, gateway=gateway)) as client:
        sessions = [load(client, request_id=f"load-{number}").json()["data"]["session"]["session_id"] for number in range(count)]
    if initial_events:
        gateway.events = {sid: [notification(sid)] for sid in sessions}
    return sessions


def test_startup_syncs_every_validated_session_before_opening_gate(polling_setup):
    settings, gateway = polling_setup
    sessions = seed_sessions(settings, gateway, 7, initial_events=True)
    before = deepcopy(gateway.views)

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.status == "READY" and worker.active_outbox is None
            assert {sid for batch in gateway.ack_batches for sid, _, _ in batch} == set(sessions)
            assert len(gateway.ack_batches[0]) == 7
            assert gateway.read_batches == []  # Startup uses already validated inspection summaries.
            assert all(recovery_row(settings, sid)["status"] == "VERIFIED" for sid in sessions)
            assert all(worker.outbox.pending(sid) == [] for sid in sessions)
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.views == before and gateway.computes == []


@pytest.mark.parametrize("count", [0, 1, 3, 4, 10, 28])
def test_periodic_round_robin_bounds_sdk_batch_and_delivers_every_session_without_duplicates(polling_setup, count):
    settings, gateway = polling_setup
    sessions = seed_sessions(settings, gateway, count)
    before = deepcopy(gateway.views)

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            gateway.events = {sid: [notification(sid)] for sid in sessions}
            for _ in range(max(1, count)):
                worker.next_outbox_poll = 0
                assert await worker.run_once() is False
                assert worker.active_outbox is None
            if count:
                assert all(len(batch) == 1 for batch in gateway.read_batches)
                assert set(sid for batch in gateway.read_batches for sid in batch) == set(sessions)
                assert all(len(worker.outbox.list_notifications(sid)["notifications"]) == 1 for sid in sessions)
                assert len(gateway.ack_receipts) == count
            else:
                assert gateway.read_batches == gateway.ack_batches == []
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.views == before and gateway.computes == []


def test_28_sessions_including_arrivals_remain_fair_across_busy_and_retirement(polling_setup, monkeypatch):
    settings, gateway = polling_setup
    sessions = seed_sessions(settings, gateway, 27)
    active = sorted(sessions)

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            monkeypatch.setattr(worker.recovery, "ready_sessions", lambda: list(reversed(active)))
            worker.next_outbox_poll = 0
            await worker.run_once()
            retired = active[-1]
            active.remove(retired)
            with sqlite3.connect(settings.metadata_path) as db:
                db.execute("UPDATE sessions SET status='RETIRED' WHERE session_id=?", (retired,))
            oldest_waiting = active[1]
            gateway.read_error, worker.next_outbox_poll = "RUNTIME_BUSY", 0
            assert await worker.run_once() is False
            assert gateway.read_batches[-1] == [oldest_waiting]
            with TestClient(create_app(settings, gateway=gateway)) as client:
                for number in range(2):  # Arrivals exceed one polling turn.
                    sid = load(client, request_id=f"arrival-{number}").json()["data"]["session"]["session_id"]
                    active.append(sid)
            assert len(active) == 28
            gateway.events = {sid: [notification(sid)] for sid in active}
            gateway.read_error = None
            first_cycle = len(gateway.read_batches)
            for _ in active:
                worker.next_outbox_poll = 0
                assert await worker.run_once() is False
            cycle = gateway.read_batches[first_cycle:]
            assert cycle[0] == [oldest_waiting]
            assert len(cycle) == 28 and {batch[0] for batch in cycle} == set(active)
            assert all(len(batch) == 1 for batch in gateway.read_batches)
            assert retired not in [sid for batch in gateway.read_batches[1:] for sid in batch]
            assert len(worker.outbox_poll_order) == len(set(worker.outbox_poll_order)) == len(active)
            assert len(gateway.ack_receipts) == 28
            assert all(len(worker.outbox.list_notifications(sid)["notifications"]) == 1 for sid in active)
            assert worker.status == "READY" and worker.last_error is None
        finally:
            await worker.shutdown()

    asyncio.run(scenario())


def test_one_periodic_batch_yields_to_job_dispatch_without_waiting_for_full_cycle(polling_setup):
    settings, gateway = polling_setup
    sessions = seed_sessions(settings, gateway, 7)
    with TestClient(create_app(settings, gateway=gateway)) as client:
        response = optimize(client, sessions[0])
        assert response.status_code == 202
        job_id = response.json()["data"]["job_id"]

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            worker.next_outbox_poll = 0
            assert await worker.run_once() is True
            assert len(gateway.read_batches) == 1 and len(gateway.read_batches[0]) == 1
            assert worker.jobs.job(sessions[0], job_id)["queue_state"] == "DONE"
            assert worker.status == "READY"
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert len(gateway.computes) == 1


@pytest.mark.parametrize("failure", ["read", "ack", "metadata"])
def test_periodic_failure_blocks_dispatch_and_all_ready_sessions_without_losing_pending_evidence(polling_setup, failure):
    settings, gateway = polling_setup
    sessions = seed_sessions(settings, gateway, 4)
    sid = sorted(sessions)[0]
    before = deepcopy(gateway.views)

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            gateway.events[sid] = [notification(sid)]
            if failure == "read":
                gateway.read_error = "RUNTIME_TIMEOUT"
            elif failure == "ack":
                gateway.ack_error = "OUTBOX_ACK_INVALID"
            else:
                with sqlite3.connect(settings.metadata_path) as db:
                    db.execute("CREATE TRIGGER reject_periodic_notification BEFORE INSERT ON notifications BEGIN SELECT RAISE(ABORT, 'injected'); END")
            worker.next_outbox_poll = 0
            assert await worker.run_once() is False
            expected = {"read": "RUNTIME_TIMEOUT", "ack": "OUTBOX_ACK_INVALID", "metadata": "OUTBOX_METADATA_UNAVAILABLE"}[failure]
            assert worker.status == "DEGRADED" and worker.last_error == expected and worker.active_outbox is None
            assert all(recovery_row(settings, session)["status"] == "BLOCKED" for session in sessions)
            assert all(any(row["operation"] == "OUTBOX_FAILED" and row["diagnostic_code"] == expected
                           for row in recovery_audit(settings, session)) for session in sessions)
            assert len(worker.outbox.pending(sid)) == (1 if failure == "ack" else 0)
            count = len(gateway.read_batches)
            assert await worker.run_once() is False and len(gateway.read_batches) == count
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.views == before and gateway.computes == []


def test_restart_retries_ambiguous_periodic_ack_with_original_committed_command(polling_setup):
    settings, gateway = polling_setup
    sid = seed_sessions(settings, gateway, 1)[0]

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        gateway.events[sid] = [notification(sid)]
        gateway.fail_ack_after_commit = True
        try:
            worker.next_outbox_poll = 0
            await worker.run_once()
            assert worker.status == "DEGRADED" and len(worker.outbox.pending(sid)) == 1
            original = gateway.ack_batches[0]
        finally:
            await worker.shutdown()
        restarted = ComputeWorker(settings, gateway=gateway)
        await restarted.startup()
        try:
            assert gateway.ack_batches == [original, original]
            assert restarted.outbox.pending(sid) == [] and len(gateway.ack_receipts) == 1
            assert restarted.status == "READY"
        finally:
            await restarted.shutdown()

    asyncio.run(scenario())


async def cancel_twice_while_owned(worker, contender, task, reached, active_attribute):
    await asyncio.wait_for(reached.wait(), 3)
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done() and worker.lock.handle is not None
        assert getattr(worker, active_attribute) is not None and not getattr(worker, active_attribute).done()
        with pytest.raises(RuntimeError, match="WORKER_ALREADY_RUNNING"):
            await contender.startup()


@pytest.mark.parametrize("phase", ["startup_ack", "periodic_read", "periodic_ack"])
def test_repeated_cancel_drains_whole_outbox_operation_and_receipts_before_singleton_release(polling_setup, phase):
    settings, gateway = polling_setup
    sid = seed_sessions(settings, gateway, 1, initial_events=phase == "startup_ack")[0]
    before = deepcopy(gateway.views[sid])

    async def scenario():
        worker, contender = ComputeWorker(settings, gateway=gateway), ComputeWorker(settings, gateway=gateway)
        if phase != "startup_ack":
            await worker.startup()
            gateway.events[sid] = [notification(sid)]
        if phase == "periodic_read":
            gateway.block_read = True
            reached, release = gateway.read_started, gateway.release_read
        else:
            gateway.block_ack = True
            reached, release = gateway.ack_started, gateway.release_ack

        async def operation():
            try:
                if phase == "startup_ack":
                    await worker.startup()
                else:
                    worker.next_outbox_poll = 0
                    await worker.run_once()
            finally:
                await worker.shutdown()

        task = asyncio.create_task(operation())
        try:
            await cancel_twice_while_owned(worker, contender, task, reached, "active_outbox")
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert worker.lock.handle is None and worker.active_outbox is None
            assert worker.outbox.pending(sid) == [] and len(gateway.ack_receipts) == 1
            assert worker.outbox.list_notifications(sid)["notifications"][0]["status"] == "ACKNOWLEDGED"
            await contender.startup()
            assert contender.status == "READY"
        finally:
            release.set()
            if not task.done():
                try:
                    await asyncio.wait_for(task, 3)
                except asyncio.CancelledError:
                    pass
            await worker.shutdown()
            await contender.shutdown()

    asyncio.run(scenario())
    assert gateway.views[sid] == before and gateway.computes == []


@pytest.mark.parametrize("phase", ["startup", "periodic"])
def test_repeated_cancel_drains_original_pending_reconcile_and_durable_mapping(polling_setup, phase):
    settings, gateway = polling_setup
    sid = seed_sessions(settings, gateway, 1)[0]
    before = deepcopy(gateway.views[sid])

    async def scenario():
        worker, contender = ComputeWorker(settings, gateway=gateway), ComputeWorker(settings, gateway=gateway)
        if phase == "periodic":
            await worker.startup()
        with TestClient(create_app(settings, gateway=gateway)) as client:
            gateway.fail_submit_after_commit = True
            code(optimize(client, sid), 503, "RUNTIME_TIMEOUT")
        original = deepcopy(gateway.submissions[0])
        gateway.submit_started.clear()
        gateway.block_submit = True

        async def operation():
            try:
                if phase == "startup":
                    await worker.startup()
                else:
                    await worker.run_once()
            finally:
                await worker.shutdown()

        task = asyncio.create_task(operation())
        try:
            await cancel_twice_while_owned(worker, contender, task, gateway.submit_started, "active_reconciliation")
            gateway.release_submit.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert gateway.submissions == [original, original]
            assert worker.jobs.pending_requests() == [] and worker.active_reconciliation is None
            assert worker.lock.handle is None and gateway.computes == []
            await contender.startup()
            assert contender.status == "READY"
        finally:
            gateway.release_submit.set()
            if not task.done():
                try:
                    await asyncio.wait_for(task, 3)
                except asyncio.CancelledError:
                    pass
            await worker.shutdown()
            await contender.shutdown()

    asyncio.run(scenario())
    assert gateway.views[sid] == before


def test_repeated_cancel_drains_stale_job_public_cancel_before_releasing_singleton(polling_setup):
    settings, gateway = polling_setup
    sid = seed_sessions(settings, gateway, 1)[0]
    with TestClient(create_app(settings, gateway=gateway)) as client:
        job_id = optimize(client, sid).json()["data"]["job_id"]
    original_row = repository(settings).job(sid, job_id)
    before = deepcopy(gateway.views[sid])
    gateway.compute_mode, gateway.block_cancel = "stale", True

    async def scenario():
        worker, contender = ComputeWorker(settings, gateway=gateway), ComputeWorker(settings, gateway=gateway)
        await worker.startup()

        async def dispatch():
            try:
                await worker.run_once()
            finally:
                await worker.shutdown()

        task = asyncio.create_task(dispatch())
        try:
            await cancel_twice_while_owned(worker, contender, task, gateway.cancel_started, "active_cancel")
            gateway.release_cancel.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert worker.active_cancel is None and worker.lock.handle is None
            assert gateway.cancellations == [(sid, job_id, original_row["stale_cancel_command_id"])]
            assert gateway.jobs[(sid, job_id)]["job_status"] == "FAILED"
            await contender.startup()
            assert contender.jobs.job(sid, job_id)["queue_state"] == "DONE"
        finally:
            gateway.release_cancel.set()
            if not task.done():
                try:
                    await asyncio.wait_for(task, 3)
                except asyncio.CancelledError:
                    pass
            await worker.shutdown()
            await contender.shutdown()

    asyncio.run(scenario())
    assert gateway.views[sid] == before and gateway.publish_commits == []
