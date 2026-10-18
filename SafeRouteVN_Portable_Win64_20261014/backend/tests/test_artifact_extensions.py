"""Export complete durable public history without private orchestration fields."""
from copy import deepcopy
import json

import pytest

from backend.services.artifact_extensions import COMPARISON_METRICS, TABLES, extension_metadata
from backend.services.profile_repository import PROFILES
from backend.tests.test_jobs import public_job
from backend.tests.test_runtime_read_timeout import SESSION, basis

BUILD, INSTALLATION = "8" * 64, "7" * 64
STAMP = "2026-10-06T00:00:00+07:00"
CID = "comparison-extension"


def encode(value):
    return json.dumps(value, allow_nan=False)


def public_controller(**changes):
    return {"mode": "STEP", "paused": True, "fully_paused": True, "in_flight": False,
        "reason": "USER_PAUSE", "speed": 8, "controller_revision": "4",
        "next_tick_at": None, "end_time": "2026-09-27T23:00:00+07:00", **changes}


def control_receipt(operation="pause", **changes):
    return {"schema_version": "saferoute-m3-playback-receipt/1", "receipt_id": "playback-control",
        "session_id": SESSION, "operation": operation, "source": "M3_PLAYBACK_CONTROL", "recorded_at": STAMP,
        "controller": public_controller(), "request_id": "pause-request", "actor_id": "alice", **changes}


@pytest.fixture
def raw():
    prefix = f"/api/sessions/{SESSION}/profiles/comparisons/{CID}"
    submission = {"schema_version": "saferoute-m3-profile-submission/1", "session_id": SESSION,
        "comparison_id": CID, "mode": "NEW_BATCH", "input_basis": basis(), "profiles": list(PROFILES),
        "links": {"poll": prefix, "cancel": prefix + "/cancel"}}
    members, compared = [], []
    for profile in PROFILES:
        jid = "job-extension-" + profile
        view = public_job(jid, basis())
        view.update(job_status="COMPLETED", business_status="FEASIBLE", internal_status="FEASIBLE",
            coverage_evaluated=True, plan_available=True, served_orders=["order-1"],
            validation={"status": "VALIDATED", "valid": True, "validator_version": "TEST_ONLY"})
        members.append({"comparison_id": CID, "profile": profile, "job_id": jid, "view_json": encode(view),
            "submission_request_id": "INTERNAL-SUBMIT-NOT-PUBLIC", "cancel_request_id": "INTERNAL-CANCEL-NOT-PUBLIC",
            "cancelled": 0, "claim_token": "CLAIM-CANARY"})
        compared.append({"job_id": jid, "profile": profile, "basis": basis(), "domain_sha256": "d" * 64,
            "metrics": {name: 1.5 for name in COMPARISON_METRICS}})
    comparison = {"schema_version": "task02-m2-runtime-comparison/1", "status": "COMPARABLE", "reason": None, "jobs": compared}
    receipt = control_receipt()
    cancel = {"schema_version": "saferoute-m3-profile-cancellation/1", "session_id": SESSION,
        "comparison_id": CID, "status": "COMPLETED_IMMUTABLE", "affects_existing_jobs": False}
    return {"profile_comparisons": [{"comparison_id": CID, "session_id": SESSION, "actor_id": "alice",
        "request_id": "compare-request", "request_sha256": "a" * 64, "installation_sha256": INSTALLATION,
        "mode": "NEW_BATCH", "basis_json": encode(basis()), "budget_seconds": 120, "poll_cursor": 3,
        "state": "COMPLETED", "outcome_json": encode({"comparison": comparison, "reason": None}),
        "error_code": None, "receipt_json": encode(submission), "created_at": 10.0, "updated_at": 20.0,
        "private_path": "PRIVATE-PATH-CANARY"}],
        "profile_members": members,
        "profile_cancel_requests": [{"actor_id": "alice", "session_id": SESSION, "request_id": "cancel-request",
            "comparison_id": CID, "installation_sha256": INSTALLATION, "response_json": encode(cancel)}],
        "playback_controllers": [{"session_id": SESSION, "installation_sha256": INSTALLATION, "actor_id": "alice",
            "epoch": 4, "speed": 8, "status": "PAUSED", "reason": "USER_PAUSE", "basis_json": encode(basis()),
            "end_time": "2026-09-27T23:00:00+07:00", "next_due": 0.0, "pending_actor": None,
            "pending_request": None, "updated_at": STAMP, "claim_token": "CLAIM-CANARY"}],
        "playback_controls": [{"actor_id": "alice", "session_id": SESSION, "operation": "pause",
            "request_id": "pause-request", "request_sha256": "b" * 64, "installation_sha256": INSTALLATION,
            "receipt_json": encode(receipt), "runtime_root": "PRIVATE-PATH-CANARY"}],
        "playback_audit": [{"receipt_id": receipt["receipt_id"], "session_id": SESSION,
            "recorded_at": STAMP, "receipt_json": encode(receipt)}]}


