"""Owned plan activation, durable receipts and physical-state boundaries.

The fake implements SDK compare-and-swap and command receipts. It activates a
forecast without moving vehicles, advancing time, or delivering an order.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3

from fastapi.testclient import TestClient
import pytest

from backend.api.errors import ApiError
from backend.api.main import create_app
from backend.services.compute_worker import ComputeWorker
from backend.tests.test_jobs import JobGateway, optimize
from backend.tests.test_sessions import code, headers, load, setup


class PlanGateway(JobGateway):
    def __init__(self, build):
        super().__init__(build)
        self.accepts, self.accept_receipts = [], {}
        self.fail_accept_after_commit = self.fail_resolve_after_commit = False
        self.accept_delay = 0
        self.after_commit = None
        self.accept_error_once = None
        self.corrupt_accept_result = None
        self.accept_participants, self.accept_arrived = 0, 0
        self.accept_gate = asyncio.Event()

    async def bootstrap(self, session_id, scenario_id, command_id):
        receipt = await super().bootstrap(session_id, scenario_id, command_id)
        basis = self.views[session_id]["basis"]
        basis.update(root_sha256="a" * 64, head_sha256="b" * 64, source_sha256="c" * 64,
                     context_version="TEST_ONLY-context", overlay_sha256=None)
        receipt["basis"] = deepcopy(basis)
        return receipt

    async def accept(self, session_id, job_id, command_id, basis):
        self.accepts.append((session_id, job_id, command_id, deepcopy(basis)))
        if self.accept_participants:
            self.accept_arrived += 1
            if self.accept_arrived >= self.accept_participants:
                self.accept_gate.set()
            await asyncio.wait_for(self.accept_gate.wait(), 3)
        if self.accept_delay:
            await asyncio.sleep(self.accept_delay)
        if self.accept_error_once:
            error_code, self.accept_error_once = self.accept_error_once, None
            raise ApiError(409, error_code, "job_id", "SDK rejected the original acceptance")
        key = (session_id, command_id)
        if key not in self.accept_receipts:
            view = self.views[session_id]
            if view["basis"] != basis:
                raise ApiError(409, "STALE_HEAD", "basis", "Activation input no longer matches the current head")
            view["basis"]["generation"] = str(int(view["basis"]["generation"]) + 1)
            view["active_job_id"] = job_id
            view["planned_served_suffix"] = deepcopy(self.jobs[(session_id, job_id)]["served_orders"])
            self.accept_receipts[key] = {"status": "ACCEPTED", "job_id": job_id, "basis": deepcopy(view["basis"])}
            if self.after_commit:
                self.after_commit()
        if self.fail_accept_after_commit:
            self.fail_accept_after_commit = False
            raise ApiError(503, "RUNTIME_TIMEOUT", "runtime", "Response interrupted after activation commit")
        result = deepcopy(self.accept_receipts[key])
        if self.corrupt_accept_result:
            field, value = self.corrupt_accept_result
            if field == "job_id":
                result[field] = value
            else:
                result["basis"][field] = value
        return result

    async def resolve(self, session_id):
        if self.fail_resolve_after_commit and self.accept_receipts:
            self.fail_resolve_after_commit = False
            raise ApiError(503, "RUNTIME_TIMEOUT", "runtime", "Live execution view temporarily unavailable")
        return await super().resolve(session_id)

    def certify(self, session_id, job_id, *, internal="FEASIBLE"):
        view = self.jobs[(session_id, job_id)]
        view.update(job_status="COMPLETED", internal_status=internal,
                    business_status="UNSUPPORTED" if internal == "RETURN_ONLY" else internal,
                    coverage_evaluated=True, plan_available=True,
                    served_orders=[] if internal == "RETURN_ONLY" else ["O1", "O2"],
                    validation={"status": "VALIDATED", "valid": True, "validator_version": "TEST_ONLY"})
        if internal == "RETURN_ONLY":
            view["diagnostics"] = [{"severity": "ERROR", "code": "RETURN_ONLY_SUPPLEMENT_REQUIRED",
                                    "path": "plan", "message": "Mandatory physical continuation only"}]


@pytest.fixture
def plans_setup(setup):
    settings, original = setup
    return settings, PlanGateway(original.build)


@pytest.fixture
def plans_client(plans_setup):
    settings, gateway = plans_setup
    with TestClient(create_app(settings, gateway=gateway), raise_server_exceptions=False) as client:
        yield client


def certified(client, gateway, request_id="optimize-1", load_request="load-1", internal="FEASIBLE"):
    session_id = load(client, request_id=load_request).json()["data"]["session"]["session_id"]
    response = optimize(client, session_id, request_id)
    assert response.status_code == 202, response.text
    job_id = response.json()["data"]["job_id"]
    gateway.certify(session_id, job_id, internal=internal)
    return session_id, job_id


def revision(gateway, session_id):
    basis = gateway.views[session_id]["basis"]
    return {key: basis[key] for key in ("head_version", "generation")}


def accept(client, server_session_id, server_job_id, request_id="accept-1", actor="alice", expected=None, **fields):
    expected = expected if expected is not None else {"head_version": "1", "generation": "0"}
    return client.post(f"/api/sessions/{server_session_id}/jobs/{server_job_id}/accept", headers=headers(actor),
                       json={"request_id": request_id, "expected_revision": expected, **fields})


def audit(client, session_id, actor="alice"):
    return client.get(f"/api/sessions/{session_id}/acceptances", headers=headers(actor))


def test_accept_activates_generation_and_records_public_receipt_without_delivery(plans_client, plans_setup):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    before = deepcopy(gateway.views[session_id])
    response = accept(plans_client, session_id, job_id)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["schema_version"] == "saferoute-m3-acceptance-view/1"
    receipt, after = data["receipt"], data["execution_view"]
    assert receipt["schema_version"] == "saferoute-m3-plan-acceptance/1"
    assert receipt["status"] == "ACCEPTED" and receipt["session_id"] == session_id and receipt["job_id"] == job_id
    assert receipt["acceptance_id"] and receipt["recorded_at"]
    assert receipt["input_basis"] == before["basis"]
    expected_basis = deepcopy(before["basis"])
    expected_basis["generation"] = "1"
    assert receipt["basis"] == expected_basis == after["basis"]
    assert after["active_job_id"] == job_id
    assert receipt["links"]["state"] == f"/api/sessions/{session_id}/state"
    assert receipt["links"]["job"] == f"/api/sessions/{session_id}/jobs/{job_id}"
    for field in ("current_time", "delivered_prefix", "vehicles", "observed_metrics", "order_ids"):
        assert after[field] == before[field]
    assert after["execution_mode"] == "SIMULATED_REPLAY" and after["real_world_observation"] is False
    assert gateway.computes == []


def test_retry_is_cached_and_historical_receipt_is_separate_from_fresh_view(plans_client, plans_setup):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    first = accept(plans_client, session_id, job_id).json()["data"]
    polls = len(gateway.polls)
    gateway.views[session_id]["basis"]["generation"] = "4"
    gateway.views[session_id]["active_job_id"] = "job-later-activation"
    gateway.jobs[(session_id, job_id)]["validation"]["valid"] = False
    retried = accept(plans_client, session_id, job_id)
    assert retried.status_code == 200, retried.text
    data = retried.json()["data"]
    assert data["receipt"] == first["receipt"] and data["receipt"]["basis"]["generation"] == "1"
    assert data["execution_view"]["basis"]["generation"] == "4"
    assert len(gateway.accepts) == 1 and len(gateway.polls) == polls
    records = audit(plans_client, session_id).json()["data"]
    assert records["schema_version"] == "saferoute-m3-acceptance-audit/1"
    assert records["session_id"] == session_id and records["acceptances"] == [first["receipt"]]


def test_request_id_cannot_be_rebound_to_changed_revision_or_job(plans_client, plans_setup):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    assert accept(plans_client, session_id, job_id).status_code == 200
    code(accept(plans_client, session_id, job_id, expected={"head_version": "1", "generation": "1"}),
         409, "IDEMPOTENCY_CONFLICT")
    _, other = certified(plans_client, gateway, request_id="optimize-2")
    code(accept(plans_client, session_id, other, expected=revision(gateway, session_id)), 409, "IDEMPOTENCY_CONFLICT")
    assert len(gateway.accepts) == 1 and len(audit(plans_client, session_id).json()["data"]["acceptances"]) == 1


def test_new_acceptance_requires_current_revision(plans_client, plans_setup):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    code(accept(plans_client, session_id, job_id, expected={"head_version": "0", "generation": "0"}),
         409, "STALE_HEAD")
    assert gateway.accepts == []


@pytest.mark.parametrize("field,value", [("head_sha256", "d" * 64), ("context_version", "new-context"),
    ("overlay_sha256", "e" * 64), ("source_sha256", "f" * 64)])
def test_same_counters_cannot_hide_changed_full_input_basis(plans_client, plans_setup, field, value):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    gateway.views[session_id]["basis"][field] = value
    code(accept(plans_client, session_id, job_id), 409, "STALE_HEAD")
    assert gateway.accepts == []


@pytest.mark.parametrize("lifecycle", ["QUEUED", "RUNNING", "FAILED"])
def test_noncompleted_jobs_cannot_be_activated(plans_client, plans_setup, lifecycle):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    gateway.jobs[(session_id, job_id)]["job_status"] = lifecycle
    response = accept(plans_client, session_id, job_id)
    code(response, 409, "WITNESS_REQUIRED")
    assert gateway.accepts == [] and audit(plans_client, session_id).json()["data"]["acceptances"] == []


@pytest.mark.parametrize("status", ["SEARCH_LIMIT", "TIME_LIMIT", "NO_SERVICE", "INVALID_DATA", "UNSUPPORTED"])
def test_completed_no_witness_outcome_cannot_be_activated(plans_client, plans_setup, status):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    view = gateway.jobs[(session_id, job_id)]
    view.update(internal_status=status, business_status="UNSUPPORTED" if status == "NO_SERVICE" else status,
                validation={"status": "NOT_RUN", "valid": None}, plan_available=False, coverage_evaluated=False,
                served_orders=[], unserved_orders=[])
    response = accept(plans_client, session_id, job_id)
    code(response, 409, "WITNESS_REQUIRED")
    assert gateway.accepts == []


@pytest.mark.parametrize("valid,available", [(False, True), (None, True), (True, False)])
def test_certification_and_plan_availability_are_both_required(plans_client, plans_setup, valid, available):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    gateway.jobs[(session_id, job_id)]["validation"]["valid"] = valid
    gateway.jobs[(session_id, job_id)]["plan_available"] = available
    code(accept(plans_client, session_id, job_id), 409, "WITNESS_REQUIRED")
    assert gateway.accepts == []


def test_certified_return_only_is_activatable_without_new_deliveries(plans_client, plans_setup):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway, internal="RETURN_ONLY")
    response = accept(plans_client, session_id, job_id)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["receipt"]["status"] == "ACCEPTED"
    assert response.json()["data"]["execution_view"]["active_job_id"] == job_id
    assert response.json()["data"]["execution_view"]["delivered_prefix"] == []
    assert gateway.jobs[(session_id, job_id)]["business_status"] == "UNSUPPORTED"


@pytest.mark.parametrize("route", ["accept", "audit"])
def test_cross_owner_denied_before_any_sdk_reads_or_acceptance(plans_client, plans_setup, route):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    counts = len(gateway.reads), len(gateway.polls)
    response = accept(plans_client, session_id, job_id, actor="bob") if route == "accept" else audit(plans_client, session_id, "bob")
    code(response, 403, "FORBIDDEN")
    assert (len(gateway.reads), len(gateway.polls)) == counts and gateway.accepts == []


def test_owned_viewer_can_read_audit_but_cannot_accept(plans_client, plans_setup):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    with sqlite3.connect(plans_setup[0].metadata_path) as db:
        db.execute("UPDATE sessions SET owner_actor_id='viewer' WHERE session_id=?", (session_id,))
    assert audit(plans_client, session_id, "viewer").status_code == 200
    code(accept(plans_client, session_id, job_id, actor="viewer"), 403, "FORBIDDEN")
    assert gateway.accepts == []


def test_missing_or_cross_session_job_fails_before_sdk_accept(plans_client, plans_setup):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    other = load(plans_client, request_id="load-other").json()["data"]["session"]["session_id"]
    code(accept(plans_client, other, job_id), 404, "JOB_NOT_FOUND")
    code(accept(plans_client, session_id, "job-unknown"), 404, "JOB_NOT_FOUND")
    assert gateway.accepts == [] and gateway.polls == []


def test_accept_and_audit_require_authentication(plans_client, plans_setup):
    session_id, job_id = certified(plans_client, plans_setup[1])
    code(plans_client.post(f"/api/sessions/{session_id}/jobs/{job_id}/accept",
         json={"request_id": "r1", "expected_revision": {"head_version": "1", "generation": "0"}}),
         401, "UNAUTHORIZED")
    code(plans_client.get(f"/api/sessions/{session_id}/acceptances"), 401, "UNAUTHORIZED")
    assert plans_setup[1].accepts == []


@pytest.mark.parametrize("body", [{"request_id": "r1"}, {"request_id": "r1", "expected_revision": None},
    {"request_id": "r1", "expected_revision": {"head_version": 1, "generation": "0"}}])
def test_accept_requires_explicit_canonical_expected_revision(plans_client, plans_setup, body):
    session_id, job_id = certified(plans_client, plans_setup[1])
    code(plans_client.post(f"/api/sessions/{session_id}/jobs/{job_id}/accept", headers=headers(), json=body),
         422, "VALIDATION_ERROR")
    assert plans_setup[1].accepts == []


@pytest.mark.parametrize("field,value", [("basis", {}), ("actor_id", "bob"), ("session_id", "chosen"),
    ("job_id", "chosen"), ("acceptance_id", "chosen"), ("snapshot_root", "D:/unsafe"), ("state", {})])
def test_accept_body_cannot_inject_authority_or_state(plans_client, plans_setup, field, value):
    session_id, job_id = certified(plans_client, plans_setup[1])
    code(accept(plans_client, session_id, job_id, **{field: value}), 422, "VALIDATION_ERROR")
    assert plans_setup[1].accepts == []


def test_sdk_commit_response_loss_retries_same_command_full_old_basis_once(plans_client, plans_setup):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    gateway.fail_accept_after_commit = True
    code(accept(plans_client, session_id, job_id), 503, "RUNTIME_TIMEOUT")
    assert gateway.views[session_id]["basis"]["generation"] == "1"
    reads, polls = len(gateway.reads), len(gateway.polls)
    retried = accept(plans_client, session_id, job_id)
    assert retried.status_code == 200, retried.text
    assert gateway.accepts[0] == gateway.accepts[1] and len(gateway.accept_receipts) == 1
    assert gateway.accepts[1][3]["generation"] == "0"
    assert len(gateway.polls) == polls and len(gateway.reads) == reads + 1
    assert len(audit(plans_client, session_id).json()["data"]["acceptances"]) == 1
    assert gateway.views[session_id]["basis"]["generation"] == "1"


def test_pending_acceptance_replays_historical_sdk_receipt_after_later_head_change(plans_client, plans_setup):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    gateway.fail_accept_after_commit = True
    code(accept(plans_client, session_id, job_id), 503, "RUNTIME_TIMEOUT")
    original = deepcopy(gateway.accepts[0])
    reads, polls = len(gateway.reads), len(gateway.polls)
    current = gateway.views[session_id]
    current["basis"].update(head_version="2", generation="4", head_sha256="e" * 64)
    current["active_job_id"] = "job-later-active"
    response = accept(plans_client, session_id, job_id)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["receipt"]["input_basis"] == original[3]
    assert data["receipt"]["basis"]["head_version"] == "1" and data["receipt"]["basis"]["generation"] == "1"
    assert data["execution_view"]["basis"] == current["basis"]
    assert data["execution_view"]["active_job_id"] == "job-later-active"
    assert gateway.accepts == [original, original] and len(gateway.accept_receipts) == 1
    assert len(gateway.polls) == polls and len(gateway.reads) == reads + 1
    assert audit(plans_client, session_id).json()["data"]["acceptances"] == [data["receipt"]]


def test_receipt_is_durable_before_fresh_execution_view_resolve(plans_client, plans_setup):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    gateway.fail_resolve_after_commit = True
    code(accept(plans_client, session_id, job_id), 503, "RUNTIME_TIMEOUT")
    saved = audit(plans_client, session_id).json()["data"]["acceptances"]
    assert len(saved) == 1 and saved[0]["basis"]["generation"] == "1"
    retry = accept(plans_client, session_id, job_id)
    assert retry.status_code == 200 and retry.json()["data"]["receipt"] == saved[0]
    assert len(gateway.accepts) == 1


def test_metadata_failure_after_sdk_commit_replays_same_command_and_records_once(plans_client, plans_setup, monkeypatch):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    repository = plans_client.app.state.plan_repository
    original = repository.complete_acceptance
    fail_next = True

    def interrupted_write(row, receipt):
        nonlocal fail_next
        if fail_next:
            fail_next = False
            raise sqlite3.OperationalError("Injected receipt persistence interruption")
        return original(row, receipt)

    monkeypatch.setattr(repository, "complete_acceptance", interrupted_write)
    code(accept(plans_client, session_id, job_id), 503, "METADATA_UNAVAILABLE")
    assert gateway.views[session_id]["basis"]["generation"] == "1"
    assert audit(plans_client, session_id).json()["data"]["acceptances"] == []
    retry = accept(plans_client, session_id, job_id)
    assert retry.status_code == 200, retry.text
    assert gateway.accepts[0] == gateway.accepts[1] and len(gateway.accept_receipts) == 1
    assert gateway.views[session_id]["basis"]["generation"] == "1"
    assert len(audit(plans_client, session_id).json()["data"]["acceptances"]) == 1


def test_audit_insert_failure_rolls_back_receipt_and_retry_commits_both_once(plans_client, plans_setup):
    settings, gateway = plans_setup
    session_id, job_id = certified(plans_client, gateway)
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("""CREATE TRIGGER test_abort_accept_audit BEFORE INSERT ON accept_audit
            BEGIN SELECT RAISE(ABORT, 'Injected audit INSERT interruption'); END""")
    try:
        code(accept(plans_client, session_id, job_id), 503, "METADATA_UNAVAILABLE")
        assert gateway.views[session_id]["basis"]["generation"] == "1"
        with sqlite3.connect(settings.metadata_path) as db:
            assert db.execute("SELECT response_json FROM accept_requests").fetchone()[0] is None
            assert db.execute("SELECT count(*) FROM accept_audit").fetchone()[0] == 0
    finally:
        with sqlite3.connect(settings.metadata_path) as db:
            db.execute("DROP TRIGGER test_abort_accept_audit")
    response = accept(plans_client, session_id, job_id)
    assert response.status_code == 200, response.text
    receipt = response.json()["data"]["receipt"]
    assert audit(plans_client, session_id).json()["data"]["acceptances"] == [receipt]
    assert gateway.accepts[0] == gateway.accepts[1] and len(gateway.accept_receipts) == 1


@pytest.mark.parametrize("field,value", [("generation", "2"), ("head_version", "2"),
    ("head_sha256", "e" * 64), ("job_id", "job-other")])
def test_sdk_receipt_must_match_job_and_generation_only_transition(plans_client, plans_setup, field, value):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    gateway.corrupt_accept_result = field, value
    code(accept(plans_client, session_id, job_id), 503, "ACCEPT_BINDING_CHANGED")
    assert audit(plans_client, session_id).json()["data"]["acceptances"] == []
    gateway.corrupt_accept_result = None
    retry = accept(plans_client, session_id, job_id)
    assert retry.status_code == 200, retry.text
    assert gateway.accepts[0] == gateway.accepts[1]
    assert len(audit(plans_client, session_id).json()["data"]["acceptances"]) == 1


def test_reserved_sdk_rejection_is_durable_and_same_request_does_not_mutate_again(plans_client, plans_setup):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    gateway.accept_error_once = "WITNESS_REQUIRED"
    code(accept(plans_client, session_id, job_id), 409, "WITNESS_REQUIRED")
    code(accept(plans_client, session_id, job_id), 409, "WITNESS_REQUIRED")
    assert len(gateway.accepts) == 1 and gateway.accept_receipts == {}
    assert gateway.views[session_id]["basis"]["generation"] == "0"
    assert accept(plans_client, session_id, job_id, request_id="accept-new").status_code == 200
    assert len(gateway.accepts) == 2


def test_restart_preserves_acceptance_owner_receipt_and_audit(plans_setup):
    settings, gateway = plans_setup
    with TestClient(create_app(settings, gateway=gateway)) as first:
        session_id, job_id = certified(first, gateway)
        receipt = accept(first, session_id, job_id).json()["data"]["receipt"]
    with TestClient(create_app(settings, gateway=gateway)) as restarted:
        response = accept(restarted, session_id, job_id)
        assert response.status_code == 200 and response.json()["data"]["receipt"] == receipt
        assert audit(restarted, session_id).json()["data"]["acceptances"] == [receipt]
        code(audit(restarted, session_id, "bob"), 403, "FORBIDDEN")
    assert len(gateway.accepts) == 1


def test_restart_after_uncertain_sdk_accept_uses_original_binding(plans_setup):
    settings, gateway = plans_setup
    with TestClient(create_app(settings, gateway=gateway)) as first:
        session_id, job_id = certified(first, gateway)
        gateway.fail_accept_after_commit = True
        code(accept(first, session_id, job_id), 503, "RUNTIME_TIMEOUT")
    with TestClient(create_app(settings, gateway=gateway)) as restarted:
        response = accept(restarted, session_id, job_id)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["receipt"]["input_basis"]["generation"] == "0"
        assert response.json()["data"]["receipt"]["basis"]["generation"] == "1"
        assert len(audit(restarted, session_id).json()["data"]["acceptances"]) == 1
    assert gateway.accepts[0] == gateway.accepts[1] and len(gateway.accept_receipts) == 1


def test_worker_reconciles_uncertain_acceptance_before_dispatch_without_fresh_stale_checks(plans_setup):
    settings, gateway = plans_setup
    with TestClient(create_app(settings, gateway=gateway)) as client:
        session_id, job_id = certified(client, gateway)
        gateway.fail_accept_after_commit = True
        code(accept(client, session_id, job_id), 503, "RUNTIME_TIMEOUT")
    original = deepcopy(gateway.accepts[0])
    reads, polls = len(gateway.reads), len(gateway.polls)
    gateway.jobs[(session_id, job_id)]["validation"]["valid"] = False

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.plans.pending_requests() == []
            saved = worker.plans.audit(session_id)
            assert len(saved) == 1 and saved[0]["input_basis"]["generation"] == "0"
            assert saved[0]["basis"]["generation"] == "1"
            assert gateway.accepts == [original, original]
            assert len(gateway.reads) == reads + 1 and len(gateway.polls) == polls  # startup inspection, no acceptance basis refresh
            assert gateway.computes == []
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    with TestClient(create_app(settings, gateway=gateway)) as restarted:
        assert accept(restarted, session_id, job_id).status_code == 200
        assert len(audit(restarted, session_id).json()["data"]["acceptances"]) == 1
    assert len(gateway.accepts) == 2 and gateway.views[session_id]["basis"]["generation"] == "1"


def test_duplicate_concurrent_acceptance_has_one_activation_and_one_audit_record(plans_client, plans_setup):
    gateway = plans_setup[1]
    session_id, job_id = certified(plans_client, gateway)
    gateway.accept_delay = 0.15
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: accept(plans_client, session_id, job_id), range(2)))
    assert any(response.status_code == 200 for response in responses)
    for response in responses:
        if response.status_code != 200:
            code(response, 409, "REQUEST_IN_PROGRESS")
    assert len(gateway.accepts) == 1 and gateway.views[session_id]["basis"]["generation"] == "1"
    assert len(audit(plans_client, session_id).json()["data"]["acceptances"]) == 1


def test_concurrent_different_jobs_from_same_basis_have_one_sdk_cas_winner(plans_client, plans_setup):
    gateway = plans_setup[1]
    session_id, first_job = certified(plans_client, gateway)
    _, second_job = certified(plans_client, gateway, request_id="optimize-2")
    assert gateway.jobs[(session_id, first_job)]["input_basis"] == gateway.jobs[(session_id, second_job)]["input_basis"]
    gateway.accept_participants = 2
    jobs = [first_job, second_job]
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda item: accept(plans_client, session_id, item[1], request_id=f"accept-{item[0]}"),
                                      enumerate(jobs)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    rejected = next(response for response in responses if response.status_code == 409)
    code(rejected, 409, "STALE_HEAD")
    accepted = next(response for response in responses if response.status_code == 200).json()["data"]["receipt"]
    assert len(gateway.accepts) == 2 and len(gateway.accept_receipts) == 1
    assert gateway.views[session_id]["basis"]["generation"] == "1"
    assert gateway.views[session_id]["active_job_id"] == accepted["job_id"]
    assert audit(plans_client, session_id).json()["data"]["acceptances"] == [accepted]


def test_openapi_documents_accept_and_owned_audit_routes(plans_client):
    schema = plans_client.get("/openapi.json").json()
    route = schema["paths"]["/api/sessions/{session_id}/jobs/{job_id}/accept"]["post"]
    assert route["security"] and "200" in route["responses"]
    assert schema["components"]["schemas"]["AcceptPlanRequest"]["required"] == ["request_id", "expected_revision"]
    assert schema["paths"]["/api/sessions/{session_id}/acceptances"]["get"]["security"]
