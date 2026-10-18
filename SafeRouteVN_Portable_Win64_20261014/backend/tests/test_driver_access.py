import hashlib
import json
import time
import sqlite3
import pytest

from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.tests.test_sessions import setup, headers
from backend.tests.test_replay import replay_setup
from backend.tests.test_replay import replay_client, active_session, control
from backend.tests.test_playback import playback_client, activated
from backend.tests.test_plans import revision
from backend.tests.test_demo_rain import demo

TOKEN = 'driver-test-' + 'z' * 36


def test_driver_follows_builtin_s4_native_variant_and_back_to_s1(demo, monkeypatch):
    settings,gateway,path,fixture,_ = demo
    path.write_text(json.dumps(fixture),encoding='utf-8')
    with TestClient(create_app(settings,gateway=gateway)) as client:
        monkeypatch.setattr(client.app.state.session_service.runs,'fixture',lambda *args:fixture)
        old=client.post('/api/scenarios/S1/load',json={'request_id':'s1'},headers=headers()).json()['data']['session']['session_id']
        driver=grant(settings,old)
        body={'request_id':'rain','replace_session_id':old}
        loaded=client.post('/api/scenarios/S4/load',json=body,headers=headers())
        assert loaded.status_code==201,loaded.text
        rain=loaded.json()['data']['session']['session_id']
        assert client.post('/api/scenarios/S4/load',json=body,headers=headers()).json()['data']['session']['session_id']==rain
        assert client.get('/api/auth/me',headers=driver).json()['data']['session']['session_id']==rain
        back=client.post('/api/scenarios/S1/load',json={'request_id':'back','replace_session_id':rain},headers=headers())
        assert back.status_code==201,back.text
        assert client.get('/api/auth/me',headers=driver).json()['data']['session']['session_id']==back.json()['data']['session']['session_id']


def test_driver_keeps_following_after_130_scenario_switches(setup):
    settings,gateway=setup
    with TestClient(create_app(settings,gateway=gateway)) as client:
        current=client.post('/api/scenarios/S0/load',json={'request_id':'initial'},headers=headers()).json()['data']['session']['session_id']
        driver=grant(settings,current)
        for n in range(130):
            response=client.post(f'/api/scenarios/S{n%2}/load',json={'request_id':f'change-{n}','replace_session_id':current},headers=headers())
            assert response.status_code==201,response.text
            current=response.json()['data']['session']['session_id']
        response=client.get('/api/auth/me',headers=driver)
        assert response.status_code==200,response.text
        assert response.json()['data']['session']['session_id']==current


def test_driver_follows_import_builtin_import_builtin_chain(setup, monkeypatch):
    settings, gateway = setup
    with TestClient(create_app(settings, gateway=gateway)) as client:
        endpoint = imported_run(client, gateway, monkeypatch)
        original = client.post(endpoint, json={'request_id':'first-import'}, headers=headers()).json()['data']['session']['session_id']
        driver = grant(settings, original)
        current = original
        for index, url in enumerate(['/api/scenarios/S1/load', endpoint, '/api/scenarios/S0/load', '/api/scenarios/S1/load', endpoint]):
            body = {'request_id':f'switch-{index}', 'replace_session_id':current}
            loaded = client.post(url, json=body, headers=headers())
            assert loaded.status_code == 201, loaded.text
            new = loaded.json()['data']['session']['session_id']
            assert client.get('/api/auth/me', headers=driver).json()['data']['session']['session_id'] == new
            assert client.get(f'/api/sessions/{current}', headers=driver).status_code == 403
            assert gateway.views[current]['delivered_prefix'] == []
            assert client.post(url, json=body, headers=headers()).json()['data']['session']['session_id'] == new
            current = new
        # Repeating an old receipt cannot rewind the driver assignment.
        assert client.post('/api/scenarios/S1/load', json={'request_id':'switch-0','replace_session_id':original}, headers=headers()).status_code == 201
        assert client.get('/api/auth/me', headers=driver).json()['data']['session']['session_id'] == current