def project(raw):
    return extension_metadata(raw, SESSION, BUILD, INSTALLATION)


def test_exact_public_history_bindings_and_no_internal_or_path_fields(raw):
    out = project(raw)
    comparison = out["comparisons"][0]
    assert comparison["status"] == "COMPLETED" and comparison["outcome"]["comparison"]["status"] == "COMPARABLE"
    assert [j["profile"] for j in comparison["jobs"]] == list(PROFILES)
    assert comparison["submission_receipt"] == json.loads(raw["profile_comparisons"][0]["receipt_json"])
    assert out["cancellations"][0]["receipt"]["status"] == "COMPLETED_IMMUTABLE"
    assert out["playback_controls"][0]["receipt"] == out["playback_audit"][0]["receipt"]
    assert out["playback_controller"]["fully_paused"] is True
    assert out["missing_optional_tables"] == []
    serialized = encode(out)
    assert not any(s in serialized for s in ("INTERNAL-SUBMIT", "INTERNAL-CANCEL", "CLAIM-CANARY", "PRIVATE-PATH-CANARY"))
    assert not any(k in comparison for k in ("budget_seconds", "poll_cursor", "basis_json", "state", "claim_token"))
    assert not any(k in out["playback_controller"] for k in ("pending_actor", "pending_request", "next_due", "epoch"))


def test_legacy_missing_optional_tables_are_explicit_and_empty():
    assert project({}) == {"comparisons": [], "cancellations": [], "playback_controls": [], "playback_audit": [],
        "playback_controller": None, "missing_optional_tables": list(TABLES)}


def test_all_audit_rows_preserved_beyond_http_100_limit(raw):
    rows = []
    for i in range(151):
        receipt = control_receipt(receipt_id=f"playback-{i}")
        rows.append({"session_id": SESSION, "receipt_id": receipt["receipt_id"], "recorded_at": STAMP, "receipt_json": encode(receipt)})
    raw["playback_audit"] = rows
    assert len(project(raw)["playback_audit"]) == 151
    assert project(raw)["playback_audit"][-1]["receipt_id"] == "playback-150"


@pytest.mark.parametrize("table", ["profile_comparisons", "profile_cancel_requests", "playback_controls", "playback_controllers", "playback_audit"])
def test_cross_session_rows_rejected(raw, table):
    raw[table][0]["session_id"] = "other-session"
    with pytest.raises(ValueError):
        project(raw)


@pytest.mark.parametrize("table", ["profile_comparisons", "profile_cancel_requests", "playback_controls", "playback_controllers"])
def test_cross_installation_rows_rejected(raw, table):
    raw[table][0]["installation_sha256"] = "6" * 64
    with pytest.raises(ValueError):
        project(raw)


@pytest.mark.parametrize("field", ["session_id", "build_sha256", "head_version", "root_sha256"])
def test_malformed_full_comparison_basis_rejected(raw, field):
    value = json.loads(raw["profile_comparisons"][0]["basis_json"])
    value[field] = "foreign" if field == "session_id" else "x" if field != "head_version" else "01"
    raw["profile_comparisons"][0]["basis_json"] = encode(value)
    with pytest.raises(ValueError):
        project(raw)


