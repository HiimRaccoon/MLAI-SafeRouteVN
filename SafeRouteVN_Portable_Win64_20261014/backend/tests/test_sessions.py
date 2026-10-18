import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from backend.api.errors import ApiError
from backend.api.main import create_app
from backend.services.scenario_catalog import ScenarioCatalog
from backend.services.session_repository import SessionRepository
from backend.services.settings import Settings

TOKENS = {name: "test-session-" + name + "-" + "x" * 32 for name in ("alice", "bob", "viewer")}
EPOCH = "2026-09-27T21:00:00+07:00"


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def setup(tmp_path):
    root, runtime = tmp_path / "project", tmp_path / "runtime"
    suite, fixtures, index, hashes = "thu-duc-binh-thanh-v1", {}, {}, {}
    for number in range(9):
        key = f"S{number}"
        base = {"latitude": 10.8, "longitude": 106.7, "graphNodeId": 123,
            "deliveryRegionId": "region", "demandKg": 4, "priority": 1, "serviceTimeHours": 0.25,
            "pickupLocationId": "DEPOT", "earliest": EPOCH, "preferredDue": EPOCH, "hardDeadline": EPOCH}
        orders = [{**base, "id": "O1", "status": "WAITING"}, {**base, "id": "O2", "status": "ONBOARD"}]
        events = [{"eventId": "urgent-1", "type": "URGENT_ORDER", "timestamp": EPOCH,
                   "orderPayload": {**base, "id": "O3", "status": "WAITING"}}] if key == "S2" else []
        fixtures[key] = {"schemaVersion": "member1-scenario-draft/1", "scenarioId": key, "sourceType": "TEST_ONLY", "events": events,
            "initialState": {"currentTime": EPOCH, "orders": orders, "vehicles": [{"id": "V1", "type": "motorcycle",
                "costPerKmVnd": 2500, "rangeKm": 120, "workingStart": EPOCH, "workingEnd": EPOCH}],
                "locations": [{"id": "DEPOT", "graphNodeId": 123, "latitude": 10.8, "longitude": 106.7,
                               "openingTime": EPOCH, "closingTime": EPOCH}]}}
        hashes[key] = write(root / f"scenarios/fixtures/{suite}/{key}.json", fixtures[key])
        index[key] = {"sha256": hashes[key], "orders": 2, "vehicles": 1}
    catalog_sha = write(root / f"scenarios/manifests/{suite}.json", {"schemaVersion": "member1-scenario-catalog/1",
        "version": "catalog-test-v1", "suiteReady": True, "scenarios": index, "sourceRun": "TEST_ONLY",
        "versions": {"routing": "test-routing"}, "seed": 42, "units": {"distance": "km", "duration": "h"}})
    receipt_name = "optimization/integration/member1_trusted_receipt.json"
    receipt_sha = write(runtime / receipt_name, {"suite_id": suite, "catalog_raw_sha256": catalog_sha,
        "catalog_version": "catalog-test-v1", "fixture_raw_sha256": hashes})
    inventory = {"files": {receipt_name: {"bytes": (runtime / receipt_name).stat().st_size, "sha256": receipt_sha}}}
    build = hashlib.sha256(json.dumps(inventory, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    write(runtime / "production_inventory.json", {**inventory, "build_sha256": build})
    write(tmp_path / "g0.json", {"status": "G0_TECHNICAL_PASS", "step1_input_completeness": "COMPLETE_VERIFIED",
        "snapshot_root": str(root), "build_sha256": build})
    write(tmp_path / "installation.json", {"schema_version": "saferoute-m3-server-installation/1", "runtime_root": str(runtime),
        "runtime_python": str(tmp_path / "python.exe"), "snapshot_root": str(root), "expected_build_sha256": build,
        "authority_store_parent": str(tmp_path / "state"), "latest_preflight_receipt": str(tmp_path / "g0.json")})
    write(tmp_path / "auth.json", {"schema_version": "saferoute-m3-auth/1", "tokens": [{"actor_id": name,
        "role": "viewer" if name == "viewer" else "dispatcher", "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
        for name, token in TOKENS.items()]})
    settings = Settings(root, tmp_path / "installation.json", tmp_path / "auth.json", tmp_path / "metadata.sqlite", tmp_path / "worker.json")
    gateway = FakeGateway(build)
    return settings, gateway


class FakeGateway:
    def __init__(self, build):
        self.build, self.views, self.commands, self.reads = build, {}, [], []
        self.fail_after_commit = False

    async def capabilities(self):
        return {"schema_version": "task02-m2-runtime-capabilities/1", "build_sha256": self.build}

    async def bootstrap(self, session_id, scenario_id, command_id):
        self.commands.append((session_id, scenario_id, command_id))
        if session_id not in self.views:
            self.views[session_id] = {"schema_version": "task02-m2-execution-view/2", "basis": {"session_id": session_id,
                "build_sha256": self.build, "head_version": "1", "generation": "0", "root_sha256": "1" * 64,
                "head_sha256": "2" * 64, "source_sha256": "3" * 64, "context_version": "test-context", "overlay_sha256": None}, "execution_mode": "SIMULATED_REPLAY",
                "real_world_observation": False, "current_time": EPOCH, "order_ids": ["O1", "O2"],
                "delivered_prefix": [], "planned_served_suffix": [], "unserved": [{"order_id": key, "reason": "NO_ACCEPTED_PLAN"} for key in ("O1", "O2")],
                "metric_scope": "OBSERVED_PREFIX_ONLY", "observed_metrics": None, "vehicles": [{"vehicle_id": "V1", "availability": "UNAVAILABLE",
                    "current_load_kg": 4, "onboard_order_ids": ["O2"], "capacity_kg": 15, "remaining_range_m": 120000,
                    "position": {"kind": "AT_NODE", "node_id": 123, "coordinates": [106.7, 10.8]}}],
                "active_job_id": None, "pending_event_ids": ["urgent-1"] if scenario_id == "S2" else [],
                "planned_suffix_metrics": None, "projected_whole_metrics": None, "accepted_trajectory": None}
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise ApiError(503, "RUNTIME_TIMEOUT", "runtime", "Response interrupted after commit")
        return {"status": "BOOTSTRAPPED", "basis": deepcopy(self.views[session_id]["basis"])}

    async def resolve(self, session_id):
        self.reads.append(session_id)
        return deepcopy(self.views[session_id])

    async def read_notifications(self, session_id):
        return []

    async def acknowledge(self, session_id, command_id, event_id):
        return {"status": "ACKNOWLEDGED", "event_id": event_id}

    async def validate_session(self, session_id):
        return {"validator_version": "task02-m2-bound-session-validator/2", "valid": True,
            "checked_physical_mutations": 0, "historical_execution_builds_preserved": True,
            "current_checker_build_sha256": self.build,
            "scope": "TRUSTED_LOG_RAW_PREFIX_EVENT_SUFFIX; SIMULATED_REPLAY; NOT_OPTIMALITY"}

    async def inspect_sessions(self, session_ids, command_id):
        values = []
        for sid in session_ids:
            recovery = await self.recover(sid, command_id + ":" + sid)
            values.append({"session_id": sid, "recovery": recovery, "validation": await self.validate_session(sid),
                           "execution_view": await self.resolve(sid), "notifications": await self.read_notifications(sid)})
        return values

    async def read_notifications_batch(self, session_ids):
        return {sid: await self.read_notifications(sid) for sid in session_ids}

    async def acknowledge_batch(self, rows):
        return [await self.acknowledge(r["session_id"], r["command_id"], r["event_id"]) for r in rows]


@pytest.fixture
def client(setup):
    settings, gateway = setup
    with TestClient(create_app(settings, gateway=gateway), raise_server_exceptions=False) as client:
        yield client


def headers(actor="alice"):
    return {"Authorization": "Bearer " + TOKENS[actor]}


def load(client, scenario="S0", request_id="load-1", actor="alice"):
    return client.post(f"/api/scenarios/{scenario}/load", json={"request_id": request_id}, headers=headers(actor))


def code(response, status, expected):
    assert response.status_code == status, response.text
    assert response.json()["diagnostics"][0]["code"] == expected


def test_catalog_counts_versions_and_auth(client):
    assert client.get("/api/scenarios").status_code == 401
    data = client.get("/api/scenarios", headers=headers("viewer")).json()["data"]
    assert [item["scenario_id"] for item in data["scenarios"]] == [f"S{i}" for i in range(9)]
    assert data["catalog_version"] == "catalog-test-v1" and data["versions"]["routing"] == "test-routing"
    assert data["source_units"]["distance"] == "km" and data["projection_units"]["distance"] == "m"
    assert client.get("/api/scenarios/S2", headers=headers()).json()["data"]["events"][0]["event_type"] == "URGENT_ORDER"
    assert load(client, "S9").status_code == 422
    code(load(client, actor="viewer"), 403, "FORBIDDEN")


def test_retry_is_durable_and_new_requests_create_independent_sessions(client, setup):
    first = load(client).json()["data"]
    retry = load(client).json()["data"]
    second = load(client, request_id="load-2").json()["data"]
    assert first == retry and first["session"]["session_id"] != second["session"]["session_id"]
    assert len(setup[1].commands) == 2
    code(load(client, "S1"), 409, "IDEMPOTENCY_CONFLICT")


@pytest.mark.parametrize("suffix", ["", "/state", "/orders", "/vehicles", "/locations"])
def test_cross_owner_denied_before_sdk(client, setup, suffix):
    session = load(client).json()["data"]["session"]["session_id"]
    reads = len(setup[1].reads)
    code(client.get(f"/api/sessions/{session}{suffix}", headers=headers("bob")), 403, "FORBIDDEN")
    assert len(setup[1].reads) == reads
    assert client.get(f"/api/sessions/{session}{suffix}", headers=headers()).status_code == 200


def test_unknown_and_unauthenticated_session(client):
    code(client.get("/api/sessions/missing/state", headers=headers()), 404, "SESSION_NOT_FOUND")
    code(client.get("/api/sessions/missing/state"), 401, "UNAUTHORIZED")


def test_read_projections_preserve_custody_units_and_null(client, setup):
    data = load(client).json()["data"]
    session = data["session"]["session_id"]
    assert data["execution_view"]["observed_metrics"] is None
    orders = client.get(f"/api/sessions/{session}/orders", headers=headers()).json()["data"]["orders"]
    assert orders[0]["status"] == "WAITING" and orders[1]["status"] == "ONBOARD"
    assert orders[1]["owner_vehicle_id"] == "V1" and orders[1]["service_time_s"] == 900
    assert orders[0]["coordinates"] == [106.7, 10.8]
    vehicles = client.get(f"/api/sessions/{session}/vehicles", headers=headers()).json()["data"]
    assert vehicles["vehicles"][0]["availability"] == "UNAVAILABLE" and vehicles["vehicle_metadata"][0]["range_m"] == 120000
    locations = client.get(f"/api/sessions/{session}/locations", headers=headers()).json()["data"]["locations"]
    assert len(locations) == 3 and locations[0]["kind"] == "DEPOT"
    assert locations[0]["graph_node_id"] == "123"
    assert "state" in data["session"]["links"]


def test_fresh_view_status_and_urgent_metadata(client, setup):
    session = load(client, "S2").json()["data"]["session"]["session_id"]
    view = setup[1].views[session]
    view["order_ids"].append("O3")
    view["delivered_prefix"] = ["O1"]
    view["planned_served_suffix"] = ["O3"]
    view["basis"]["head_version"] = "9007199254740993"
    response = client.get(f"/api/sessions/{session}/orders", headers=headers()).json()["data"]
    assert response["basis"]["head_version"] == "9007199254740993"
    assert response["orders"][0]["status"] == "DELIVERED"
    assert response["orders"][2]["status"] == "WAITING" and response["orders"][2]["planned_in_accepted_suffix"] is True
    assert len(client.get(f"/api/sessions/{session}/locations", headers=headers()).json()["data"]["locations"]) == 4


def test_runtime_order_without_verified_metadata_fails_closed(client, setup):
    session = load(client).json()["data"]["session"]["session_id"]
    setup[1].views[session]["order_ids"].append("UNVERIFIED")
    code(client.get(f"/api/sessions/{session}/orders", headers=headers()), 503, "ORDER_METADATA_UNAVAILABLE")


def test_restart_retains_owner_and_load_receipt(setup):
    settings, gateway = setup
    with TestClient(create_app(settings, gateway=gateway)) as first:
        data = load(first).json()["data"]
    with TestClient(create_app(settings, gateway=gateway)) as restarted:
        assert load(restarted).json()["data"] == data
        session = data["session"]["session_id"]
        assert restarted.get(f"/api/sessions/{session}/state", headers=headers()).status_code == 200
        code(restarted.get(f"/api/sessions/{session}/state", headers=headers("bob")), 403, "FORBIDDEN")
    assert len(gateway.commands) == 1


def test_uncertain_commit_retries_same_binding(client, setup):
    setup[1].fail_after_commit = True
    code(load(client), 503, "RUNTIME_TIMEOUT")
    result = load(client)
    assert result.status_code == 201
    assert setup[1].commands[0] == setup[1].commands[1] and len(setup[1].views) == 1


def test_same_request_id_is_scoped_to_actor(client):
    alice = load(client).json()["data"]["session"]["session_id"]
    bob = load(client, actor="bob").json()["data"]["session"]["session_id"]
    assert alice != bob


@pytest.mark.parametrize("field,value", [("session_id", "chosen-by-client"), ("actor_id", "bob"), ("snapshot_root", "D:/unsafe")])
def test_load_rejects_overrides(client, field, value):
    code(client.post("/api/scenarios/S0/load", headers=headers(), json={"request_id": "r1", field: value}), 422, "VALIDATION_ERROR")


def test_source_changed_after_startup_blocks_catalog_load_and_reads(client, setup):
    session = load(client).json()["data"]["session"]["session_id"]
    path = setup[0].project_root / "scenarios/fixtures/thu-duc-binh-thanh-v1/S0.json"
    path.write_bytes(path.read_bytes() + b" ")
    code(client.get("/api/scenarios", headers=headers()), 503, "SOURCE_CHANGED")
    code(load(client, request_id="another"), 503, "SOURCE_CHANGED")
    code(client.get(f"/api/sessions/{session}/state", headers=headers()), 503, "SOURCE_CHANGED")
    assert client.get("/ready").json()["data"]["checks"]["catalog"] == "CATALOG_NOT_VERIFIED"


def test_unpinned_receipt_rejected_at_startup(setup):
    settings, gateway = setup
    runtime = Path(settings.installation()["runtime_root"])
    path = runtime / "optimization/integration/member1_trusted_receipt.json"
    path.write_bytes(path.read_bytes() + b" ")
    with TestClient(create_app(settings, gateway=gateway)) as client:
        assert client.get("/health").status_code == 200
        code(client.get("/api/scenarios", headers=headers()), 503, "CATALOG_NOT_VERIFIED")


def test_installation_change_cannot_rebind_existing_session(client, setup):
    session = load(client).json()["data"]["session"]["session_id"]
    path = setup[0].installation_path
    value = json.loads(path.read_text())
    value["authority_store_parent"] += "-another"
    write(path, value)
    code(load(client), 409, "INSTALLATION_BINDING_CHANGED")
    code(client.get(f"/api/sessions/{session}/state", headers=headers()), 409, "INSTALLATION_BINDING_CHANGED")


def test_load_claim_fencing_and_expired_claim_recovery(setup):
    settings, _ = setup
    repo = SessionRepository(settings.metadata_path)
    repo.initialize()
    args = ("alice", "retry", "digest", "S0", settings.installation_identity(), "fixture", "catalog")
    first = repo.reserve_load(*args)
    with pytest.raises(ApiError) as caught:
        repo.reserve_load(*args)
    assert caught.value.code == "REQUEST_IN_PROGRESS"
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("UPDATE load_requests SET claim_expires=0")
    second = repo.reserve_load(*args)
    assert first["session_id"] == second["session_id"] and first["command_id"] == second["command_id"]
    with pytest.raises(ApiError) as caught:
        repo.complete_load(first, {"old": True})
    assert caught.value.code == "BOOTSTRAP_LEASE_LOST"
    repo.complete_load(second, {"success": True})
    assert repo.reserve_load(*args)["cached"] == {"success": True}


def test_old_metadata_schema_migrates_without_losing_owner(setup):
    settings, _ = setup
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("CREATE TABLE sessions(session_id TEXT PRIMARY KEY, owner_actor_id TEXT NOT NULL)")
        db.execute("INSERT INTO sessions VALUES('previous', 'alice')")
    repo = SessionRepository(settings.metadata_path)
    repo.initialize()
    assert repo.owner("previous") == "alice" and repo.record("previous")["status"] is None


def test_display_projection_returns_contacts_without_changing_execution(client, setup, monkeypatch):
    session = load(client).json()['data']['session']['session_id']
    service = client.app.state.session_service
    fixture = deepcopy(service.fixture(service.session(session)))
    fixture['initialState']['orders'][0].update(customerName='Khách thử nghiệm',customerPhone='0900000001',address='Bình Thạnh',notes='Gọi trước')
    fixture['initialState']['vehicles'][0].update(driverName='Tài xế thử nghiệm',driverPhone='0900000002',licensePlate='TEST-001',vehicleModel='Xe máy')
    monkeypatch.setattr(service,'fixture',lambda row:fixture)
    before = deepcopy(setup[1].views[session]) if hasattr(setup[1], 'views') else None
    orders = client.get(f'/api/sessions/{session}/orders',headers=headers()).json()['data']['orders']
    vehicles = client.get(f'/api/sessions/{session}/vehicles',headers=headers()).json()['data']
    assert orders[0]['display']['customerName'] == 'Khách thử nghiệm'
    assert orders[0]['coordinates'] == [106.7,10.8]
    assert vehicles['vehicle_metadata'][0]['display']['licensePlate'] == 'TEST-001'
    assert 'display' not in vehicles['vehicles'][0]
    if before is not None: assert setup[1].views[session] == before