def test_builtin_replacement_fails_closed_and_retries_same_intent(setup):
    settings, gateway = setup
    with TestClient(create_app(settings, gateway=gateway)) as client:
        old = client.post('/api/scenarios/S0/load', json={'request_id':'old'}, headers=headers()).json()['data']['session']['session_id']
        driver = grant(settings, old)
        body = {'request_id':'switch','replace_session_id':old}
        url = '/api/scenarios/S1/load'
        assert client.post(url, json=body, headers=headers('bob')).status_code == 403
        assert client.post(url, json=body, headers=driver).status_code == 403
        gateway.fail_after_commit = True
        assert client.post(url, json=body, headers=headers()).status_code == 503
        assert client.get('/api/auth/me', headers=driver).json()['data']['session']['session_id'] == old
        loaded = client.post(url, json=body, headers=headers())
        assert loaded.status_code == 201, loaded.text
        new = loaded.json()['data']['session']['session_id']
        assert client.get('/api/auth/me', headers=driver).json()['data']['session']['session_id'] == new
        assert client.post(url, json={**body,'replace_session_id':new}, headers=headers()).status_code == 409


def imported_run(client, gateway, monkeypatch):
    """Stub only published fixture/runtime I/O; exercise real load and assignment metadata."""
    service = client.app.state.session_service
    fixture = service.catalog.fixture('S0')
    monkeypatch.setattr(service.runs, 'manifest', lambda *args: {
        'source_kind': 'IMPORTED_SCENARIO', 'scenario_id': 'S0',
        'artifacts': {'fixture': {'sha256': service.catalog.fixture_sha256['S0']}}})
    monkeypatch.setattr(service.runs, 'row', lambda *args: {'manifest_sha256': 'f' * 64})
    monkeypatch.setattr(service.runs, 'fixture', lambda *args: fixture)
    async def bootstrap(sid, command, *args):
        return await gateway.bootstrap(sid, 'S0', command)
    monkeypatch.setattr(gateway, 'bootstrap_run', bootstrap, raising=False)
    return '/api/runs/run-' + 'a' * 32 + '/load'


def test_import_replaces_unfinished_driver_session_and_survives_retry_restart(setup, monkeypatch):
    settings, gateway = setup
    with TestClient(create_app(settings, gateway=gateway)) as client:
        old = client.post('/api/scenarios/S0/load', json={'request_id': 'old'}, headers=headers()).json()['data']['session']['session_id']
        driver = grant(settings, old)
        endpoint = imported_run(client, gateway, monkeypatch)
        body = {'request_id': 'import-load', 'replace_session_id': old}
        result = client.post(endpoint, json=body, headers=headers())
        assert result.status_code == 201, result.text
        new = result.json()['data']['session']['session_id']
        assert gateway.views[old]['delivered_prefix'] == []  # Old work is unfinished.
        assert client.get('/api/auth/me', headers=driver).json()['data']['session']['session_id'] == new
        assert client.get(f'/api/sessions/{old}', headers=driver).status_code == 403
        assert client.post(endpoint, json=body, headers=headers()).json()['data']['session']['session_id'] == new
        # A second import is followed, and a delayed retry cannot roll it back.
        second = client.post(endpoint, json={'request_id': 'import-two', 'replace_session_id': new}, headers=headers())
        assert second.status_code == 201, second.text
        final = second.json()['data']['session']['session_id']
        assert client.post(endpoint, json=body, headers=headers()).status_code == 201
        assert client.get('/api/auth/me', headers=driver).json()['data']['session']['session_id'] == final
        assert client.post(endpoint, json={**body, 'replace_session_id': new}, headers=headers()).status_code == 409
    with TestClient(create_app(settings, gateway=gateway)) as restarted:
        imported_run(restarted, gateway, monkeypatch)
        assert restarted.get('/api/auth/me', headers=driver).json()['data']['session']['session_id'] == final


def test_failed_import_load_keeps_driver_and_cannot_replace_another_owner(setup, monkeypatch):
    settings, gateway = setup
    with TestClient(create_app(settings, gateway=gateway)) as client:
        old = client.post('/api/scenarios/S0/load', json={'request_id': 'old'}, headers=headers()).json()['data']['session']['session_id']
        driver = grant(settings, old)
        endpoint = imported_run(client, gateway, monkeypatch)
        body = {'request_id': 'import-load', 'replace_session_id': old}
        assert client.post(endpoint, json=body, headers=headers('bob')).status_code == 403
        assert client.post(endpoint, json=body, headers=driver).status_code == 403
        gateway.fail_after_commit = True
        assert client.post(endpoint, json=body, headers=headers()).status_code == 503
        assert client.get('/api/auth/me', headers=driver).json()['data']['session']['session_id'] == old
        assert client.post(endpoint, json=body, headers=headers()).status_code == 201
        assert client.get('/api/auth/me', headers=driver).json()['data']['session']['session_id'] != old