@pytest.mark.parametrize("change", [lambda v: v["jobs"][0].update(job_id="foreign"),
    lambda v: v["jobs"][0].update(profile="SAFER"), lambda v: v["jobs"][0]["basis"].update(generation="7"),
    lambda v: v.update(status="RANKED_BEST"), lambda v: v["jobs"][0].update(metrics={"server_path": "PRIVATE-PATH-CANARY"}),
    lambda v: v["jobs"][0]["metrics"].update(total_distance_m=True),
    lambda v: v["jobs"][0]["metrics"].update(total_cost_vnd=-1)])
def test_sdk_comparison_malformed_child_verdict_or_metric_projection_rejected(raw, change):
    value = json.loads(raw["profile_comparisons"][0]["outcome_json"])
    change(value["comparison"])
    raw["profile_comparisons"][0]["outcome_json"] = encode(value)
    with pytest.raises(ValueError):
        project(raw)


def test_sdk_non_comparable_verdict_preserved_without_local_domain_inference(raw):
    value = json.loads(raw["profile_comparisons"][0]["outcome_json"])
    value["comparison"]["status"] = "NON_COMPARABLE"
    value["comparison"]["reason"] = value["reason"] = "AUTHENTICATED_BASIS_OR_PHYSICAL_DOMAIN_DIFFERS"
    value["comparison"]["jobs"][2]["domain_sha256"] = "e" * 64
    raw["profile_comparisons"][0]["outcome_json"] = encode(value)
    assert project(raw)["comparisons"][0]["outcome"] == value


def test_existing_inputs_may_have_distinct_historical_full_bases(raw):
    group = raw["profile_comparisons"][0]
    group["mode"] = "EXISTING_JOBS"
    receipt = json.loads(group["receipt_json"])
    receipt["mode"] = "EXISTING_JOBS"
    group["receipt_json"] = encode(receipt)
    member = raw["profile_members"][2]
    view = json.loads(member["view_json"])
    view["input_basis"]["head_version"] = "19"
    member["view_json"] = encode(view)
    value = json.loads(group["outcome_json"])
    value["comparison"]["jobs"][2]["basis"] = view["input_basis"]
    value["comparison"]["status"] = "NON_COMPARABLE"
    value["comparison"]["reason"] = value["reason"] = "AUTHENTICATED_BASIS_OR_PHYSICAL_DOMAIN_DIFFERS"
    group["outcome_json"] = encode(value)
    assert project(raw)["comparisons"][0]["jobs"][2]["public_view"]["input_basis"]["head_version"] == "19"


def test_no_witness_remains_null_sdk_comparison_and_preserves_empty_coverage(raw):
    member = raw["profile_members"][2]
    view = json.loads(member["view_json"])
    view.update(business_status="SEARCH_LIMIT", internal_status="SEARCH_LIMIT", plan_available=False,
        coverage_evaluated=False, served_orders=[], unserved_orders=[], validation={"status": "NOT_RUN", "valid": None})
    member["view_json"] = encode(view)
    raw["profile_comparisons"][0]["outcome_json"] = encode({"comparison": None, "reason": "NO_CERTIFIED_WITNESS_FOR_EVERY_PROFILE"})
    out = project(raw)["comparisons"][0]
    assert out["outcome"]["comparison"] is None
    assert out["jobs"][2]["public_view"]["coverage_evaluated"] is False


@pytest.mark.parametrize("change", [lambda rows: rows.pop(), lambda rows: rows[1].update(profile="FASTEST"),
    lambda rows: rows[0].update(comparison_id="foreign"), lambda rows: rows[2].update(job_id=rows[0]["job_id"])])
def test_member_binding_missing_duplicate_or_foreign_rejected(raw, change):
    change(raw["profile_members"])
    with pytest.raises(ValueError):
        project(raw)


def test_pending_new_batch_keeps_null_members_without_inventing_outcome(raw):
    raw["profile_comparisons"][0].update(state="QUEUED", outcome_json=None)
    for member in raw["profile_members"]:
        member.update(job_id=None, view_json=None)
    out = project(raw)["comparisons"][0]
    assert out["status"] == "QUEUED" and out["outcome"] is None
    assert all(j["job_id"] is j["public_view"] is None for j in out["jobs"])


