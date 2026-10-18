"""Durable batch and cancellation behavior against a public SDK fake."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3

from fastapi.testclient import TestClient
import pytest

from backend.api.errors import ApiError
from backend.api.main import create_app
from backend.api.routers import profiles
from backend.models.http import CompareProfilesRequest, CancelJobRequest
from backend.services.auth import Actor
from backend.services.profile_repository import PROFILES, ProfileRepository
from backend.services.profile_service import ProfileService
from backend.services.runtime_gateway import RuntimeGateway
from backend.tests.test_jobs import optimize
from backend.tests.test_sessions import setup, load, headers, code
from backend.tests.test_worker import WorkerGateway


class ProfileGateway(WorkerGateway):
    def __init__(self, build):
        super().__init__(build)
        self.comparisons = []
        self.domains = {p: "d" * 64 for p in PROFILES}
        self.profiles = {}
        self.strict_cas = False
        self.bad_comparison = None

    async def submit(self, sid, command, basis, profile, budget):
        if self.strict_cas and basis != self.views[sid]["basis"] and (sid, command) not in self.submit_receipts:
            raise ApiError(409, "STALE_HEAD", "basis", "SDK full-basis CAS failed")
        result = await super().submit(sid, command, basis, profile, budget)
        self.profiles[result["job_id"]] = profile
        return result

    async def compare_profiles(self, sid, job_ids):
        self.comparisons.append((sid, list(job_ids)))
        rows = []
        for jid in job_ids:
            view = self.jobs[(sid, jid)]
            if view["job_status"] != "COMPLETED" or view["validation"].get("valid") is not True:
                raise ApiError(409, "WITNESS_REQUIRED", "job_ids", "Public SDK needs certified witnesses")
            profile = self.profiles[jid]
            rows.append({"job_id": jid, "profile": profile, "basis": deepcopy(view["input_basis"]),
                         "domain_sha256": self.domains[profile], "metrics": {"distance_m": 100, "exposure_proxy": 7}})
        same = rows[0]["basis"] == rows[1]["basis"] == rows[2]["basis"] and len({r["domain_sha256"] for r in rows}) == 1
        value = {"schema_version": "task02-m2-runtime-comparison/1", "status": "COMPARABLE" if same else "NON_COMPARABLE",
            "reason": None if same else "AUTHENTICATED_BASIS_OR_PHYSICAL_DOMAIN_DIFFERS", "jobs": rows}
        if self.bad_comparison:
            self.bad_comparison(value)
        return value


@pytest.fixture
def profile_setup(setup):
    settings, original = setup
    return settings, ProfileGateway(original.build)


def profile_app(settings, gateway):
    app = create_app(settings, gateway=gateway)
    repository = ProfileRepository(settings.metadata_path)
    repository.initialize()
    app.state.profile_repository = repository
    app.state.profile_service = ProfileService(settings, repository, app.state.session_service, app.state.job_service, gateway)
    # Root combines this same wiring with the independent replay additions.
    app.include_router(profiles.router)
    return app


@pytest.fixture
def profile_client(profile_setup):
    with TestClient(profile_app(*profile_setup), raise_server_exceptions=False) as client:
        yield client


def create(client, sid, request="compare-1", actor="alice", **fields):
    return client.post(f"/api/sessions/{sid}/profiles/compare", headers=headers(actor), json={"request_id": request, **fields})


def poll(client, sid, cid, actor="alice"):
    return client.get(f"/api/sessions/{sid}/profiles/comparisons/{cid}", headers=headers(actor))


def cancel(client, sid, cid, request="cancel-1", actor="alice"):
    return client.post(f"/api/sessions/{sid}/profiles/comparisons/{cid}/cancel", headers=headers(actor), json={"request_id": request})


def batch(client):
    sid = load(client).json()["data"]["session"]["session_id"]
    result = create(client, sid)
    assert result.status_code == 202, result.text
    return sid, result.json()["data"]["comparison_id"]


def advance(service, limit=20):
    for _ in range(limit):
        if service.repository.next_pending() is None:
            return
        asyncio.run(service.reconcile_one())
    raise AssertionError("Comparison did not terminate")


def submit_all(service):
    for _ in range(3):
        assert asyncio.run(service.reconcile_one()) is True


def certify_all(gateway, sid):
    for (owner, jid), view in gateway.jobs.items():
        if owner == sid:
            gateway.completed(view)


def test_create_fast_durable_no_submit_or_compute_retry_after_head_change(profile_client, profile_setup):
    client, gateway = profile_client, profile_setup[1]
    sid, cid = batch(client)
    first = create(client, sid).json()["data"]
    before = deepcopy(gateway.views[sid])
    gateway.views[sid]["basis"]["head_version"] = "2"
    assert create(client, sid).json()["data"] == first
    data = poll(client, sid, cid).json()["data"]
    assert data["status"] == "QUEUED" and data["input_basis"] == before["basis"]
    assert data["outcome"] is None and all(m["view"] is None for m in data["jobs"])
    assert gateway.submissions == gateway.computes == gateway.comparisons == []
    assert not {"budget_seconds", "actor_id", "installation_sha256", "submission_request_id", "cancel_request_id"}.intersection(data)
    code(create(client, sid, expected_revision={"head_version": "2", "generation": "0"}), 409, "IDEMPOTENCY_CONFLICT")


@pytest.mark.parametrize("different_domain", [False, True])
def test_three_durable_same_basis_submissions_preserve_sdk_verdict_and_physical_state(profile_client, profile_setup, different_domain):
    settings, gateway = profile_setup
    sid, cid = batch(profile_client)
    before = deepcopy(gateway.views[sid])
    service = profile_client.app.state.profile_service
    submit_all(service)
    assert [args[3] for args in gateway.submissions] == list(PROFILES)
    assert all(args[2] == before["basis"] and args[4] == settings.compute_budget_seconds for args in gateway.submissions)
    assert gateway.computes == []
    certify_all(gateway, sid)
    if different_domain:
        gateway.domains["SAFER"] = "e" * 64
    advance(service)
    data = poll(profile_client, sid, cid).json()["data"]
    assert data["status"] == "COMPLETED"
    assert data["outcome"]["comparison"]["status"] == ("NON_COMPARABLE" if different_domain else "COMPARABLE")
    assert data["metric_scope"] == "FORECAST_ONLY" and data["exposure_is_proxy"] is True
    assert gateway.views[sid] == before and len(gateway.comparisons) == 1
    assert asyncio.run(service.reconcile_one()) is False
    assert len(gateway.comparisons) == 1


@pytest.mark.parametrize("outcome", ["SEARCH_LIMIT", "TIME_LIMIT", "FAILED"])
def test_missing_witness_is_transport_success_without_fabricated_sdk_verdict_or_coverage(profile_client, profile_setup, outcome):
    sid, cid = batch(profile_client)
    service, gateway = profile_client.app.state.profile_service, profile_setup[1]
    submit_all(service)
    certify_all(gateway, sid)
    jid = service.repository.comparison(sid, cid)["members"][2]["job_id"]
    view = gateway.jobs[(sid, jid)]
    view.update(job_status="FAILED" if outcome == "FAILED" else "COMPLETED", business_status=None if outcome == "FAILED" else outcome,
        internal_status=None if outcome == "FAILED" else outcome, validation={"status": "NOT_RUN", "valid": None},
        coverage_evaluated=False, served_orders=[], unserved_orders=[], plan_available=False,
        diagnostics=[{"severity": "WARNING", "code": outcome, "path": "job_id", "message": "No certified witness"}])
    advance(service)
    response = poll(profile_client, sid, cid)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "COMPLETED" and data["outcome"] == {"comparison": None, "reason": "NO_CERTIFIED_WITNESS_FOR_EVERY_PROFILE"}
    assert data["jobs"][2]["view"] == view and gateway.comparisons == []


def test_restart_after_sdk_submit_committed_and_response_lost_uses_original_command(profile_setup):
    settings, gateway = profile_setup
    with TestClient(profile_app(settings, gateway)) as client:
        sid, cid = batch(client)
        gateway.fail_submit_after_commit = True
        assert asyncio.run(client.app.state.profile_service.reconcile_one()) is False
        original = deepcopy(gateway.submissions[0])
    gateway.views[sid]["basis"]["head_version"] = "2"
    with TestClient(profile_app(settings, gateway)) as restarted:
        assert asyncio.run(restarted.app.state.profile_service.reconcile_one()) is True
        assert gateway.submissions[0] == gateway.submissions[1] == original
        row = restarted.app.state.profile_repository.comparison(sid, cid)
        assert row["members"][0]["job_id"] is not None and len(gateway.jobs) == 1
        assert create(restarted, sid).json()["data"]["comparison_id"] == cid


def test_partial_full_basis_cas_failure_cancels_owned_submitted_child_and_keeps_physical_state(profile_client, profile_setup):
    gateway, service = profile_setup[1], profile_client.app.state.profile_service
    gateway.strict_cas = True
    sid, cid = batch(profile_client)
    assert asyncio.run(service.reconcile_one()) is True
    gateway.views[sid]["basis"]["head_sha256"] = "9" * 64
    before = deepcopy(gateway.views[sid])
    assert asyncio.run(service.reconcile_one()) is True
    advance(service)
    data = poll(profile_client, sid, cid).json()["data"]
    assert data["status"] == "FAILED" and data["outcome"] == {"comparison": None, "reason": "STALE_HEAD"}
    assert len(gateway.jobs) == 1 and len(gateway.cancellations) == 1
    assert next(iter(gateway.jobs.values()))["job_status"] == "FAILED" and gateway.views[sid] == before


def test_cancel_before_dispatch_creates_no_jobs_and_retry_is_immutable(profile_client, profile_setup):
    sid, cid = batch(profile_client)
    first = cancel(profile_client, sid, cid)
    assert first.status_code == 200 and first.json()["data"]["status"] == "CANCEL_REQUESTED"
    service = profile_client.app.state.profile_service
    advance(service)
    assert poll(profile_client, sid, cid).json()["data"]["status"] == "CANCELLED"
    assert cancel(profile_client, sid, cid).json()["data"] == first.json()["data"]
    assert cancel(profile_client, sid, cid, "other-cancel").json()["data"]["status"] == "COMPLETED_IMMUTABLE"
    assert profile_setup[1].submissions == profile_setup[1].cancellations == []


def test_cancel_known_running_jobs_fences_while_preserving_completed_children(profile_client, profile_setup):
    gateway, service = profile_setup[1], profile_client.app.state.profile_service
    sid, cid = batch(profile_client)
    submit_all(service)
    row = service.repository.comparison(sid, cid)
    first = gateway.jobs[(sid, row["members"][0]["job_id"])]
    gateway.completed(first)
    second = gateway.jobs[(sid, row["members"][1]["job_id"])]
    second["job_status"] = "RUNNING"
    before = deepcopy(first)
    assert cancel(profile_client, sid, cid).status_code == 200
    assert second["job_status"] == "FAILED" and first == before
    advance(service)
    assert poll(profile_client, sid, cid).json()["data"]["status"] == "CANCELLED"
    assert len(gateway.cancellations) == 3 and gateway.comparisons == []


def test_cancel_uncertain_submit_is_retrieved_and_fenced_with_no_new_siblings(profile_client, profile_setup):
    gateway, service = profile_setup[1], profile_client.app.state.profile_service
    sid, cid = batch(profile_client)
    gateway.fail_submit_after_commit = True
    assert asyncio.run(service.reconcile_one()) is False
    assert cancel(profile_client, sid, cid).status_code == 200
    advance(service)
    assert gateway.submissions[0] == gateway.submissions[1]
    assert len(gateway.jobs) == len(gateway.cancellations) == 1
    assert poll(profile_client, sid, cid).json()["data"]["status"] == "CANCELLED"


def test_cancel_uncertain_sdk_commit_retries_original_cancel_command(profile_client, profile_setup):
    gateway, service = profile_setup[1], profile_client.app.state.profile_service
    sid, cid = batch(profile_client)
    assert asyncio.run(service.reconcile_one()) is True
    gateway.fail_cancel_after_commit = True
    code(cancel(profile_client, sid, cid), 503, "RUNTIME_TIMEOUT")
    assert cancel(profile_client, sid, cid).status_code == 200
    assert gateway.cancellations[0] == gateway.cancellations[1]
    advance(service)
    assert len(gateway.jobs) == 1 and poll(profile_client, sid, cid).json()["data"]["status"] == "CANCELLED"


def existing_jobs(client, sid):
    return [optimize(client, sid, "existing-" + profile, profile=profile).json()["data"]["job_id"] for profile in PROFILES]


@pytest.mark.parametrize("cancelled", [False, True])
def test_existing_mode_never_submits_computes_accepts_or_cancels_inputs(profile_client, profile_setup, cancelled):
    gateway = profile_setup[1]
    sid = load(profile_client).json()["data"]["session"]["session_id"]
    job_ids = existing_jobs(profile_client, sid)
    certify_all(gateway, sid)
    gateway.jobs[(sid, job_ids[2])]["input_basis"]["head_version"] = "9"
    with sqlite3.connect(profile_setup[0].metadata_path) as db:
        import json
        db.execute("UPDATE compute_queue SET basis_json=? WHERE job_id=?", (json.dumps(gateway.jobs[(sid, job_ids[2])]["input_basis"]), job_ids[2]))
    inputs = deepcopy(gateway.jobs)
    result = create(profile_client, sid, job_ids=job_ids[::-1])
    assert result.status_code == 202
    cid = result.json()["data"]["comparison_id"]
    if cancelled:
        assert cancel(profile_client, sid, cid).status_code == 200
    advance(profile_client.app.state.profile_service)
    data = poll(profile_client, sid, cid).json()["data"]
    assert data["mode"] == "EXISTING_JOBS"
    if cancelled:
        assert data["status"] == "CANCELLED" and gateway.comparisons == []
    else:
        assert data["outcome"]["comparison"]["status"] == "NON_COMPARABLE"
    assert gateway.jobs == inputs and len(gateway.submissions) == 3 and gateway.computes == gateway.cancellations == []


@pytest.mark.parametrize("route", ["create", "poll", "cancel"])
def test_cross_owner_and_unauthenticated_denied_before_gateway_or_metadata_mutation(profile_client, profile_setup, route):
    sid, cid = batch(profile_client)
    gateway = profile_setup[1]
    reads = len(gateway.reads)
    response = create(profile_client, sid, "other", "bob") if route == "create" else poll(profile_client, sid, cid, "bob") if route == "poll" else cancel(profile_client, sid, cid, actor="bob")
    code(response, 403, "FORBIDDEN")
    url = f"/api/sessions/{sid}/profiles/" + ("compare" if route == "create" else f"comparisons/{cid}" + ("/cancel" if route == "cancel" else ""))
    unauth = profile_client.get(url) if route == "poll" else profile_client.post(url, json={"request_id": "unauth"})
    code(unauth, 401, "UNAUTHORIZED")
    assert len(gateway.reads) == reads and gateway.submissions == gateway.cancellations == []


def test_viewer_read_only(profile_client, profile_setup):
    sid, cid = batch(profile_client)
    with sqlite3.connect(profile_setup[0].metadata_path) as db:
        db.execute("UPDATE sessions SET owner_actor_id='viewer' WHERE session_id=?", (sid,))
    assert poll(profile_client, sid, cid, "viewer").status_code == 200
    code(create(profile_client, sid, "other", "viewer"), 403, "FORBIDDEN")
    code(cancel(profile_client, sid, cid, actor="viewer"), 403, "FORBIDDEN")


@pytest.mark.parametrize("operation", ["create", "cancel"])
@pytest.mark.parametrize("actor", [Actor("bob", "dispatcher"), Actor("alice", "viewer")])
def test_service_direct_owner_and_role_guard_precedes_sdk_or_write(profile_client, profile_setup, operation, actor):
    sid, cid = batch(profile_client)
    service, gateway = profile_client.app.state.profile_service, profile_setup[1]
    before = len(gateway.reads)
    pending = service.create(sid, CompareProfilesRequest(request_id="direct"), actor) if operation == "create" else service.cancel(sid, cid, CancelJobRequest(request_id="direct"), actor)
    with pytest.raises(ApiError) as caught:
        asyncio.run(pending)
    assert caught.value.code == "FORBIDDEN" and caught.value.status_code == 403
    assert len(gateway.reads) == before and gateway.submissions == gateway.cancellations == []
    assert service.repository.comparison(sid, cid)["state"] == "QUEUED"


def test_session_scoping_and_unknown_job_do_not_reach_sdk(profile_client, profile_setup):
    sid, cid = batch(profile_client)
    other = load(profile_client, request_id="other-session").json()["data"]["session"]["session_id"]
    code(poll(profile_client, other, cid), 404, "COMPARISON_NOT_FOUND")
    code(create(profile_client, sid, "existing", job_ids=["j1", "j2", "j3"]), 404, "JOB_NOT_FOUND")
    assert profile_setup[1].polls == []


@pytest.mark.parametrize("fields", [{"job_ids": ["j", "j", "k"]}, {"job_ids": []}, {"job_ids": ["a", "b"]},
    {"budget_seconds": 600}, {"basis": {}}, {"weights": {}}, {"profile": "BALANCED"}, {"snapshot_root": "D:/bad"}])
def test_comparison_strict_boundary_forbids_authority_budget_profile_and_duplicates(profile_client, fields):
    sid = load(profile_client).json()["data"]["session"]["session_id"]
    code(create(profile_client, sid, **fields), 422, "VALIDATION_ERROR")


def test_expected_revision_checks_before_group_creation(profile_client):
    sid = load(profile_client).json()["data"]["session"]["session_id"]
    code(create(profile_client, sid, expected_revision={"head_version": "0", "generation": "0"}), 409, "STALE_HEAD")
    assert profile_client.app.state.profile_repository.next_pending() is None


def test_simultaneous_duplicate_creation_has_one_group(profile_client, profile_setup):
    sid = load(profile_client).json()["data"]["session"]["session_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(profile_client, sid), range(2)))
    assert all(r.status_code == 202 for r in results)
    assert results[0].json()["data"] == results[1].json()["data"]
    with sqlite3.connect(profile_setup[0].metadata_path) as db:
        assert db.execute("SELECT count(*) FROM profile_comparisons").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM profile_members").fetchone()[0] == 3


@pytest.mark.parametrize("change", [lambda v: v["jobs"][0].update(profile="SAFER"),
    lambda v: v["jobs"][0]["basis"].update(head_version="5"),
    lambda v: v["jobs"][0].update(job_id="foreign-job"),
    lambda v: v.update(status="BEST_PROFILE"), lambda v: v["jobs"][0].update(metrics={"risk": float("nan")})])
def test_unverified_sdk_comparison_fails_closed(profile_client, profile_setup, change):
    sid, cid = batch(profile_client)
    service, gateway = profile_client.app.state.profile_service, profile_setup[1]
    submit_all(service)
    certify_all(gateway, sid)
    for _ in range(3):
        assert asyncio.run(service.reconcile_one()) is True
    gateway.bad_comparison = change
    with pytest.raises(ApiError) as caught:
        asyncio.run(service.reconcile_one())
    assert caught.value.code == "COMPARISON_BINDING_CHANGED"
    assert poll(profile_client, sid, cid).json()["data"]["outcome"] is None


def test_each_tick_is_bounded_and_groups_are_round_robin(profile_client, profile_setup):
    sid, cid = batch(profile_client)
    other = create(profile_client, sid, "compare-2").json()["data"]["comparison_id"]
    service, gateway = profile_client.app.state.profile_service, profile_setup[1]
    assert asyncio.run(service.reconcile_one()) is True and len(gateway.submissions) == 1
    assert asyncio.run(service.reconcile_one()) is True and len(gateway.submissions) == 2
    assert service.repository.comparison(sid, cid)["members"][0]["job_id"] is not None
    assert service.repository.comparison(sid, other)["members"][0]["job_id"] is not None


def test_cancel_cas_wins_over_inflight_comparison_forecast(profile_client, profile_setup):
    sid, cid = batch(profile_client)
    service, gateway = profile_client.app.state.profile_service, profile_setup[1]
    submit_all(service)
    certify_all(gateway, sid)
    for _ in range(3):
        asyncio.run(service.reconcile_one())
    gateway.bad_comparison = lambda _: service.repository.cancel("alice", sid, "concurrent-cancel", cid, service.sessions.installation_identity())
    assert asyncio.run(service.reconcile_one()) is True
    assert poll(profile_client, sid, cid).json()["data"]["status"] == "CANCEL_REQUESTED"
    assert poll(profile_client, sid, cid).json()["data"]["outcome"] is None
    advance(service)
    assert poll(profile_client, sid, cid).json()["data"]["status"] == "CANCELLED"