def grant(settings, sid):
    auth = json.loads(settings.auth_path.read_bytes())
    auth['tokens'].append({'actor_id': 'driver-one', 'role': 'driver', 'session_id': sid,
                           'expires_at': int(time.time()) + 600,
                           'token_sha256': hashlib.sha256(TOKEN.encode()).hexdigest()})
    settings.auth_path.write_text(json.dumps(auth), encoding='utf-8')
    return {'Authorization': 'Bearer ' + TOKEN}


def test_reset_cannot_commit_after_import_won_the_same_source(replay_client, replay_setup, monkeypatch):
    settings, gateway = replay_setup
    old, _ = active_session(replay_client, gateway)
    driver = grant(settings, old)
    endpoint = imported_run(replay_client, gateway, monkeypatch)
    loaded = replay_client.post(endpoint, json={'request_id': 'concurrent-import'}, headers=headers())
    assert loaded.status_code == 201, loaded.text
    destination = loaded.json()['data']['session']['session_id']
    repository = replay_client.app.state.replay_service.repository
    original_complete = repository.complete_replay
    def concurrent_import(row, receipt):
        # Deterministic commit barrier: another transaction wins immediately
        # before this reset commits. Both destinations are real READY sessions.
        with sqlite3.connect(settings.metadata_path) as db:
            db.execute('INSERT INTO driver_session_successors SELECT session_id,?,owner_actor_id,installation_sha256 FROM sessions WHERE session_id=?', (destination, old))
        original_complete(row, receipt)
    monkeypatch.setattr(repository, 'complete_replay', concurrent_import)
    result = control(replay_client, old, 'reset', request_id='concurrent-reset')
    assert result.status_code == 409, result.text
    assert replay_client.get('/api/auth/me', headers=driver).json()['data']['session']['session_id'] == destination


def test_driver_identity_session_scope_dispatcher_denial_and_revocation(setup):
    settings, gateway = setup
    with TestClient(create_app(settings, gateway=gateway)) as client:
        first = client.post('/api/scenarios/S0/load', json={'request_id': 'driver-session'}, headers=headers()).json()['data']
        second = client.post('/api/scenarios/S0/load', json={'request_id': 'other-session'}, headers=headers()).json()['data']
        sid = first['session']['session_id']
        driver = grant(settings, sid)
        me = client.get('/api/auth/me', headers=driver)
        assert me.status_code == 200 and me.json()['data']['session']['session_id'] == sid
        assert TOKEN not in me.text
        assert client.get(f'/api/sessions/{sid}', headers=driver).status_code == 200
        assert client.get(f"/api/sessions/{second['session']['session_id']}", headers=driver).status_code == 403
        for endpoint in ('/api/scenarios/S0/load', f'/api/sessions/{sid}/optimize', f'/api/sessions/{sid}/replay/reset'):
            assert client.post(endpoint, json={'request_id': 'denied'}, headers=driver).status_code == 403
        auth = json.loads(settings.auth_path.read_bytes())
        auth['tokens'][-1]['revoked'] = True
        settings.auth_path.write_text(json.dumps(auth))
        assert client.get('/api/auth/me', headers=driver).status_code == 401
        auth['tokens'][-1]['revoked'] = False
        auth['tokens'][-1]['expires_at'] = int(time.time()) - 1
        settings.auth_path.write_text(json.dumps(auth))
        assert client.get(f'/api/sessions/{sid}', headers=driver).status_code == 401


def test_driver_delivery_has_action_boundary(setup):
    settings, gateway = setup
    calls = []
    gateway._call = lambda operation, fields: (calls.append(fields) or {'status': 'DISPATCHED', 'basis': fields['basis']})
    with TestClient(create_app(settings, gateway=gateway)) as client:
        loaded = client.post('/api/scenarios/S0/load', json={'request_id': 'delivery'}, headers=headers()).json()['data']
        sid, basis = loaded['session']['session_id'], loaded['execution_view']['basis']
        driver = grant(settings, sid)
        body = {'request_id': 'driver-deliver', 'expected_revision': {k: basis[k] for k in ('head_version', 'generation')},
                'basis': basis, 'action': 'deliver', 'data': {'vehicle_id': 'V1', 'order_id': 'O1'}}
        response = client.post(f'/api/sessions/{sid}/delivery-day', json=body, headers=driver)
        assert response.status_code == 200, response.text
        assert len(calls) == 1
        body.update(action='add_order', request_id='driver-denied')
        assert client.post(f'/api/sessions/{sid}/delivery-day', json=body, headers=driver).status_code == 403
        assert len(calls) == 1