@pytest.mark.parametrize("change", [lambda v: v.update(session_id="foreign"), lambda v: v.update(comparison_id="foreign"),
    lambda v: v.update(affects_existing_jobs=True), lambda v: v.update(status="CANCELLED_ALREADY")])
def test_cancellation_receipt_binding_or_false_existing_job_policy_rejected(raw, change):
    value = json.loads(raw["profile_cancel_requests"][0]["response_json"])
    change(value)
    raw["profile_cancel_requests"][0]["response_json"] = encode(value)
    with pytest.raises(ValueError):
        project(raw)


@pytest.mark.parametrize("change", [lambda v: v.update(session_id="foreign"), lambda v: v.update(actor_id="bob"),
    lambda v: v.update(request_id="other"), lambda v: v.update(operation="start"),
    lambda v: v["controller"].update(speed=3), lambda v: v["controller"].update(controller_revision=9007199254740993),
    lambda v: v["controller"].update(fully_paused=False)])
def test_control_public_receipt_exact_binding_and_controller_semantics_rejected(raw, change):
    value = json.loads(raw["playback_controls"][0]["receipt_json"])
    change(value)
    raw["playback_controls"][0]["receipt_json"] = encode(value)
    with pytest.raises(ValueError):
        project(raw)


def test_receipt_nested_private_extras_are_omitted(raw):
    value = json.loads(raw["playback_controls"][0]["receipt_json"])
    value["private_path"] = "PRIVATE-PATH-CANARY"
    value["controller"]["claim_token"] = "CLAIM-CANARY"
    raw["playback_controls"][0]["receipt_json"] = encode(value)
    assert "CANARY" not in encode(project(raw))


def test_never_started_speed_only_controller_with_nullable_basis_is_supported(raw):
    raw["playback_controllers"][0].update(basis_json=None, end_time=None, reason="NOT_STARTED", epoch=0, speed=1)
    out = project(raw)["playback_controller"]
    assert out["basis"] is None and out["end_time"] is None and out["reason"] == "NOT_STARTED"


def test_pending_controller_exports_only_public_in_flight_flag(raw):
    raw["playback_controllers"][0].update(pending_actor="alice", pending_request="autoplay-private-request")
    out = project(raw)["playback_controller"]
    assert out["paused"] and out["in_flight"] and not out["fully_paused"]
    assert "autoplay-private-request" not in encode(out)


@pytest.mark.parametrize("change", [lambda r: r.update(installation_sha256="0" * 64),
    lambda r: r.update(epoch=True), lambda r: r.update(epoch=1 << 63), lambda r: r.update(speed="8"),
    lambda r: r.update(status="RUNNING", basis_json=None), lambda r: r.update(end_time="no-timezone")])
def test_invalid_controller_binding_clock_and_exact_counter_rejected(raw, change):
    change(raw["playback_controllers"][0])
    with pytest.raises(ValueError):
        project(raw)


def test_tick_and_stop_receipts_preserve_public_links_without_private_tick_commands(raw):
    base = {"schema_version": "saferoute-m3-playback-receipt/1", "session_id": SESSION,
            "source": "M3_PLAYBACK_CONTROL", "recorded_at": STAMP}
    tick = {**base, "receipt_id": "tick-receipt", "operation": "tick_settled", "mutation_id": "replay-123",
        "status": "ADVANCED", "reason": "EVENT_BARRIER", "target_time": "2026-09-27T21:15:00+07:00", "controller": public_controller(reason="EVENT_BARRIER")}
    stop = {**base, "receipt_id": "stop-receipt", "operation": "stop", "reason": "USER_PAUSE", "in_flight": False}
    raw["playback_audit"] = [{"session_id": SESSION, "receipt_id": r["receipt_id"], "recorded_at": STAMP,
        "receipt_json": encode(r)} for r in (tick, stop)]
    assert [r["receipt"]["operation"] for r in project(raw)["playback_audit"]] == ["tick_settled", "stop"]


@pytest.mark.parametrize("field", ["receipt_id", "recorded_at", "session_id"])
def test_audit_row_and_receipt_mismatch_rejected(raw, field):
    raw["playback_audit"][0][field] = "other"
    with pytest.raises(ValueError):
        project(raw)
