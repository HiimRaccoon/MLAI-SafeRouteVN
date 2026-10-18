"""S0–S8 through real HTTP/SDK; creates private test sessions and no compute jobs."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project))
    from backend.services.settings import Settings
    settings = Settings.from_environment()
    config = settings.installation()
    credentials = json.loads((settings.auth_path.parent / "dev_access.json").read_text(encoding="utf-8"))["credentials"]
    tokens = {row["actor_id"]: row["token"] for row in credentials}
    run_id = "m3-step3-" + uuid4().hex
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    log = args.output.with_suffix(".server.log").open("w", encoding="utf-8")
    process = None

    def start():
        nonlocal process
        process = subprocess.Popen([sys.executable, "-B", str(project / "backend/scripts/start_backend.py"), "--port", str(port)],
            cwd=project, stdout=log, stderr=log, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                raise RuntimeError("HTTP server stopped")
            try:
                if http("/health", actor=None)[0] == 200:
                    return
            except (URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.15)

    def stop():
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def http(path, body=None, actor="member3"):
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["Authorization"] = "Bearer " + tokens[actor]
        raw = None if body is None else json.dumps(body).encode()
        try:
            response = urlopen(Request(base_url + path, data=raw, headers=headers), timeout=90)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    receipt = {"schema_version": "saferoute-m3-step3-native-sessions/1", "started_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(project), "runtime_build_sha256": config["expected_build_sha256"], "run_id": run_id,
        "checks": {}, "scenarios": []}
    try:
        start()
        status, catalog = http("/api/scenarios")
        if status != 200 or len(catalog["data"]["scenarios"]) != 9:
            raise ValueError("S0-S8 catalog invariant")
        receipt["checks"]["catalog"] = {"status": "PASS", "catalog": catalog["data"]}
        if http("/api/scenarios", actor=None)[0] != 401:
            raise ValueError("Catalog authentication invariant")
        sessions = {}
        for item in catalog["data"]["scenarios"]:
            scenario_id = item["scenario_id"]
            request_id = run_id + "-" + scenario_id
            status, body = http(f"/api/scenarios/{scenario_id}/load", {"request_id": request_id})
            if status != 201:
                raise ValueError("Load failed: " + scenario_id + " " + json.dumps(body))
            data = body["data"]
            session_id, view = data["session"]["session_id"], data["execution_view"]
            if (len(view["order_ids"]) != item["order_count"] or len(view["vehicles"]) != item["vehicle_count"]
                    or view["observed_metrics"] is not None or view["active_job_id"] is not None
                    or view["basis"]["build_sha256"] != config["expected_build_sha256"]):
                raise ValueError("Initial typed view invariant: " + scenario_id)
            read_status, current = http(f"/api/sessions/{session_id}/state")
            if read_status != 200 or current["data"] != view:
                raise ValueError("Fresh execution state invariant")
            order_status, orders = http(f"/api/sessions/{session_id}/orders")
            if order_status != 200 or len(orders["data"]["orders"]) != item["order_count"]:
                raise ValueError("HTTP order count invariant")
            sessions[scenario_id] = data
            receipt["scenarios"].append({"scenario_id": scenario_id, "session_id": session_id, "status": "PASS",
                "orders": item["order_count"], "vehicles": item["vehicle_count"], "basis": view["basis"],
                "observed_metrics": view["observed_metrics"], "pending_event_ids": view["pending_event_ids"]})
            print(scenario_id + " NATIVE_LOAD_READ_PASS", flush=True)
        first = sessions["S0"]
        session_id = first["session"]["session_id"]
        status, replay = http("/api/scenarios/S0/load", {"request_id": run_id + "-S0"})
        if status != 201 or replay["data"] != first:
            raise ValueError("Durable retry invariant")
        if http("/api/scenarios/S1/load", {"request_id": run_id + "-S0"})[0] != 409:
            raise ValueError("Idempotency conflict invariant")
        status, second = http("/api/scenarios/S0/load", {"request_id": run_id + "-second"})
        if status != 201 or second["data"]["session"]["session_id"] == session_id:
            raise ValueError("Independent sessions invariant")
        for suffix in ("", "/state", "/orders", "/vehicles", "/locations"):
            if http(f"/api/sessions/{session_id}{suffix}", actor="member4")[0] != 403:
                raise ValueError("Cross-owner access invariant")
        for suffix, field, count in (("/vehicles", "vehicles", 2), ("/locations", "locations", 4)):
            status, body = http(f"/api/sessions/{session_id}{suffix}")
            if status != 200 or len(body["data"][field]) != count:
                raise ValueError("HTTP read projection invariant")
        receipt["checks"]["sessions_and_ownership"] = {"status": "PASS", "independent_s0_session_id": second["data"]["session"]["session_id"],
            "idempotent_retry": True, "changed_content_conflict": True, "cross_owner_denied_routes": 5}
        stop()
        start()
        status, replay = http("/api/scenarios/S0/load", {"request_id": run_id + "-S0"})
        if status != 201 or replay["data"] != first or http(f"/api/sessions/{session_id}/state")[0] != 200:
            raise ValueError("HTTP process restart persistence invariant")
        receipt["checks"]["restart"] = {"status": "PASS", "owner_and_load_receipt_preserved": True}
        status, ready = http("/ready", actor=None)
        checks = ready["data"]["checks"]
        if (status != (200 if checks.get("worker") == "PASS" else 503)
                or any(checks.get(key) != "PASS" for key in ("g0", "authentication", "metadata", "runtime", "catalog", "queue"))
                or checks.get("worker") not in ("PASS", "WORKER_NOT_CONFIGURED", "WORKER_NOT_READY")):
            raise ValueError("Worker readiness gate invariant")
        receipt["checks"]["readiness"] = {"status": "PASS", "http_status": status, "data": ready["data"]}
        status, openapi = http("/openapi.json", actor=None)
        if status != 200 or "/api/scenarios/{scenario_id}/load" not in openapi["paths"]:
            raise ValueError("HTTP schema invariant")
        (args.output.parent / "openapi.json").write_text(json.dumps(openapi, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        stop()
        log.close()
    check = subprocess.run([sys.executable, "-B", str(project / "docs/M2_STEP7_VERIFY_20261004.py"), "--project-root", str(project)],
        cwd=project, capture_output=True, text=True, encoding="utf-8", timeout=60)
    verified = json.loads(check.stdout)
    if check.returncode or verified["status"] != "TEAM_HANDOFF_BYTES_VERIFIED":
        raise ValueError("Frozen handoff changed")
    receipt["checks"]["frozen_handoff"] = {"status": "PASS", "result": verified}
    receipt["status"] = "M3_STEP3_NATIVE_SESSIONS_PASS"
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    receipt["server_running_after_test"] = False
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": receipt["status"], "scenarios_passed": len(receipt["scenarios"]), "receipt": str(args.output)}), flush=True)


if __name__ == "__main__":
    main()
