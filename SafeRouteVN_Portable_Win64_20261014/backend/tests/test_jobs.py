"""HTTP security, durable request binding and public job-view behavior.

The deterministic gateway models SDK receipts and public projections. Native
compute is verified separately; these tests never invent physical progress.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import sqlite3

from fastapi.testclient import TestClient
import pytest

from backend.api.errors import ApiError
from backend.api.main import create_app
from backend.tests.test_sessions import FakeGateway, code, headers, load, setup


def public_job(job_id, basis):
    return {"schema_version": "task02-m2-runtime-job-view/1", "job_id": job_id,
        "job_status": "QUEUED", "input_basis": deepcopy(basis), "business_status": None,
        "internal_status": None, "diagnostics": [], "validation": {"status": "NOT_RUN", "valid": None},
        "coverage_evaluated": False, "served_orders": [], "unserved_orders": [], "plan_available": False,
        "execution_view_required": True, "public_api_v1_dynamic_plan_available": False}


class JobGateway(FakeGateway):
    def __init__(self, build):
        super().__init__(build)
        self.jobs, self.submit_receipts, self.cancel_receipts = {}, {}, {}
        self.submissions, self.polls, self.cancellations, self.computes = [], [], [], []
        self.fail_submit_after_commit = self.fail_cancel_after_commit = False
        self.submit_delay = 0

    async def submit(self, session_id, command_id, basis, profile, budget_seconds):
        args = (session_id, command_id, deepcopy(basis), profile, budget_seconds)
        self.submissions.append(args)
        if self.submit_delay:
            await asyncio.sleep(self.submit_delay)
        key = (session_id, command_id)
        if key not in self.submit_receipts:
            job_id = "job-" + hashlib.sha256(command_id.encode()).hexdigest()[:24]
            self.jobs[(session_id, job_id)] = public_job(job_id, basis)
            self.submit_receipts[key] = {"status": "QUEUED", "job_id": job_id, "input_basis": deepcopy(basis)}
        if self.fail_submit_after_commit:
            self.fail_submit_after_commit = False
            raise ApiError(503, "RUNTIME_TIMEOUT", "runtime", "Response interrupted after submit commit")
        return deepcopy(self.submit_receipts[key])

    async def job_view(self, session_id, job_id):
        self.polls.append((session_id, job_id))
        if (session_id, job_id) not in self.jobs:
            raise ApiError(404, "JOB_NOT_FOUND", "job_id", "Unknown job in this session")
        return deepcopy(self.jobs[(session_id, job_id)])

    async def cancel(self, session_id, job_id, command_id):
        self.cancellations.append((session_id, job_id, command_id))
        key = (session_id, command_id)
        if key not in self.cancel_receipts:
            view = self.jobs[(session_id, job_id)]
            outcome = "COMPLETED_IMMUTABLE" if view["job_status"] == "COMPLETED" else "JOB_CANCELLED"
            if view["job_status"] in ("QUEUED", "RUNNING"):
                view["job_status"] = "FAILED"
                view["diagnostics"] = [{"severity": "ERROR", "code": "JOB_CANCELLED", "path": "job_id",
                                        "message": "Cancelled; physical commits are unchanged"}]
            self.cancel_receipts[key] = {"status": outcome, "job_id": job_id}
        if self.fail_cancel_after_commit:
            self.fail_cancel_after_commit = False
            raise ApiError(503, "RUNTIME_TIMEOUT", "runtime", "Response interrupted after cancel commit")
        return deepcopy(self.cancel_receipts[key])

    async def compute(self, session_id, job_id, command_id, budget_seconds):
        self.computes.append((session_id, job_id, command_id, budget_seconds))
        raise AssertionError("HTTP request handler must not run native compute")

    async def recover(self, session_id, command_id):
        return {"status": "RECOVERED", "fenced_jobs": [], "basis": deepcopy(self.views[session_id]["basis"])}


@pytest.fixture
def jobs_setup(setup):
    settings, original = setup
    return settings, JobGateway(original.build)


@pytest.fixture
def jobs_client(jobs_setup):
    settings, gateway = jobs_setup
    with TestClient(create_app(settings, gateway=gateway), raise_server_exceptions=False) as client:
        yield client


def optimize(client, server_session_id, request_id="optimize-1", actor="alice", **fields):
    return client.post(f"/api/sessions/{server_session_id}/optimize", headers=headers(actor),
                       json={"request_id": request_id, **fields})


def poll(client, session_id, job_id, actor="alice"):
    return client.get(f"/api/sessions/{session_id}/jobs/{job_id}", headers=headers(actor))


def cancel(client, session_id, job_id, request_id="cancel-1", actor="alice"):
    return client.post(f"/api/sessions/{session_id}/jobs/{job_id}/cancel", headers=headers(actor),
                       json={"request_id": request_id})


def submitted(client, request_id="optimize-1", scenario="S0", load_request="load-1", **fields):
    session_id = load(client, scenario, load_request).json()["data"]["session"]["session_id"]
    response = optimize(client, session_id, request_id, **fields)
    assert response.status_code == 202, response.text
    return session_id, response.json()["data"]


def test_submission_is_202_queued_public_and_does_not_compute(jobs_client, jobs_setup):
    client, gateway = jobs_client, jobs_setup[1]
    session_id, data = submitted(client)
    assert data["schema_version"] == "saferoute-m3-job-submission/1"
    assert data["session_id"] == session_id and data["profile"] == "BALANCED"
    assert data["input_basis"] == gateway.views[session_id]["basis"]
    assert data["links"]["poll"] == f"/api/sessions/{session_id}/jobs/{data['job_id']}"
    assert data["links"]["cancel"] == data["links"]["poll"] + "/cancel"
    response = poll(client, session_id, data["job_id"])
    assert response.status_code == 200
    view = response.json()["data"]
    assert view == gateway.jobs[(session_id, data["job_id"])]
    assert view["job_status"] == "QUEUED" and view["business_status"] is None
    assert view["validation"]["valid"] is None and view["plan_available"] is False
    assert gateway.computes == []
    assert not {"lease", "expires", "request", "result", "root"}.intersection(view)


def test_retry_uses_one_job_and_conflicting_profile_is_rejected(jobs_client, jobs_setup):
    session_id, first = submitted(jobs_client)
    retry = optimize(jobs_client, session_id, profile="BALANCED")
    assert retry.status_code == 202 and retry.json()["data"] == first
    code(optimize(jobs_client, session_id, profile="SAFER"), 409, "IDEMPOTENCY_CONFLICT")
    assert len(jobs_setup[1].submissions) == 1 and len(jobs_setup[1].jobs) == 1


@pytest.mark.parametrize("profile", ["FASTEST", "BALANCED", "SAFER"])
def test_profile_is_forwarded_and_budget_is_server_owned(jobs_client, jobs_setup, profile):
    _, data = submitted(jobs_client, profile=profile)
    assert data["profile"] == profile
    args = jobs_setup[1].submissions[0]
    assert args[3] == profile and isinstance(args[4], (int, float)) and args[4] > 0


def test_response_lost_after_submit_keeps_original_command_and_basis(jobs_client, jobs_setup):
    gateway = jobs_setup[1]
    session_id = load(jobs_client).json()["data"]["session"]["session_id"]
    gateway.fail_submit_after_commit = True
    code(optimize(jobs_client, session_id), 503, "RUNTIME_TIMEOUT")
    original = deepcopy(gateway.submissions[0])
    resolves = len(gateway.reads)
    gateway.views[session_id]["basis"]["head_version"] = "2"
    retried = optimize(jobs_client, session_id)
    assert retried.status_code == 202, retried.text
    assert gateway.submissions[0] == gateway.submissions[1] == original
    assert len(gateway.reads) == resolves and len(gateway.jobs) == 1
    assert retried.json()["data"]["input_basis"]["head_version"] == "1"


def test_restart_preserves_submission_receipt_and_owner(jobs_setup):
    settings, gateway = jobs_setup
    with TestClient(create_app(settings, gateway=gateway)) as first:
        session_id, data = submitted(first)
    with TestClient(create_app(settings, gateway=gateway)) as restarted:
        retry = optimize(restarted, session_id)
        assert retry.status_code == 202 and retry.json()["data"] == data
        assert poll(restarted, session_id, data["job_id"]).status_code == 200
        code(poll(restarted, session_id, data["job_id"], "bob"), 403, "FORBIDDEN")
    assert len(gateway.submissions) == 1


def test_restart_after_uncertain_commit_retries_original_binding(jobs_setup):
    settings, gateway = jobs_setup
    with TestClient(create_app(settings, gateway=gateway)) as first:
        session_id = load(first).json()["data"]["session"]["session_id"]
        gateway.fail_submit_after_commit = True
        code(optimize(first, session_id), 503, "RUNTIME_TIMEOUT")
    gateway.views[session_id]["basis"]["generation"] = "1"
    with TestClient(create_app(settings, gateway=gateway)) as restarted:
        assert optimize(restarted, session_id).status_code == 202
    assert gateway.submissions[0] == gateway.submissions[1] and len(gateway.jobs) == 1


def test_stale_revision_denied_before_submit(jobs_client, jobs_setup):
    session_id = load(jobs_client).json()["data"]["session"]["session_id"]
    code(optimize(jobs_client, session_id, expected_revision={"head_version": "0", "generation": "0"}),
         409, "STALE_HEAD")
    assert jobs_setup[1].submissions == []


def test_canonical_large_revision_is_preserved_exactly(jobs_client, jobs_setup):
    session_id = load(jobs_client).json()["data"]["session"]["session_id"]
    jobs_setup[1].views[session_id]["basis"]["head_version"] = "9007199254740993"
    result = optimize(jobs_client, session_id, expected_revision={"head_version": "9007199254740993", "generation": "0"})
    assert result.status_code == 202, result.text
    assert result.json()["data"]["input_basis"]["head_version"] == "9007199254740993"


@pytest.mark.parametrize("route", ["optimize", "poll", "cancel"])
def test_cross_owner_denied_before_runtime_calls(jobs_client, jobs_setup, route):
    session_id, data = submitted(jobs_client)
    gateway = jobs_setup[1]
    counts = tuple(map(len, (gateway.submissions, gateway.polls, gateway.cancellations, gateway.reads)))
    response = (optimize(jobs_client, session_id, "other", "bob") if route == "optimize" else
                poll(jobs_client, session_id, data["job_id"], "bob") if route == "poll" else
                cancel(jobs_client, session_id, data["job_id"], actor="bob"))
    code(response, 403, "FORBIDDEN")
    assert tuple(map(len, (gateway.submissions, gateway.polls, gateway.cancellations, gateway.reads))) == counts


def test_viewer_can_read_owned_job_but_cannot_mutate(jobs_client, jobs_setup):
    session_id, data = submitted(jobs_client)
    with sqlite3.connect(jobs_setup[0].metadata_path) as db:
        db.execute("UPDATE sessions SET owner_actor_id='viewer' WHERE session_id=?", (session_id,))
    assert poll(jobs_client, session_id, data["job_id"], "viewer").status_code == 200
    code(optimize(jobs_client, session_id, "other", "viewer"), 403, "FORBIDDEN")
    code(cancel(jobs_client, session_id, data["job_id"], actor="viewer"), 403, "FORBIDDEN")
    assert len(jobs_setup[1].submissions) == 1 and jobs_setup[1].cancellations == []


def test_job_namespace_is_session_scoped_and_unknown_job_does_not_poll_sdk(jobs_client, jobs_setup):
    session_id, data = submitted(jobs_client)
    other = load(jobs_client, request_id="load-other").json()["data"]["session"]["session_id"]
    code(poll(jobs_client, other, data["job_id"]), 404, "JOB_NOT_FOUND")
    code(cancel(jobs_client, other, data["job_id"]), 404, "JOB_NOT_FOUND")
    code(poll(jobs_client, session_id, "job-unknown"), 404, "JOB_NOT_FOUND")
    assert jobs_setup[1].polls == [] and jobs_setup[1].cancellations == []


def test_unauthenticated_submit_is_rejected(jobs_client, jobs_setup):
    session_id = load(jobs_client).json()["data"]["session"]["session_id"]
    response = jobs_client.post(f"/api/sessions/{session_id}/optimize", json={"request_id": "r1"})
    code(response, 401, "UNAUTHORIZED")
    assert jobs_setup[1].submissions == []


@pytest.mark.parametrize("field,value", [("session_id", "client-session"), ("actor_id", "bob"),
    ("basis", {}), ("budget_seconds", 999999), ("snapshot_root", "D:/unsafe"), ("job_id", "chosen")])
def test_client_cannot_override_job_authority(jobs_client, jobs_setup, field, value):
    session_id = load(jobs_client).json()["data"]["session"]["session_id"]
    code(optimize(jobs_client, session_id, **{field: value}), 422, "VALIDATION_ERROR")
    assert jobs_setup[1].submissions == []


@pytest.mark.parametrize("profile", ["ECO", "balanced", 1, None])
def test_unknown_or_coerced_profile_is_rejected(jobs_client, jobs_setup, profile):
    session_id = load(jobs_client).json()["data"]["session"]["session_id"]
    code(optimize(jobs_client, session_id, profile=profile), 422, "VALIDATION_ERROR")
    assert jobs_setup[1].submissions == []


@pytest.mark.parametrize("raw", [
    '{"request_id":"r1","request_id":"r2"}',
    '{"request_id":"r1","expected_revision":{"head_version":9007199254740993,"generation":"0"}}',
    '{"request_id":"r1","profile":NaN}',
])
def test_job_endpoint_preserves_strict_json_boundary(jobs_client, jobs_setup, raw):
    session_id = load(jobs_client).json()["data"]["session"]["session_id"]
    response = jobs_client.post(f"/api/sessions/{session_id}/optimize", content=raw,
                               headers={**headers(), "Content-Type": "application/json"})
    assert response.status_code == 422 and response.json()["status"] == "ERROR"
    assert jobs_setup[1].submissions == []


@pytest.mark.parametrize("business", ["SEARCH_LIMIT", "FEASIBLE"])
def test_no_witness_and_certified_plan_are_distinct_public_outcomes(jobs_client, jobs_setup, business):
    session_id, data = submitted(jobs_client)
    view = jobs_setup[1].jobs[(session_id, data["job_id"])]
    view.update(job_status="COMPLETED", business_status=business, internal_status=business)
    if business == "FEASIBLE":
        view.update(plan_available=True, coverage_evaluated=True, served_orders=["O1", "O2"],
                    validation={"status": "VALIDATED", "valid": True, "validator_version": "TEST_ONLY"})
    else:
        view["diagnostics"] = [{"severity": "WARNING", "code": "SEARCH_LIMIT", "path": "job_id",
                                "message": "No independently certified witness"}]
    result = poll(jobs_client, session_id, data["job_id"])
    assert result.status_code == 200 and result.json()["data"] == view
    if business == "SEARCH_LIMIT":
        assert result.json()["data"]["unserved_orders"] == []
        assert result.json()["data"]["validation"]["valid"] is None
        assert result.json()["data"]["coverage_evaluated"] is False


@pytest.mark.parametrize("lifecycle", ["QUEUED", "RUNNING", "FAILED"])
def test_poll_preserves_lifecycle_and_failure_diagnostics(jobs_client, jobs_setup, lifecycle):
    session_id, data = submitted(jobs_client)
    view = jobs_setup[1].jobs[(session_id, data["job_id"])]
    view["job_status"] = lifecycle
    if lifecycle == "FAILED":
        view["diagnostics"] = [{"severity": "ERROR", "code": "BUDGET_EXHAUSTED", "path": "job_id",
                                "message": "No witness certified within server budget"}]
    result = poll(jobs_client, session_id, data["job_id"])
    assert result.status_code == 200 and result.json()["data"] == view
    assert result.json()["data"]["business_status"] is None


def test_cancel_queued_is_idempotent_and_preserves_physical_state(jobs_client, jobs_setup):
    session_id, data = submitted(jobs_client)
    before = deepcopy(jobs_setup[1].views[session_id])
    first = cancel(jobs_client, session_id, data["job_id"])
    assert first.status_code == 200, first.text
    assert first.json()["data"]["schema_version"] == "saferoute-m3-job-cancellation/1"
    assert cancel(jobs_client, session_id, data["job_id"]).json()["data"] == first.json()["data"]
    view = poll(jobs_client, session_id, data["job_id"]).json()["data"]
    assert view["job_status"] == "FAILED" and view["diagnostics"][0]["code"] == "JOB_CANCELLED"
    assert jobs_setup[1].views[session_id] == before and len(jobs_setup[1].cancellations) == 1


def test_cancel_completed_cannot_destroy_certified_result(jobs_client, jobs_setup):
    session_id, data = submitted(jobs_client)
    view = jobs_setup[1].jobs[(session_id, data["job_id"])]
    view.update(job_status="COMPLETED", business_status="FEASIBLE", internal_status="FEASIBLE",
                plan_available=True, coverage_evaluated=True, served_orders=["O1", "O2"],
                validation={"status": "VALIDATED", "valid": True, "validator_version": "TEST_ONLY"})
    before = deepcopy(view)
    response = cancel(jobs_client, session_id, data["job_id"])
    assert response.status_code == 200, response.text
    assert "COMPLETED_IMMUTABLE" in str(response.json()["data"])
    assert poll(jobs_client, session_id, data["job_id"]).json()["data"] == before


def test_uncertain_cancel_retries_same_command_and_survives_restart(jobs_setup):
    settings, gateway = jobs_setup
    with TestClient(create_app(settings, gateway=gateway)) as first:
        session_id, data = submitted(first)
        gateway.fail_cancel_after_commit = True
        code(cancel(first, session_id, data["job_id"]), 503, "RUNTIME_TIMEOUT")
    with TestClient(create_app(settings, gateway=gateway)) as restarted:
        assert cancel(restarted, session_id, data["job_id"]).status_code == 200
    assert gateway.cancellations[0] == gateway.cancellations[1]
    assert len(gateway.cancel_receipts) == 1


def test_optimize_and_poll_are_forecasts_without_activation_or_delivery(jobs_client, jobs_setup):
    session_id = load(jobs_client).json()["data"]["session"]["session_id"]
    before = jobs_client.get(f"/api/sessions/{session_id}/state", headers=headers()).json()["data"]
    response = optimize(jobs_client, session_id)
    assert response.status_code == 202
    poll(jobs_client, session_id, response.json()["data"]["job_id"])
    after = jobs_client.get(f"/api/sessions/{session_id}/state", headers=headers()).json()["data"]
    assert after == before
    assert after["active_job_id"] is None and after["delivered_prefix"] == []
    assert after["observed_metrics"] is None and after["execution_mode"] == "SIMULATED_REPLAY"


def test_concurrent_duplicate_submission_cannot_create_two_sdk_jobs(jobs_client, jobs_setup):
    session_id = load(jobs_client).json()["data"]["session"]["session_id"]
    gateway = jobs_setup[1]
    gateway.submit_delay = 0.15
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: optimize(jobs_client, session_id), range(2)))
    assert any(response.status_code == 202 for response in responses)
    for response in responses:
        if response.status_code != 202:
            code(response, 409, "REQUEST_IN_PROGRESS")
    assert len(gateway.jobs) == 1 and len(gateway.submissions) == 1
    assert optimize(jobs_client, session_id).status_code == 202


def test_openapi_documents_async_submission_and_authenticated_job_routes(jobs_client):
    schema = jobs_client.get("/openapi.json").json()
    operation = schema["paths"]["/api/sessions/{session_id}/optimize"]["post"]
    assert "202" in operation["responses"] and operation["security"]
    assert "/api/sessions/{session_id}/jobs/{job_id}" in schema["paths"]
    assert "/api/sessions/{session_id}/jobs/{job_id}/cancel" in schema["paths"]
    assert "compute" not in " ".join(schema["paths"])
