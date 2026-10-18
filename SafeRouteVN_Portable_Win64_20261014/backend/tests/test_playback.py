"""Playback durability/race tests against the SDK command receipt model.

Fixtures own temporary data and metadata. No frozen source is modified and no
physical state is calculated here; the fake gateway models the SDK CAS.
"""
import asyncio
from copy import deepcopy
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from backend.api.errors import ApiError
from backend.api.main import create_app
from backend.api.routers import playback as playback_routes
from backend.models.http import ReplayControlRequest
from backend.models.playback import PlaybackSpeedRequest, StartPlaybackRequest
from backend.services.playback_repository import PlaybackRepository
from backend.services.playback_service import PlaybackService
from backend.tests.test_replay import BARRIER, FIRST_STEP, active_session, replay_setup
from backend.tests.test_plans import revision
from backend.tests.test_sessions import EPOCH, code, headers, load, setup


@pytest.fixture
def playback_client(replay_setup):
    settings, gateway = replay_setup
    app = create_app(settings, gateway=gateway)
    if not hasattr(app.state, "playback_service"):
        repository = PlaybackRepository(settings.metadata_path)
        app.state.playback_repository = repository
        app.state.playback_service = PlaybackService(settings, repository, app.state.session_service, app.state.replay_service, gateway)
        app.include_router(playback_routes.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.playback_repository.initialize()
        yield client


def activated(client, gateway, scenario="S0", end_us=360_000_000):
    sid, job = active_session(client, gateway, scenario)
    gateway.views[sid]["accepted_trajectory"] = {"job_id": job, "profile": "BALANCED", "forecast": True,
        "domain_sha256": "a" * 64, "vehicle_routes": [{"return_us": str(end_us)}]}
    return sid, job


def start(client, gateway, sid, request_id="play-1", actor="alice", **extra):
    return client.post(f"/api/sessions/{sid}/replay/start", headers=headers(actor),
        json={"request_id": request_id, "expected_revision": revision(gateway, sid), **extra})


def control(client, sid, operation, request_id="control-1", actor="alice", **extra):
    path = "playback/pause" if operation == "pause" else operation
    return client.post(f"/api/sessions/{sid}/replay/{path}", headers=headers(actor), json={"request_id": request_id, **extra})


def description(client, sid, actor="alice"):
    return client.get(f"/api/sessions/{sid}/replay/playback", headers=headers(actor))


def due(settings, sid):
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("UPDATE playback_controllers SET next_due=0 WHERE session_id=?", (sid,))


def test_start_is_durable_idempotent_and_never_advances_or_computes(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    before = deepcopy(gateway.views[sid])
    first = start(playback_client, gateway, sid, speed=4)
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["controller"]["mode"] == "AUTOMATIC" and data["controller"]["speed"] == 4
    assert data["controller"]["paused"] is False and data["controller"]["catch_up"] is False
    assert start(playback_client, gateway, sid, speed=4).json()["data"]["receipt"] == data["receipt"]
    code(start(playback_client, gateway, sid, speed=8), 409, "IDEMPOTENCY_CONFLICT")
    assert gateway.views[sid] == before and gateway.advances == [] and gateway.computes == []
    assert playback_client.app.state.playback_repository.healthy()


@pytest.mark.parametrize('in_flight',[False,True])
def test_weather_profile_comparison_pauses_and_waits_for_reserved_tick(playback_client,replay_setup,monkeypatch,in_flight):
    settings,gateway=replay_setup
    sid,_=activated(playback_client,gateway)
    assert start(playback_client,gateway,sid).status_code==200
    sessions=playback_client.app.state.session_service
    original=sessions.session
    monkeypatch.setattr(sessions,'session',lambda selected:{**original(selected),'run_id':'run-'+'a'*32})
    if in_flight:
        with sqlite3.connect(settings.metadata_path) as db:
            db.execute('UPDATE playback_controllers SET pending_request=? WHERE session_id=?',('reserved-tick',sid))
    response=playback_client.post(f'/api/sessions/{sid}/profiles/compare',headers=headers(),json={'request_id':'weather-compare'})
    controller=playback_client.app.state.playback_repository.get(sid)
    assert controller['status']=='PAUSED' and controller['reason']=='PROFILE_COMPARISON'
    if in_flight:code(response,409,'PLAYBACK_TICK_IN_PROGRESS')
    else:
        assert response.status_code==202,response.text
        receipt=response.json()['data']
        assert receipt['input_basis']==gateway.views[sid]['basis']
    assert gateway.advances==[] and gateway.computes==[]


@pytest.mark.parametrize("speed", [0, 3, 256, -1, 1.0, "2", True, None])
def test_speed_has_strict_bounded_wire_values(playback_client, replay_setup, speed):
    sid, _ = activated(playback_client, replay_setup[1])
    assert control(playback_client, sid, "speed", speed=speed).status_code == 422
    assert start(playback_client, replay_setup[1], sid, speed=speed).status_code == 422


def test_owner_and_dispatcher_gate_all_playback_mutations_and_reads(playback_client, replay_setup):
    sid, _ = activated(playback_client, replay_setup[1])
    for method, suffix, body in (("get", "playback", None), ("get", "playback/history", None),
        ("post", "speed", {"request_id": "speed-1", "speed": 2}),
        ("post", "start", {"request_id": "play-1", "expected_revision": revision(replay_setup[1], sid)}),
        ("post", "playback/pause", {"request_id": "pause-1"})):
        path = f"/api/sessions/{sid}/replay/{suffix}"
        kwargs = {"json": body} if body is not None else {}
        code(getattr(playback_client, method)(path, headers=headers("bob"), **kwargs), 403, "FORBIDDEN")
        code(getattr(playback_client, method)(path, **kwargs), 401, "UNAUTHORIZED")


def test_start_requires_current_certified_active_trajectory(playback_client, replay_setup):
    gateway = replay_setup[1]
    sid = load(playback_client).json()["data"]["session"]["session_id"]
    code(start(playback_client, gateway, sid), 409, "ACCEPTED_PLAN_REQUIRED")
    sid, _ = activated(playback_client, gateway)
    gateway.views[sid]["accepted_trajectory"] = None
    code(start(playback_client, gateway, sid), 409, "ACCEPTED_TRAJECTORY_REQUIRED")
    gateway.views[sid]["accepted_trajectory"] = {"job_id": gateway.views[sid]["active_job_id"], "vehicle_routes": [{"return_us": 0}]}
    code(start(playback_client, gateway, sid), 409, "PLAN_COMPLETE")


def test_one_tick_uses_exact_public_basis_and_normal_sdk_receipt(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    before = deepcopy(gateway.views[sid]["basis"])
    assert start(playback_client, gateway, sid).status_code == 200
    service = playback_client.app.state.playback_service
    due(settings, sid)
    assert asyncio.run(service.run_once()) is True
    assert gateway.advances[0][2] == before and gateway.advances[0][3] == FIRST_STEP
    assert len(gateway.replay_receipts) == 1
    state = description(playback_client, sid).json()["data"]
    assert state["paused"] is False and state["in_flight"] is False
    assert state["execution_view"]["basis"]["head_version"] == "2"
    assert asyncio.run(service.run_once()) is False  # New cadence scheduled from completion, no catch-up.
    assert len(gateway.advances) == 1


def test_event_barrier_exact_fractional_time_pauses_without_applying(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway, "S2")
    assert start(playback_client, gateway, sid, speed=8).status_code == 200
    for _ in range(2):
        due(settings, sid)
        assert asyncio.run(playback_client.app.state.playback_service.run_once()) is True
    current = description(playback_client, sid).json()["data"]
    assert current["paused"] is True and current["fully_paused"] is True
    assert current["reason"] == "EVENT_BARRIER" and current["execution_view"]["current_time"] == BARRIER
    assert current["execution_view"]["pending_event_ids"] == ["urgent-1"] and gateway.applies == []
    code(start(playback_client, gateway, sid, request_id="resume-due"), 409, "EVENT_TRANSITION_REQUIRED")
    due(settings, sid)
    assert asyncio.run(playback_client.app.state.playback_service.run_once()) is False


def test_final_public_return_pauses_at_exact_time_and_does_not_fabricate_finish(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway, end_us=30_000_001)
    assert start(playback_client, gateway, sid).status_code == 200
    due(settings, sid)
    assert asyncio.run(playback_client.app.state.playback_service.run_once()) is True
    data = description(playback_client, sid).json()["data"]
    assert data["reason"] == "PLAN_COMPLETE" and data["fully_paused"] is True
    assert data["execution_view"]["current_time"] == "2026-09-27T21:00:30.000001+07:00"
    assert data["execution_view"]["delivered_prefix"] == []  # SDK fake, no M3 completion synthesis.


@pytest.mark.parametrize("speed", [8, 16, 32, 64, 128])
def test_speed_update_on_paused_controller_does_not_resume_or_change_head(playback_client, replay_setup, speed):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    before = deepcopy(gateway.views[sid])
    response = control(playback_client, sid, "speed", speed=speed)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["controller"]["speed"] == speed
    assert response.json()["data"]["controller"]["fully_paused"] is True
    due(settings, sid)
    assert asyncio.run(playback_client.app.state.playback_service.run_once()) is False
    assert gateway.views[sid] == before and gateway.advances == []


@pytest.mark.parametrize('speed', [16, 32, 64, 128])
@pytest.mark.parametrize('scenario', ['S0', 'S2'])
def test_fast_replay_advances_larger_intervals_without_crossing_events(playback_client, replay_setup, speed, scenario):
    from datetime import datetime, timedelta
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway, scenario, end_us=3_600_000_000)
    assert start(playback_client, gateway, sid, speed=speed).status_code == 200
    due(settings, sid)
    assert asyncio.run(playback_client.app.state.playback_service.run_once()) is True
    current = description(playback_client, sid).json()['data']
    expected = BARRIER if scenario == 'S2' else (datetime.fromisoformat(EPOCH) + timedelta(seconds=60*(speed//8))).isoformat()
    assert gateway.advances[0][3] == expected
    assert current['speed'] == speed
    if scenario == 'S2':
        assert current['fully_paused'] and current['reason'] == 'EVENT_BARRIER'
        assert gateway.applies == []


def test_pause_or_speed_wins_before_reservation_without_any_sdk_tick(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    assert start(playback_client, gateway, sid).status_code == 200
    repository = playback_client.app.state.playback_repository
    due(settings, sid)
    old = repository.get(sid)
    assert control(playback_client, sid, "speed", speed=2).status_code == 200
    assert repository.reserve_tick(old, old["basis"], FIRST_STEP, EPOCH, now=10**12) is None
    due(settings, sid)
    old = repository.get(sid)
    assert control(playback_client, sid, "pause").status_code == 200
    assert repository.reserve_tick(old, old["basis"], FIRST_STEP, EPOCH, now=10**12) is None
    assert gateway.advances == []


def test_pause_during_reserved_tick_discloses_inflight_then_settles_once(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    assert start(playback_client, gateway, sid).status_code == 200
    service = playback_client.app.state.playback_service
    original = gateway.advance

    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            entered.set()
            await finish.wait()
            return await original(*args)
        gateway.advance = delayed
        due(settings, sid)
        task = asyncio.create_task(service.run_once())
        await asyncio.wait_for(entered.wait(), 3)
        response = await service.control(sid, "pause", ReplayControlRequest(request_id="pause-mid-tick"), SimpleNamespace(actor_id="alice", role="dispatcher"))
        paused = response["controller"]
        assert paused["paused"] is True and paused["in_flight"] is True and paused["fully_paused"] is False
        with pytest.raises(ApiError) as error:
            await service.control(sid, "start", StartPlaybackRequest(request_id="resume-mid-tick", expected_revision=revision(gateway, sid)), SimpleNamespace(actor_id="alice", role="dispatcher"))
        assert error.value.code == "PLAYBACK_TICK_IN_PROGRESS"
        finish.set()
        assert await task is True
        assert service.repository.public(service.repository.get(sid))["fully_paused"] is True
        due(settings, sid)
        assert await service.run_once() is False
    asyncio.run(scenario())
    assert len(gateway.advances) == 1 and gateway.views[sid]["basis"]["head_version"] == "2"


def test_uncertain_sdk_commit_keeps_exact_payload_and_retry_cannot_double_advance(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    assert start(playback_client, gateway, sid).status_code == 200
    service = playback_client.app.state.playback_service
    due(settings, sid)
    gateway.fail_mutation_after_commit = True
    with pytest.raises(ApiError) as error:
        asyncio.run(service.run_once())
    assert error.value.code == "RUNTIME_TIMEOUT"
    first = deepcopy(gateway.advances[0])
    state = service.repository.get(sid)
    assert service.repository.public(state)["in_flight"] is True and state["status"] == "PAUSED"
    assert asyncio.run(service.run_once()) is True
    assert gateway.advances == [first, first] and len(gateway.replay_receipts) == 1
    assert gateway.views[sid]["basis"]["head_version"] == "2"
    assert service.repository.public(service.repository.get(sid))["fully_paused"] is True


def test_worker_restart_never_resumes_and_settles_existing_replay_command(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    assert start(playback_client, gateway, sid).status_code == 200
    service = playback_client.app.state.playback_service
    due(settings, sid)
    state = service.repository.get(sid)
    row = service.repository.reserve_tick(state, state["basis"], FIRST_STEP, EPOCH)
    service.fence_restart()
    assert service.repository.get(sid)["reason"] == "PAUSED_WORKER_RESTART"
    asyncio.run(service.replay.reconcile(row))
    assert asyncio.run(service.run_once()) is True
    data = description(playback_client, sid).json()["data"]
    assert data["fully_paused"] is True and data["reason"] == "PAUSED_WORKER_RESTART"
    assert len(gateway.advances) == 1
    due(settings, sid)
    assert asyncio.run(service.run_once()) is False
    # Retrying the historical start receipt cannot resume the controller.
    assert start(playback_client, gateway, sid).status_code == 409  # Caller supplied changed expected revision.
    original = {"request_id": "play-1", "expected_revision": {"head_version": "1", "generation": "1"}}
    response = playback_client.post(f"/api/sessions/{sid}/replay/start", headers=headers(), json=original)
    assert response.status_code == 200 and response.json()["data"]["controller"]["fully_paused"] is True


def test_basis_change_pauses_without_refreshing_tick_to_new_generation(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    assert start(playback_client, gateway, sid).status_code == 200
    gateway.views[sid]["basis"]["generation"] = "2"
    due(settings, sid)
    assert asyncio.run(playback_client.app.state.playback_service.run_once()) is True
    assert description(playback_client, sid).json()["data"]["reason"] == "STALE_HEAD"
    assert gateway.advances == []


def test_control_audit_transaction_rollback_allows_exact_retry(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("""CREATE TRIGGER abort_control_audit BEFORE INSERT ON playback_audit
            BEGIN SELECT RAISE(ABORT,'Injected playback audit failure'); END""")
    code(start(playback_client, gateway, sid), 503, "METADATA_UNAVAILABLE")
    assert playback_client.app.state.playback_repository.get(sid) is None
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("DROP TRIGGER abort_control_audit")
    assert start(playback_client, gateway, sid).status_code == 200
    assert playback_client.app.state.playback_repository.get(sid)["epoch"] == 1


def test_paused_control_history_has_no_credentials_and_documents_routes(playback_client, replay_setup):
    sid, _ = activated(playback_client, replay_setup[1])
    assert control(playback_client, sid, "pause").status_code == 200
    data = playback_client.get(f"/api/sessions/{sid}/replay/playback/history", headers=headers()).json()["data"]
    assert data["history"][0]["operation"] == "pause" and data["history"][0]["controller"]["fully_paused"] is True
    assert not {"token", "auth", "claim_token", "installation_path"}.intersection(data["history"][0])
    schema = playback_client.get("/openapi.json").json()
    for suffix, method in (("start", "post"), ("speed", "post"), ("playback/pause", "post"), ("playback", "get"), ("playback/history", "get")):
        assert schema["paths"][f"/api/sessions/{{session_id}}/replay/{suffix}"][method]["security"]


def test_runtime_busy_keeps_same_pending_command_and_backs_off(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    assert start(playback_client, gateway, sid).status_code == 200
    service = playback_client.app.state.playback_service
    due(settings, sid)
    original = gateway.advance
    attempts = []
    async def busy_once(*args):
        attempts.append(deepcopy(args))
        if len(attempts) == 1:
            raise ApiError(503, "RUNTIME_BUSY", "runtime", "Injected SDK coordination contention")
        return await original(*args)
    gateway.advance = busy_once
    assert asyncio.run(service.run_once()) is False
    assert service.repository.get(sid)["status"] == "RUNNING"
    assert service.repository.public(service.repository.get(sid))["in_flight"] is True
    assert asyncio.run(service.run_once()) is False and len(attempts) == 1
    due(settings, sid)
    assert asyncio.run(service.run_once()) is True and attempts[0] == attempts[1]
    assert len(gateway.replay_receipts) == 1


def test_failed_replay_audit_after_sdk_commit_fences_restart_and_retries_once(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    assert start(playback_client, gateway, sid).status_code == 200
    service = playback_client.app.state.playback_service
    due(settings, sid)
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute("""CREATE TRIGGER abort_tick_audit BEFORE INSERT ON replay_audit
            BEGIN SELECT RAISE(ABORT,'Injected tick audit failure'); END""")
    try:
        with pytest.raises(ApiError) as error:
            asyncio.run(service.run_once())
        assert error.value.code == "METADATA_UNAVAILABLE"
        assert gateway.views[sid]["basis"]["head_version"] == "2"
        assert service.repository.public(service.repository.get(sid))["in_flight"] is True
    finally:
        with sqlite3.connect(settings.metadata_path) as db:
            db.execute("DROP TRIGGER abort_tick_audit")
    service.fence_restart()
    original = deepcopy(gateway.advances[0])
    assert asyncio.run(service.run_once()) is True
    assert gateway.advances == [original, original] and len(gateway.replay_receipts) == 1
    assert service.repository.public(service.repository.get(sid))["reason"] == "PAUSED_WORKER_RESTART"
    assert service.repository.public(service.repository.get(sid))["fully_paused"] is True


@pytest.mark.parametrize("invalid", [True, 0.5, "01", "+1", "-1", "9223372036854775808", None])
def test_invalid_public_trajectory_clock_fails_closed_before_controller_start(playback_client, replay_setup, invalid):
    gateway = replay_setup[1]
    sid, _ = activated(playback_client, gateway)
    gateway.views[sid]["accepted_trajectory"]["vehicle_routes"][0]["return_us"] = invalid
    code(start(playback_client, gateway, sid), 503, "PLAYBACK_TRAJECTORY_INVALID")
    assert playback_client.app.state.playback_repository.get(sid) is None
    assert gateway.advances == []


@pytest.mark.parametrize("actor,role", [("bob", "dispatcher"), ("alice", "viewer"), ("alice", None)])
def test_direct_service_controls_also_require_owner_dispatcher(playback_client, replay_setup, actor, role):
    sid, _ = activated(playback_client, replay_setup[1])
    service = playback_client.app.state.playback_service
    with pytest.raises(ApiError) as error:
        asyncio.run(service.control(sid, "pause", ReplayControlRequest(request_id="direct-owner-guard"), SimpleNamespace(actor_id=actor, role=role)))
    assert error.value.code == "FORBIDDEN" and service.repository.get(sid) is None


def day_waiting(gateway, sid):
    view = gateway.views[sid]
    view['delivery_day'] = {'date':'2026-09-27','pending_events':[]}
    view['delivery_barrier'] = view['current_time']
    view['vehicles'][0].update(awaiting_delivery='O001', availability='AVAILABLE')
    return view


def test_auto_deliver_setting_is_owner_only_persisted_and_does_not_start(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    code(control(playback_client,sid,'auto-deliver',enabled=True),409,'DELIVERY_DAY_REQUIRED')
    day_waiting(gateway,sid)
    code(control(playback_client,sid,'auto-deliver',actor='bob',enabled=True),403,'FORBIDDEN')
    response=control(playback_client,sid,'auto-deliver',enabled=True)
    assert response.status_code==200,response.text
    assert response.json()['data']['controller']['auto_deliver'] is True
    assert response.json()['data']['controller']['fully_paused'] is True
    fresh=PlaybackRepository(settings.metadata_path)
    fresh.initialize()
    assert fresh.get(sid)['auto_deliver']==1
    assert control(playback_client,sid,'auto-deliver',enabled=True).json()['data']==response.json()['data']
    code(control(playback_client,sid,'auto-deliver',enabled=False),409,'IDEMPOTENCY_CONFLICT')


@pytest.mark.parametrize("end_us",[360_000_000,0])
def test_auto_deliver_uses_durable_dispatch_and_continues_with_new_basis(playback_client,replay_setup,monkeypatch,end_us):
    settings,gateway=replay_setup
    sid,_=activated(playback_client,gateway,end_us=end_us)
    view=day_waiting(gateway,sid)
    calls=[]
    receipts={}
    def dispatch(operation,command):
        assert operation=='dispatch' and command['action']=='deliver'
        calls.append(deepcopy(command))
        key=command['command_id']
        if key not in receipts:
            assert command['basis']==view['basis']
            assert command['data']=={'vehicle_id':view['vehicles'][0]['vehicle_id'],'order_id':'O001'}
            view['basis']['head_version']=str(int(view['basis']['head_version'])+1)
            view['basis']['head_sha256']='e'*64
            view['vehicles'][0]['awaiting_delivery']=None
            view['delivery_barrier']=None
            receipts[key]={'status':'DISPATCHED','basis':deepcopy(view['basis'])}
        return gateway.output(receipts[key])
    monkeypatch.setattr(gateway,'_call',dispatch,raising=False)
    assert control(playback_client,sid,'auto-deliver',enabled=True).status_code==200
    assert start(playback_client,gateway,sid).status_code==200
    service=playback_client.app.state.playback_service
    due(settings,sid)
    gateway.fail_mutation_after_commit=True
    with pytest.raises(ApiError):asyncio.run(service.run_once())
    assert service.repository.public(service.repository.get(sid))['in_flight']
    assert asyncio.run(service.run_once()) is True
    assert calls[0]==calls[1] and len(receipts)==1
    assert service.repository.get(sid)['basis']==view['basis']
    assert service.replay.history(sid)['history'][0]['operation']=='deliver'
    if end_us==0:
        code(start(playback_client,gateway,sid,request_id='complete'),409,'PLAN_COMPLETE')
        return
    assert start(playback_client,gateway,sid,request_id='continue-after-deliver').status_code==200
    due(settings,sid)
    assert asyncio.run(service.run_once()) is True
    assert len(gateway.advances)==1


def test_disabling_auto_deliver_fences_unreserved_confirmation(playback_client,replay_setup):
    settings,gateway=replay_setup
    sid,_=activated(playback_client,gateway)
    view=day_waiting(gateway,sid)
    assert control(playback_client,sid,'auto-deliver',enabled=True).status_code==200
    assert start(playback_client,gateway,sid).status_code==200
    service=playback_client.app.state.playback_service
    due(settings,sid)
    old=service.repository.get(sid)
    assert control(playback_client,sid,'auto-deliver',request_id='off',enabled=False).status_code==200
    assert service.repository.reserve_tick(old,view['basis'],view['current_time'],view['current_time'],delivery={'vehicle_id':'V1','order_id':'O001'}) is None
    due(settings,sid)
    assert asyncio.run(service.run_once()) is True
    assert service.repository.get(sid)['reason']=='DELIVERY_CONFIRMATION_REQUIRED'
