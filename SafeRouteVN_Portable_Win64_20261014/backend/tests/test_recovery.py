"""Startup authority recovery gates and honest public validation evidence."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from backend.api.errors import ApiError
from backend.api.main import create_app
from backend.services.compute_worker import ComputeWorker
from backend.services.recovery_repository import RecoveryRepository
from backend.services.runtime_gateway import RuntimeGateway
from backend.tests.test_jobs import optimize
from backend.tests.test_outbox import notification
from backend.tests.test_sessions import code, headers, load, setup
from backend.tests.test_worker import WorkerGateway, heartbeat, repository, seed_job, worker_setup


def recovery_row(settings, session_id):
    with sqlite3.connect(settings.metadata_path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM session_recovery WHERE session_id=?", (session_id,)).fetchone())


def recovery_audit(settings, session_id):
    with sqlite3.connect(settings.metadata_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM recovery_audit WHERE session_id=? ORDER BY rowid", (session_id,))]


def test_all_ready_sessions_are_recovered_and_validated_before_pending_submit_reconciliation(worker_setup):
    settings, gateway = worker_setup
    with TestClient(create_app(settings, gateway=gateway)) as client:
        first = load(client).json()["data"]["session"]["session_id"]
        second = load(client, "S1", "load-second").json()["data"]["session"]["session_id"]
        gateway.fail_submit_after_commit = True
        code(optimize(client, first), 503, "RUNTIME_TIMEOUT")
    original_submit = deepcopy(gateway.submissions[0])
    before = deepcopy(gateway.views)
    original_validation, original_submit_method = gateway.validate_session, gateway.submit
    validated, inspected_commands = [], []

    async def validation(session_id):
        validated.append(session_id)
        return await original_validation(session_id)

    async def submit(session_id, command_id, basis, profile, budget_seconds):
        assert set(validated) == {first, second}
        assert all(recovery_row(settings, sid)["status"] == "VALIDATED" for sid in (first, second))
        inspected_commands.append((session_id, command_id, deepcopy(basis), profile, budget_seconds))
        return await original_submit_method(session_id, command_id, basis, profile, budget_seconds)

    gateway.validate_session, gateway.submit = validation, submit

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.status == "READY"
            assert worker.jobs.pending_requests() == []
            assert gateway.computes == []
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert inspected_commands == [original_submit]
    assert {sid for sid, _ in gateway.recoveries} == {first, second}
    assert gateway.views == before
    for sid in (first, second):
        row = recovery_row(settings, sid)
        assert row["status"] == "VERIFIED" and row["diagnostic_code"] is None
        assert json.loads(row["basis_json"]) == before[sid]["basis"]
        operations = [record["operation"] for record in recovery_audit(settings, sid)]
        assert operations == ["RECOVERY_STARTED", "RECOVER_VALIDATE"]


@pytest.mark.parametrize("change", [
    {"valid": False}, {"current_checker_build_sha256": "f" * 64},
    {"validator_version": "untrusted-validator"}, {"checked_physical_mutations": True},
])
def test_invalid_public_validation_degrades_and_audits_without_dispatch_or_queue_rewrite(worker_setup, change):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    before_view = deepcopy(gateway.views[session_id])
    before_queue = repository(settings).job(session_id, data["job_id"])
    original_validation = gateway.validate_session

    async def invalid(session_id):
        return {**await original_validation(session_id), **change}

    gateway.validate_session = invalid

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.status == "DEGRADED" and worker.last_error == "SESSION_VALIDATION_INVALID"
            assert heartbeat(settings)["status"] == "DEGRADED"
            assert await worker.run_once() is False
            with pytest.raises(ApiError) as caught:
                worker.recovery.assert_mutation_allowed(session_id)
            assert caught.value.code == "SESSION_VALIDATION_INVALID"
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.computes == [] and gateway.views[session_id] == before_view
    assert repository(settings).job(session_id, data["job_id"]) == before_queue
    row = recovery_row(settings, session_id)
    assert row["status"] == "BLOCKED" and row["validation_json"] is None
    failures = [record for record in recovery_audit(settings, session_id) if record["operation"] == "RECOVERY_FAILED"]
    assert len(failures) == 1 and failures[0]["diagnostic_code"] == "SESSION_VALIDATION_INVALID"


@pytest.mark.parametrize("diagnostic", ["STORE_INVALID", "SOURCE_CHANGED", "JOURNAL_HASH_MISMATCH"])
def test_inspection_integrity_failure_preserves_bound_source_and_durable_queue(worker_setup, diagnostic):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    before_queue = repository(settings).job(session_id, data["job_id"])
    before_view = deepcopy(gateway.views[session_id])
    source = settings.project_root / "scenarios/fixtures/thu-duc-binh-thanh-v1/S0.json"
    before_source = source.read_bytes()

    async def corrupt(session_ids, command_id):
        assert session_ids == [session_id]
        return [{"session_id": session_id, "diagnostic_code": diagnostic}]

    gateway.inspect_sessions = corrupt

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.status == "DEGRADED" and worker.last_error == diagnostic
            assert await worker.run_once() is False
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.computes == [] and gateway.views[session_id] == before_view
    assert repository(settings).job(session_id, data["job_id"]) == before_queue
    assert source.read_bytes() == before_source
    row = recovery_row(settings, session_id)
    assert row["status"] == "BLOCKED" and row["diagnostic_code"] == diagnostic
    assert any(record["operation"] == "RECOVERY_FAILED" and record["diagnostic_code"] == diagnostic
               for record in recovery_audit(settings, session_id))


@pytest.mark.parametrize("malformation", [
    "missing_execution_field", "recovery_basis_mismatch", "private_recovery_field",
    "private_inspection_field", "private_execution_field",
])
def test_malformed_successful_inspection_never_verifies_or_persists_public_evidence(worker_setup, malformation):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    before_view = deepcopy(gateway.views[session_id])
    before_queue = repository(settings).job(session_id, data["job_id"])
    with sqlite3.connect(settings.metadata_path) as db:
        evidence_before = db.execute("SELECT COUNT(*) FROM evidence_observations WHERE session_id=?", (session_id,)).fetchone()[0]
    original = gateway.inspect_sessions

    async def malformed(session_ids, command_id):
        result = await original(session_ids, command_id)
        row = result[0]
        if malformation == "missing_execution_field":
            row["execution_view"].pop("vehicles")
        elif malformation == "recovery_basis_mismatch":
            row["recovery"]["basis"]["head_version"] = "999"
        elif malformation == "private_recovery_field":
            row["recovery"]["authority_store"] = "private-store-secret"
        elif malformation == "private_inspection_field":
            row["installation_token"] = "private-token-secret"
        else:
            row["execution_view"]["raw_journal"] = {"token": "private-journal-secret"}
        return result

    gateway.inspect_sessions = malformed

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.status == "DEGRADED" and worker.last_error == "SESSION_RECOVERY_INVALID"
            assert await worker.run_once() is False
            with pytest.raises(ApiError):
                worker.recovery.assert_mutation_allowed(session_id)
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    row = recovery_row(settings, session_id)
    assert row["status"] == "BLOCKED" and row["validation_json"] is None and row["basis_json"] is None
    assert gateway.computes == [] and gateway.views[session_id] == before_view
    assert repository(settings).job(session_id, data["job_id"]) == before_queue
    audited = recovery_audit(settings, session_id)
    assert [record["operation"] for record in audited] == ["RECOVERY_STARTED", "RECOVERY_FAILED"]
    assert "private-" not in json.dumps(audited)
    with sqlite3.connect(settings.metadata_path) as db:
        evidence_after = db.execute("SELECT COUNT(*) FROM evidence_observations WHERE session_id=?", (session_id,)).fetchone()[0]
    assert evidence_after == evidence_before


@pytest.mark.parametrize("operation", ["optimize", "accept", "advance"])
def test_newly_blocked_pending_reconciliation_never_reclaims_or_invokes_sdk(worker_setup, monkeypatch, operation):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    with TestClient(create_app(settings, gateway=gateway)) as client:
        recovery = client.app.state.recovery_repository
        recovery.record(session_id, "VERIFIED", "worker", settings.installation_identity(),
                        basis=gateway.views[session_id]["basis"])
        basis, digest = deepcopy(gateway.views[session_id]["basis"]), "d" * 64
        if operation == "optimize":
            repo, service, table, sdk_method = client.app.state.job_repository, client.app.state.job_service, "job_requests", "submit"
            row = repo.reserve_request("alice", session_id, operation, "pending-blocked", digest, settings.installation_identity(),
                basis=basis, profile="BALANCED", budget_seconds=settings.compute_budget_seconds)
        elif operation == "accept":
            repo, service, table, sdk_method = client.app.state.plan_repository, client.app.state.plan_service, "accept_requests", "accept"
            row = repo.reserve_request("alice", session_id, "pending-blocked", digest, settings.installation_identity(),
                basis=basis, job_id=data["job_id"])
        else:
            repo, service, table, sdk_method = client.app.state.replay_repository, client.app.state.replay_service, "replay_requests", "advance"
            row = repo.reserve_request("alice", session_id, operation, "pending-blocked", digest, settings.installation_identity(),
                basis=basis, payload={"target_time": "2026-09-27T21:01:00+07:00"})
        repo.release_request(row)
        pending = next(item for item in repo.pending_requests() if item["request_id"] == "pending-blocked")
        recovery.record(session_id, "BLOCKED", "sdk-gateway", settings.installation_identity(), diagnostic="STORE_INVALID")
        with sqlite3.connect(settings.metadata_path) as db:
            stored_before = db.execute(f"SELECT * FROM {table} WHERE session_id=? AND request_id='pending-blocked'", (session_id,)).fetchone()
        sdk_calls = []

        async def forbidden_sdk(*args, **kwargs):
            sdk_calls.append(args)
            raise AssertionError("Blocked pending request invoked the SDK")

        def forbidden_claim(*args, **kwargs):
            raise AssertionError("Blocked pending request acquired a new durable lease")

        monkeypatch.setattr(gateway, sdk_method, forbidden_sdk, raising=False)
        monkeypatch.setattr(repo, "reserve_request", forbidden_claim)
        with pytest.raises(ApiError) as caught:
            asyncio.run(service.reconcile(pending))
        assert caught.value.code == "STORE_INVALID"
        with sqlite3.connect(settings.metadata_path) as db:
            stored_after = db.execute(f"SELECT * FROM {table} WHERE session_id=? AND request_id='pending-blocked'", (session_id,)).fetchone()
        assert stored_after == stored_before and repo.pending_requests() == [pending]
        assert sdk_calls == [] and pending["basis"] == basis


def test_twice_cancelled_startup_retains_singleton_until_sdk_recovery_finishes(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    repository(settings).claim_job("dead-worker")
    gateway.jobs[(session_id, data["job_id"])]["job_status"] = "RUNNING"
    before = deepcopy(gateway.views[session_id])
    gateway.block_recover = True

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        contender = ComputeWorker(settings, gateway=gateway)
        task = asyncio.create_task(worker.startup())
        try:
            await asyncio.wait_for(gateway.recover_started.wait(), 3)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0.03)
                assert not task.done(), "Every cancellation must retain singleton ownership until the SDK task ends"
                assert worker.lock.handle is not None
                with pytest.raises(RuntimeError, match="WORKER_ALREADY_RUNNING"):
                    await contender.startup()
                assert len(gateway.recoveries) == 1
                assert gateway.jobs[(session_id, data["job_id"])]["job_status"] == "RUNNING"
            gateway.release_recover.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert worker.lock.handle is None and not worker.started
            assert gateway.jobs[(session_id, data["job_id"])]["job_status"] == "FAILED"
            assert recovery_row(settings, session_id)["status"] == "BLOCKED"
            assert any(row["operation"] == "STARTUP_FAILED" and row["diagnostic_code"] == "RECOVERY_INTERRUPTED"
                       for row in recovery_audit(settings, session_id))
            await contender.startup()
            assert contender.status == "READY"
            assert contender.jobs.job(session_id, data["job_id"])["queue_state"] == "DONE"
        finally:
            gateway.release_recover.set()
            if not task.done():
                try:
                    await asyncio.wait_for(task, 3)
                except asyncio.CancelledError:
                    pass
            await worker.shutdown()
            await contender.shutdown()

    asyncio.run(scenario())
    assert gateway.views[session_id] == before and gateway.computes == []


def test_source_change_after_catalog_verification_blocks_and_audits_before_recovery(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    before_queue = repository(settings).job(session_id, data["job_id"])
    source = settings.project_root / "scenarios/fixtures/thu-duc-binh-thanh-v1/S0.json"
    original = gateway.capabilities

    async def mutate_source_after_catalog():
        result = await original()
        source.write_bytes(source.read_bytes() + b" ")
        return result

    gateway.capabilities = mutate_source_after_catalog

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        with pytest.raises(ApiError) as caught:
            await worker.startup()
        assert caught.value.code == "SOURCE_CHANGED"
        assert worker.lock.handle is None and not worker.started

    asyncio.run(scenario())
    assert source.read_bytes().endswith(b" ") and gateway.recoveries == gateway.computes == []
    assert repository(settings).job(session_id, data["job_id"]) == before_queue
    row = recovery_row(settings, session_id)
    assert row["status"] == "BLOCKED" and row["diagnostic_code"] == "SOURCE_CHANGED"
    assert any(record["operation"] == "RECOVERY_FAILED" and record["diagnostic_code"] == "SOURCE_CHANGED"
               for record in recovery_audit(settings, session_id))


def test_inspection_timeout_blocks_every_ready_session_without_dispatch(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    with TestClient(create_app(settings, gateway=gateway)) as client:
        other_id = load(client, "S1", "load-other").json()["data"]["session"]["session_id"]
    before = deepcopy(gateway.views)

    async def timeout(session_ids, command_id):
        assert set(session_ids) == {session_id, other_id}
        raise ApiError(503, "RUNTIME_TIMEOUT", "runtime", "Inspection subprocess was interrupted")

    gateway.inspect_sessions = timeout

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.status == "DEGRADED" and await worker.run_once() is False
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.computes == [] and gateway.views == before
    assert repository(settings).job(session_id, data["job_id"])["queue_state"] == "QUEUED"
    for sid in (session_id, other_id):
        assert recovery_row(settings, sid)["diagnostic_code"] == "RUNTIME_TIMEOUT"
        assert any(row["operation"] == "RECOVERY_FAILED" and row["diagnostic_code"] == "RUNTIME_TIMEOUT"
                   for row in recovery_audit(settings, sid))


def test_orphan_fencing_is_validated_and_audited_without_physical_progress(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    repository(settings).claim_job("dead-worker")
    gateway.jobs[(session_id, data["job_id"])]["job_status"] = "RUNNING"
    before_view = deepcopy(gateway.views[session_id])

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.status == "READY"
            assert worker.jobs.job(session_id, data["job_id"])["queue_state"] == "DONE"
            assert await worker.run_once() is False
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    assert gateway.views[session_id] == before_view and gateway.computes == []
    assert gateway.jobs[(session_id, data["job_id"])]["job_status"] == "FAILED"
    receipt = next(row for row in recovery_audit(settings, session_id) if row["operation"] == "RECOVER_VALIDATE")
    result = json.loads(receipt["response_json"])
    assert result["recovery"]["fenced_jobs"] == [data["job_id"]]
    assert result["validation"]["valid"] is True and result["validation"]["checked_physical_mutations"] == 0
    assert result["validation"]["historical_execution_builds_preserved"] is True


@pytest.mark.parametrize("status,diagnostic", [("RECOVERING", None), ("BLOCKED", "STORE_INVALID")])
@pytest.mark.parametrize("operation", ["optimize", "accept", "advance"])
def test_recovery_gate_blocks_http_mutations_before_runtime_reads_and_commands(worker_setup, status, diagnostic, operation):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    recovery = RecoveryRepository(settings.metadata_path)
    recovery.initialize()
    recovery.record(session_id, status, "server-worker", settings.installation_identity(), diagnostic=diagnostic)
    before = deepcopy(gateway.views)
    reads, submits, polls = len(gateway.reads), len(gateway.submissions), len(gateway.polls)
    paths = {"optimize": f"/api/sessions/{session_id}/optimize",
             "accept": f"/api/sessions/{session_id}/jobs/{data['job_id']}/accept",
             "advance": f"/api/sessions/{session_id}/replay/step"}
    body = {"request_id": "blocked-new-request", "expected_revision": {"head_version": "1", "generation": "0"}}
    with TestClient(create_app(settings, gateway=gateway), raise_server_exceptions=False) as client:
        code(client.post(paths[operation], json=body, headers=headers()), 503, diagnostic or "SESSION_RECOVERING")
    assert len(gateway.reads) == reads and len(gateway.submissions) == submits and len(gateway.polls) == polls
    assert gateway.views == before and gateway.computes == []


@pytest.mark.parametrize("operation", ["resolve", "capabilities"])
def test_missing_authority_with_existing_ready_metadata_never_constructs_runtime_store(worker_setup, monkeypatch, operation):
    settings, fake = worker_setup
    session_id, _ = seed_job(settings, fake)
    authority = Path(settings.installation()["authority_store_parent"]) / "backend_authority.sqlite"
    assert not authority.exists()
    called = []

    def forbidden_constructor(*args, **kwargs):
        called.append(args)
        raise AssertionError("Runtime bridge could create a replacement authority store")

    monkeypatch.setattr("backend.services.runtime_gateway.subprocess.run", forbidden_constructor)
    gateway = RuntimeGateway(settings)
    with pytest.raises(ApiError) as caught:
        asyncio.run(gateway.resolve(session_id) if operation == "resolve" else gateway.capabilities())
    assert caught.value.status_code == 503 and caught.value.code == "STORE_MISSING"
    assert called == [] and not authority.exists()


def test_each_restart_repeats_public_recovery_and_records_distinct_honest_audit(worker_setup):
    settings, gateway = worker_setup
    with TestClient(create_app(settings, gateway=gateway)) as client:
        session_id = load(client).json()["data"]["session"]["session_id"]
    before = deepcopy(gateway.views[session_id])

    async def scenario():
        for _ in range(2):
            worker = ComputeWorker(settings, gateway=gateway)
            await worker.startup()
            try:
                assert worker.status == "READY" and worker.recovery.healthy()
            finally:
                await worker.shutdown()

    asyncio.run(scenario())
    assert len(gateway.recoveries) == 2 and len({command for _, command in gateway.recoveries}) == 2
    audited = recovery_audit(settings, session_id)
    assert [row["operation"] for row in audited] == ["RECOVERY_STARTED", "RECOVER_VALIDATE"] * 2
    successes = [row for row in audited if row["operation"] == "RECOVER_VALIDATE"]
    assert len({row["worker_id"] for row in successes}) == 2
    for row in successes:
        result = json.loads(row["response_json"])
        assert result["recovery"]["status"] == "RECOVERED" and result["recovery"]["fenced_jobs"] == []
        assert result["validation"]["valid"] is True
    assert gateway.views[session_id] == before and gateway.computes == []


def test_startup_outbox_database_batch_failure_blocks_verified_session_and_preserves_source(worker_setup):
    settings, gateway = worker_setup
    session_id, data = seed_job(settings, gateway)
    events = [notification(session_id, "first"), notification(session_id, "second")]
    before_view = deepcopy(gateway.views[session_id])
    before_queue = repository(settings).job(session_id, data["job_id"])
    source = settings.project_root / "scenarios/fixtures/thu-duc-binh-thanh-v1/S0.json"
    before_source = source.read_bytes()
    ack_calls = []

    async def notifications(sid):
        assert sid == session_id
        return deepcopy(events)

    async def ack(sid, command_id, event_id):
        ack_calls.append((sid, command_id, event_id))
        return {"status": "ACKNOWLEDGED", "event_id": event_id}

    gateway.read_notifications, gateway.acknowledge = notifications, ack
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("CREATE TRIGGER reject_startup_notification BEFORE INSERT ON notifications "
                   "WHEN NEW.event_id='" + events[1]["event_id"] + "' BEGIN SELECT RAISE(ABORT, 'injected'); END")

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.status == "DEGRADED" and worker.last_error == "OUTBOX_METADATA_UNAVAILABLE"
            assert heartbeat(settings)["status"] == "DEGRADED" and await worker.run_once() is False
            assert worker.outbox.pending(session_id) == []
            assert worker.outbox.list_notifications(session_id)["notifications"] == []
            with pytest.raises(ApiError) as caught:
                worker.recovery.assert_mutation_allowed(session_id)
            assert caught.value.code == "OUTBOX_METADATA_UNAVAILABLE"
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    row = recovery_row(settings, session_id)
    assert row["status"] == "BLOCKED" and row["diagnostic_code"] == "OUTBOX_METADATA_UNAVAILABLE"
    assert ack_calls == [] and gateway.computes == [] and gateway.views[session_id] == before_view
    assert repository(settings).job(session_id, data["job_id"]) == before_queue
    assert source.read_bytes() == before_source
    assert any(row["operation"] == "OUTBOX_FAILED" and row["diagnostic_code"] == "OUTBOX_METADATA_UNAVAILABLE"
               for row in recovery_audit(settings, session_id))


@pytest.mark.parametrize("delayed_operation", ["outbox_ack", "pending_reconcile"])
def test_http_gate_opens_only_after_outbox_and_original_pending_reconciliation_finish(worker_setup, delayed_operation):
    settings, gateway = worker_setup
    with TestClient(create_app(settings, gateway=gateway)) as client:
        session_id = load(client).json()["data"]["session"]["session_id"]
        gateway.fail_submit_after_commit = True
        code(optimize(client, session_id), 503, "RUNTIME_TIMEOUT")
        original_payload = deepcopy(gateway.submissions[0])
        before = deepcopy(gateway.views[session_id])
        reached, release = asyncio.Event(), asyncio.Event()
        original_submit = gateway.submit
        internal_submissions = []

        async def submit(sid, command_id, basis, profile, budget_seconds):
            assert recovery_row(settings, sid)["status"] == "VALIDATED"
            internal_submissions.append((sid, command_id, deepcopy(basis), profile, budget_seconds))
            if delayed_operation == "pending_reconcile":
                reached.set()
                await release.wait()
            return await original_submit(sid, command_id, basis, profile, budget_seconds)

        async def notifications(sid):
            return [notification(sid)] if delayed_operation == "outbox_ack" else []

        async def ack(sid, command_id, event_id):
            assert recovery_row(settings, sid)["status"] == "VALIDATED"
            reached.set()
            await release.wait()
            return {"status": "ACKNOWLEDGED", "event_id": event_id}

        gateway.submit, gateway.read_notifications, gateway.acknowledge = submit, notifications, ack

        async def scenario():
            worker = ComputeWorker(settings, gateway=gateway)
            startup = asyncio.create_task(worker.startup())
            try:
                await asyncio.wait_for(reached.wait(), 3)
                assert not startup.done() and worker.status == "STARTING"
                assert recovery_row(settings, session_id)["status"] == "VALIDATED"
                assert not getattr(client.app.state.session_service, "allow_validated_recovery", False)
                response = optimize(client, session_id, request_id="during-recovery")
                code(response, 503, "SESSION_RECOVERING")
                with sqlite3.connect(settings.metadata_path) as db:
                    assert db.execute("SELECT 1 FROM job_requests WHERE session_id=? AND request_id='during-recovery'", (session_id,)).fetchone() is None
                assert gateway.computes == []
                release.set()
                await asyncio.wait_for(startup, 3)
                assert worker.status == "READY" and recovery_row(settings, session_id)["status"] == "VERIFIED"
                assert not worker.session_service.allow_validated_recovery
                assert internal_submissions == [original_payload]
                assert gateway.submissions == [original_payload, original_payload]
                assert gateway.views[session_id] == before
                gateway.submit = original_submit
                allowed = optimize(client, session_id, request_id="after-recovery")
                assert allowed.status_code == 202, allowed.text
                assert len(gateway.submissions) == 3 and gateway.computes == []
            finally:
                release.set()
                if not startup.done():
                    await asyncio.wait_for(startup, 3)
                await worker.shutdown()

        asyncio.run(scenario())
