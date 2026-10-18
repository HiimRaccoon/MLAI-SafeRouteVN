from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from backend.api.main import create_app
from backend.services.scenario_import import validate_fixture
from backend.tests.test_sessions import setup, headers

ROOT=Path(__file__).resolve().parents[2]


@pytest.fixture
def fixture():
    return json.loads((ROOT/'scenarios/fixtures/thu-duc-binh-thanh-v1/S0.json').read_bytes())


@pytest.fixture
def network(tmp_path,fixture):
    path=tmp_path/'network.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE nodes(nodeId INTEGER PRIMARY KEY,longitude REAL,latitude REAL)')
        points=fixture['initialState']['locations']+fixture['initialState']['orders']
        db.executemany('INSERT INTO nodes VALUES(?,?,?)',[(p['graphNodeId'],p['longitude'],p['latitude']) for p in points])
    return path


def test_import_accepts_changed_orders_without_mutating_template(fixture,network):
    original=deepcopy(fixture); changed=deepcopy(fixture)
    changed['initialState']['orders'][0]['demandKg']=2
    changed['tradeoffEvidence']={'claim':'optimal'}
    result=validate_fixture(changed,fixture,network)
    assert result['initialState']['orders'][0]['demandKg']==2 and fixture==original
    assert 'tradeoffEvidence' not in result and result['validation']['jointVRPFeasibilityProven'] is False


@pytest.mark.parametrize('kind',['duplicate','coordinates','graph','capacity','epoch','version','path','nonfinite','oversize'])
def test_invalid_imports_fail_before_any_publication(fixture,network,kind):
    changed=deepcopy(fixture);s=changed['initialState']
    if kind=='duplicate':s['orders'][1]['id']=s['orders'][0]['id']
    if kind=='coordinates':s['orders'][0]['longitude']+=1
    if kind=='graph':s['orders'][0]['graphNodeId']=999
    if kind=='capacity':s['vehicles'][0]['capacityKg']=-1
    if kind=='epoch':s['currentTime']='2030-01-01T00:00:00+07:00'
    if kind=='version':changed['routingVersion']='other'
    if kind=='path':s['orders'][0]['id']='../../escape'
    if kind=='nonfinite':s['orders'][0]['demandKg']=float('nan')
    if kind=='oversize':s['orders']*=40
    with pytest.raises((ValueError,TypeError)):
        validate_fixture(changed,fixture,network)


def test_http_import_ownership_retry_and_listing(setup,fixture,monkeypatch):
    settings,gateway=setup
    monkeypatch.setattr('backend.services.scenario_import.validate_fixture',lambda *args:fixture)
    calls=[]
    def publish(*args):calls.append(args[2]);return 'f'*64
    monkeypatch.setattr('backend.services.scenario_import.publish_import',publish)
    # Publication is tested separately; use a minimal manifest for public projection.
    monkeypatch.setattr('backend.services.run_service.RunService.manifest',lambda *args:{'weather':{},'traffic_source':'ESTIMATED','temporal_model':'FROZEN_DECISION_EPOCH','artifacts':{'fixture':{'sha256':'e'*64}}})
    body={'request_id':'import-1','name':'my-scenario.json','fixture':fixture}
    with TestClient(create_app(settings,gateway=gateway)) as client:
        first=client.post('/api/scenarios/import',json=body,headers=headers());assert first.status_code==201,first.text
        rid=first.json()['data']['run_id']
        assert client.post('/api/scenarios/import',json=body,headers=headers()).json()['data']['run_id']==rid
        assert len(calls)==1
        assert client.post('/api/scenarios/import',json={**body,'name':'other'},headers=headers()).status_code==409
        assert client.post('/api/scenarios/import',json=body,headers=headers('viewer')).status_code==403
        assert len(client.get('/api/scenarios/imports/list',headers=headers()).json()['data']['imports'])==1
        assert client.get('/api/scenarios/imports/list',headers=headers('bob')).json()['data']['imports']==[]
        assert gateway.commands==[]  # Import alone does not switch the running session.


def test_optional_display_fields_survive_import_without_changing_domain(fixture, network):
    from optimization.integration.member1_decision_state_adapter import _parse_fixture
    changed = deepcopy(fixture)
    changed['initialState']['orders'][0].update(customerName='Khách thử nghiệm', customerPhone='0900000001', address='Địa chỉ hiển thị', notes='Gọi trước khi giao')
    changed['initialState']['vehicles'][0].update(driverName='Tài xế thử nghiệm', driverPhone='0900000002', licensePlate='TEST-001', vehicleModel='Xe máy')
    result = validate_fixture(changed, fixture, network)
    assert result['initialState']['orders'][0]['notes'] == 'Gọi trước khi giao'
    assert result['initialState']['vehicles'][0]['licensePlate'] == 'TEST-001'
    assert _parse_fixture(result).to_dict() == _parse_fixture(fixture).to_dict()


@pytest.mark.parametrize('field,value', [('customerPhone', 900000001), ('customerName', 'x'*161), ('notes', '\x00bad')])
def test_invalid_optional_display_fields_are_rejected(fixture, network, field, value):
    fixture['initialState']['orders'][0][field] = value
    with pytest.raises(ValueError):
        validate_fixture(fixture, fixture, network)
