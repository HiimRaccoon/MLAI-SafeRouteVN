"""Durable worker coordination against a deterministic public-SDK fake.

The fake implements runtime lease fencing. Native witness generation and its
authority store are exercised by the separate integration smoke check.
"""
import asyncio
from copy import deepcopy
from datetime import datetime
import json

from fastapi.testclient import TestClient
import pytest

from backend.api.errors import ApiError
from backend.api.main import create_app
from backend.models.http import CancelJobRequest
from backend.services.auth import Actor
from backend.services.compute_worker import ComputeWorker
from backend.services.heartbeat_io import read_heartbeat_json
from backend.services.job_repository import JobRepository
from backend.tests.test_jobs import JobGateway, optimize, submitted
from backend.tests.test_sessions import code, load, setup


class WorkerGateway(JobGateway):
    def __init__(self, build):
        super().__init__(build)
        self.recoveries, self.publish_attempts, self.publish_commits = [], [], []
        self.compute_mode, self.recover_mode = "complete", "fence"
        self.compute_outcome = "FEASIBLE"
        self.compute_started, self.release_compute = asyncio.Event(), asyncio.Event()
        self.recover_started, self.release_recover = asyncio.Event(), asyncio.Event()
        self.block_compute = False
        self.block_recover = False
        self.capabilities_error = self.recover_error = self.cancel_error = None

    async def capabilities(self):
        if self.capabilities_error:
            raise ApiError(503, self.capabilities_error, "runtime", "Runtime unavailable")
        return await super().capabilities()

    @staticmethod
    def failed(view, code_value):
        view["job_status"] = "FAILED"
        view["diagnostics"] = [{"severity": "ERROR", "code": code_value, "path": "job_id",
                                "message": "Runtime lease cannot publish"}]

    @staticmethod
    def completed(view):
        view.update(job_status="COMPLETED", business_status="FEASIBLE", internal_status="FEASIBLE",
            plan_available=True, coverage_evaluated=True, served_orders=["O1", "O2"],
            validation={"status": "VALIDATED", "valid": True, "validator_version": "TEST_ONLY"})

    async def compute(self, session_id, job_id, command_id, budget_seconds):
        self.computes.append((session_id, job_id, command_id, budget_seconds))
        view = self.jobs[(session_id, job_id)]
        if self.compute_mode == "stale":
            raise ApiError(409, "JOB_STALE", "job_id", "Input basis is no longer current")
        view["job_status"] = "RUNNING"
        self.compute_started.set()
        if self.block_compute:
            await self.release_compute.wait()
        if self.compute_mode in ("RUNTIME_TIMEOUT", "JOB_LIFECYCLE", "STORE_INVALID", "BUILD_FENCED"):
            raise ApiError(503, self.compute_mode, "runtime", "Injected SDK failure")
        if self.compute_mode in ("BUDGET_EXHAUSTED", "WITNESS_INVALID"):
            self.failed(view, self.compute_mode)
            raise ApiError(409, self.compute_mode, "job_id", "Runtime computation was rejected")
        if self.compute_mode == "nonterminal":
            return deepcopy(view)
        self.publish_attempts.append((session_id, job_id))
        if view["job_status"] == "RUNNING":
            if self.compute_outcome == "SEARCH_LIMIT":
                view.update(job_status="COMPLETED", business_status="SEARCH_LIMIT", internal_status="SEARCH_LIMIT")
                view["diagnostics"] = [{"severity": "WARNING", "code": "SEARCH_LIMIT", "path": "job_id",
                                        "message": "No certified witness found"}]
            else:
                self.completed(view)
            self.publish_commits.append((session_id, job_id))
        result = deepcopy(view)
        if self.compute_mode == "wrong_basis":
            result["input_basis"]["head_version"] = "999"
        return result

    async def recover(self, session_id, command_id):
        self.recoveries.append((session_id, command_id))
        self.recover_started.set()
        if self.block_recover:
            await self.release_recover.wait()
        if self.recover_error:
            raise ApiError(503, self.recover_error, "runtime", "Recovery failed")
        fenced = []
        if self.recover_mode == "fence":
            for (owner, job_id), view in self.jobs.items():
                if owner == session_id and view["job_status"] == "RUNNING":
                    self.failed(view, "WORKER_RECOVERED_FENCED")
                    fenced.append(job_id)
        return {"status": "RECOVERED", "fenced_jobs": fenced,
                "basis": deepcopy(self.views[session_id]["basis"])}

    async def cancel(self, session_id, job_id, command_id):
        if self.cancel_error:
            self.cancellations.append((session_id, job_id, command_id))
            raise ApiError(503, self.cancel_error, "runtime", "Cancellation failed")
        return await super().cancel(session_id, job_id, command_id)


