"""Pure pending-event candidate, committed only by the M2 store CAS.

Change Impact Analysis: new runtime transition /2. No legacy applier modified.
Unavailable owner is immobilized; O009 readiness is the locked synthetic policy.
"""
from copy import deepcopy
from optimization.integration.member1_decision_state_adapter import _order
from optimization.models.member1_dynamic_state import sha as physical_sha
from optimization.rolling_horizon.member1_motion_replay import LEADER_POLICY_ID,LEADER_POLICY_SHA256
from optimization.rolling_horizon.member1_rain_source import load_source_model,build_overlay
from optimization.rolling_horizon.member1_rain_validation import validate_overlay
from .protocol import require,offset,sha

def apply_candidate(state,graph,snapshot,before,event_id):
    if 'delivery_day' in before:
        return apply_day_event(state,graph,snapshot,before,event_id)
    event=next((e for e in state.pending_events if e.event_id==event_id),None)
    require(event is not None and event_id in {e['event_id'] for e in before['pending_events']},'EVENT_BINDING','event_id','receipt-gated pending event required')
    require(before['current_time']==event.timestamp,'EVENT_NOT_DUE','current_time','replay exactly to event boundary first')
    after=deepcopy(before);after.pop('content_sha256',None);overlay=None
    after['pending_events']=[e for e in after['pending_events'] if e['event_id']!=event_id]
    after['applied_event_ids']=after['applied_event_ids']+[event_id];after['state_version']+=1
    if event.event_type=='URGENT_ORDER':
        order=_order(event.to_dict()['payload']['orderPayload'],0,'event.orderPayload')
        require(order.order_id not in {o['order_id'] for o in after['orders']},'EVENT_CONFLICT','event.orderPayload','order already exists')
        after['orders'].append({'order_id':order.order_id,'status':'WAITING','owner_vehicle_id':None,'picked_up_at':None,'delivered_at':None,'demand_kg':order.demand_kg,'provenance':'PINNED_EVENT_SYNTHETIC_READY_AT_DEPOT'})
        after['event_order']=order.to_dict()
    elif event.event_type=='VEHICLE_UNAVAILABLE':
        vid=event.to_dict()['payload']['vehicleId'];vehicle=next(v for v in after['vehicles'] if v['vehicle_id']==vid)
        vehicle.update(availability='UNAVAILABLE',suspended_commitment=vehicle['active_commitment'],active_commitment=None,suspension_reason='VEHICLE_UNAVAILABLE_NO_RECOVERY',activity='IMMOBILIZED')
    else:
        require(event.event_type=='LOCAL_RAIN_WHAT_IF','UNSUPPORTED','event.event_type','locked suite event only')
        model=load_source_model(snapshot,state);overlay=build_overlay(state,graph,model);validate_overlay(state,graph,model,overlay)
        after['overlay_sha256']=overlay['content_sha256']
    after['content_sha256']=physical_sha(after)
    proof={'schema_version':'task02-m2-runtime-event-receipt/2','event_id':event_id,'event_sha256':sha(event.to_dict()),
           'before_sha256':before['content_sha256'],'after_sha256':after['content_sha256'],'policy_id':LEADER_POLICY_ID,'policy_sha256':LEADER_POLICY_SHA256,
           'current_time':event.timestamp,'synthetic':True,'overlay_sha256':overlay['content_sha256'] if overlay else None}
    return after,overlay,proof

