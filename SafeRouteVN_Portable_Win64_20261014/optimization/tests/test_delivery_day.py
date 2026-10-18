import json
from copy import deepcopy
from pathlib import Path
from datetime import timedelta
import pytest
from optimization.integration.member1_decision_state_adapter import parse_unverified_fixture
from optimization.models.decision_state import DecisionState
from optimization.runtime.execution import initial_observation
from optimization.runtime.delivery_day import mutate, effective_state, complete
from optimization.runtime.protocol import RuntimeError, instant

ROOT=Path(__file__).resolve().parents[2]

def frame(scenario='S3'):
    fixture=json.loads((ROOT/f'scenarios/fixtures/thu-duc-binh-thanh-v1/{scenario}.json').read_bytes())
    state=DecisionState.from_dict(parse_unverified_fixture(fixture)['state'])
    return state,mutate(state,initial_observation(state),'enable',{})


@pytest.mark.parametrize('vehicles,orders,events', [(4,18,2), (5,22,3), (5,25,0)])
def test_imported_day_starts_at_eight_preserving_order_and_event_offsets(vehicles, orders, events):
    from dataclasses import replace
    fixture = json.loads((ROOT/'scenarios/fixtures/thu-duc-binh-thanh-v1/S0.json').read_bytes())
    initial = fixture['initialState']
    initial['vehicles'] = [{**deepcopy(initial['vehicles'][0]), 'id':f'V{i+1}'} for i in range(vehicles)]
    initial['orders'] = [{**deepcopy(initial['orders'][0]), 'id':f'O{i+1}'} for i in range(orders)]
    origin = instant(initial['currentTime'], 'time')
    fixture['events'] = [{'eventId':f'E{i+1}', 'type':'URGENT_ORDER', 'sourceType':'SYNTHETIC',
        'timestamp':(origin+timedelta(minutes=15*(i+1))).isoformat(),
        'orderPayload':{**deepcopy(initial['orders'][0]), 'id':f'URGENT{i+1}',
            'earliest':(origin+timedelta(minutes=15*(i+1))).isoformat()}}
        for i in range(events)]
    parsed = parse_unverified_fixture(fixture)
    state = replace(DecisionState.from_dict(parsed['state']),
                    source_authentication='RUN_MANIFEST_VERIFIED',
                    source_kind='IMPORTED_SCENARIO', fixture_raw_sha256='a'*64,
                    catalog_raw_sha256='b'*64, receipt_sha256='c'*64, receipt_version='test')
    # Exercise exactly the serialization used in persisted roots and restart.
    state = DecisionState.from_dict(json.loads(json.dumps(state.to_dict())))
    before = initial_observation(state)
    after = mutate(state, before, 'enable', {})
    assert after['current_time'] == '2026-09-28T08:00:00+07:00'
    delta = instant(after['current_time'], 'time') - instant(before['current_time'], 'time')
    effective = effective_state(state, after)
    assert len(effective.vehicles) == len(state.vehicles)
    assert len(effective.orders) == len(state.orders)
    for original, shifted in zip(state.orders, effective.orders):
        for field in ('earliest', 'preferred_due', 'hard_deadline'):
            assert instant(getattr(shifted, field), field) - instant(getattr(original, field), field) == delta
    for original, shifted, pending in zip(state.pending_events, effective.pending_events, after['pending_events']):
        assert shifted.timestamp == pending['timestamp']
        assert instant(shifted.timestamp, 'event') - instant(original.timestamp, 'event') == delta
    assert all(v.working_start == after['current_time'] for v in effective.vehicles)
    assert before == initial_observation(state)

def add(state,head,oid,urgent=False,depot='DEPOT'):
    return mutate(state,head,'add_order',dict(template_id=next(iter(head['delivery_day']['catalog'])),order_id=oid,urgent=urgent,depot_id=depot,name='Test customer',phone='0900000000'))

def test_cutoff_and_urgent_use_arrival_not_pickup_time():
    state,head=frame('S0')
    assert head['current_time'].endswith('08:00:00+07:00')
    head=add(state,head,'AT_8')
    assert 'AT_8' not in head['delivery_day']['queue']
    head['current_time']=(instant(head['current_time'],'time')+timedelta(seconds=1)).isoformat()
    head=add(state,head,'LATE')
    head=add(state,head,'URGENT',True)
    assert set(head['delivery_day']['queue'])=={'LATE'}
    assert head['delivery_day']['urgent_ids']==['URGENT']
    assert 'AT_8' not in head['delivery_day']['queue']
    assert effective_state(state,head).orders[-1].order_id=='URGENT'


