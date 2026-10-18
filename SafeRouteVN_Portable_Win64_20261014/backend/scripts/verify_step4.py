"""Native localhost HTTP + separate SDK compute worker acceptance.

Creates private, uniquely named S0/S1 sessions. It never imports M2 internals,
prints credentials, changes frozen files, or accepts/advances a forecast plan.
"""
import argparse
from datetime import datetime, timezone
import hashlib
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


TERMINAL = ("COMPLETED", "FAILED")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


class NativeHarness:
    def __init__(self, project, settings, output):
        self.project, self.settings, self.output = project, settings, output
        access = json.loads((settings.auth_path.parent / "dev_access.json").read_text(encoding="utf-8"))
        self.tokens = {item["actor_id"]: item["token"] for item in access["credentials"]}
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server = self.worker = None
        self.server_log = output.with_suffix(".server.log").open("w", encoding="utf-8")
        self.worker_log = output.with_suffix(".worker.log").open("w", encoding="utf-8")
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    def http(self, path, body=None, *, actor="member3", timeout=90):
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["Authorization"] = "Bearer " + self.tokens[actor]
        raw = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
        started = time.monotonic()
        try:
            response = urlopen(Request(self.base_url + path, data=raw, headers=headers), timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read()), time.monotonic() - started

    def spawn(self, script, log, *args):
        return subprocess.Popen([sys.executable, "-B", str(self.project / "backend/scripts" / script), *args],
            cwd=self.project, stdout=log, stderr=log, env=self.env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def start_server(self):
        self.server = self.spawn("start_backend.py", self.server_log, "--port", str(self.port))
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            require(self.server.poll() is None, "HTTP server stopped during startup; inspect server log")
            try:
                if self.http("/health", actor=None, timeout=10)[0] == 200:
                    return
            except (URLError, TimeoutError, OSError):
                pass
            time.sleep(0.2)
        raise TimeoutError("HTTP server startup deadline exceeded")

    def start_worker(self):
        self.worker = self.spawn("start_worker.py", self.worker_log)
        return self.wait_ready()

    def wait_ready(self, timeout=90):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            require(self.worker is not None and self.worker.poll() is None,
                "Compute worker stopped; inspect worker log")
            status, body, _ = self.http("/ready", actor=None)
            last = body.get("data")
            if status == 200 and last["ready"] is True:
                return last
            time.sleep(0.5)
        raise TimeoutError("Worker readiness deadline exceeded: " + json.dumps(last))

    def heartbeat(self):
        from backend.services.heartbeat_io import read_heartbeat_json
        value = read_heartbeat_json(self.settings.heartbeat_path)
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(value["updated_at"])).total_seconds()
        require(value["schema_version"] == "saferoute-m3-worker-heartbeat/1" and value["status"] == "READY"
            and value["installation_sha256"] == self.settings.installation_identity() and 0 <= age <= 15,
            "Real worker heartbeat is not fresh/ready/bound to this installation")
        return value, age

    def wait_idle(self, timeout=150):
        deadline = time.monotonic() + timeout
        idle_since = None
        while time.monotonic() < deadline:
            require(self.worker is not None and self.worker.poll() is None, "Worker stopped while waiting for native compute exit")
            value, _ = self.heartbeat()
            if value["current_job_id"] is None:
                # A heartbeat from just before dispatch can still say idle.
                # Require stability across several 2-second heartbeat cycles.
                if idle_since is None:
                    idle_since = time.monotonic()
                if time.monotonic() - idle_since >= 5:
                    return value
            else:
                idle_since = None
            time.sleep(0.5)
        raise TimeoutError("Native compute worker did not become idle")

    @staticmethod
    def stop(process):
        if process is None or process.poll() is not None:
            return
        # Only our own Popen PID is targeted. Include its bridge children so a
        # failed verification cannot leave native compute running unnoticed.
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def stop_worker(self):
        self.stop(self.worker)
        self.worker = None

    def stop_server(self):
        self.stop(self.server)
        self.server = None

    def close(self):
        self.stop_worker()
        self.stop_server()
        self.worker_log.close()
        self.server_log.close()

    def state(self, session_id):
        status, body, elapsed = self.http(f"/api/sessions/{session_id}/state")
        require(status == 200, "Native execution-state read failed")
        return body["data"], elapsed

    def load(self, scenario_id, request_id):
        status, body, _ = self.http(f"/api/scenarios/{scenario_id}/load", {"request_id": request_id})
        require(status == 201, "Native scenario load failed: " + scenario_id)
        return body["data"]["session"]["session_id"]

    def submit(self, session_id, request_id):
        status, body, elapsed = self.http(f"/api/sessions/{session_id}/optimize", {"request_id": request_id})
        require(status == 202, "Native optimize must return HTTP 202")
        require(body["data"]["profile"] == "BALANCED", "Default server profile is not BALANCED")
        return body["data"], elapsed

    def view(self, session_id, job_id):
        status, body, elapsed = self.http(f"/api/sessions/{session_id}/jobs/{job_id}")
        require(status == 200, "Native job polling failed")
        value = body["data"]
        require(value["schema_version"] == "task02-m2-runtime-job-view/1" and value["job_id"] == job_id
            and value["input_basis"]["session_id"] == session_id and value["job_status"] in (*TERMINAL, "QUEUED", "RUNNING"),
            "Native public typed job-view invariant failed")
        return value, elapsed

    def cancel(self, session_id, job_id, request_id):
        status, body, _ = self.http(f"/api/sessions/{session_id}/jobs/{job_id}/cancel", {"request_id": request_id})
        require(status == 200, "Native cancellation failed")
        return body["data"]

    def poll_terminal(self, session_id, job_id, before, timeout=240):
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        observations = []
        running_evidence = None
        while time.monotonic() < deadline:
            value, elapsed = self.view(session_id, job_id)
            observations.append({"job_status": value["job_status"], "read_seconds": round(elapsed, 4),
                "elapsed_seconds": round(time.monotonic() - started, 4)})
            if value["job_status"] == "RUNNING" and running_evidence is None:
                current, state_seconds = self.state(session_id)
                require(current == before, "Optimize altered physical execution state while RUNNING")
                ready_status, ready, ready_seconds = self.http("/ready", actor=None)
                require(ready_status == 200 and ready["data"]["ready"] is True, "Readiness is not healthy during native compute")
                heartbeat, age = self.heartbeat()
                running_evidence = {"state_http_status": 200, "state_read_seconds": round(state_seconds, 4),
                    "full_execution_view_unchanged": True, "ready_http_status": ready_status,
                    "ready_seconds": round(ready_seconds, 4), "ready_data": ready["data"],
                    "heartbeat": heartbeat, "heartbeat_age_seconds": round(age, 4)}
            if value["job_status"] in TERMINAL:
                return value, observations, running_evidence
            time.sleep(1)
        raise TimeoutError("Native job did not reach a terminal state within the 240-second acceptance deadline")


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
    run_id = "m3-step4-" + uuid4().hex
    receipt = {"schema_version": "saferoute-m3-step4-native-worker/1", "status": "RUNNING",
        "started_at": datetime.now(timezone.utc).isoformat(), "project_root": str(project), "run_id": run_id,
        "runtime_build_sha256": config["expected_build_sha256"], "compute_budget_seconds": settings.compute_budget_seconds,
        "execution_mode": "SIMULATED_REPLAY", "online_target_is_sla": False,
        "checks": {}, "sessions": {}, "notes": ["Forecast only: no accept, advance, physical delivery or real GPS.",
            "Timings are native observations, not an SLA.", "The verification starts and stops its own localhost HTTP server and worker."]}
    harness = NativeHarness(project, settings, args.output)
    failed = None
    try:
        harness.start_server()
        initial_ready = harness.start_worker()
        receipt["checks"]["real_worker_readiness"] = {"status": "PASS", "http_status": 200, "data": initial_ready}
        session_id = harness.load("S0", run_id + "-load-S0")
        receipt["sessions"]["S0"] = session_id
        before, _ = harness.state(session_id)
        require(before["active_job_id"] is None and before["observed_metrics"] is None and before["delivered_prefix"] == [],
            "Native test session does not start with an unactivated plan and empty actual delivery")
        request_id = run_id + "-optimize-S0"
        submission, submit_seconds = harness.submit(session_id, request_id)
        job_id = submission["job_id"]
        require(submission["input_basis"] == before["basis"], "Submit receipt changed the exact server basis")
        require(submit_seconds < settings.compute_budget_seconds, "Submission consumed the full server solver budget")
        retry, _ = harness.submit(session_id, request_id)
        require(retry == submission, "Same optimize request ID did not preserve the persisted job mapping")
        status, conflict, _ = harness.http(f"/api/sessions/{session_id}/optimize",
            {"request_id": request_id, "profile": "FASTEST"})
        require(status == 409 and conflict["diagnostics"][0]["code"] == "IDEMPOTENCY_CONFLICT",
            "Changed profile reused an optimize request ID")
        receipt["checks"]["asynchronous_submit"] = {"status": "PASS", "http_status": 202,
            "submit_seconds": round(submit_seconds, 4), "submission": submission,
            "same_id_same_job": True, "changed_profile_http_status": status}
        terminal, observations, running = harness.poll_terminal(session_id, job_id, before)
        require(running is not None, "Did not observe actual native RUNNING lifecycle")
        require(terminal["job_status"] == "COMPLETED" and terminal["validation"]["valid"] is True
            and terminal["plan_available"] is True and terminal["coverage_evaluated"] is True,
            "S0 did not produce a completed independently validated native plan witness")
        after, _ = harness.state(session_id)
        require(after == before, "Completed forecast altered the physical execution view")
        receipt["checks"]["real_native_witness"] = {"status": "PASS", "job_view": terminal,
            "observations": observations, "while_running": running}
        receipt["checks"]["forecast_only"] = {"status": "PASS", "before_sha256": fingerprint(before),
            "after_sha256": fingerprint(after), "full_execution_view_unchanged": True,
            "basis": after["basis"], "active_job_id": after["active_job_id"], "delivered_prefix": after["delivered_prefix"],
            "observed_metrics": after["observed_metrics"]}
        completed_cancel = harness.cancel(session_id, job_id, run_id + "-cancel-completed")
        require(completed_cancel["status"] == "COMPLETED_IMMUTABLE", "Completed native job was mutable through cancel")
        require(harness.view(session_id, job_id)[0] == terminal, "Completed cancellation changed the typed job view")
        receipt["checks"]["completed_cancel"] = {"status": "PASS", "receipt": completed_cancel, "job_view_unchanged": True}
        harness.wait_idle()
        heartbeat_before, _ = harness.heartbeat()
        singleton_log_path = args.output.with_suffix(".singleton.log")
        with singleton_log_path.open("w", encoding="utf-8") as singleton_log:
            second = harness.spawn("start_worker.py", singleton_log)
            try:
                singleton_exit = second.wait(timeout=45)
            finally:
                harness.stop(second)
        heartbeat_after, _ = harness.heartbeat()
        require(singleton_exit == 3 and heartbeat_after["worker_id"] == heartbeat_before["worker_id"],
            "Second compute worker did not reject startup with singleton exit 3")
        require(harness.view(session_id, job_id)[0] == terminal, "Rejected second worker changed completed native job")
        receipt["checks"]["singleton"] = {"status": "PASS", "second_worker_exit_code": singleton_exit,
            "active_worker_id": heartbeat_after["worker_id"], "no_recovery_or_job_mutation": True}
        harness.stop_worker()
        # Forced Windows process-tree termination cannot publish STOPPED;
        # readiness expires the last READY heartbeat within its 30-second TTL.
        deadline = time.monotonic() + 45
        while True:
            ready_status, ready, _ = harness.http("/ready", actor=None)
            if ready_status == 503:
                break
            require(time.monotonic() < deadline, "Readiness remained healthy after the worker process stopped")
            time.sleep(0.25)
        queued_submission, _ = harness.submit(session_id, run_id + "-queued")
        queued_id = queued_submission["job_id"]
        require(harness.view(session_id, queued_id)[0]["job_status"] == "QUEUED", "Stopped worker unexpectedly executed queued job")
        queued_cancel = harness.cancel(session_id, queued_id, run_id + "-cancel-queued")
        require(queued_cancel["status"] == "JOB_CANCELLED", "Queued cancellation receipt did not report JOB_CANCELLED")
        require(harness.cancel(session_id, queued_id, run_id + "-cancel-queued") == queued_cancel,
            "Queued cancellation retry did not preserve its receipt")
        cancelled_view, _ = harness.view(session_id, queued_id)
        require(cancelled_view["job_status"] == "FAILED" and cancelled_view["plan_available"] is False
            and any(item["code"] == "JOB_CANCELLED" for item in cancelled_view["diagnostics"]),
            "Queued cancellation did not publish the typed FAILED/JOB_CANCELLED outcome")
        require(harness.state(session_id)[0] == before, "Queued cancellation changed physical execution state")
        receipt["checks"]["queued_cancel"] = {"status": "PASS", "worker_stopped_ready_http_status": ready_status,
            "receipt": queued_cancel, "job_view": cancelled_view, "same_id_same_receipt": True}
        harness.stop_server()
        harness.start_server()
        restarted_ready = harness.start_worker()
        retry, _ = harness.submit(session_id, request_id)
        require(retry == submission and harness.view(session_id, job_id)[0] == terminal,
            "HTTP/worker restart lost optimize mapping or completed native result")
        require(harness.cancel(session_id, queued_id, run_id + "-cancel-queued") == queued_cancel
            and harness.view(session_id, queued_id)[0] == cancelled_view, "Restart lost durable cancellation receipt")
        require(harness.state(session_id)[0] == before, "Restart changed physical execution view")
        receipt["checks"]["restart_persistence"] = {"status": "PASS", "same_id_same_job": True,
            "same_id_same_cancellation": True, "completed_and_cancelled_views_preserved": True,
            "real_worker_ready_http_status": 200, "readiness": restarted_ready}
        running_session = harness.load("S1", run_id + "-load-S1")
        receipt["sessions"]["S1"] = running_session
        running_before, _ = harness.state(running_session)
        running_submission, _ = harness.submit(running_session, run_id + "-running-cancel")
        running_id = running_submission["job_id"]
        deadline = time.monotonic() + 90
        while True:
            running_view, _ = harness.view(running_session, running_id)
            if running_view["job_status"] == "RUNNING":
                break
            if running_view["job_status"] in TERMINAL:
                break
            require(time.monotonic() < deadline, "S1 was not dispatched before the running-cancel deadline")
            time.sleep(0.25)
        running_cancel = harness.cancel(running_session, running_id, run_id + "-cancel-running")
        if running_cancel["status"] == "JOB_CANCELLED":
            require(running_view["job_status"] == "RUNNING", "Running cancellation only observed a queued job")
            cancelled_running, _ = harness.view(running_session, running_id)
            require(cancelled_running["job_status"] == "FAILED" and cancelled_running["plan_available"] is False
                and any(item["code"] == "JOB_CANCELLED" for item in cancelled_running["diagnostics"]),
                "Running cancellation did not expose FAILED/JOB_CANCELLED")
            # Do not call a quick cancel response proof that native solve ended:
            # wait for the real compute bridge to exit and the worker to drain.
            idle_heartbeat = harness.wait_idle(timeout=min(240, settings.compute_budget_seconds + 90))
            require(harness.view(running_session, running_id)[0] == cancelled_running,
                "Cancelled running compute published a late result")
            require(harness.cancel(running_session, running_id, run_id + "-cancel-running") == running_cancel,
                "Running cancellation retry lost its persisted receipt")
            require(harness.state(running_session)[0] == running_before, "Running cancellation changed physical execution state")
            receipt["checks"]["running_cancel"] = {"status": "PASS", "observed_before_cancel": "RUNNING",
                "receipt": running_cancel, "job_view": cancelled_running, "no_late_publish_after_native_exit": True,
                "worker_idle_heartbeat": idle_heartbeat, "full_execution_view_unchanged": True}
        else:
            require(running_cancel["status"] == "COMPLETED_IMMUTABLE", "Unexpected cancellation race outcome")
            harness.wait_idle()
            require(harness.state(running_session)[0] == running_before, "Completed S1 race changed physical execution state")
            receipt["checks"]["running_cancel"] = {"status": "COMPLETION_RACE_OBSERVED",
                "observed_before_cancel": running_view["job_status"], "receipt": running_cancel,
                "note": "Completed before cancellation won; no running-cancel pass is claimed."}
        final_ready = harness.wait_ready()
        receipt["checks"]["final_readiness"] = {"status": "PASS", "http_status": 200, "data": final_ready}
        status, openapi, _ = harness.http("/openapi.json", actor=None)
        require(status == 200 and all(path in openapi["paths"] for path in
            ("/api/sessions/{session_id}/optimize", "/api/sessions/{session_id}/jobs/{job_id}",
                "/api/sessions/{session_id}/jobs/{job_id}/cancel")), "Native OpenAPI does not expose the job lifecycle")
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
    receipt["status"] = "M3_STEP4_NATIVE_WORKER_PASS" if failed is None else "M3_STEP4_NATIVE_WORKER_FAIL"
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"status": receipt["status"], "receipt": str(args.output),
        "server_stopped": True, "worker_stopped": True}), flush=True)
    return 0 if failed is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
