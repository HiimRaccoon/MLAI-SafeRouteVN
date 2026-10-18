"""Immutable owner exports, audit completeness and honest historical evidence."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import sqlite3
import threading

from fastapi.testclient import TestClient
import pytest

from backend.api.errors import ApiError
from backend.api.main import create_app
from backend.services.artifact_repository import ArtifactRepository, canonical_bytes
from backend.services.auth import Actor
from backend.tests.test_jobs import optimize
from backend.tests.test_plans import PlanGateway, accept
from backend.tests.test_sessions import code, headers, load, setup


class ArtifactGateway(PlanGateway):
    def __init__(self, build):
        super().__init__(build)
        self.validation_calls = []
        self.validation_delay = 0
        self.validation_started = threading.Event()
        self.validation_error = None
        self.validation_override = None
        self.during_validation = None

    async def validate_session(self, session_id):
        self.validation_calls.append(session_id)
        self.validation_started.set()
        if self.validation_delay:
            await asyncio.sleep(self.validation_delay)
        if self.validation_error:
            raise self.validation_error
        if self.during_validation:
            self.during_validation()
        return deepcopy(self.validation_override) if self.validation_override is not None else {
            "validator_version": "task02-m2-bound-session-validator/2", "valid": True,
            "checked_physical_mutations": 0, "historical_execution_builds_preserved": True,
            "current_checker_build_sha256": self.build, "scope": "TRUSTED_LOG_RAW_PREFIX_EVENT_SUFFIX; SIMULATED_REPLAY; NOT_OPTIMALITY"}


@pytest.fixture
def artifacts_setup(setup):
    settings, original = setup
    return settings, ArtifactGateway(original.build)


@pytest.fixture
def artifacts_client(artifacts_setup):
    settings, gateway = artifacts_setup
    with TestClient(create_app(settings, gateway=gateway), raise_server_exceptions=False) as client:
        yield client


def export(client, session_id, request_id="export-1", actor="alice", **extra):
    return client.post(f"/api/sessions/{session_id}/artifacts", headers=headers(actor), json={"request_id": request_id, **extra})


def content(client, session_id, artifact_id, actor="alice"):
    return client.get(f"/api/sessions/{session_id}/artifacts/{artifact_id}/content", headers=headers(actor))


def exported(client, session_id, request_id="export-1"):
    response = export(client, session_id, request_id)
    assert response.status_code == 201, response.text
    manifest = response.json()["data"]
    response = content(client, session_id, manifest["artifact_id"])
    assert response.status_code == 200, response.text
    wrapper = response.json()["data"]
    bundle = json.loads(wrapper["content_utf8"])
    files = {file["name"]: json.loads(file["content_utf8"]) for file in bundle["files"]}
    return manifest, wrapper, bundle, files


def session(client, scenario="S0", request_id="load-1", actor="alice"):
    response = load(client, scenario, request_id, actor)
    assert response.status_code == 201, response.text
    return response.json()["data"]["session"]["session_id"]


def advance_fake(gateway, session_id):
    gateway.views[session_id]["basis"]["head_version"] = str(int(gateway.views[session_id]["basis"]["head_version"]) + 1)
    gateway.views[session_id]["basis"]["head_sha256"] = "f" * 64
    gateway.views[session_id]["current_time"] = "2026-09-27T21:01:00+07:00"


def test_export_has_exact_bundle_and_file_hashes_public_labels_and_no_private_paths(artifacts_client, artifacts_setup):
    sid = session(artifacts_client, "S2")
    before = deepcopy(artifacts_setup[1].views[sid])
    manifest, wrapper, bundle, files = exported(artifacts_client, sid)
    raw = wrapper["content_utf8"].encode("utf-8")
    assert hashlib.sha256(raw).hexdigest() == wrapper["sha256"] and len(raw) == wrapper["bytes"]
    assert canonical_bytes(bundle) == raw and bundle["manifest"] == manifest
    assert manifest["status"] == "READY" and manifest["real_world_observation"] is False
    for item in manifest["files"]:
        file = next(file for file in bundle["files"] if file["name"] == item["name"])
        encoded = file["content_utf8"].encode("utf-8")
        assert len(encoded) == item["bytes"] and hashlib.sha256(encoded).hexdigest() == item["sha256"]
        assert item["media_type"] == "application/json" and "/" not in item["name"]
    assert files["execution_frames.json"]["frames"][-1]["public_view"] == before
    assert files["execution_frames.json"]["frames"][0]["capture_time_known"] is False
    assert files["validation.json"]["receipt"]["valid"] is True
    assert files["events.json"]["source_events"][0]["event_id"] == "urgent-1"
    assert "EXPOSURE_IS_PROXY" in manifest["limitations"] and "SIMULATED_REPLAY_NOT_GPS" in manifest["limitations"]
    assert artifacts_setup[1].views[sid] == before and not artifacts_setup[1].accepts and not artifacts_setup[1].computes
    for private in (str(artifacts_setup[0].installation_path), "claim_token", "claim_expires", "token_sha256", "auth_tokens", "backend_authority.sqlite"):
        assert private not in raw.decode()


def test_retry_and_reads_use_same_committed_bytes_without_fresh_sdk_checks(artifacts_client, artifacts_setup):
    gateway = artifacts_setup[1]
    sid = session(artifacts_client)
    manifest, original, _, _ = exported(artifacts_client, sid)
    calls = (len(gateway.validation_calls), len(gateway.reads))
    advance_fake(gateway, sid)
    gateway.validation_error = ApiError(503, "STORE_INVALID", "runtime", "SDK temporarily unavailable")
    assert export(artifacts_client, sid).json()["data"] == manifest
    assert content(artifacts_client, sid, manifest["artifact_id"]).json()["data"] == original
    assert artifacts_client.get(manifest["links"]["manifest"], headers=headers()).json()["data"] == manifest
    assert (len(gateway.validation_calls), len(gateway.reads)) == calls
    assert len(artifacts_client.get(f"/api/sessions/{sid}/artifacts", headers=headers()).json()["data"]["artifacts"]) == 1


def test_new_request_captures_later_view_and_preserves_old_bundle(artifacts_client, artifacts_setup):
    sid = session(artifacts_client)
    first, original, _, _ = exported(artifacts_client, sid)
    advance_fake(artifacts_setup[1], sid)
    later, _, _, files = exported(artifacts_client, sid, "export-2")
    assert first["artifact_id"] != later["artifact_id"] and later["captured_basis"]["head_version"] == "2"
    assert content(artifacts_client, sid, first["artifact_id"]).json()["data"] == original
    assert files["execution_frames.json"]["frames"][-1]["basis"] == later["captured_basis"]


@pytest.mark.parametrize("suffix", ["", "/content"])
def test_artifact_owner_and_session_scope_are_enforced_before_sdk(artifacts_client, artifacts_setup, suffix):
    sid = session(artifacts_client)
    manifest, _, _, _ = exported(artifacts_client, sid)
    calls = len(artifacts_setup[1].reads)
    code(artifacts_client.get(manifest["links"]["manifest"] + suffix, headers=headers("bob")), 403, "FORBIDDEN")
    code(export(artifacts_client, sid, actor="bob"), 403, "FORBIDDEN")
    assert len(artifacts_setup[1].reads) == calls
    other = session(artifacts_client, request_id="load-2", actor="bob")
    code(artifacts_client.get(f"/api/sessions/{other}/artifacts/{manifest['artifact_id']}" + suffix, headers=headers("bob")), 404, "ARTIFACT_NOT_FOUND")
    assert artifacts_client.get(manifest["links"]["manifest"] + suffix).status_code == 401


def test_current_owner_controls_export_access_and_owned_viewer_can_read(artifacts_client, artifacts_setup):
    sid = session(artifacts_client)
    manifest, _, _, _ = exported(artifacts_client, sid)
    with sqlite3.connect(artifacts_setup[0].metadata_path) as db:
        db.execute("UPDATE sessions SET owner_actor_id='viewer' WHERE session_id=?", (sid,))
    code(content(artifacts_client, sid, manifest["artifact_id"]), 403, "FORBIDDEN")
    assert content(artifacts_client, sid, manifest["artifact_id"], "viewer").status_code == 200
    code(export(artifacts_client, sid, actor="viewer"), 403, "FORBIDDEN")


@pytest.mark.parametrize("extra", [{"path": "D:/secret"}, {"basis": {}}, {"store_path": "D:/store"}, {"actor": "bob"}])
def test_export_request_never_accepts_client_paths_or_trust_fields(artifacts_client, artifacts_setup, extra):
    sid = session(artifacts_client)
    code(artifacts_client.post(f"/api/sessions/{sid}/artifacts", headers=headers(), json={"request_id": "export-1", **extra}), 422, "VALIDATION_ERROR")
    assert artifacts_setup[1].validation_calls == []


def test_concurrent_same_request_claims_one_capture(artifacts_client, artifacts_setup):
    sid = session(artifacts_client)
    gateway = artifacts_setup[1]
    gateway.validation_delay = 0.15
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(export, artifacts_client, sid)
        assert gateway.validation_started.wait(2)
        second = pool.submit(export, artifacts_client, sid)
        responses = [first.result(), second.result()]
    assert sorted(response.status_code for response in responses) == [201, 409]
    code(next(response for response in responses if response.status_code == 409), 409, "REQUEST_IN_PROGRESS")
    assert len(gateway.validation_calls) == 1


def test_changed_state_during_validation_never_commits_mixed_snapshot(artifacts_client, artifacts_setup):
    sid = session(artifacts_client)
    gateway = artifacts_setup[1]
    gateway.during_validation = lambda: advance_fake(gateway, sid)
    code(export(artifacts_client, sid), 409, "ARTIFACT_STATE_CHANGED")
    with sqlite3.connect(artifacts_setup[0].metadata_path) as db:
        artifact_id, payload = db.execute("SELECT artifact_id,bundle FROM artifacts").fetchone()
        assert payload is None
    gateway.during_validation = None
    assert exported(artifacts_client, sid)[0]["artifact_id"] == artifact_id


@pytest.mark.parametrize("bad", ["session", "hash", "counter", "unexpected", "validation"])
def test_invalid_public_evidence_fails_closed_before_bundle_commit(artifacts_client, artifacts_setup, bad):
    sid = session(artifacts_client)
    gateway = artifacts_setup[1]
    if bad == "session": gateway.views[sid]["basis"]["session_id"] = "wrong-session"
    elif bad == "hash": gateway.views[sid]["basis"]["head_sha256"] = ""
    elif bad == "counter": gateway.views[sid]["basis"]["head_version"] = "01"
    elif bad == "unexpected": gateway.views[sid]["private_authority"] = {"path": "secret"}
    else:
        gateway.validation_override = {"valid": False}
    code(export(artifacts_client, sid), 503, "ARTIFACT_BINDING_CHANGED")
    with sqlite3.connect(artifacts_setup[0].metadata_path) as db:
        assert db.execute("SELECT count(*) FROM artifacts WHERE bundle IS NOT NULL").fetchone()[0] == 0


def test_no_witness_job_export_preserves_search_limit_and_no_fabricated_plan(artifacts_client, artifacts_setup):
    sid = session(artifacts_client)
    response = optimize(artifacts_client, sid)
    jid = response.json()["data"]["job_id"]
    artifacts_setup[1].jobs[(sid, jid)].update(job_status="COMPLETED", internal_status="SEARCH_LIMIT", business_status="SEARCH_LIMIT",
        diagnostics=[{"severity": "ERROR", "code": "SEARCH_LIMIT", "path": "job", "message": "No witness; not infeasibility"}])
    _, _, _, files = exported(artifacts_client, sid)
    view = files["jobs.json"]["public_job_views"][0]["public_view"]
    assert view["business_status"] == "SEARCH_LIMIT" and view["plan_available"] is False
    assert view["coverage_evaluated"] is False and view["validation"]["valid"] is None
    assert files["accepted_trajectories.json"]["records"] == []


def test_wrong_job_input_binding_rejects_export(artifacts_client, artifacts_setup):
    sid = session(artifacts_client)
    jid = optimize(artifacts_client, sid).json()["data"]["job_id"]
    artifacts_setup[1].jobs[(sid, jid)]["input_basis"]["head_sha256"] = "d" * 64
    code(export(artifacts_client, sid), 503, "ARTIFACT_BINDING_CHANGED")


def test_all_committed_history_exports_beyond_http_history_limit_and_retains_ack(artifacts_client, artifacts_setup):
    sid = session(artifacts_client)
    for index in range(103):
        response = artifacts_client.post(f"/api/sessions/{sid}/replay/pause", headers=headers(), json={"request_id": f"pause-{index}"})
        assert response.status_code == 200, response.text
    outbox = artifacts_client.app.state.outbox_repository
    installation = artifacts_setup[0].installation_identity()
    notifications = [{"session_id": sid, "event_id": hashlib.sha256(str(i).encode()).hexdigest(), "kind": "RECEIPT", "body_sha256": "e" * 64} for i in range(102)]
    outbox.ingest(sid, installation, notifications)
    first = outbox.pending(sid)[0]
    outbox.complete_ack(sid, first["event_id"], first["command_id"], {"status": "ACKNOWLEDGED", "event_id": first["event_id"]})
    _, _, _, files = exported(artifacts_client, sid)
    assert len(artifacts_client.get(f"/api/sessions/{sid}/replay/history", headers=headers()).json()["data"]["history"]) == 100
    assert len(files["events.json"]["replay_audit"]) == 103 and len(files["requests.json"]["records"]) == 104
    assert len(files["notifications.json"]["records"]) == 102
    assert files["notifications.json"]["records"][0]["ack_receipt"]["status"] == "ACKNOWLEDGED"
    assert files["notifications.json"]["records"][0]["cursor"] == "1"


def test_metadata_change_during_capture_requires_retry_and_pending_requests_are_honest(artifacts_client, artifacts_setup):
    sid = session(artifacts_client)
    settings, gateway = artifacts_setup
    artifacts_client.app.state.job_repository.reserve_request("alice", sid, "optimize", "pending-submit", "a" * 64,
        settings.installation_identity(), basis=gateway.views[sid]["basis"], profile="BALANCED", budget_seconds=60)
    outbox = artifacts_client.app.state.outbox_repository
    gateway.during_validation = lambda: outbox.ingest(sid, settings.installation_identity(),
        [{"session_id": sid, "event_id": "d" * 64, "kind": "RECEIPT", "body_sha256": "e" * 64}])
    code(export(artifacts_client, sid), 409, "ARTIFACT_STATE_CHANGED")
    gateway.during_validation = None
    _, _, _, files = exported(artifacts_client, sid)
    pending = next(item for item in files["requests.json"]["records"] if item["request_id"] == "pending-submit")
    assert pending["metadata_status"] == "PENDING_UNCERTAIN" and "receipt" not in pending
    assert len(files["notifications.json"]["records"]) == 1


def test_historical_trajectory_gaps_are_explicit_and_future_observations_immutable(artifacts_client, artifacts_setup):
    sid = session(artifacts_client)
    gateway = artifacts_setup[1]
    jid = optimize(artifacts_client, sid).json()["data"]["job_id"]
    gateway.certify(sid, jid)
    assert accept(artifacts_client, sid, jid).status_code == 200
    repository = artifacts_client.app.state.artifact_repository
    first = repository.observe_job(sid, jid, gateway.jobs[(sid, jid)], source="WORKER_TERMINAL", reference_id="compute-1")
    assert repository.observe_job(sid, jid, gateway.jobs[(sid, jid)], source="WORKER_TERMINAL", reference_id="compute-1") == first
    # A historical missing trajectory is distinct from the current public
    # accepted trajectory, which must always bind to the active job.
    current_job = optimize(artifacts_client, sid, request_id="later-job").json()["data"]["job_id"]
    gateway.certify(sid, current_job)
    assert accept(artifacts_client, sid, current_job, request_id="later-accept",
        expected={key: gateway.views[sid]["basis"][key] for key in ("head_version", "generation")}).status_code == 200
    gateway.views[sid]["accepted_trajectory"] = {"job_id": current_job, "profile": "BALANCED",
        "forecast": True, "domain_sha256": "d" * 64, "vehicle_routes": []}
    gateway.views[sid]["unserved"] = []
    _, _, _, files = exported(artifacts_client, sid)
    assert files["accepted_trajectories.json"]["missing_historical_job_ids"] == [jid]
    assert any(item["evidence_id"] == first["evidence_id"] for item in files["jobs.json"]["public_job_views"] if "evidence_id" in item)
    gateway.jobs[(sid, jid)]["input_basis"]["head_sha256"] = "f" * 64
    with pytest.raises(ApiError) as raised:
        repository.observe_job(sid, jid, gateway.jobs[(sid, jid)], source="WORKER_TERMINAL")
    assert raised.value.code == "EVIDENCE_BINDING_CHANGED"


def test_wrapped_recovery_audit_keeps_public_validation_and_fencing_lineage(artifacts_client, artifacts_setup):
    sid = session(artifacts_client)
    gateway = artifacts_setup[1]
    validation = asyncio.run(gateway.validate_session(sid))
    receipt = {"status": "RECOVERED", "fenced_jobs": [], "basis": deepcopy(gateway.views[sid]["basis"])}
    recovery = artifacts_client.app.state.recovery_repository
    recovery.record(sid, "VERIFIED", "worker-test", artifacts_setup[0].installation_identity(), validation=validation, basis=receipt["basis"])
    recovery.audit(sid, "worker-test", "recover-test", "RECOVER_VALIDATE", response={"recovery": receipt, "validation": validation})
    _, _, _, files = exported(artifacts_client, sid)
    assert files["recovery.json"]["audit"][0]["response"] == {"recovery": receipt, "validation": validation}
    assert files["recovery.json"]["status"][0]["validation"] == validation


def test_bundle_commit_failure_retries_same_artifact_without_publishing_partial_bytes(artifacts_client, artifacts_setup):
    sid = session(artifacts_client)
    with sqlite3.connect(artifacts_setup[0].metadata_path) as db:
        db.execute("CREATE TRIGGER abort_artifact BEFORE UPDATE OF bundle ON artifacts BEGIN SELECT RAISE(ABORT,'Injected bundle commit failure'); END")
    code(export(artifacts_client, sid), 503, "METADATA_UNAVAILABLE")
    with sqlite3.connect(artifacts_setup[0].metadata_path) as db:
        artifact_id, blob = db.execute("SELECT artifact_id,bundle FROM artifacts").fetchone()
        assert blob is None
        db.execute("DROP TRIGGER abort_artifact")
    assert exported(artifacts_client, sid)[0]["artifact_id"] == artifact_id


@pytest.mark.parametrize("bad_blob", [b"corrupt", "wrong-sqlite-type"])
def test_artifact_corruption_is_typed_and_never_served(artifacts_client, artifacts_setup, bad_blob):
    sid = session(artifacts_client)
    manifest, _, _, _ = exported(artifacts_client, sid)
    with sqlite3.connect(artifacts_setup[0].metadata_path) as db:
        db.execute("UPDATE artifacts SET bundle=? WHERE artifact_id=?", (bad_blob, manifest["artifact_id"]))
    code(content(artifacts_client, sid, manifest["artifact_id"]), 503, "ARTIFACT_CORRUPT")
    code(export(artifacts_client, sid), 503, "ARTIFACT_CORRUPT")


def test_restart_preserves_export_bytes_and_owner(artifacts_setup):
    settings, gateway = artifacts_setup
    with TestClient(create_app(settings, gateway=gateway)) as client:
        sid = session(client)
        manifest, wrapper, _, _ = exported(client, sid)
    with TestClient(create_app(settings, gateway=gateway)) as restarted:
        assert export(restarted, sid).json()["data"] == manifest
        assert content(restarted, sid, manifest["artifact_id"]).json()["data"] == wrapper
        code(content(restarted, sid, manifest["artifact_id"], "bob"), 403, "FORBIDDEN")


def test_service_itself_checks_owner_and_repository_fences_expired_capture(artifacts_client, artifacts_setup):
    sid = session(artifacts_client)
    with pytest.raises(ApiError) as raised:
        asyncio.run(artifacts_client.app.state.artifact_service.create(sid, "direct", Actor("bob", "dispatcher")))
    assert raised.value.status_code == 403
    repository = ArtifactRepository(artifacts_setup[0].metadata_path)
    row = repository.reserve("alice", sid, "lease-test", "a" * 64, artifacts_setup[0].installation_identity())
    with sqlite3.connect(artifacts_setup[0].metadata_path) as db:
        db.execute("UPDATE artifacts SET claim_expires=0 WHERE artifact_id=?", (row["artifact_id"],))
    with pytest.raises(ApiError) as raised:
        repository.complete(row, {"manifest": {}})
    assert raised.value.code == "ARTIFACT_LEASE_LOST"
