"""Isolated read-only mock examples; no runtime, source or authority mutations."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

from fastapi.testclient import TestClient
import pytest

from backend.mock.app import MODE, UNITS, create_mock_app
from backend.mock.fixtures import DEFAULT_ROOT, FixtureStore, MANIFEST_SHA256, SCENARIOS, SOURCE_BUILD
from backend.mock.serve import main
from backend.services.runtime_gateway import RuntimeGateway


TOKEN = "mock-test-token-" + "a" * 48
AUTH = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def fixture_root(tmp_path):
    path = tmp_path / "examples"
    shutil.copytree(DEFAULT_ROOT, path)
    return path


@pytest.fixture
def client(fixture_root):
    with TestClient(create_mock_app(enabled=True, bearer_token=TOKEN, fixture_root=fixture_root)) as value:
        yield value


def assert_error(response, status, code):
    assert response.status_code == status
    value = response.json()
    assert value["mode"] == MODE and value["authority_access"] is False and value["runtime_jobs"] is False
    assert value["status"] == "ERROR" and value["data"] is None
    assert value["diagnostics"][0]["code"] == code
    assert response.headers["X-SafeRoute-Mode"] == MODE
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("enabled", [False, None, 1, "true"])
def test_explicit_demo_opt_in(enabled):
    with pytest.raises(ValueError, match="opt-in"):
        create_mock_app(enabled=enabled, bearer_token=TOKEN)


@pytest.mark.parametrize("token", [None, "short", "x" * 257, "x" * 31 + "é", "x" * 31 + " ", "x" * 31 + "\n"])
def test_private_separate_token_required(token):
    with pytest.raises(ValueError, match="bearer token"):
        create_mock_app(enabled=True, bearer_token=token)


@pytest.mark.parametrize("origins", [(), ("*",), ("file:///tmp",), ("http://localhost:5173/path",),
                                    ("http://alice:secret@localhost:5173",), ("http://localhost:5173?x=1",)])
def test_explicit_cors_origins(origins):
    with pytest.raises(ValueError, match="origin"):
        create_mock_app(enabled=True, bearer_token=TOKEN, cors_origins=origins)


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer wrong"},
                                     {"Authorization": "Bearer " + TOKEN + " "}])
def test_read_authentication(client, headers):
    response = client.get("/api/mock/capabilities", headers=headers)
    assert_error(response, 401, "MOCK_UNAUTHORIZED")
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_duplicate_authorization_is_rejected(client):
    assert_error(client.get("/api/mock/capabilities", headers=[("Authorization", "Bearer " + TOKEN),
                 ("Authorization", "Bearer " + TOKEN)]), 401, "MOCK_UNAUTHORIZED")


def test_capabilities_truthfully_label_historical_example_scope(client):
    result = client.get("/api/mock/capabilities", headers=AUTH).json()
    assert result["mode"] == "MOCK_DEMO" and result["authority_access"] is False and result["runtime_jobs"] is False
    data = result["data"]
    assert data["source_package_build_sha256"] == SOURCE_BUILD
    assert data["example_execution_build_may_be_historical"] is True
    assert data["production_fallback"] is False and data["native_solver_or_raw_validator_test"] is False
    assert all(data[k] is False for k in ("can_optimize", "can_accept", "can_apply_event", "can_replay"))
    assert data["metric_units"] == UNITS


def test_catalog_exact_pinned_examples(client):
    response = client.get("/api/mock/scenarios", headers=AUTH)
    assert response.status_code == 200
    rows = response.json()["data"]["scenarios"]
    assert [row["scenario_id"] for row in rows] == list(SCENARIOS)
    for row in rows:
        assert row["file"] == row["scenario_id"] + "_execution_view.json"
        assert row["fixture_build_sha256"] != SOURCE_BUILD


def test_head_is_authenticated_labeled_and_returns_no_body(client):
    response = client.head("/api/mock/scenarios/S2/source", headers=AUTH)
    assert response.status_code == 200 and response.content == b""
    assert response.headers["X-SafeRoute-Mode"] == MODE
    assert response.headers["X-Fixture-SHA256"] == FixtureStore().record("S2")["sha256"]
    assert client.head("/api/mock/capabilities").status_code == 401


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_source_exact_bytes_and_wrapper_has_unchanged_units_numbers_and_nulls(client, fixture_root, scenario_id):
    raw = (fixture_root / f"{scenario_id}_execution_view.json").read_bytes()
    expected = json.loads(raw)
    source = client.get(f"/api/mock/scenarios/{scenario_id}/source", headers=AUTH)
    assert source.status_code == 200 and source.content == raw
    assert source.headers["X-Fixture-SHA256"] == hashlib.sha256(raw).hexdigest()
    assert source.headers["X-SafeRoute-Mode"] == MODE
    response = client.get(f"/api/mock/scenarios/{scenario_id}/execution-view", headers=AUTH)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["execution_view"] == expected
    assert data["metric_units"] == UNITS
    assert data["fixture_build_sha256"] == expected["basis"]["build_sha256"] != SOURCE_BUILD
    assert data["source_package_build_sha256"] == SOURCE_BUILD
    assert data["fixture_sha256"] == hashlib.sha256(raw).hexdigest()
    assert isinstance(data["execution_view"]["basis"]["head_version"], str)
    assert data["execution_view"]["metric_scope"] == "OBSERVED_PREFIX_ONLY"
    assert "relative_exposure_proxy" in data["execution_view"]["observed_metrics"]


@pytest.mark.parametrize("method,path", [("POST", "/api/mock/scenarios/S2/optimize"),
    ("POST", "/api/mock/scenarios/S2/accept"), ("POST", "/api/mock/scenarios/S3/events/E1/apply"),
    ("POST", "/api/mock/scenarios/S2/replay/step"), ("PUT", "/api/mock/scenarios"),
    ("PATCH", "/api/mock/scenarios/S4/execution-view"), ("DELETE", "/api/mock/scenarios/S2")])
def test_all_mutations_are_explicit_read_only_errors(client, method, path):
    response = client.request(method, path, headers=AUTH, json={"state": "browser-controlled"})
    assert_error(response, 405, "MOCK_READ_ONLY")
    assert response.headers["Allow"] == "GET, HEAD, OPTIONS"


@pytest.mark.parametrize("query", ["?fixture_root=D:/private", "?scenario_id=S5", "?build_sha256=fake", "?limit=1"])
def test_query_cannot_change_fixture_or_server_paths(client, query):
    assert_error(client.get("/api/mock/scenarios" + query, headers=AUTH), 422, "MOCK_REQUEST_FIELDS")


@pytest.mark.parametrize("body", [b'{"fixture":"outside"}', b'x' * 2_000_000], ids=["json_body", "large_body"])
def test_get_bodies_are_rejected_without_decode_or_buffer(client, body):
    assert_error(client.request("GET", "/api/mock/capabilities", content=body, headers=AUTH), 422, "MOCK_REQUEST_FIELDS")


@pytest.mark.parametrize("scenario_id", ["S0", "S5", "s2", "S2_execution_view.json", "%2e%2e%5cS2"])
def test_unknown_fixture_has_strict_id_binding(client, scenario_id):
    assert_error(client.get(f"/api/mock/scenarios/{scenario_id}/execution-view", headers=AUTH), 404, "MOCK_SCENARIO_NOT_FOUND")


def test_cors_preflight_only_explicit_origin_and_get(client):
    good = client.options("/api/mock/scenarios", headers={"Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "Authorization"})
    assert good.status_code == 200 and good.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert good.headers["X-SafeRoute-Mode"] == MODE
    evil = client.options("/api/mock/scenarios", headers={"Origin": "https://other.example",
        "Access-Control-Request-Method": "GET"})
    assert evil.status_code == 400 and "Access-Control-Allow-Origin" not in evil.headers
    mutation = client.options("/api/mock/scenarios", headers={"Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST"})
    assert mutation.status_code == 400


def test_tampered_fixture_fails_before_start_and_on_read(client, fixture_root):
    path = fixture_root / "S2_execution_view.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="bytes differ"):
        FixtureStore(fixture_root)
    assert_error(client.get("/api/mock/scenarios/S2/execution-view", headers=AUTH), 503, "MOCK_FIXTURE_INVALID")
    assert_error(client.get("/api/mock/scenarios", headers=AUTH), 503, "MOCK_FIXTURE_INVALID")


def test_manifest_cannot_repin_a_changed_fixture(fixture_root):
    path = fixture_root / "manifest.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == MANIFEST_SHA256
    value = json.loads(path.read_bytes())
    value["fixtures"]["S2"]["file"] = "../private.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest hash"):
        FixtureStore(fixture_root)


def test_no_runtime_sql_or_authority_files_are_used(tmp_path, fixture_root, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Mock invoked authority or runtime")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(RuntimeGateway, "_call", forbidden)
    monkeypatch.setenv("SAFEROUTE_INSTALLATION_CONFIG", str(tmp_path / "must-not-exist-installation.json"))
    monkeypatch.setenv("SAFEROUTE_METADATA_DB", str(tmp_path / "must-not-exist-metadata.sqlite"))
    before = {p.relative_to(tmp_path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in tmp_path.rglob("*") if p.is_file()}
    with TestClient(create_mock_app(enabled=True, bearer_token=TOKEN, fixture_root=fixture_root)) as value:
        for sid in SCENARIOS:
            assert value.get(f"/api/mock/scenarios/{sid}/execution-view", headers=AUTH).status_code == 200
            assert value.get(f"/api/mock/scenarios/{sid}/source", headers=AUTH).status_code == 200
    after = {p.relative_to(tmp_path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_launcher_requires_flag_private_token_and_fixed_loopback(tmp_path, monkeypatch):
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    token = tmp_path / "private-token.txt"
    token.write_text(TOKEN, encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--token-file", str(token)])
    with pytest.raises(SystemExit):
        main(["--enable-read-only-demo", "--token-file", "relative.txt"])
    with pytest.raises(SystemExit):
        main(["--enable-read-only-demo", "--token-file", str(token), "--host", "0.0.0.0"])
    assert main(["--enable-read-only-demo", "--token-file", str(token), "--port", "8051"]) == 0
    assert len(calls) == 1 and calls[0][1] == {"host": "127.0.0.1", "port": 8051, "access_log": False}
