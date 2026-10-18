from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from fastapi.testclient import TestClient
import pytest

from backend.api.dependencies import dispatcher, session_owner
from backend.api.errors import ApiError, envelope
from backend.api.main import create_app
from backend.models.http import OptimizeRequest, ReplayStepRequest
from backend.services.auth import Actor
from backend.services.settings import Settings

BUILD = "8" * 64
TOKENS = {"alice": "test-alice-" + "a" * 32, "bob": "test-bob-" + "b" * 32, "viewer": "test-viewer-" + "c" * 32}


class FakeGateway:
    async def capabilities(self):
        return {"schema_version": "task02-m2-runtime-capabilities/1", "build_sha256": BUILD,
                "execution_mode": "SIMULATED_REPLAY", "real_world_observation": False,
                "head_version": "9007199254740993", "observed_metrics": None,
                "ratio": {"numerator": "9007199254740993", "denominator": "3"}}


class FakeCatalog:
    def initialize(self):
        pass

    def healthy(self):
        return True


@pytest.fixture
def app(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"schema_version": "saferoute-m3-auth/1", "tokens": [
        {"actor_id": name, "role": "viewer" if name == "viewer" else "dispatcher",
         "token_sha256": hashlib.sha256(token.encode()).hexdigest()} for name, token in TOKENS.items()]}))
    receipt = tmp_path / "g0.json"
    receipt.write_text(json.dumps({"status": "G0_TECHNICAL_PASS", "step1_input_completeness": "COMPLETE_VERIFIED",
                                   "build_sha256": BUILD, "snapshot_root": str(tmp_path)}))
    installation = tmp_path / "installation.json"
    installation.write_text(json.dumps({"schema_version": "saferoute-m3-server-installation/1",
        "runtime_root": str(tmp_path), "runtime_python": str(tmp_path / "python.exe"), "snapshot_root": str(tmp_path),
        "latest_preflight_receipt": str(receipt), "authority_store_parent": str(tmp_path / "state"), "expected_build_sha256": BUILD}))
    settings = Settings(tmp_path, installation, auth, tmp_path / "metadata.sqlite", tmp_path / "heartbeat.json")
    result = create_app(settings, gateway=FakeGateway(), catalog=FakeCatalog())

    # Test-only routes exercise shared dependencies/models without exposing fake production endpoints.
    @result.post("/test/optimize")
    def validate(request: Request, body: OptimizeRequest, actor: Annotated[Actor, Depends(dispatcher)]):
        return envelope(request.state.request_id, body.model_dump())

    @result.post("/test/replay")
    def replay(body: ReplayStepRequest):
        return body.model_dump()

    @result.get("/test/sessions/{session_id}")
    def owned(actor: Annotated[Actor, Depends(session_owner)]):
        return {"actor_id": actor.actor_id}

    @result.get("/test/crash")
    def crash():
        raise RuntimeError("private installation details")

    return result


@pytest.fixture
def client(app):
    with TestClient(app, raise_server_exceptions=False) as result:
        app.state.sessions.register("session-alice", "alice")
        yield result


def headers(actor="alice", **extra):
    return {"Authorization": "Bearer " + TOKENS[actor], **extra}


def diagnostic(response, status, code):
    assert response.status_code == status
    body = response.json()
    assert body["status"] == "ERROR" and body["diagnostics"][0]["code"] == code
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["data"] is None


def test_liveness_and_trace(client):
    first = client.get("/health", headers={"X-Request-ID": "browser-controlled"})
    second = client.get("/health")
    assert first.status_code == 200 and first.json()["data"]["alive"] is True
    assert first.json()["request_id"] == first.headers["X-Request-ID"]
    assert first.headers["X-Request-ID"] not in ("browser-controlled", second.headers["X-Request-ID"])
    assert float(first.headers["X-Process-Time"]) >= 0


@pytest.mark.parametrize("authorization", [None, "Basic abc", "Bearer wrong-token-aaaaaaaaaaaaaaaa", "Bearer x"])
def test_unauthorized(client, authorization):
    response = client.get("/api/runtime/capabilities", headers={} if authorization is None else {"Authorization": authorization})
    diagnostic(response, 401, "UNAUTHORIZED")
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_exact_numbers_and_null_projection(client):
    response = client.get("/api/runtime/capabilities", headers=headers())
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["head_version"] == "9007199254740993" and data["observed_metrics"] is None
    assert data["ratio"] == {"numerator": "9007199254740993", "denominator": "3"}