def test_driver_replay_records_real_actor(playback_client, replay_setup):
    settings, gateway = replay_setup
    sid, _ = activated(playback_client, gateway)
    driver = grant(settings, sid)
    response = playback_client.post(f'/api/sessions/{sid}/replay/start',
        json={'request_id': 'driver-start', 'speed': 1, 'expected_revision': revision(gateway, sid)}, headers=driver)
    assert response.status_code == 200, response.text
    assert response.json()['data']['receipt']['actor_id'] == 'driver-one'
    assert playback_client.post(f'/api/sessions/{sid}/replay/playback/pause',
        json={'request_id': 'driver-pause'}, headers=driver).status_code == 200


def test_driver_follows_committed_reset_chain_without_access_to_unrelated_sessions(replay_client, replay_setup):
    settings, gateway = replay_setup
    original, _ = active_session(replay_client, gateway)
    driver = grant(settings, original)
    assigned = original
    for index in range(2):
        reset = control(replay_client, assigned, 'reset', request_id=f'driver-reset-{index}')
        assert reset.status_code == 200, reset.text
        assigned = reset.json()['data']['session']['session_id']
    # A newer unrelated session must never hijack this account.
    unrelated = replay_client.post('/api/scenarios/S0/load', json={'request_id': 'unrelated-new'}, headers=headers()).json()['data']['session']['session_id']
    me = replay_client.get('/api/auth/me', headers=driver)
    assert me.status_code == 200, me.text
    assert me.json()['data']['session']['session_id'] == assigned
    assert replay_client.get(f'/api/sessions/{assigned}', headers=driver).status_code == 200
    assert replay_client.get(f'/api/sessions/{original}', headers=driver).status_code == 403
    assert replay_client.get(f'/api/sessions/{unrelated}', headers=driver).status_code == 403
    # Durable mapping survives app/server recreation with the same metadata.
    with TestClient(create_app(settings, gateway=gateway)) as restarted:
        assert restarted.get('/api/auth/me', headers=driver).json()['data']['session']['session_id'] == assigned
    auth = json.loads(settings.auth_path.read_bytes())
    auth['tokens'][-1]['revoked'] = True
    settings.auth_path.write_text(json.dumps(auth))
    assert replay_client.get('/api/auth/me', headers=driver).status_code == 401


def test_driver_does_not_follow_uncommitted_reset(replay_client, replay_setup):
    settings, gateway = replay_setup
    original, _ = active_session(replay_client, gateway)
    driver = grant(settings, original)
    gateway.fail_after_commit = True
    assert control(replay_client, original, 'reset').status_code == 503
    assert replay_client.get('/api/auth/me', headers=driver).json()['data']['session']['session_id'] == original
    reset = control(replay_client, original, 'reset')
    assert reset.status_code == 200, reset.text
    assert replay_client.get('/api/auth/me', headers=driver).json()['data']['session']['session_id'] == reset.json()['data']['session']['session_id']


@pytest.mark.parametrize('field,value', [('owner_actor_id', 'bob'), ('installation_sha256', '0' * 64), ('status', 'PENDING')])
def test_driver_reset_assignment_fails_closed_for_invalid_destination(replay_client, replay_setup, field, value):
    settings, gateway = replay_setup
    original, _ = active_session(replay_client, gateway)
    driver = grant(settings, original)
    reset = control(replay_client, original, 'reset')
    assert reset.status_code == 200
    destination = reset.json()['data']['session']['session_id']
    with sqlite3.connect(settings.metadata_path) as db:
        db.execute(f'UPDATE sessions SET {field}=? WHERE session_id=?', (value, destination))
    response = replay_client.get('/api/auth/me', headers=driver)
    assert response.status_code == 503, response.text
    assert response.json()['diagnostics'][0]['code'] == 'ASSIGNMENT_UNAVAILABLE'
    assert replay_client.get(f'/api/sessions/{destination}', headers=driver).status_code == 503
