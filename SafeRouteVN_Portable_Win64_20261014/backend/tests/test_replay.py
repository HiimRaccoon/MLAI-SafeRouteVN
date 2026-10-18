"""Manual observed replay, event barriers and durable SDK command receipts.

All source retiming and pin rebuilding happen inside pytest's private fixture
directory before catalog initialization. Frozen project data is untouched.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from backend.api.errors import ApiError
from backend.api.main import create_app
from backend.services.compute_worker import ComputeWorker
import backend.services.heartbeat_io as heartbeat_module
from backend.services.readiness import Readiness
from backend.services.runtime_gateway import RuntimeGateway
import backend.services.runtime_gateway as gateway_module
from backend.tests.test_jobs import optimize
from backend.tests.test_plans import PlanGateway, accept, certified, revision
from backend.tests.test_sessions import EPOCH, code, headers, load, setup, write

BARRIER = "2026-09-27T21:01:30.250000+07:00"
FIRST_STEP = "2026-09-27T21:01:00+07:00"


def retime_fixture(settings):
    config = settings.installation()
    suite = "thu-duc-binh-thanh-v1"
    path = settings.project_root / f"scenarios/fixtures/{suite}/S2.json"
    fixture = json.loads(path.read_text())
    fixture["events"][0]["timestamp"] = BARRIER
    fixture_sha = write(path, fixture)
    catalog_path = settings.project_root / f"scenarios/manifests/{suite}.json"
    catalog = json.loads(catalog_path.read_text())
    catalog["scenarios"]["S2"]["sha256"] = fixture_sha
    catalog_sha = write(catalog_path, catalog)
    runtime = Path(config["runtime_root"])
    receipt_name = "optimization/integration/member1_trusted_receipt.json"
    receipt_path = runtime / receipt_name
    receipt = json.loads(receipt_path.read_text())
    receipt["fixture_raw_sha256"]["S2"] = fixture_sha
    receipt["catalog_raw_sha256"] = catalog_sha
    receipt_sha = write(receipt_path, receipt)
    inventory = {"files": {receipt_name: {"bytes": receipt_path.stat().st_size, "sha256": receipt_sha}}}
    build = hashlib.sha256(json.dumps(inventory, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    write(runtime / "production_inventory.json", {**inventory, "build_sha256": build})
    config["expected_build_sha256"] = build
    write(settings.installation_path, config)
    g0_path = Path(config["latest_preflight_receipt"])
    g0 = json.loads(g0_path.read_text())
    g0["build_sha256"] = build
    write(g0_path, g0)
    return build


class ReplayGateway(PlanGateway):
    def __init__(self, build):
        super().__init__(build)
        self.advances, self.applies, self.replay_receipts = [], [], {}
        self.fail_mutation_after_commit = False
        self.fail_resolve_after_mutation = False
        self.corrupt_replay_result = None
        self.advance_participants, self.advance_arrived = 0, 0
        self.advance_gate = asyncio.Event()

    async def resolve(self, session_id):
        if self.fail_resolve_after_mutation and self.replay_receipts:
            self.fail_resolve_after_mutation = False
            raise ApiError(503, "RUNTIME_TIMEOUT", "runtime", "Live view temporarily unavailable")
        return await super().resolve(session_id)

    def output(self, result):
        if self.fail_mutation_after_commit:
            self.fail_mutation_after_commit = False
            raise ApiError(503, "RUNTIME_TIMEOUT", "runtime", "Response interrupted after observed mutation commit")
        out = deepcopy(result)
        if self.corrupt_replay_result:
            field, value = self.corrupt_replay_result
            if field == "$result":
                return value
            if field.startswith("omit:basis:"):
                out["basis"].pop(field.removeprefix("omit:basis:"), None)
            elif field.startswith("omit:receipt:"):
                out.pop(field.removeprefix("omit:receipt:"), None)
            elif field == "basis":
                out["basis"] = value
            elif field in ("status", "event_id", "event_sha256"):
                out[field] = value
            else:
                out["basis"][field] = value
        return out

    async def advance(self, session_id, command_id, basis, target_time):
        self.advances.append((session_id, command_id, deepcopy(basis), target_time))
        if self.advance_participants:
            self.advance_arrived += 1
            if self.advance_arrived >= self.advance_participants:
                self.advance_gate.set()
            await asyncio.wait_for(self.advance_gate.wait(), 3)
        key = (session_id, command_id)
        if key not in self.replay_receipts:
            view = self.views[session_id]
            if basis != view["basis"]:
                raise ApiError(409, "STALE_HEAD", "basis", "Observed replay input no longer matches head")
            if datetime.fromisoformat(target_time) == datetime.fromisoformat(view["current_time"]):
                status = "NOOP"
            else:
                status = "ADVANCED"
                view["current_time"] = target_time
                view["basis"]["head_version"] = str(int(view["basis"]["head_version"]) + 1)
                view["basis"]["head_sha256"] = hashlib.sha256(target_time.encode()).hexdigest()
                view["observed_metrics"] = {"scope": "TEST_ONLY_OBSERVED_PREFIX", "distance_m": 0}
            self.replay_receipts[key] = {"status": status, "basis": deepcopy(view["basis"])}
        return self.output(self.replay_receipts[key])

    async def apply_event(self, session_id, command_id, basis, event_id):
        self.applies.append((session_id, command_id, deepcopy(basis), event_id))
        key = (session_id, command_id)
        if key not in self.replay_receipts:
            view = self.views[session_id]
            if basis != view["basis"]:
                raise ApiError(409, "STALE_HEAD", "basis", "Event input no longer matches head")
            view["pending_event_ids"].remove(event_id)
            view["order_ids"].append("O3")
            view["active_job_id"] = None
            view["planned_served_suffix"] = []
            view["basis"]["head_version"] = str(int(view["basis"]["head_version"]) + 1)
            view["basis"]["head_sha256"] = hashlib.sha256(event_id.encode()).hexdigest()
            self.replay_receipts[key] = {"status": "APPLIED", "event_id": event_id, "event_sha256": "d" * 64,
                                         "basis": deepcopy(view["basis"])}
        return self.output(self.replay_receipts[key])


@pytest.fixture
def replay_setup(setup):
    settings, _ = setup
    return settings, ReplayGateway(retime_fixture(settings))


@pytest.fixture
def replay_client(replay_setup):
    settings, gateway = replay_setup
    with TestClient(create_app(settings, gateway=gateway), raise_server_exceptions=False) as client:
        yield client


def active_session(client, gateway, scenario="S0"):
    session_id = load(client, scenario).json()["data"]["session"]["session_id"]
    response = optimize(client, session_id)
    assert response.status_code == 202, response.text
    job_id = response.json()["data"]["job_id"]
    gateway.certify(session_id, job_id)
    activation = accept(client, session_id, job_id)
    assert activation.status_code == 200, activation.text
    return session_id, job_id


def step(client, server_session_id, expected, request_id="step-1", actor="alice", **fields):
    return client.post(f"/api/sessions/{server_session_id}/replay/step", headers=headers(actor),
                       json={"request_id": request_id, "expected_revision": expected, **fields})


def apply(client, server_session_id, expected, request_id="apply-1", event_id="urgent-1", actor="alice", **fields):
    return client.post(f"/api/sessions/{server_session_id}/events/{event_id}/apply", headers=headers(actor),
                       json={"request_id": request_id, "expected_revision": expected, **fields})


def control(client, session_id, operation, request_id="control-1", actor="alice", **fields):
    return client.post(f"/api/sessions/{session_id}/replay/{operation}", headers=headers(actor),
                       json={"request_id": request_id, **fields})


def history(client, session_id, actor="alice"):
    return client.get(f"/api/sessions/{session_id}/replay/history", headers=headers(actor))


def events(client, session_id, actor="alice"):
    return client.get(f"/api/sessions/{session_id}/events", headers=headers(actor))


def due_event(client, gateway):
    session_id, job_id = active_session(client, gateway, "S2")
    response = step(client, session_id, revision(gateway, session_id), target_time=BARRIER)
    assert response.status_code == 200, response.text
    return session_id, job_id


def test_step_advances_manual_observation_and_records_exact_basis(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, job_id = active_session(replay_client, gateway)
    before = deepcopy(gateway.views[session_id])
    response = step(replay_client, session_id, revision(gateway, session_id))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["schema_version"] == "saferoute-m3-replay-view/1"
    receipt = data["receipt"]
    assert receipt["schema_version"] == "saferoute-m3-replay-receipt/1" and receipt["status"] == "ADVANCED"
    assert receipt["operation"] == "advance" and receipt["input_basis"] == before["basis"]
    assert receipt["basis"]["head_version"] == "2" and receipt["basis"]["generation"] == "1"
    assert receipt["mutation_id"] and receipt["recorded_at"]
    assert receipt["links"]["state"] == f"/api/sessions/{session_id}/state"
    assert datetime.fromisoformat(receipt["target_time"]) == datetime.fromisoformat(FIRST_STEP)
    current = data["execution_view"]
    assert datetime.fromisoformat(current["current_time"]) == datetime.fromisoformat(FIRST_STEP)
    assert current["active_job_id"] == job_id and current["observed_metrics"] is not None
    assert current["execution_mode"] == "SIMULATED_REPLAY" and current["real_world_observation"] is False
    assert current["delivered_prefix"] == [] and current["vehicles"] == before["vehicles"]


def test_default_step_caps_exact_fractional_pending_barrier_and_then_requires_event(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway, "S2")
    pending = events(replay_client, session_id).json()["data"]
    assert pending["schema_version"] == "saferoute-m3-pending-events/1"
    assert pending["events"][0]["timestamp"] == BARRIER and pending["events"][0]["apply_allowed"] is False
    assert step(replay_client, session_id, revision(gateway, session_id)).status_code == 200
    response = step(replay_client, session_id, revision(gateway, session_id), request_id="step-2")
    assert response.status_code == 200, response.text
    assert datetime.fromisoformat(response.json()["data"]["receipt"]["target_time"]) == datetime.fromisoformat(BARRIER)
    assert events(replay_client, session_id).json()["data"]["events"][0]["apply_allowed"] is True
    count = len(gateway.advances)
    code(step(replay_client, session_id, revision(gateway, session_id), request_id="step-3"), 409, "EVENT_TRANSITION_REQUIRED")
    assert len(gateway.advances) == count


def test_explicit_current_time_noop_preserves_all_basis_and_observation(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway)
    before = deepcopy(gateway.views[session_id])
    response = step(replay_client, session_id, revision(gateway, session_id), target_time=EPOCH)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["receipt"]["status"] == "NOOP"
    assert response.json()["data"]["receipt"]["basis"] == before["basis"]
    assert response.json()["data"]["execution_view"] == before


@pytest.mark.parametrize("target,diagnostic", [("2026-09-27T20:59:59+07:00", "TIME_REWIND"),
    ("2026-09-27T21:01:30.250001+07:00", "EVENT_TRANSITION_REQUIRED")])
def test_explicit_time_cannot_rewind_or_cross_fractional_barrier(replay_client, replay_setup, target, diagnostic):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway, "S2")
    code(step(replay_client, session_id, revision(gateway, session_id), target_time=target), 409, diagnostic)
    assert gateway.advances == []


def test_step_requires_an_active_accepted_plan(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id = load(replay_client).json()["data"]["session"]["session_id"]
    assert step(replay_client, session_id, revision(gateway, session_id)).status_code == 409
    assert gateway.advances == []


def test_event_at_exact_barrier_updates_head_and_suspends_plan(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, _ = due_event(replay_client, gateway)
    before = deepcopy(gateway.views[session_id])
    response = apply(replay_client, session_id, revision(gateway, session_id))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    receipt, current = data["receipt"], data["execution_view"]
    assert receipt["status"] == "APPLIED" and receipt["operation"] == "apply_event"
    assert receipt["event_id"] == "urgent-1" and receipt["event_sha256"] == "d" * 64
    assert receipt["input_basis"] == before["basis"]
    assert int(receipt["basis"]["head_version"]) == int(before["basis"]["head_version"]) + 1
    assert receipt["basis"]["generation"] == before["basis"]["generation"]
    assert current["active_job_id"] is None and current["pending_event_ids"] == []
    assert current["order_ids"] == ["O1", "O2", "O3"] and current["current_time"] == before["current_time"]
    assert current["delivered_prefix"] == before["delivered_prefix"] and current["vehicles"] == before["vehicles"]
    assert events(replay_client, session_id).json()["data"]["events"] == []
    code(apply(replay_client, session_id, revision(gateway, session_id), request_id="apply-again"), 409, "EVENT_ALREADY_APPLIED")


def test_event_apply_can_use_observed_head_with_no_active_plan(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, _ = due_event(replay_client, gateway)
    gateway.views[session_id]["active_job_id"] = None
    assert gateway.views[session_id]["observed_metrics"] is not None
    assert apply(replay_client, session_id, revision(gateway, session_id)).status_code == 200


def test_event_requires_due_time_and_own_observation(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway, "S2")
    code(apply(replay_client, session_id, revision(gateway, session_id)), 409, "EVENT_NOT_DUE")
    gateway.views[session_id]["current_time"] = BARRIER
    code(apply(replay_client, session_id, revision(gateway, session_id)), 409, "ACCEPTED_REPLAY_REQUIRED")
    assert gateway.applies == []


def test_unknown_fixture_event_and_unknown_pending_metadata_fail_closed(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway, "S2")
    code(apply(replay_client, session_id, revision(gateway, session_id), event_id="missing"), 404, "EVENT_NOT_FOUND")
    gateway.views[session_id]["pending_event_ids"].append("unverified-event")
    code(events(replay_client, session_id), 503, "EVENT_METADATA_UNAVAILABLE")
    assert gateway.applies == []


@pytest.mark.parametrize("operation", ["step", "apply"])
def test_new_mutation_requires_current_revision(replay_client, replay_setup, operation):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway, "S2")
    expected = {"head_version": "0", "generation": "0"}
    response = step(replay_client, session_id, expected) if operation == "step" else apply(replay_client, session_id, expected)
    code(response, 409, "STALE_HEAD")
    assert gateway.advances == gateway.applies == []


def test_pause_ack_is_durable_and_does_not_mutate_runtime_or_advance_time(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway)
    before = deepcopy(gateway.views[session_id])
    first = control(replay_client, session_id, "pause")
    assert first.status_code == 200, first.text
    receipt = first.json()["data"]["receipt"]
    assert receipt["status"] == "PAUSED" and receipt["operation"] == "pause"
    assert receipt["input_basis"] == receipt["basis"] == before["basis"]
    assert control(replay_client, session_id, "pause").json()["data"]["receipt"] == receipt
    assert first.json()["data"]["execution_view"] == before
    assert gateway.advances == gateway.applies == []
    assert history(replay_client, session_id).json()["data"]["history"] == [receipt]


def test_reset_creates_independent_same_fixture_session_and_preserves_old_history(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, job_id = active_session(replay_client, gateway, "S2")
    assert step(replay_client, session_id, revision(gateway, session_id)).status_code == 200
    original_view = deepcopy(gateway.views[session_id])
    original_history = history(replay_client, session_id).json()["data"]
    reset = control(replay_client, session_id, "reset")
    assert reset.status_code == 200, reset.text
    data = reset.json()["data"]
    assert data["schema_version"] == "saferoute-m3-replay-reset/1" and data["source_session_id"] == session_id
    fresh = data["session"]["session_id"]
    assert fresh != session_id and data["session"]["scenario_id"] == "S2"
    assert data["execution_view"]["current_time"] == EPOCH and data["execution_view"]["active_job_id"] is None
    assert data["execution_view"]["observed_metrics"] is None and data["execution_view"]["delivered_prefix"] == []
    assert control(replay_client, session_id, "reset").json()["data"]["session"]["session_id"] == fresh
    assert control(replay_client, session_id, "reset", request_id="another-reset").json()["data"]["session"]["session_id"] != fresh
    assert gateway.views[session_id] == original_view and original_view["active_job_id"] == job_id
    prior_receipts = original_history["history"]
    current_history = history(replay_client, session_id).json()["data"]["history"]
    assert all(receipt in current_history for receipt in prior_receipts)
    assert len([receipt for receipt in current_history if receipt["operation"] == "reset"]) == 2
    assert reset.json()["data"]["receipt"]["new_session_id"] == fresh
    assert history(replay_client, fresh).json()["data"]["history"] == []


@pytest.mark.parametrize("operation", ["events", "history", "step", "apply", "pause", "reset"])
def test_cross_owner_is_rejected_before_runtime_reads_and_writes(replay_client, replay_setup, operation):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway, "S2")
    reads = len(gateway.reads)
    if operation == "events":
        response = events(replay_client, session_id, "bob")
    elif operation == "history":
        response = history(replay_client, session_id, "bob")
    elif operation == "step":
        response = step(replay_client, session_id, revision(gateway, session_id), actor="bob")
    elif operation == "apply":
        response = apply(replay_client, session_id, revision(gateway, session_id), actor="bob")
    else:
        response = control(replay_client, session_id, operation, actor="bob")
    code(response, 403, "FORBIDDEN")
    assert len(gateway.reads) == reads and gateway.advances == gateway.applies == []


def test_owned_viewer_can_read_but_cannot_replay_or_reset(replay_client, replay_setup):
    settings, gateway = replay_setup
    session_id, _ = active_session(replay_client, gateway, "S2")
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("UPDATE sessions SET owner_actor_id='viewer' WHERE session_id=?", (session_id,))
    assert events(replay_client, session_id, "viewer").status_code == 200
    assert history(replay_client, session_id, "viewer").status_code == 200
    code(step(replay_client, session_id, revision(gateway, session_id), actor="viewer"), 403, "FORBIDDEN")
    code(apply(replay_client, session_id, revision(gateway, session_id), actor="viewer"), 403, "FORBIDDEN")
    for operation in ("pause", "reset"):
        code(control(replay_client, session_id, operation, actor="viewer"), 403, "FORBIDDEN")
    assert gateway.advances == gateway.applies == []


@pytest.mark.parametrize("field,value", [("basis", {}), ("actor_id", "bob"), ("session_id", "chosen"),
    ("state", {}), ("snapshot_root", "D:/unsafe"), ("mode", "GPS")])
def test_replay_client_cannot_inject_state_authority_or_mode(replay_client, replay_setup, field, value):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway)
    code(step(replay_client, session_id, revision(gateway, session_id), **{field: value}), 422, "VALIDATION_ERROR")
    assert gateway.advances == []


@pytest.mark.parametrize("target", ["2026-09-27T21:00:01Z", "2026-09-27T21:00:01.1234567+07:00",
    "2026-02-30T21:00:01+07:00", 42])
def test_replay_rejects_invalid_or_inexact_timestamp(replay_client, replay_setup, target):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway)
    code(step(replay_client, session_id, revision(gateway, session_id), target_time=target), 422, "VALIDATION_ERROR")
    assert gateway.advances == []


@pytest.mark.parametrize("field,value", [("event_type", "URGENT_ORDER"), ("order_payload", {}),
    ("event_sha256", "d" * 64), ("observed_metrics", {}), ("timestamp", BARRIER)])
def test_event_apply_accepts_only_request_id_and_expected_revision(replay_client, replay_setup, field, value):
    gateway = replay_setup[1]
    session_id, _ = due_event(replay_client, gateway)
    code(apply(replay_client, session_id, revision(gateway, session_id), **{field: value}), 422, "VALIDATION_ERROR")
    assert gateway.applies == []


@pytest.mark.parametrize("operation", ["step", "apply"])
def test_observed_mutation_requires_explicit_revision(replay_client, replay_setup, operation):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway, "S2")
    suffix = "replay/step" if operation == "step" else "events/urgent-1/apply"
    response = replay_client.post(f"/api/sessions/{session_id}/{suffix}", headers=headers(), json={"request_id": "r1"})
    code(response, 422, "VALIDATION_ERROR")
    assert gateway.advances == gateway.applies == []


def test_step_commit_ambiguity_retries_persisted_default_target_without_recomputing(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway)
    original_revision = revision(gateway, session_id)
    gateway.fail_mutation_after_commit = True
    code(step(replay_client, session_id, original_revision), 503, "RUNTIME_TIMEOUT")
    original = deepcopy(gateway.advances[0])
    polls, reads = len(gateway.polls), len(gateway.reads)
    gateway.views[session_id]["current_time"] = "2026-09-27T21:05:00+07:00"
    gateway.views[session_id]["basis"]["head_version"] = "8"
    retry = step(replay_client, session_id, original_revision)
    assert retry.status_code == 200, retry.text
    assert gateway.advances == [original, original] and len(gateway.replay_receipts) == 1
    assert len(gateway.reads) == reads + 1 and len(gateway.polls) == polls
    data = retry.json()["data"]
    assert data["receipt"]["basis"]["head_version"] == "2" and data["execution_view"]["basis"]["head_version"] == "8"
    assert datetime.fromisoformat(data["receipt"]["target_time"]) == datetime.fromisoformat(FIRST_STEP)


def test_apply_commit_ambiguity_retries_same_nonpending_event_and_old_basis(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, _ = due_event(replay_client, gateway)
    original_revision = revision(gateway, session_id)
    gateway.fail_mutation_after_commit = True
    code(apply(replay_client, session_id, original_revision), 503, "RUNTIME_TIMEOUT")
    assert gateway.views[session_id]["pending_event_ids"] == []
    response = apply(replay_client, session_id, original_revision)
    assert response.status_code == 200, response.text
    assert gateway.applies[0] == gateway.applies[1] and len(gateway.applies) == 2
    assert gateway.views[session_id]["order_ids"].count("O3") == 1


def test_reset_bootstrap_response_loss_reuses_one_new_session_and_lineage(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway, "S2")
    gateway.fail_after_commit = True
    code(control(replay_client, session_id, "reset"), 503, "RUNTIME_TIMEOUT")
    original_command = gateway.commands[-1]
    response = control(replay_client, session_id, "reset")
    assert response.status_code == 200, response.text
    fresh = response.json()["data"]["session"]["session_id"]
    assert gateway.commands[-2] == gateway.commands[-1] == original_command
    assert fresh == original_command[0] and len(gateway.views) == 2
    assert len([receipt for receipt in history(replay_client, session_id).json()["data"]["history"]
                if receipt["operation"] == "reset"]) == 1


def test_fresh_view_failure_does_not_lose_durable_replay_receipt_or_history(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway)
    original_revision = revision(gateway, session_id)
    gateway.fail_resolve_after_mutation = True
    code(step(replay_client, session_id, original_revision), 503, "RUNTIME_TIMEOUT")
    saved = history(replay_client, session_id).json()["data"]["history"]
    assert len(saved) == 1
    retry = step(replay_client, session_id, original_revision)
    assert retry.status_code == 200 and retry.json()["data"]["receipt"] == saved[0]
    assert len(gateway.advances) == 1


def test_audit_insert_abort_rolls_back_response_and_retry_records_once(replay_client, replay_setup):
    settings, gateway = replay_setup
    session_id, _ = active_session(replay_client, gateway)
    original_revision = revision(gateway, session_id)
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("""CREATE TRIGGER test_abort_replay_audit BEFORE INSERT ON replay_audit
            BEGIN SELECT RAISE(ABORT, 'Injected replay audit INSERT interruption'); END""")
    try:
        code(step(replay_client, session_id, original_revision), 503, "METADATA_UNAVAILABLE")
        assert gateway.views[session_id]["basis"]["head_version"] == "2"
        with sqlite3.connect(settings.metadata_path) as db:
            assert db.execute("SELECT response_json FROM replay_requests WHERE operation='advance'").fetchone()[0] is None
            assert db.execute("SELECT count(*) FROM replay_audit").fetchone()[0] == 0
    finally:
        with sqlite3.connect(settings.metadata_path) as db:
            db.execute("DROP TRIGGER test_abort_replay_audit")
    response = step(replay_client, session_id, original_revision)
    assert response.status_code == 200, response.text
    assert gateway.advances[0] == gateway.advances[1] and len(gateway.replay_receipts) == 1
    assert history(replay_client, session_id).json()["data"]["history"] == [response.json()["data"]["receipt"]]


def test_restart_after_uncertain_step_preserves_original_payload_and_owner(replay_setup):
    settings, gateway = replay_setup
    with TestClient(create_app(settings, gateway=gateway)) as first:
        session_id, _ = active_session(first, gateway)
        original_revision = revision(gateway, session_id)
        gateway.fail_mutation_after_commit = True
        code(step(first, session_id, original_revision), 503, "RUNTIME_TIMEOUT")
    original = deepcopy(gateway.advances[0])
    with TestClient(create_app(settings, gateway=gateway)) as restarted:
        response = step(restarted, session_id, original_revision)
        assert response.status_code == 200, response.text
        receipt = response.json()["data"]["receipt"]
        assert history(restarted, session_id).json()["data"]["history"] == [receipt]
        code(history(restarted, session_id, "bob"), 403, "FORBIDDEN")
        assert step(restarted, session_id, original_revision).json()["data"]["receipt"] == receipt
    assert gateway.advances == [original, original] and len(gateway.replay_receipts) == 1


@pytest.mark.parametrize("operation", ["advance", "apply_event"])
def test_worker_reconciles_observed_commit_with_exact_old_sdk_payload(replay_setup, operation):
    settings, gateway = replay_setup
    with TestClient(create_app(settings, gateway=gateway)) as first:
        session_id, _ = due_event(first, gateway) if operation == "apply_event" else active_session(first, gateway)
        original_revision = revision(gateway, session_id)
        gateway.fail_mutation_after_commit = True
        response = apply(first, session_id, original_revision) if operation == "apply_event" else step(first, session_id, original_revision)
        code(response, 503, "RUNTIME_TIMEOUT")
    commands = gateway.applies if operation == "apply_event" else gateway.advances
    original = deepcopy(commands[-1])
    reads = len(gateway.reads)
    committed_version = gateway.views[session_id]["basis"]["head_version"]
    gateway.views[session_id]["basis"]["head_version"] = "9"

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.replays.pending_requests() == []
            saved = worker.replays.history(session_id)
            matching = [receipt for receipt in saved if receipt["operation"] == operation]
            assert len(matching) == 1 and matching[0]["basis"]["head_version"] == committed_version
            assert commands[-2] == commands[-1] == original
            assert len(gateway.reads) == reads + 1 and gateway.computes == []  # startup inspection, no replay basis refresh
        finally:
            await worker.shutdown()

    asyncio.run(scenario())
    with TestClient(create_app(settings, gateway=gateway)) as restarted:
        response = apply(restarted, session_id, original_revision) if operation == "apply_event" else step(restarted, session_id, original_revision)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["execution_view"]["basis"]["head_version"] == "9"
    assert len(commands) == 2


def test_replay_request_id_cannot_change_target(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway)
    original_revision = revision(gateway, session_id)
    assert step(replay_client, session_id, original_revision, target_time=FIRST_STEP).status_code == 200
    code(step(replay_client, session_id, original_revision, target_time="2026-09-27T21:02:00+07:00"),
         409, "IDEMPOTENCY_CONFLICT")
    assert len(gateway.advances) == 1


@pytest.mark.parametrize("field,value", [("generation", "2"), ("head_version", "9"),
    ("source_sha256", "e" * 64), ("status", "APPLIED")])
def test_advance_receipt_must_match_allowed_transition(replay_client, replay_setup, field, value):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway)
    original_revision = revision(gateway, session_id)
    gateway.corrupt_replay_result = field, value
    code(step(replay_client, session_id, original_revision), 503, "REPLAY_BINDING_CHANGED")
    assert history(replay_client, session_id).json()["data"]["history"] == []
    gateway.corrupt_replay_result = None
    assert step(replay_client, session_id, original_revision).status_code == 200
    assert gateway.advances[0] == gateway.advances[1]


@pytest.mark.parametrize("field,value", [("event_id", "other-event"), ("event_sha256", "not-a-digest")])
def test_event_receipt_must_match_event_and_valid_digest(replay_client, replay_setup, field, value):
    gateway = replay_setup[1]
    session_id, _ = due_event(replay_client, gateway)
    original_revision = revision(gateway, session_id)
    before_history = history(replay_client, session_id).json()["data"]["history"]
    gateway.corrupt_replay_result = field, value
    code(apply(replay_client, session_id, original_revision), 503, "REPLAY_BINDING_CHANGED")
    assert history(replay_client, session_id).json()["data"]["history"] == before_history
    gateway.corrupt_replay_result = None
    assert apply(replay_client, session_id, original_revision).status_code == 200
    assert gateway.applies[0] == gateway.applies[1]


def test_noop_receipt_cannot_increment_head_version(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway)
    original_revision = revision(gateway, session_id)
    gateway.corrupt_replay_result = "head_version", "2"
    code(step(replay_client, session_id, original_revision, target_time=EPOCH), 503, "REPLAY_BINDING_CHANGED")
    assert gateway.views[session_id]["basis"]["head_version"] == "1"
    assert history(replay_client, session_id).json()["data"]["history"] == []


@pytest.mark.parametrize("field,value", [
    ("head_sha256", ""), ("head_sha256", 123), ("head_sha256", {}), ("head_sha256", "A" * 64),
    ("omit:basis:head_sha256", None), ("omit:receipt:basis", None), ("basis", None), ("basis", []),
    ("head_version", "9223372036854775808"), ("generation", "9223372036854775808"),
    ("head_version", 2), ("head_version", "02"), ("context_version", {}), ("overlay_sha256", {}),
    ("$result", None), ("omit:receipt:status", None),
], ids=["empty_head_digest", "numeric_head_digest", "object_head_digest", "uppercase_head_digest",
    "missing_head_digest", "missing_basis", "null_basis", "list_basis", "head_counter_overflow",
    "generation_counter_overflow", "numeric_counter", "leading_zero_counter", "object_context",
    "object_overlay", "null_receipt", "missing_status"])
def test_malformed_advance_receipt_is_typed_retryable_and_never_audited(replay_client, replay_setup, field, value):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway)
    original_revision = revision(gateway, session_id)
    gateway.corrupt_replay_result = field, value
    code(step(replay_client, session_id, original_revision), 503, "REPLAY_BINDING_CHANGED")
    assert history(replay_client, session_id).json()["data"]["history"] == []
    assert gateway.views[session_id]["basis"]["head_version"] == "2"
    original = deepcopy(gateway.advances[0])
    gateway.corrupt_replay_result = None
    repaired = step(replay_client, session_id, original_revision)
    assert repaired.status_code == 200, repaired.text
    assert gateway.advances == [original, original] and len(gateway.replay_receipts) == 1
    assert history(replay_client, session_id).json()["data"]["history"] == [repaired.json()["data"]["receipt"]]


@pytest.mark.parametrize("field,value", [("omit:receipt:event_sha256", None),
    ("event_sha256", None), ("event_sha256", {})], ids=["missing_event_digest", "null_event_digest", "object_event_digest"])
def test_malformed_event_receipt_preserves_pending_request_for_exact_retry(replay_client, replay_setup, field, value):
    gateway = replay_setup[1]
    session_id, _ = due_event(replay_client, gateway)
    original_revision = revision(gateway, session_id)
    initial_history = history(replay_client, session_id).json()["data"]["history"]
    gateway.corrupt_replay_result = field, value
    code(apply(replay_client, session_id, original_revision), 503, "REPLAY_BINDING_CHANGED")
    assert history(replay_client, session_id).json()["data"]["history"] == initial_history
    assert gateway.views[session_id]["pending_event_ids"] == []
    original = deepcopy(gateway.applies[0])
    gateway.corrupt_replay_result = None
    repaired = apply(replay_client, session_id, original_revision)
    assert repaired.status_code == 200, repaired.text
    assert gateway.applies == [original, original]
    records = history(replay_client, session_id).json()["data"]["history"]
    assert len(records) == len(initial_history) + 1 and repaired.json()["data"]["receipt"] in records
    assert gateway.views[session_id]["order_ids"].count("O3") == 1


def test_worker_malformed_replay_receipt_degrades_without_crash_and_reconciles_after_repair(replay_setup):
    settings, gateway = replay_setup
    with TestClient(create_app(settings, gateway=gateway)) as first:
        session_id, _ = active_session(first, gateway)
        original_revision = revision(gateway, session_id)
        gateway.corrupt_replay_result = "head_sha256", {}
        code(step(first, session_id, original_revision), 503, "REPLAY_BINDING_CHANGED")
    original = deepcopy(gateway.advances[0])

    async def scenario():
        worker = ComputeWorker(settings, gateway=gateway)
        await worker.startup()
        try:
            assert worker.status == "DEGRADED" and worker.last_error == "REPLAY_BINDING_CHANGED"
            assert await worker.run_once() is False
            assert len(worker.replays.pending_requests()) == 1 and worker.replays.history(session_id) == []
            ready = Readiness(settings, None, None, gateway)
            assert ready.worker_status(settings.installation()) == "WORKER_NOT_READY"
        finally:
            await worker.shutdown()
        gateway.corrupt_replay_result = None
        repaired = ComputeWorker(settings, gateway=gateway)
        await repaired.startup()
        try:
            assert repaired.status == "READY" and repaired.replays.pending_requests() == []
            assert len(repaired.replays.history(session_id)) == 1
        finally:
            await repaired.shutdown()

    asyncio.run(scenario())
    assert gateway.advances == [original, original, original]
    assert gateway.views[session_id]["basis"]["head_version"] == "2" and gateway.computes == []


def test_heartbeat_publisher_retries_transient_windows_sharing_violation(replay_setup, monkeypatch):
    settings, gateway = replay_setup
    worker = ComputeWorker(settings, gateway=gateway)
    worker.installation_sha256 = settings.installation_identity()
    worker.build_sha256 = gateway.build
    worker.status = "READY"
    replace = heartbeat_module.os.replace
    attempts = []

    def blocked_once(source, destination):
        if Path(destination) == settings.heartbeat_path:
            attempts.append((source, destination))
            if len(attempts) == 1:
                raise PermissionError("Injected transient Windows sharing violation")
        return replace(source, destination)

    monkeypatch.setattr(heartbeat_module.os, "replace", blocked_once)
    worker.write_heartbeat()
    assert len(attempts) == 2
    value = json.loads(settings.heartbeat_path.read_text())
    assert value["status"] == "READY" and value["worker_id"] == worker.worker_id
    assert value["installation_sha256"] == settings.installation_identity()
    assert worker.status == "READY" and worker.last_error is None


def test_readiness_retries_transient_heartbeat_read_and_fails_closed_on_permanent_io(replay_setup, monkeypatch):
    settings, gateway = replay_setup
    write(settings.heartbeat_path, {"schema_version": "saferoute-m3-worker-heartbeat/1", "worker_id": "worker-test",
        "status": "READY", "updated_at": datetime.now(timezone.utc).isoformat(), "build_sha256": gateway.build,
        "installation_sha256": settings.installation_identity(), "current_job_id": None, "last_error_code": None})
    ready = Readiness(settings, None, None, gateway)
    config = settings.installation()
    read_text = Path.read_text
    attempts = []

    def reader_blocked_once(path, *args, **kwargs):
        if path == settings.heartbeat_path:
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError("Injected transient reader sharing violation")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reader_blocked_once)
    assert ready.worker_status(config) == "PASS" and len(attempts) == 2
    attempts.clear()

    def reader_io_failure(path, *args, **kwargs):
        if path == settings.heartbeat_path:
            attempts.append(path)
            raise OSError("Injected permanent generic I/O failure")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reader_io_failure)
    assert ready.worker_status(config) == "WORKER_NOT_READY" and len(attempts) == 1
    attempts.clear()

    def reader_permanently_denied(path, *args, **kwargs):
        if path == settings.heartbeat_path:
            attempts.append(path)
            raise PermissionError("Injected permanent access denial")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reader_permanently_denied)
    assert ready.worker_status(config) == "WORKER_NOT_READY"
    assert len(attempts) == 5, "Sharing retries must remain bounded"


def test_concurrent_steps_have_one_cas_winner_and_one_history_record(replay_client, replay_setup):
    gateway = replay_setup[1]
    session_id, _ = active_session(replay_client, gateway)
    original_revision = revision(gateway, session_id)
    gateway.advance_participants = 2
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda number: step(replay_client, session_id, original_revision,
            request_id=f"step-{number}", target_time=FIRST_STEP), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    code(next(response for response in responses if response.status_code == 409), 409, "STALE_HEAD")
    assert gateway.views[session_id]["basis"]["head_version"] == "2"
    assert len(gateway.replay_receipts) == 1 and len(history(replay_client, session_id).json()["data"]["history"]) == 1


def test_openapi_documents_owned_manual_replay_routes(replay_client):
    schema = replay_client.get("/openapi.json").json()
    for suffix, method in (("events", "get"), ("events/{event_id}/apply", "post"),
        ("replay/step", "post"), ("replay/pause", "post"), ("replay/reset", "post"), ("replay/history", "get")):
        assert schema["paths"]["/api/sessions/{session_id}/" + suffix][method]["security"]


def test_capabilities_and_execution_reads_have_bounded_independent_timeouts(replay_setup, monkeypatch):
    settings, fake = replay_setup
    assert settings.runtime_capabilities_timeout_seconds == 60
    assert settings.runtime_timeout_seconds == 60
    session_id = "session-timeout-check"
    asyncio.run(fake.bootstrap(session_id, "S0", "bootstrap-timeout-check"))
    config = settings.installation()
    capabilities = {"schema_version": "task02-m2-runtime-capabilities/1", "build_sha256": config["expected_build_sha256"],
        "execution_mode": "SIMULATED_REPLAY", "real_world_observation": False, "online_target_is_sla": False}
    execution_view = deepcopy(fake.views[session_id])
    captured = []
    monkeypatch.setenv("PYTHONPATH", "untrusted-team-module-path")
    monkeypatch.setenv("PYTHONHOME", "untrusted-python-home")

    def run(command, **kwargs):
        operation = command[command.index("--operation") + 1] if "--operation" in command else "capabilities"
        expected = [config["runtime_python"], "-I", "-B",
            str(Path(gateway_module.__file__).with_name("runtime_bridge.py")),
            "--installation", str(settings.installation_path)]
        if operation != "capabilities":
            expected.extend(["--operation", operation])
        assert command == expected
        assert kwargs["cwd"] == config["runtime_root"]
        assert "PYTHONPATH" not in kwargs["env"] and "PYTHONHOME" not in kwargs["env"]
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        assert kwargs["capture_output"] is True and kwargs["text"] is True and kwargs["encoding"] == "utf-8"
        if operation == "capabilities":
            assert kwargs["input"] is None
            value = capabilities
            command_id = "m3-capabilities-test"
        else:
            assert operation == "resolve"
            body = json.loads(kwargs["input"])
            assert body["session_id"] == session_id and body["command_id"].startswith("m3-read-")
            value = execution_view
            command_id = body["command_id"]
        captured.append((operation, kwargs["timeout"]))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"schema_version": "task02-m2-runtime-response/1",
            "command_id": command_id, "status": "OK", "value": value, "diagnostics": []}))

    monkeypatch.setattr(subprocess, "run", run)
    gateway = RuntimeGateway(settings)
    assert asyncio.run(gateway.capabilities()) == capabilities
    assert asyncio.run(gateway.resolve(session_id)) == execution_view
    assert [operation for operation, _ in captured] == ["capabilities", "resolve"]
    assert all(0 < timeout <= 60 for _, timeout in captured)