def test_revoked_token(client, app):
    value = json.loads(app.state.settings.auth_path.read_text())
    value["tokens"] = value["tokens"][1:]
    app.state.settings.auth_path.write_text(json.dumps(value))
    diagnostic(client.get("/api/runtime/capabilities", headers=headers()), 401, "UNAUTHORIZED")


def test_missing_auth_fails_closed(client, app):
    app.state.settings.auth_path.unlink()
    diagnostic(client.get("/api/runtime/capabilities", headers=headers()), 503, "AUTH_NOT_CONFIGURED")


def test_dispatcher_role(client):
    diagnostic(client.post("/test/optimize", json={"request_id": "r1"}, headers=headers("viewer")), 403, "FORBIDDEN")


def test_session_owner_and_unknown(client):
    assert client.get("/test/sessions/session-alice", headers=headers()).status_code == 200
    diagnostic(client.get("/test/sessions/session-alice", headers=headers("bob")), 403, "FORBIDDEN")
    diagnostic(client.get("/test/sessions/missing", headers=headers()), 404, "SESSION_NOT_FOUND")
    diagnostic(client.get("/test/sessions/session-alice"), 401, "UNAUTHORIZED")


@pytest.mark.parametrize("raw,code", [
    (b'{"request_id":"a","request_id":"b"}', "DUPLICATE_JSON_KEY"),
    (b'{"request_id":"a","nested":{"v":1,"v":2}}', "DUPLICATE_JSON_KEY"),
    (b'{"request_id":"a","n":NaN}', "INVALID_JSON_NUMBER"),
    (b'{"request_id":"a","n":Infinity}', "INVALID_JSON_NUMBER"),
    (b'{"request_id":"a","n":-Infinity}', "INVALID_JSON_NUMBER"),
    (b'{"request_id":"a","n":1e999}', "INVALID_JSON_NUMBER"),
    (b'{"request_id":"a","n":9007199254740992}', "UNSAFE_JSON_INTEGER"),
    (b'{"request_id":"a","n":-9007199254740992}', "UNSAFE_JSON_INTEGER"),
    (b'{"request_id":"\xff"}', "INVALID_JSON"),
    (b'{', "INVALID_JSON"), (b'[]', "INVALID_JSON"), (b'null', "INVALID_JSON"),
    (b'{"request_id":"a","n":' + b'[' * 97 + b'0' + b']' * 97 + b'}', "JSON_DEPTH_EXCEEDED")])
def test_raw_decoder(client, raw, code):
    diagnostic(client.post("/test/optimize", content=raw, headers=headers(**{"Content-Type": "application/json"})), 422, code)


def test_body_limit_and_mime(client, app):
    app.state.settings  # boundary limit is configured at construction, not client-controlled.
    diagnostic(client.post("/test/optimize", content=b'x' * (1024 * 1024 + 1), headers=headers()), 413, "BODY_TOO_LARGE")
    diagnostic(client.post("/test/optimize", content=b'{"request_id":"r1"}', headers=headers()), 415, "UNSUPPORTED_MEDIA_TYPE")


@pytest.mark.parametrize("field,value", [("runtime_root", "D:/unsafe"), ("snapshot_root", "D:/unsafe"),
    ("build_sha256", BUILD), ("routes", []), ("state", {}), ("actor_id", "bob"), ("load", 20), ("budget_seconds", 3)])
def test_client_cannot_override_authority(client, field, value):
    diagnostic(client.post("/test/optimize", json={"request_id": "r1", field: value}, headers=headers()), 422, "VALIDATION_ERROR")


@pytest.mark.parametrize("revision", [
    {"head_version": 1, "generation": "0"}, {"head_version": "01", "generation": "0"},
    {"head_version": "9223372036854775808", "generation": "0"}, {"head_version": "1", "generation": "-1"}])
def test_strict_revision(client, revision):
    diagnostic(client.post("/test/optimize", json={"request_id": "r1", "expected_revision": revision}, headers=headers()), 422, "VALIDATION_ERROR")


def test_valid_model_preserves_int64(client):
    response = client.post("/test/optimize", headers=headers(), json={"request_id": "retry-1", "expected_revision":
        {"head_version": "9223372036854775807", "generation": "0"}})
    assert response.status_code == 200
    assert response.json()["data"]["profile"] == "BALANCED"
    assert response.json()["data"]["expected_revision"]["head_version"] == "9223372036854775807"


