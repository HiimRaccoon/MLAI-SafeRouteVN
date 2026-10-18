from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.services.run_service import RunService
from backend.tests.test_sessions import setup, headers, code
from optimization.runtime.run_builder import rebase, fetch_weather, write
from optimization.runtime.run_source import run_directory, read_manifest, file_sha, load_state, artifact, SCHEMA

ROOT=Path(__file__).resolve().parents[2]

@pytest.mark.parametrize('scenario',['S0','S2','S3','S4'])
def test_rebase_midnight_keeps_offsets_custody_and_event_windows(scenario):
    source=json.loads((ROOT/f'scenarios/fixtures/thu-duc-binh-thanh-v1/{scenario}.json').read_bytes())
    original=deepcopy(source); epoch='2026-10-10T23:57:30+07:00'
    rebased=rebase(source,epoch,7)
    delta=datetime.fromisoformat(epoch)-datetime.fromisoformat(source['initialState']['currentTime'])
    assert source==original
    for before,after in zip(source['initialState']['orders'],rebased['initialState']['orders']):
        for field in ('earliest','preferredDue','hardDeadline','pickedUpAt','deliveredAt'):
            assert after[field] == ((datetime.fromisoformat(before[field])+delta).isoformat() if before[field] else None)
        assert after['assignedVehicleId']==before['assignedVehicleId']
    for before,after in zip(source['initialState']['vehicles'],rebased['initialState']['vehicles']):
        assert after['onboardOrderIds']==before['onboardOrderIds']
        assert datetime.fromisoformat(after['workingEnd'])-datetime.fromisoformat(after['workingStart'])==datetime.fromisoformat(before['workingEnd'])-datetime.fromisoformat(before['workingStart'])
    if rebased['events']:
        event=rebased['events'][0]; prior=source['events'][0]
        assert datetime.fromisoformat(event['timestamp'])==datetime.fromisoformat(epoch)+timedelta(minutes=7)
        if 'orderPayload' in event:
            assert datetime.fromisoformat(event['orderPayload']['hardDeadline'])-datetime.fromisoformat(event['timestamp'])==datetime.fromisoformat(prior['orderPayload']['hardDeadline'])-datetime.fromisoformat(prior['timestamp'])
        if 'endTime' in event:
            assert datetime.fromisoformat(event['endTime'])-datetime.fromisoformat(event['startTime'])==timedelta(hours=1)
    assert rebased['validation']['jointVRPFeasibilityProven'] is False

def weather_payload(epoch):
    stamp=int(datetime.fromisoformat(epoch).replace(minute=0,second=0).timestamp())
    return {'hourly_units':{'time':'unixtime','precipitation':'mm','wind_speed_10m':'km/h','visibility':'m','weather_code':'wmo code'},
        'hourly':{'time':[stamp],'precipitation':[1.5],'wind_speed_10m':[12.0],'visibility':[None],'weather_code':[61]}}

def test_weather_distinguishes_fetch_hour_missing_values_and_units():
    epoch='2026-10-10T16:48:20+07:00'; payload=weather_payload(epoch)
    region={'regionId':'r','latitude':10.8,'longitude':106.7}
    def opener(*args,**kwargs):return io.StringIO(json.dumps(payload))
    weather,raw=fetch_weather([region],epoch,opener=opener)
    item=weather['regions'][0]
    assert item['validAt']=='2026-10-10T16:00:00+07:00'
    assert item['fetchedAt']!=item['validAt'] and item['sourceTimestamp'] is None
    assert 'visibilityM' in item['missingFlags'] and item['fallbackUsed'] is False
    payload['hourly_units']['wind_speed_10m']='m/s'
    with pytest.raises(ValueError,match='units'):fetch_weather([region],epoch,opener=opener)

@pytest.mark.parametrize('bad',['../escape','run-../escape','run-'+'a'*31,'C:/run-'+'a'*32])
def test_run_id_cannot_escape_registry(tmp_path,bad):
    with pytest.raises(ValueError):run_directory(tmp_path,bad)

def test_registered_manifest_tampering_is_rejected(tmp_path):
    rid='run-'+'a'*32;path=run_directory(tmp_path,rid)/'manifest.json'
    value={'schema_version':SCHEMA,'run_id':rid,'epoch':'2026-10-10T16:00:00+07:00'}
    write(path,value);digest=file_sha(path)
    assert read_manifest(tmp_path,rid,digest)==value
    value['epoch']='2026-10-11T16:00:00+07:00';write(path,value)
    with pytest.raises(ValueError,match='digest'):read_manifest(tmp_path,rid,digest)

def test_http_run_owner_retry_offline_and_no_implicit_load(setup,monkeypatch):
    settings,gateway=setup
    monkeypatch.setattr(RunService,'build',lambda *args:None)
    monkeypatch.delenv('SAFEROUTE_OFFLINE_MODE',raising=False)
    body={'request_id':'weather-1','scenario_id':'S2','event_after_minutes':9}
    with TestClient(create_app(settings,gateway=gateway)) as client:
        first=client.post('/api/runs',json=body,headers=headers());assert first.status_code==202,first.text
        rid=first.json()['data']['run_id']
        assert client.post('/api/runs',json=body,headers=headers()).json()['data']['run_id']==rid
        code(client.get('/api/runs/'+rid,headers=headers('bob')),403,'FORBIDDEN')
        code(client.post('/api/runs',json={**body,'event_after_minutes':10},headers=headers()),409,'IDEMPOTENCY_CONFLICT')
        code(client.post('/api/runs/'+rid+'/load',json={'request_id':'load-run'},headers=headers()),409,'RUN_NOT_READY')
        assert gateway.commands==[]
        monkeypatch.setenv('SAFEROUTE_OFFLINE_MODE','loopback-only')
        assert client.post('/api/runs',json=body,headers=headers()).json()['data']['run_id']==rid
        code(client.post('/api/runs',json={**body,'request_id':'offline'},headers=headers()),409,'OFFLINE_MODE')
        assert client.post('/api/runs',json=body,headers=headers('viewer')).status_code==403


def test_registered_artifact_checks_content_and_path(tmp_path):
    path=tmp_path/'weather_raw.json';write(path,{'rain':1})
    manifest={'artifacts':{'weather_raw':{'path':path.name,'bytes':path.stat().st_size,'sha256':file_sha(path)}}}
    assert artifact(tmp_path,manifest,'weather_raw')==path
    write(path,{'rain':2})
    with pytest.raises(ValueError,match='changed'):artifact(tmp_path,manifest,'weather_raw')
    manifest['artifacts']['weather_raw']['path']='../outside.json'
    with pytest.raises(ValueError,match='outside'):artifact(tmp_path,manifest,'weather_raw')
