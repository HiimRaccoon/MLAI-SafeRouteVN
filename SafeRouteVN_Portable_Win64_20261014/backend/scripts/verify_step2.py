"""Native HTTP smoke: real Uvicorn socket and installed public RuntimeClient."""
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project))
    from backend.services.settings import Settings
    settings = Settings.from_environment()
    config = settings.installation()
    credentials = json.loads((settings.auth_path.parent / "dev_access.json").read_text(encoding="utf-8"))
    token = credentials["credentials"][0]["token"]
    with socket.socket() as selection:
        selection.bind(("127.0.0.1", 0))
        port = selection.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    log_path = args.output.with_suffix(".server.log")
    receipt = {"schema_version": "saferoute-m3-step2-native-http/1", "started_at": datetime.now(timezone.utc).isoformat(),
               "project_root": str(project), "backend_python": sys.executable, "runtime_python": config["runtime_python"],
               "build_sha256": config["expected_build_sha256"], "base_url": base_url, "checks": {}}

    def get(path, *, authorized=False, extra=None, method="GET"):
        headers = dict(extra or {})
        if authorized:
            headers["Authorization"] = "Bearer " + token
        try:
            response = urlopen(Request(base_url + path, headers=headers, method=method), timeout=25)
        except HTTPError as response_error:
            response = response_error
        with response:
            raw = response.read()
            content = json.loads(raw) if raw and "application/json" in response.headers.get("Content-Type", "") else raw.decode("utf-8")
            return response.status, {key.lower(): value for key, value in response.headers.items()}, content

    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-B", str(project / "backend/scripts/start_backend.py"), "--port", str(port)],
            cwd=project, stdout=log, stderr=log, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 25
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Uvicorn stopped before health check")
                try:
                    health = get("/health")
                    break
                except (URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.15)
            if health[0] != 200 or health[2]["data"]["alive"] is not True:
                raise ValueError("Health invariant")
            receipt["checks"]["health"] = {"status": "PASS", "http_status": health[0]}
            unauthenticated = get("/api/runtime/capabilities")
            if unauthenticated[0] != 401:
                raise ValueError("Authentication invariant")
            receipt["checks"]["authentication"] = {"status": "PASS", "unauthenticated_http_status": 401}
            capabilities = get("/api/runtime/capabilities", authorized=True)
            value = capabilities[2]["data"]
            if (capabilities[0] != 200 or value["schema_version"] != "task02-m2-runtime-capabilities/1"
                    or value["build_sha256"] != config["expected_build_sha256"] or value["real_world_observation"] is not False):
                raise ValueError("Real SDK capability invariant")
            receipt["checks"]["native_capabilities"] = {"status": "PASS", "http_status": 200, "view": value}
            readiness = get("/ready")
            checks = readiness[2]["data"]["checks"]
            worker_ready = checks.get("worker") == "PASS"
            if (readiness[0] != (200 if worker_ready else 503)
                    or any(checks.get(key) != "PASS" for key in ("g0", "authentication", "metadata", "runtime", "catalog", "queue"))
                    or checks.get("worker") not in ("PASS", "WORKER_NOT_CONFIGURED", "WORKER_NOT_READY")):
                raise ValueError("Readiness must reflect the real worker heartbeat")
            receipt["checks"]["readiness_gate"] = {"status": "PASS", "http_status": 503, "data": readiness[2]["data"]}
            preflight = get("/api/runtime/capabilities", method="OPTIONS", extra={"Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "Authorization"})
            if preflight[0] != 200 or preflight[1].get("access-control-allow-origin") != "http://localhost:5173":
                raise ValueError("CORS invariant")
            receipt["checks"]["frontend_preflight"] = {"status": "PASS", "http_status": 200}
            openapi = get("/openapi.json")
            if openapi[0] != 200 or openapi[2]["components"]["schemas"]["OptimizeRequest"]["additionalProperties"] is not False:
                raise ValueError("OpenAPI invariant")
            (args.output.parent / "openapi.json").write_text(json.dumps(openapi[2], ensure_ascii=False, indent=2), encoding="utf-8")
            receipt["checks"]["openapi"] = {"status": "PASS", "http_status": 200}
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    checker = subprocess.run([sys.executable, "-B", str(project / "docs/M2_STEP7_VERIFY_20261004.py"),
                              "--project-root", str(project)], cwd=project, capture_output=True, text=True, encoding="utf-8", timeout=60)
    verified = json.loads(checker.stdout)
    if checker.returncode != 0 or verified["status"] != "TEAM_HANDOFF_BYTES_VERIFIED":
        raise ValueError("Frozen handoff changed during M3-02")
    receipt["checks"]["frozen_handoff"] = {"status": "PASS", "result": verified}
    receipt["status"] = "M3_STEP2_NATIVE_HTTP_PASS"
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    receipt["server_running_after_test"] = False
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": receipt["status"], "checks": list(receipt["checks"]), "receipt": str(args.output)}))


if __name__ == "__main__":
    main()