@pytest.fixture
def worker_setup(setup):
    settings, original = setup
    return settings, WorkerGateway(original.build)


def seed_job(settings, gateway, request_id="optimize-1"):
    with TestClient(create_app(settings, gateway=gateway)) as client:
        return submitted(client, request_id=request_id)


def repository(settings):
    repo = JobRepository(settings.metadata_path)
    repo.initialize()
    return repo


def heartbeat(settings):
    return read_heartbeat_json(settings.heartbeat_path)


def test_startup_and_shutdown_publish_bound_heartbeat_and_release_lock(worker_setup):
    settings, gateway = worker_setup

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            ready = heartbeat(settings)
            assert worker.started and ready["status"] == "READY"
            assert ready["worker_id"] == worker.worker_id and ready["current_job_id"] is None
            assert ready["installation_sha256"] == settings.installation_identity()
            assert ready["build_sha256"] == gateway.build
            assert await worker.run_once() is False
        finally:
            await worker.shutdown()
        assert heartbeat(settings)["status"] == "STOPPED" and worker.lock.handle is None
        assert not worker.started
        restarted = ComputeWorker(settings, gateway=gateway)
        await restarted.startup()
        await restarted.shutdown()

    asyncio.run(scenario())


def test_run_once_requires_startup_and_singleton_lock(worker_setup):
    settings, gateway = worker_setup
    worker = ComputeWorker(settings, gateway=gateway)
    with pytest.raises(RuntimeError, match="startup/singleton"):
        asyncio.run(worker.run_once())
    assert gateway.computes == [] and gateway.recoveries == []


