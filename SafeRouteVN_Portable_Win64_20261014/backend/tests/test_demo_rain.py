from copy import deepcopy
import json
from fastapi.testclient import TestClient
import pytest
from backend.api.main import create_app
from backend.tests.test_sessions import setup,headers,EPOCH


@pytest.fixture
def demo(setup,monkeypatch):
    settings,gateway=setup
    path=settings.project_root/'scenarios/demo_variants/S4.json'
    path.parent.mkdir(parents=True)
    fixture=json.loads((settings.project_root/'scenarios/fixtures/thu-duc-binh-thanh-v1/S4.json').read_bytes())
    published=[]
    monkeypatch.setattr('backend.services.scenario_import.validate_fixture',lambda value,*a:deepcopy(value))
    def publish(*args):published.append(args[3]);return 'f'*64
    monkeypatch.setattr('backend.services.scenario_import.publish_import',publish)
    monkeypatch.setattr('backend.services.run_service.RunService.manifest',lambda *a:{
        'scenario_id':'S4','source_kind':'IMPORTED_SCENARIO','weather':{},'traffic_source':'ESTIMATED','temporal_model':'FROZEN_DECISION_EPOCH',
        'artifacts':{'fixture':{'sha256':'e'*64}}})
    async def bootstrap_run(sid,command,rid,digest):return await gateway.bootstrap(sid,'S4',command)
    gateway.bootstrap_run=bootstrap_run
    return settings,gateway,path,fixture,published


def test_default_s4_uses_owned_native_variant_and_retries_keep_source(demo):
    settings,gateway,path,fixture,published=demo
    path.write_text(json.dumps(fixture),encoding='utf8')
    with TestClient(create_app(settings,gateway=gateway)) as client:
        first=client.post('/api/scenarios/S4/load',json={'request_id':'new-rain'},headers=headers())
        assert first.status_code==201,first.text
        session=first.json()['data']['session'];assert session['run_id'].startswith('run-')
        # Even a later demo release cannot change an existing request's source.
        fixture['revision']='next';path.write_text(json.dumps(fixture),encoding='utf8')
        retry=client.post('/api/scenarios/S4/load',json={'request_id':'new-rain'},headers=headers())
        assert retry.json()['data']['session']==session and len(published)==1
        assert client.post('/api/scenarios/S4/load',json={'request_id':'new-rain'},headers=headers('viewer')).status_code==403


def test_old_pinned_load_retry_stays_pinned_and_other_scenario_conflicts(demo):
    settings,gateway,path,fixture,published=demo
    with TestClient(create_app(settings,gateway=gateway)) as client:
        old=client.post('/api/scenarios/S4/load',json={'request_id':'old-rain'},headers=headers()).json()['data']
        other=client.post('/api/scenarios/S0/load',json={'request_id':'other'},headers=headers())
        assert other.status_code==201
        path.write_text(json.dumps(fixture),encoding='utf8')
        retry=client.post('/api/scenarios/S4/load',json={'request_id':'old-rain'},headers=headers())
        assert retry.json()['data']==old and 'run_id' not in old['session'] and not published
        conflict=client.post('/api/scenarios/S4/load',json={'request_id':'other'},headers=headers())
        assert conflict.status_code==409 and conflict.json()['diagnostics'][0]['code']=='IDEMPOTENCY_CONFLICT'


def test_ambiguous_bootstrap_retry_keeps_reserved_variant_and_session(demo):
    settings,gateway,path,fixture,published=demo
    path.write_text(json.dumps(fixture),encoding='utf8');gateway.fail_after_commit=True
    with TestClient(create_app(settings,gateway=gateway)) as client:
        assert client.post('/api/scenarios/S4/load',json={'request_id':'interrupted'},headers=headers()).status_code==503
        fixture['revision']='changed';path.write_text(json.dumps(fixture),encoding='utf8')
        retry=client.post('/api/scenarios/S4/load',json={'request_id':'interrupted'},headers=headers())
        assert retry.status_code==201,retry.text
        assert len(published)==1 and len({r[0] for r in gateway.commands})==1


def test_invalid_bundled_variant_never_falls_back_to_another_rain(demo):
    settings,gateway,path,fixture,published=demo
    path.write_text('{broken',encoding='utf8')
    with TestClient(create_app(settings,gateway=gateway)) as client:
        result=client.post('/api/scenarios/S4/load',json={'request_id':'broken'},headers=headers())
        assert result.status_code==503
        assert result.json()['diagnostics'][0]['code']=='DEMO_SCENARIO_INVALID'
        assert not published and not gateway.commands