@pytest.mark.parametrize("time", ["2026-09-25T08:00:00Z", "2026-09-25T08:00:00.1234567+07:00", "2026-02-30T08:00:00+07:00"])
def test_replay_timestamp(client, time):
    diagnostic(client.post("/test/replay", json={"request_id": "r1", "expected_revision":
        {"head_version": "0", "generation": "0"}, "target_time": time}), 422, "VALIDATION_ERROR")


def test_cors_preflight_and_error(client):
    origin = "http://localhost:5173"
    response = client.options("/api/runtime/capabilities", headers={"Origin": origin,
        "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "Authorization"})
    assert response.status_code == 200 and response.headers["Access-Control-Allow-Origin"] == origin
    assert "X-Request-ID" in response.headers
    response = client.post("/test/optimize", content=b'{', headers={"Origin": origin, "Content-Type": "application/json"})
    assert response.status_code == 422 and response.headers["Access-Control-Allow-Origin"] == origin
    assert "Access-Control-Allow-Origin" not in client.get("/health", headers={"Origin": "https://untrusted.invalid"}).headers


def test_readiness_requires_worker(client):
    response = client.get("/ready")
    assert response.status_code == 503 and response.json()["data"]["ready"] is False
    assert response.json()["data"]["checks"] == {"g0": "PASS", "authentication": "PASS", "metadata": "PASS",
        "runtime": "PASS", "worker": "WORKER_NOT_CONFIGURED", "catalog": "PASS", "queue": "PASS", "acceptances": "PASS", "replay": "PASS",
        "outbox": "PASS", "recovery": "PASS", "artifacts": "PASS", "profiles": "PASS", "playback": "PASS"}


def heartbeat(app, *, build=BUILD, seconds=0):
    app.state.settings.heartbeat_path.write_text(json.dumps({"schema_version": "saferoute-m3-worker-heartbeat/1",
        "build_sha256": build, "installation_sha256": app.state.settings.installation_identity(),
        "status": "READY", "updated_at": (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()}))


def test_readiness_all_dependencies_with_test_worker(client, app):
    heartbeat(app)
    assert client.get("/ready").status_code == 200


@pytest.mark.parametrize("build,seconds", [(BUILD, 60), ("9" * 64, 0), (BUILD, -60)])
def test_readiness_rejects_bad_worker(client, app, build, seconds):
    heartbeat(app, build=build, seconds=seconds)
    assert client.get("/ready").json()["data"]["checks"]["worker"] == "WORKER_NOT_READY"


def test_readiness_rejects_other_source_receipt(client, app):
    config = app.state.settings.installation()
    receipt_path = Path(config["latest_preflight_receipt"])
    receipt = json.loads(receipt_path.read_text())
    receipt["snapshot_root"] = str(receipt_path.parent / "another-project")
    receipt_path.write_text(json.dumps(receipt))
    assert client.get("/ready").json()["data"]["checks"]["g0"] == "G0_NOT_VERIFIED"


def test_readiness_runtime_failure(client, app):
    async def fail():
        raise ApiError(503, "RUNTIME_UNAVAILABLE", "runtime", "Unavailable")
    app.state.gateway.capabilities = fail
    response = client.get("/ready")
    assert response.status_code == 503 and response.json()["data"]["checks"]["runtime"] == "RUNTIME_UNAVAILABLE"
    diagnostic(client.get("/api/runtime/capabilities", headers=headers()), 503, "RUNTIME_UNAVAILABLE")


def test_uniform_errors_no_private_leak(client):
    diagnostic(client.get("/missing"), 404, "HTTP_404")
    response = client.get("/test/crash")
    diagnostic(response, 500, "INTERNAL_ERROR")
    assert "private" not in response.text


def test_audit_identity_no_token_or_body(client, caplog):
    with caplog.at_level(logging.INFO, logger="saferoute.http"):
        client.post("/test/optimize", headers=headers(), json={"request_id": "body-secret-marker"})
    text = caplog.text
    assert '"actor_id": "alice"' in text
    assert TOKENS["alice"] not in text and "body-secret-marker" not in text


def test_openapi_documents_auth_and_reserved_models(client):
    value = client.get("/openapi.json").json()
    assert value["paths"]["/api/runtime/capabilities"]["get"]["security"] == [{"ServerBearer": []}]
    assert value["components"]["schemas"]["OptimizeRequest"]["additionalProperties"] is False
    assert value["components"]["schemas"]["ExpectedRevision"]["properties"]["head_version"]["type"] == "string"
    assert client.get("/docs").status_code == 200