def test_s4_day_shifts_rain_payload_interval_and_preserves_source():
    from optimization.rolling_horizon.member1_rain_source import event_interval
    from optimization.models.decision_state import EventEnvelope
    state, head = frame('S4')
    original = state.pending_events[0].to_dict()
    dry = effective_state(state, head)
    assert dry.pending_events[0].to_dict()['payload'] == original['payload']
    shifted = effective_state(state, head, rain_event=True)
    event, edges, start, end = event_interval(shifted)
    payload = event.to_dict()['payload']
    assert event.timestamp == head['pending_events'][0]['timestamp']
    assert payload['timestamp'] == payload['startTime'] == event.timestamp
    assert instant(payload['endTime'], 'end') - instant(payload['startTime'], 'start') == (
        instant(original['payload']['endTime'], 'end') - instant(original['payload']['startTime'], 'start'))
    assert edges == original['payload']['affectedEdgeIds'] and end > start
    assert EventEnvelope.from_dict(event.to_dict()) == event
    assert effective_state(shifted, head, rain_event=True) == shifted
    head['overlay_sha256'] = 'a' * 64
    assert effective_state(state, head) == shifted
    assert state.pending_events[0].to_dict() == original

def test_recovery_keeps_custody_until_original_depot_receipt():
    state,head=frame()
    original=deepcopy(head)
    after=mutate(state,head,'unavailable',dict(vehicle_id='V1',recovery_minutes=45))
    assert head==original
    assert next(o for o in after['orders'] if o['order_id']=='O001')['status']=='ONBOARD'
    assert after['delivery_day']['recoveries'][0]['depot_id']=='DEPOT'
    # Goods still waiting for pickup remain eligible for another vehicle today.
    assert set(after['delivery_day']['queue'])=={'O001'}
    settled=mutate(state,after,'wait_recovery',{})
    assert settled['vehicles'][0]['current_load_kg']==0
    assert not settled['vehicles'][0]['onboard_order_ids']
    assert settled['delivery_day']['recoveries'][0]['status']=='RECOVERED'
    assert 'O001' in settled['delivery_day']['queue']

def test_multi_depot_recovery_is_per_parcel_and_no_origin_rewrite_onboard():
    state,head=frame()
    head=mutate(state,head,'add_depot',{'depot_id':'DEPOT-2','location_order_id':'O002'})
    head=mutate(state,head,'assign_depot',{'order_id':'O002','depot_id':'DEPOT-2'})
    with pytest.raises(RuntimeError,match='already picked'):
        mutate(state,head,'assign_depot',{'order_id':'O001','depot_id':'DEPOT-2'})
    order=next(o for o in head['orders'] if o['order_id']=='O002')
    order.update(status='ONBOARD',owner_vehicle_id='V1',picked_up_at=head['current_time'])
    head['vehicles'][0]['onboard_order_ids'].append('O002')
    head['vehicles'][0]['current_load_kg']+=order['demand_kg']
    after=mutate(state,head,'unavailable',{'vehicle_id':'V1'})
    assert {r['order_id']:r['depot_id'] for r in after['delivery_day']['recoveries']}=={'O001':'DEPOT','O002':'DEPOT-2'}

def test_deliver_requires_owner_arrival_and_explicit_confirmation():
    state,head=frame()
    with pytest.raises(RuntimeError):mutate(state,head,'deliver',{'vehicle_id':'V1','order_id':'O001'})
    vehicle=head['vehicles'][0]
    vehicle['position']['node_id']=head['delivery_day']['catalog']['O001']['graph_node_id']
    vehicle['awaiting_delivery']='O001'
    with pytest.raises(RuntimeError):mutate(state,head,'deliver',{'vehicle_id':'V2','order_id':'O001'})
    after=mutate(state,head,'deliver',{'vehicle_id':'V1','order_id':'O001'})
    assert after['orders'][0]['status']=='DELIVERED'
    assert after['vehicles'][0]['current_load_kg']==0
    with pytest.raises(RuntimeError):mutate(state,after,'deliver',{'vehicle_id':'V1','order_id':'O001'})