def validate_event(state,graph,snapshot,before,after,proof,overlay):
    """Independent delta check, no call to apply_candidate."""
    if 'delivery_day' in before:
        expected,expected_overlay,expected_proof=apply_day_event(state,graph,snapshot,before,proof['event_id'])
        require(after==expected and proof==expected_proof and overlay==expected_overlay,'EVENT_PHYSICS','event','delivery-day transition differs')
        return {'validator_version':'delivery-day-event/1','valid':True,'scope':'DELIVERY_DAY_POLICY_TRANSITION','diagnostics':[]}
    require(isinstance(proof,dict),'INVALID_DATA','event_receipt','object required')
    event=next((e for e in state.pending_events if e.event_id==proof.get('event_id')),None)
    require(event is not None and proof.get('event_sha256')==sha(event.to_dict()),'EVENT_BINDING','event_receipt.event_sha256','pinned pending event differs')
    require(proof.get('policy_id')==LEADER_POLICY_ID and proof.get('policy_sha256')==LEADER_POLICY_SHA256 and proof.get('synthetic') is True,'POLICY_BINDING','event_receipt','locked simulation policy required')
    require(before['current_time']==event.timestamp and proof.get('before_sha256')==before['content_sha256'] and proof.get('after_sha256')==after['content_sha256'],'EVENT_BINDING','event_receipt','boundary/state digests differ')
    expected=deepcopy(before);expected.pop('content_sha256',None)
    expected['state_version']+=1;expected['pending_events']=[e for e in before['pending_events'] if e['event_id']!=event.event_id];expected['applied_event_ids']=before['applied_event_ids']+[event.event_id]
    if event.event_type=='URGENT_ORDER':
        o=_order(event.to_dict()['payload']['orderPayload'],0,'event.orderPayload')
        expected['event_order']=o.to_dict();expected['orders'].append({'order_id':o.order_id,'status':'WAITING','owner_vehicle_id':None,'picked_up_at':None,'delivered_at':None,'demand_kg':o.demand_kg,'provenance':'PINNED_EVENT_SYNTHETIC_READY_AT_DEPOT'})
        require(overlay is None,'EVENT_BINDING','overlay','S2 cannot install rain')
    elif event.event_type=='VEHICLE_UNAVAILABLE':
        vid=event.to_dict()['payload']['vehicleId']
        for v in expected['vehicles']:
            if v['vehicle_id']==vid:v.update(availability='UNAVAILABLE',suspended_commitment=v['active_commitment'],active_commitment=None,suspension_reason='VEHICLE_UNAVAILABLE_NO_RECOVERY',activity='IMMOBILIZED')
        require(overlay is None,'EVENT_BINDING','overlay','S3 cannot install rain')
    else:
        require(event.event_type=='LOCAL_RAIN_WHAT_IF','UNSUPPORTED','event.event_type','unknown event')
        model=load_source_model(snapshot,state);validate_overlay(state,graph,model,overlay);expected['overlay_sha256']=overlay['content_sha256']
    expected['content_sha256']=physical_sha(expected)
    require(after==expected,'EVENT_PHYSICS','after_state','event changed prefix/custody/position outside locked delta')
    return {'validator_version':'task02-m2-runtime-event-validator/2','valid':True,'scope':'PINNED_EVENT_DELTA; NOT_SUFFIX_FEASIBILITY','diagnostics':[]}


def apply_day_event(state,graph,snapshot,before,event_id):
    from .delivery_day import effective_state,mutate
    from datetime import timedelta
    from .protocol import instant
    state=effective_state(state,before,rain_event=True)
    event=next((e for e in state.pending_events if e.event_id==event_id),None)
    require(event is not None and event_id in {e['event_id'] for e in before['pending_events']},'EVENT_BINDING','event','pending event required')
    require(before['current_time']==event.timestamp,'EVENT_NOT_DUE','time','event boundary required')
    after=deepcopy(before);overlay=None
    if event.event_type=='VEHICLE_UNAVAILABLE':
        after=mutate(state,after,'unavailable',{'vehicle_id':event.to_dict()['payload']['vehicleId']})
    elif event.event_type=='URGENT_ORDER':
        order=_order(event.to_dict()['payload']['orderPayload'],0,'event.orderPayload').to_dict()
        require(order['order_id'] not in after['delivery_day']['catalog'],'ORDER_EXISTS','event','duplicate urgent')
        now=instant(event.timestamp,'time')
        order.update(earliest=now.isoformat(),preferred_due=(now+timedelta(hours=2)).isoformat(),hard_deadline=(now+timedelta(hours=8)).isoformat())
        after['delivery_day']['catalog'][order['order_id']]=order
        after['delivery_day']['customers'][order['order_id']]={'name':'','phone':'','arrived_at':event.timestamp}
        after['delivery_day']['urgent_ids'].append(order['order_id'])
        after['orders'].append({'order_id':order['order_id'],'status':'WAITING','owner_vehicle_id':None,'picked_up_at':None,'delivered_at':None,'demand_kg':order['demand_kg']})
        for v in after['vehicles']:
            if v.get('active_commitment') and v['active_commitment']['kind']=='SERVICE':v['active_commitment']=None
    else:
        model=load_source_model(snapshot,state);overlay=build_overlay(state,graph,model);validate_overlay(state,graph,model,overlay)
        after['overlay_sha256']=overlay['content_sha256']
    after['pending_events']=[e for e in after['pending_events'] if e['event_id']!=event_id]
    after['applied_event_ids'].append(event_id);after['state_version']+=1
    after.pop('content_sha256',None);after['content_sha256']=physical_sha(after)
    proof={'schema_version':'delivery-day-event/1','event_id':event_id,'event_sha256':sha(event.to_dict()),'before_sha256':before['content_sha256'],'after_sha256':after['content_sha256']}
    return after,overlay,proof