def test_successful_compute_is_terminal_once_and_preserves_physical_head(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    before = deepcopy(gateway.views[session_id])
    original = repository(settings).job(session_id, data["job_id"])

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert await worker.run_once() is True
            row = worker.jobs.job(session_id, data["job_id"])
            assert row["queue_state"] == "DONE" and row["claim_token"] is None
            assert await worker.run_once() is False
            assert worker.status == "READY" and worker.current_job is None
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.computes == [(session_id, data["job_id"], original["compute_command_id"], original["budget_seconds"])]
    assert gateway.jobs[(session_id, data["job_id"])]["job_status"] == "COMPLETED"
    assert gateway.views[session_id] == before and before["active_job_id"] is None


def test_restart_recovers_m3_claim_before_native_claim_and_requeues(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    crashed_claim = repository(settings).claim_job("crashed-worker")
    assert crashed_claim["queue_state"] == "RUNNING"

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.jobs.job(session_id, data["job_id"])["queue_state"] == "QUEUED"
            assert len(gateway.recoveries) == 1 and gateway.computes == []
            assert await worker.run_once() is True
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.computes[0][2] == crashed_claim["compute_command_id"]
    assert repository(settings).job(session_id, data["job_id"])["queue_state"] == "DONE"


def test_restart_fences_orphan_runtime_running_without_recomputing(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    repository(settings).claim_job("crashed-worker")
    gateway.jobs[(session_id, data["job_id"])]["job_status"] = "RUNNING"
    before = deepcopy(gateway.views[session_id])

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            view = gateway.jobs[(session_id, data["job_id"])]
            assert view["job_status"] == "FAILED"
            assert view["diagnostics"][0]["code"] == "WORKER_RECOVERED_FENCED"
            assert worker.jobs.job(session_id, data["job_id"])["queue_state"] == "DONE"
            assert await worker.run_once() is False
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.computes == [] and gateway.views[session_id] == before


@pytest.mark.parametrize("terminal", ["COMPLETED", "FAILED"])
def test_restart_retains_runtime_terminal_result_after_queue_claim(worker_setup, terminal):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    repository(settings).claim_job("crashed-worker")
    view = gateway.jobs[(session_id, data["job_id"])]
    if terminal == "COMPLETED":
        gateway.completed(view)
    else:
        gateway.failed(view, "BUDGET_EXHAUSTED")
    before = deepcopy(view)

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.jobs.job(session_id, data["job_id"])["queue_state"] == "DONE"
            assert await worker.run_once() is False
            assert view == before
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.computes == []


def test_runtime_running_discovered_at_dispatch_is_fenced_before_compute(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    gateway.jobs[(session_id, data["job_id"])]["job_status"] = "RUNNING"

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert await worker.run_once() is True
            assert worker.jobs.job(session_id, data["job_id"])["queue_state"] == "DONE"
            assert gateway.jobs[(session_id, data["job_id"])]["job_status"] == "FAILED"
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert len(gateway.recoveries) == 1 and gateway.computes == []


def test_cancel_during_compute_prevents_late_publish_and_queue_overwrite(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    gateway.block_compute = True
    before = deepcopy(gateway.views[session_id])

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        task = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(gateway.compute_started.wait(), 3)
            receipt = await worker.job_service.cancel(session_id, data["job_id"],
                CancelJobRequest(request_id="cancel-during-compute"), Actor("alice", "dispatcher"))
            assert receipt["status"] == "JOB_CANCELLED"
            assert worker.jobs.job(session_id, data["job_id"])["queue_state"] == "DONE"
            gateway.release_compute.set()
            assert await asyncio.wait_for(task, 3) is True
            assert worker.jobs.job(session_id, data["job_id"])["queue_state"] == "DONE"
            assert gateway.jobs[(session_id, data["job_id"])]["job_status"] == "FAILED"
        finally:
            gateway.release_compute.set()
            await task
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.publish_attempts and gateway.publish_commits == []
    assert gateway.views[session_id] == before


def test_heartbeat_remains_fresh_while_compute_awaits(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    gateway.block_compute = True

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        old_time = datetime.fromisoformat(heartbeat(settings)["updated_at"])
        task = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(gateway.compute_started.wait(), 3)
            await asyncio.sleep(2.2)
            current = heartbeat(settings)
            assert datetime.fromisoformat(current["updated_at"]) > old_time
            assert current["status"] == "READY" and current["current_job_id"] == data["job_id"]
            assert current["installation_sha256"] == settings.installation_identity()
            assert not task.done()
        finally:
            gateway.release_compute.set()
            await task
            await worker.shutdown()

    asyncio.run(scenario())


def test_second_worker_cannot_recover_claim_or_replace_heartbeat(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)

    async def scenario():
        first = ComputeWorker(settings, gateway=gateway)
        second = ComputeWorker(settings, gateway=gateway)
        await first.startup()
        try:
            before = len(gateway.recoveries)
            with pytest.raises(RuntimeError, match="WORKER_ALREADY_RUNNING"):
                await second.startup()
            assert len(gateway.recoveries) == before and gateway.computes == []
            assert heartbeat(settings)["worker_id"] == first.worker_id
            assert first.jobs.job(session_id, data["job_id"])["queue_state"] == "QUEUED"
            assert second.lock.handle is None
        finally:
            await second.shutdown()
            await first.shutdown()

    asyncio.run(scenario())


def test_stale_job_uses_persisted_public_cancel_and_is_never_retried(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    original = repository(settings).job(session_id, data["job_id"])
    gateway.compute_mode = "stale"
    before = deepcopy(gateway.views[session_id])

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert await worker.run_once() is True
            row = worker.jobs.job(session_id, data["job_id"])
            assert row["queue_state"] == "DONE" and row["error_code"] == "JOB_STALE"
            assert await worker.run_once() is False and worker.status == "READY"
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.cancellations == [(session_id, data["job_id"], original["stale_cancel_command_id"])]
    assert len(gateway.computes) == 1 and gateway.views[session_id] == before


@pytest.mark.parametrize("failure", ["RUNTIME_TIMEOUT", "JOB_LIFECYCLE"])
def test_transport_or_lifecycle_failure_recovers_to_terminal(worker_setup, failure):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    gateway.compute_mode = failure

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert await worker.run_once() is True
            row = worker.jobs.job(session_id, data["job_id"])
            assert row["queue_state"] == "DONE" and row["error_code"] == failure
            assert gateway.jobs[(session_id, data["job_id"])]["job_status"] == "FAILED"
            assert worker.status == "READY" and await worker.run_once() is False
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert len(gateway.recoveries) == 2 and len(gateway.computes) == 1  # startup validation recovery, then failed compute recovery


@pytest.mark.parametrize("failure", ["STORE_INVALID", "BUILD_FENCED", "nonterminal"])
def test_invalid_or_nonterminal_runtime_output_quarantines_without_retry(worker_setup, failure):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    gateway.compute_mode = failure
    expected = "COMPUTE_NOT_TERMINAL" if failure == "nonterminal" else failure

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert await worker.run_once() is True
            row = worker.jobs.job(session_id, data["job_id"])
            assert row["queue_state"] == "QUARANTINED" and row["error_code"] == expected
            assert worker.status == "DEGRADED" and worker.last_error == expected
            assert await worker.run_once() is False
            assert heartbeat(settings)["status"] == "DEGRADED"
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert len(gateway.computes) == 1


def test_timeout_with_nonterminal_recovery_is_quarantined(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    gateway.compute_mode, gateway.recover_mode = "RUNTIME_TIMEOUT", "preserve"

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert await worker.run_once() is True
            row = worker.jobs.job(session_id, data["job_id"])
            assert row["queue_state"] == "QUARANTINED" and worker.status == "DEGRADED"
            assert await worker.run_once() is False
        finally:
            await worker.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["BUDGET_EXHAUSTED", "WITNESS_INVALID"])
def test_terminal_runtime_failure_finishes_queue_without_degrading_worker(worker_setup, failure):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    gateway.compute_mode = failure
    before = deepcopy(gateway.views[session_id])

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert await worker.run_once() is True
            row = worker.jobs.job(session_id, data["job_id"])
            assert row["queue_state"] == "DONE" and row["error_code"] == failure
            view = gateway.jobs[(session_id, data["job_id"])]
            assert view["job_status"] == "FAILED" and view["diagnostics"][0]["code"] == failure
            assert worker.status == "READY" and await worker.run_once() is False
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert len(gateway.recoveries) == 1 and gateway.views[session_id] == before  # startup recovery preserves physical head


def test_search_limit_is_terminal_without_inventing_delivery_or_order_coverage(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    gateway.compute_outcome = "SEARCH_LIMIT"
    before = deepcopy(gateway.views[session_id])

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert await worker.run_once() is True
            view = gateway.jobs[(session_id, data["job_id"])]
            assert view["job_status"] == "COMPLETED" and view["business_status"] == "SEARCH_LIMIT"
            assert view["validation"]["valid"] is None and view["plan_available"] is False
            assert not view["coverage_evaluated"] and view["served_orders"] == view["unserved_orders"] == []
            assert worker.jobs.job(session_id, data["job_id"])["queue_state"] == "DONE"
            assert worker.status == "READY"
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.views[session_id] == before


def test_compute_projection_with_different_basis_is_quarantined(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    gateway.compute_mode = "wrong_basis"

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert await worker.run_once() is True
            row = worker.jobs.job(session_id, data["job_id"])
            assert row["queue_state"] == "QUARANTINED" and row["error_code"] == "JOB_BINDING_CHANGED"
            assert worker.status == "DEGRADED" and await worker.run_once() is False
        finally:
            await worker.shutdown()

    asyncio.run(scenario())


def test_quarantine_survives_restart_and_blocks_further_dispatch(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    claim = repository(settings).claim_job("crashed-worker")
    repository(settings).finish_job(claim, error_code="STORE_INVALID", quarantine=True)

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.status == "DEGRADED" and worker.last_error == "STORE_INVALID"
            assert await worker.run_once() is False
            assert worker.jobs.job(session_id, data["job_id"])["queue_state"] == "QUARANTINED"
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.computes == []


def test_uncertain_submit_is_reconciled_before_dispatch_with_original_basis(worker_setup):
    settings, gateway = worker_setup
    with TestClient(create_app(settings, gateway=gateway)) as client:
        session_id = load(client).json()["data"]["session"]["session_id"]
        gateway.fail_submit_after_commit = True
        code(optimize(client, session_id), 503, "RUNTIME_TIMEOUT")
    original = deepcopy(gateway.submissions[0])
    reads = len(gateway.reads)
    gateway.views[session_id]["basis"]["head_version"] = "2"

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.jobs.pending_requests() == []
            job_id = next(iter(gateway.jobs))[1]
            row = worker.jobs.job(session_id, job_id)
            assert row["queue_state"] == "QUEUED" and row["basis"]["head_version"] == "1"
            assert gateway.computes == []
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.submissions == [original, original] and len(gateway.jobs) == 1
    assert len(gateway.reads) == reads + 1  # one public startup inspection, no fresh submit basis


def test_startup_capability_failure_releases_lock_and_never_reports_ready(worker_setup):
    settings, gateway = worker_setup
    gateway.capabilities_error = "STORE_INVALID"

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        with pytest.raises(ApiError) as caught:
            await worker.startup()
        assert caught.value.code == "STORE_INVALID"
        assert not worker.started and worker.lock.handle is None
        assert heartbeat(settings)["status"] == "STOPPED"
        gateway.capabilities_error = None
        restarted = ComputeWorker(settings, gateway=gateway)
        await restarted.startup()
        await restarted.shutdown()

    asyncio.run(scenario())


def test_heartbeat_io_failure_stops_dispatch_and_shutdown_always_releases_lock(worker_setup, monkeypatch):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)

    def failed_write():
        raise OSError("Injected heartbeat I/O failure")

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            monkeypatch.setattr(worker, "write_heartbeat", failed_write)
            await asyncio.wait_for(worker.heartbeat_task, 3)
            assert worker.status == "DEGRADED" and worker.last_error == "HEARTBEAT_UNAVAILABLE"
            assert await worker.run_once() is False
            assert worker.jobs.job(session_id, data["job_id"])["queue_state"] == "QUEUED"
        finally:
            await worker.shutdown()
        assert worker.lock.handle is None and not worker.started
        restarted = ComputeWorker(settings, gateway=gateway)
        await restarted.startup()
        await restarted.shutdown()

    asyncio.run(scenario())
    assert gateway.computes == []


def test_installation_change_marks_heartbeat_degraded_and_blocks_dispatch(worker_setup):
    settings, gateway = worker_setup
    seed_job(settings, gateway)

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            pinned = heartbeat(settings)["installation_sha256"]
            changed = json.loads(settings.installation_path.read_text())
            changed["authority_store_parent"] += "-changed"
            settings.installation_path.write_text(json.dumps(changed), encoding="utf-8")
            await asyncio.to_thread(worker.write_heartbeat)
            value = heartbeat(settings)
            assert value["status"] == "DEGRADED" and value["last_error_code"] == "INSTALLATION_BINDING_CHANGED"
            assert value["installation_sha256"] == pinned
            assert await worker.run_once() is False
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.computes == []


@pytest.mark.parametrize("failure", ["recover", "stale_cancel"])
def test_secondary_recovery_failure_fails_closed_without_ready_dispatch(worker_setup, failure):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    if failure == "recover":
        gateway.compute_mode = "RUNTIME_TIMEOUT"
    else:
        gateway.compute_mode, gateway.cancel_error = "stale", "RUNTIME_TIMEOUT"

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        if failure == "recover":
            gateway.recover_error = "STORE_INVALID"
        try:
            try:
                await worker.run_once()
            except ApiError:
                # Propagation is safe only when worker dispatch/readiness is fenced.
                pass
            assert worker.status in ("DEGRADED", "STOPPED")
            assert heartbeat(settings)["status"] in ("DEGRADED", "STOPPED")
            assert worker.jobs.job(session_id, data["job_id"])["queue_state"] == "QUARANTINED"
            assert await worker.run_once() is False
        finally:
            await worker.shutdown()

    asyncio.run(scenario())


def test_startup_cancellation_holds_singleton_until_inflight_recovery_finishes(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    repository(settings).claim_job("crashed-worker")
    gateway.jobs[(session_id, data["job_id"])]["job_status"] = "RUNNING"
    gateway.block_recover = True

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        contender = ComputeWorker(settings, gateway=gateway)
        task = asyncio.create_task(worker.startup())
        try:
            await asyncio.wait_for(gateway.recover_started.wait(), 3)
            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done(), "Recovery must finish before cancelled startup releases its lock"
            with pytest.raises(RuntimeError, match="WORKER_ALREADY_RUNNING"):
                await contender.startup()
            assert len(gateway.recoveries) == 1
            assert gateway.jobs[(session_id, data["job_id"])]["job_status"] == "RUNNING"
            gateway.release_recover.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert worker.lock.handle is None and not worker.started
            assert heartbeat(settings)["status"] == "STOPPED"
            assert gateway.jobs[(session_id, data["job_id"])]["job_status"] == "FAILED"
            await contender.startup()
            assert contender.jobs.job(session_id, data["job_id"])["queue_state"] == "DONE"
        finally:
            gateway.release_recover.set()
            if not task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await worker.shutdown()
            await contender.shutdown()

    asyncio.run(scenario())
    assert gateway.computes == []


@pytest.mark.parametrize("recovery_phase", ["orphan_at_dispatch", "timeout_failure"])
def test_dispatch_cancellation_holds_singleton_until_recovery_finishes(worker_setup, recovery_phase):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    before = deepcopy(gateway.views[session_id])

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        contender = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        if recovery_phase == "orphan_at_dispatch":
            gateway.jobs[(session_id, data["job_id"])]["job_status"] = "RUNNING"
        else:
            gateway.compute_mode = "RUNTIME_TIMEOUT"
        gateway.recover_started.clear()  # startup now also performs a public recovery inspection
        gateway.block_recover = True

        async def dispatch():
            try:
                return await worker.run_once()
            finally:
                await worker.shutdown()

        task = asyncio.create_task(dispatch())
        try:
            await asyncio.wait_for(gateway.recover_started.wait(), 3)
            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done(), "A cancelled dispatch must retain ownership until recovery finishes"
            with pytest.raises(RuntimeError, match="WORKER_ALREADY_RUNNING"):
                await contender.startup()
            assert len(gateway.recoveries) == 2
            assert gateway.jobs[(session_id, data["job_id"])]["job_status"] == "RUNNING"
            gateway.release_recover.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert worker.lock.handle is None and not worker.started
            assert gateway.jobs[(session_id, data["job_id"])]["job_status"] == "FAILED"
            await contender.startup()
            assert contender.jobs.job(session_id, data["job_id"])["queue_state"] == "DONE"
        finally:
            gateway.release_recover.set()
            if not task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await worker.shutdown()
            await contender.shutdown()

    asyncio.run(scenario())
    assert gateway.views[session_id] == before
    assert len(gateway.computes) == (1 if recovery_phase == "timeout_failure" else 0)