def test_new_day_requires_completion_but_not_return_and_keeps_pending_recovery():
    state,head=frame()
    with pytest.raises(RuntimeError):mutate(state,head,'next_day',{})
    head=mutate(state,head,'unavailable',{'vehicle_id':'V1','recovery_minutes':1440})
    for order in head['orders']:
        if order['status']=='WAITING':head=mutate(state,head,'defer',{'order_id':order['order_id']})
    assert complete(head)
    # Place recovery after next 08:00; day rollover cannot fabricate returned goods.
    head['delivery_day']['recoveries'][0]['ready_at']='2026-09-29T09:00:00+07:00'
    before_position=deepcopy(head['vehicles'][0]['position'])
    after=mutate(state,head,'next_day',{})
    assert after['current_time']=='2026-09-29T08:00:00+07:00'
    assert set(after['delivery_day']['queue'])=={'O001'}
    assert after['vehicles'][0]['position']==before_position
    assert effective_state(state,after).vehicles[1].working_start==after['current_time']


def test_replay_settles_recovery_once_and_cannot_restore_old_cargo():
    from optimization.runtime.execution import replay_actions
    from optimization.runtime.execution_validation import validate_replay
    from optimization.models.member1_dynamic_state import sha
    from types import SimpleNamespace
    state,head=frame()
    head=mutate(state,head,'unavailable',{'vehicle_id':'V1','recovery_minutes':30})
    head['pending_events']=[]
    head.pop('content_sha256',None);head['content_sha256']=sha(head)
    target=head['delivery_day']['recoveries'][0]['ready_at']
    observed=replay_actions(state,SimpleNamespace(),head,[],target,'plan')
    assert observed['vehicles'][0]['onboard_order_ids']==[]
    assert observed['orders'][0]['status']=='WAITING'
    assert validate_replay(state,SimpleNamespace(),head,[],target,'plan',observed)['valid']
    later=(instant(target,'time')+timedelta(minutes=1)).isoformat()
    again=replay_actions(state,SimpleNamespace(),head,[],later,'plan',dispatch_frame=observed)
    assert again['vehicles'][0]['current_load_kg']==0
    assert len([e for e in again['delivery_day']['history'] if e['kind']=='DEPOT_RECEIPT'])==1
    assert validate_replay(state,SimpleNamespace(),head,[],later,'plan',again,dispatch_frame=observed)['valid']


def test_live_weather_keeps_its_clock_and_context_when_enabling_day():
    from dataclasses import replace
    state,_=frame('S0')
    state=replace(state,source_authentication='RUN_MANIFEST_VERIFIED',fixture_raw_sha256='a'*64,catalog_raw_sha256='b'*64,receipt_sha256='c'*64,receipt_version='test')
    after=mutate(state,initial_observation(state),'enable',{})
    assert after['current_time']==state.current_time
    assert after['delivery_day']['cutoff'].endswith('08:00:00+07:00')


def test_durable_dispatch_retry_cas_and_restart(tmp_path):
    from optimization.runtime.store import Store
    store=Store(tmp_path/'authority.sqlite','a'*64)
    store._bootstrap('s',{'source_hashes':{}},{'context_version':'c','current_time':'2026-10-11T08:00:00+07:00'},'boot')
    basis=store.head('s')['basis'];head={**store.head('s')['head'],'delivery':1}
    first=store.dispatch('s','same',basis,'test',{},head)
    reopened=Store(store.path,'a'*64)
    assert reopened.dispatch('s','same',basis,'test',{},head)==first
    assert reopened.verify()
    with pytest.raises(RuntimeError):reopened.dispatch('s','new',basis,'test',{},head)


def test_next_day_preserves_a_mid_return_edge_without_teleport():
    state,head=frame('S0')
    for o in head['orders']:o['status']='DELIVERED'
    vehicle=head['vehicles'][0]
    vehicle['position']={'kind':'ON_EDGE','edge_id':'e','from_node':1,'to_node':2,'progress':0.5,'coordinates':[106.7,10.8],'edge_entry_us':0,'expected_exit_us':10}
    vehicle['active_commitment']={'kind':'EDGE','edge_id':'e','to_node':2,'until_us':10}
    after=mutate(state,head,'next_day',{})
    assert after['vehicles'][0]['position']['progress']==0.5
    assert after['vehicles'][0]['position']['coordinates']==[106.7,10.8]
    assert after['vehicles'][0]['active_commitment']['until_us']==86_400_000_010
