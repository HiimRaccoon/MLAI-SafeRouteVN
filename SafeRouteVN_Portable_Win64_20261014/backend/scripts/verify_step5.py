"""Native plan acceptance through localhost HTTP and the verified public SDK.

Creates a private S0 session, computes one witnessed forecast and accepts it.
Never applies events/advances simulation, prints tokens or changes frozen files.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from verify_step4 import NativeHarness, fingerprint, require


def revision(basis):
    return {key: basis[key] for key in ("head_version", "generation")}


def require_error(harness, path, body, status, code, *, actor="member3"):
    actual_status, response, _ = harness.http(path, body, actor=actor)
    require(actual_status == status and response["status"] == "ERROR"
        and any(item["code"] == code for item in response["diagnostics"]),
        "Expected HTTP " + str(status) + " " + code + "; actual HTTP " + str(actual_status)
        + " diagnostics " + json.dumps([item.get("code") for item in response.get("diagnostics", [])]))
    return {"http_status": actual_status, "code": code}


def accept(harness, path, body):
    status, response, seconds = harness.http(path, body)
    require(status == 200 and response["status"] == "OK", "Native plan acceptance returned HTTP " + str(status)
        + " diagnostics " + json.dumps([item.get("code") for item in response.get("diagnostics", [])]))
    data = response["data"]
    require(data["schema_version"] == "saferoute-m3-acceptance-view/1", "Native acceptance view schema changed")
    receipt = data["receipt"]
    require(receipt["schema_version"] == "saferoute-m3-plan-acceptance/1" and receipt["status"] == "ACCEPTED"
        and isinstance(receipt["acceptance_id"], str) and receipt["acceptance_id"]
        and isinstance(receipt["links"], dict), "Native acceptance receipt fields are invalid")
    recorded = datetime.fromisoformat(receipt["recorded_at"])
    require(recorded.utcoffset() is not None, "Acceptance audit timestamp must include timezone")
    require(data["execution_view"]["schema_version"] == "task02-m2-execution-view/2",
        "Acceptance response did not resolve a fresh public execution view")
    return data, seconds


def audit(harness, session_id):
    status, response, _ = harness.http(f"/api/sessions/{session_id}/acceptances")
    require(status == 200 and isinstance(response["data"]["acceptances"], list), "Acceptance audit HTTP projection failed")
    return response["data"]["acceptances"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project))
    from backend.services.settings import Settings
    settings = Settings.from_environment()
    config = settings.installation()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    run_id = "m3-step5-" + uuid4().hex
    receipt = {"schema_version": "saferoute-m3-step5-native-acceptance/1", "status": "RUNNING",
        "started_at": datetime.now(timezone.utc).isoformat(), "run_id": run_id, "project_root": str(project),
        "runtime_build_sha256": config["expected_build_sha256"], "compute_budget_seconds": settings.compute_budget_seconds,
        "execution_mode": "SIMULATED_REPLAY", "real_world_observation": False,
        "checks": {}, "notes": ["One private S0 forecast is activated; no event or advance is performed.",
            "Accept changes only active plan/generation and forecast projections, not physical delivery.",
            "HTTP server and compute worker started by this verifier are stopped after it finishes."]}
    harness = NativeHarness(project, settings, args.output)
    failed = None
    try:
        harness.start_server()
        ready = harness.start_worker()
        receipt["checks"]["initial_readiness"] = {"status": "PASS", "http_status": 200, "data": ready}
        session_id = harness.load("S0", run_id + "-load-S0")
        receipt["session_id"] = session_id
        before, _ = harness.state(session_id)
        require(before["active_job_id"] is None and before["accepted_trajectory"] is None
            and before["delivered_prefix"] == [] and before["observed_metrics"] is None,
            "Fresh S0 session already has activation, delivery or observation")
        require(audit(harness, session_id) == [], "Fresh private session already has acceptance audits")
        submission, submit_seconds = harness.submit(session_id, run_id + "-compute-S0")
        job_id = submission["job_id"]
        receipt["job_id"] = job_id
        terminal, observations, running = harness.poll_terminal(session_id, job_id, before)
        require(terminal["job_status"] == "COMPLETED" and terminal["plan_available"] is True
            and terminal["validation"]["valid"] is True and terminal["served_orders"],
            "S0 native solve did not produce a validated serving witness")
        require(harness.state(session_id)[0] == before, "Compute changed execution state before accept")
        receipt["checks"]["native_witness"] = {"status": "PASS", "submit_seconds": round(submit_seconds, 4),
            "job_view": terminal, "poll_observations": observations, "while_running": running,
            "full_execution_view_unchanged_before_accept": True}
        harness.wait_idle()
        harness.stop_worker()  # Keep sibling queued: it must not obtain a witness by racing a worker.
        sibling, _ = harness.submit(session_id, run_id + "-sibling")
        sibling_id = sibling["job_id"]
        require(sibling["input_basis"] == before["basis"] and harness.view(session_id, sibling_id)[0]["job_status"] == "QUEUED",
            "Sibling was not queued on the original physical basis")
        accept_path = f"/api/sessions/{session_id}/jobs/{job_id}/accept"
        sibling_path = f"/api/sessions/{session_id}/jobs/{sibling_id}/accept"
        no_witness = {}
        no_witness["queued"] = require_error(harness, sibling_path,
            {"request_id": run_id + "-accept-queued", "expected_revision": revision(before["basis"])}, 409, "WITNESS_REQUIRED")
        cancelled = harness.cancel(session_id, sibling_id, run_id + "-cancel-sibling")
        require(cancelled["status"] == "JOB_CANCELLED" and harness.view(session_id, sibling_id)[0]["job_status"] == "FAILED",
            "Native cancelled sibling did not become FAILED")
        no_witness["failed"] = require_error(harness, sibling_path,
            {"request_id": run_id + "-accept-failed", "expected_revision": revision(before["basis"])}, 409, "WITNESS_REQUIRED")
        missing_status, missing_body, _ = harness.http(accept_path, {"request_id": run_id + "-missing-revision"})
        require(missing_status == 422 and missing_body["status"] == "ERROR", "Accept did not require expected_revision")
        wrong_owner = require_error(harness, accept_path,
            {"request_id": run_id + "-wrong-owner", "expected_revision": revision(before["basis"])}, 403, "FORBIDDEN", actor="member4")
        stale_revision = revision(before["basis"])
        stale_revision["generation"] = str(int(stale_revision["generation"]) + 1)
        stale_check = require_error(harness, accept_path,
            {"request_id": run_id + "-stale-revision", "expected_revision": stale_revision}, 409, "STALE_HEAD")
        require(harness.state(session_id)[0] == before and audit(harness, session_id) == [],
            "Rejected acceptance changed physical state or produced a successful audit")
        receipt["checks"]["preconditions"] = {"status": "PASS", "no_witness": no_witness,
            "missing_expected_revision_http_status": missing_status, "wrong_owner": wrong_owner,
            "stale_revision": stale_check, "no_state_or_successful_audit_change": True}
        accept_body = {"request_id": run_id + "-accept-witness", "expected_revision": revision(before["basis"])}
        accepted, accept_seconds = accept(harness, accept_path, accept_body)
        recorded_receipt = accepted["receipt"]
        after = accepted["execution_view"]
        require(recorded_receipt["session_id"] == session_id and recorded_receipt["job_id"] == job_id
            and recorded_receipt["input_basis"] == before["basis"] and recorded_receipt["basis"] == after["basis"],
            "Acceptance receipt does not bind exact input/post-activation basis")
        require(after["active_job_id"] == job_id and int(after["basis"]["generation"]) == int(before["basis"]["generation"]) + 1,
            "Acceptance did not activate exactly one generation")
        for key in before["basis"]:
            if key != "generation":
                require(after["basis"][key] == before["basis"][key], "Accept changed immutable basis field " + key)
        physical_keys = ("current_time", "vehicles", "order_ids", "delivered_prefix", "observed_metrics", "pending_event_ids",
            "metric_scope", "execution_mode", "real_world_observation")
        for key in physical_keys:
            require(after[key] == before[key], "Accept changed physical execution field " + key)
        trajectory = after["accepted_trajectory"]
        require(isinstance(trajectory, dict) and trajectory["job_id"] == job_id and trajectory["forecast"] is True
            and trajectory["vehicle_routes"] and set(after["planned_served_suffix"]) == set(terminal["served_orders"]),
            "Accepted trajectory or planned serving suffix is not the witnessed forecast")
        require(harness.state(session_id)[0] == after, "Accept response execution view was not freshly resolved")
        initial_audit = audit(harness, session_id)
        require(initial_audit == [recorded_receipt], "Acceptance audit did not record exactly the successful receipt")
        require_error(harness, f"/api/sessions/{session_id}/acceptances", None, 403, "FORBIDDEN", actor="member4")
        order_status, orders, _ = harness.http(f"/api/sessions/{session_id}/orders")
        require(order_status == 200 and orders["data"]["basis"] == after["basis"], "Accepted orders projection basis differs")
        require(all(item["status"] != "DELIVERED" for item in orders["data"]["orders"])
            and {item["order_id"] for item in orders["data"]["orders"] if item["planned_in_accepted_suffix"]} == set(after["planned_served_suffix"]),
            "Accepted forecast was misrepresented as physical delivery in orders API")
        receipt["checks"]["activation"] = {"status": "PASS", "accept_seconds": round(accept_seconds, 4),
            "receipt": recorded_receipt, "active_job_id": after["active_job_id"],
            "generation_before": before["basis"]["generation"], "generation_after": after["basis"]["generation"],
            "physical_fields_unchanged": list(physical_keys), "basis_except_generation_unchanged": True,
            "physical_before_sha256": fingerprint({key: before[key] for key in physical_keys}),
            "physical_after_sha256": fingerprint({key: after[key] for key in physical_keys}),
            "accepted_trajectory_forecast": trajectory["forecast"], "planned_order_ids": after["planned_served_suffix"],
            "orders_remain_not_delivered": True, "audit_count": len(initial_audit)}
        repeat, _ = accept(harness, accept_path, accept_body)
        require(repeat["receipt"] == recorded_receipt and repeat["execution_view"] == after,
            "Retry same acceptance ID changed receipt or added activation")
        changed_body = {**accept_body, "expected_revision": revision(after["basis"])}
        changed_check = require_error(harness, accept_path, changed_body, 409, "IDEMPOTENCY_CONFLICT")
        stale_sibling = require_error(harness, sibling_path,
            {"request_id": run_id + "-old-sibling-basis", "expected_revision": revision(after["basis"])}, 409, "STALE_HEAD")
        second_accept = require_error(harness, accept_path,
            {"request_id": run_id + "-second-activation", "expected_revision": revision(after["basis"])}, 409, "STALE_HEAD")
        require(harness.state(session_id)[0] == after and audit(harness, session_id) == initial_audit,
            "Retry or rejected old-basis acceptance added generation/audit")
        receipt["checks"]["retry_and_staleness"] = {"status": "PASS", "exact_receipt_preserved": True,
            "no_second_generation": True, "changed_body": changed_check, "sibling_old_basis": stale_sibling,
            "same_job_new_request_id": second_accept, "successful_audit_count": 1}
        harness.stop_server()
        harness.start_server()
        restarted_ready = harness.start_worker()
        repeat_after_restart, _ = accept(harness, accept_path, accept_body)
        require(repeat_after_restart["receipt"] == recorded_receipt and repeat_after_restart["execution_view"] == after,
            "Restart lost persisted acceptance receipt or active plan")
        require(harness.state(session_id)[0] == after and audit(harness, session_id) == [recorded_receipt],
            "Restart lost exact generation/state or duplicated acceptance audit")
        require(harness.view(session_id, job_id)[0] == terminal, "Acceptance changed completed public job view")
        harness.wait_idle()
        receipt["checks"]["restart_persistence"] = {"status": "PASS", "real_worker_ready_http_status": 200,
            "readiness": restarted_ready, "exact_receipt_and_active_state_preserved": True, "successful_audit_count": 1}
        status, openapi, _ = harness.http("/openapi.json", actor=None)
        require(status == 200 and all(path in openapi["paths"] for path in
            ("/api/sessions/{session_id}/jobs/{job_id}/accept", "/api/sessions/{session_id}/acceptances")),
            "Native OpenAPI lacks acceptance endpoints")
        (args.output.parent / "openapi.json").write_text(json.dumps(openapi, ensure_ascii=False, indent=2), encoding="utf-8")
        receipt["checks"]["openapi"] = {"status": "PASS", "path": str(args.output.parent / "openapi.json")}
    except Exception as error:
        failed = error
        receipt["failure"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        harness.close()
        receipt["server_running_after_test"] = False
        receipt["worker_running_after_test"] = False
    try:
        check = subprocess.run([sys.executable, "-B", str(project / "docs/M2_STEP7_VERIFY_20261004.py"),
            "--project-root", str(project)], cwd=project, capture_output=True, text=True, encoding="utf-8", timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        verified = json.loads(check.stdout)
        require(check.returncode == 0 and verified["status"] == "TEAM_HANDOFF_BYTES_VERIFIED", "Frozen M2 handoff changed")
        receipt["checks"]["frozen_handoff"] = {"status": "PASS", "result": verified}
    except Exception as error:
        failed = failed or error
        receipt["checks"]["frozen_handoff"] = {"status": "FAIL", "type": type(error).__name__, "message": str(error)}
    receipt["status"] = "M3_STEP5_NATIVE_ACCEPTANCE_PASS" if failed is None else "M3_STEP5_NATIVE_ACCEPTANCE_FAIL"
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"status": receipt["status"], "receipt": str(args.output),
        "server_stopped": True, "worker_stopped": True}), flush=True)
    return 0 if failed is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
