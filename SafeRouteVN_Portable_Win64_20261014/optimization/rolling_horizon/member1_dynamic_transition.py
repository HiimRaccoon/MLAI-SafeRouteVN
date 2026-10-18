"""Local M2 authority/CAS ledger, outside read-only M1. Not an M3 endpoint.

Only the internal runner registers independently validated state/plan digests.
Caller-supplied rehashes are compared to stored authority, not trusted anew.
BEGIN IMMEDIATE binds event insertion and head update in one rollback-able tx.
"""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from typing import Mapping
from optimization.models.decision_state import DecisionState,EventEnvelope
from optimization.models.member1_dynamic_state import (DynamicError,DynamicState,STATE_VERSION,TRANSITION_VERSION,sha,canonical,require,finalize)
from optimization.integration.member1_decision_state_adapter import _order
from optimization.rolling_horizon.member1_motion_replay import LEADER_POLICY_ID,LEADER_POLICY_SHA256
from optimization.models.motion_state import _instant,MotionContractError

class LocalAuthority:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS authorities(id TEXT PRIMARY KEY, root TEXT NOT NULL, head TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL); CREATE TABLE IF NOT EXISTS events(authority TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL, receipt TEXT NOT NULL, PRIMARY KEY(authority,id));')
    def connect(self):return sqlite3.connect(self.path,timeout=30)
    def register(self,authority_id,initial,plan,accepted,before,pre_validation,motion_validation,*,parents=()):
        require(isinstance(authority_id,str) and bool(authority_id),'INVALID_DATA','authority_id','nonempty local authority ID required')
        require(isinstance(initial,DecisionState),'INVALID_DATA','initial','typed initial state required')
        for key,value in [('plan',plan),('accepted',accepted),('before',before),('pre_validation',pre_validation),('motion_validation',motion_validation)]:
            require(isinstance(value,Mapping),'INVALID_DATA',key,'trusted bootstrap object required')
        require(pre_validation.get('valid') is True and motion_validation.get('valid') is True,'AUTHORITY_VALIDATION','authority','raw independent validations required')
        require(isinstance(parents,(list,tuple)) and all(isinstance(p,Mapping) for p in parents),'INVALID_DATA','parents','trusted parent observations required')
        root={'initial':initial.to_dict(),'plan':plan,'accepted':accepted,'before':before,'parents':list(parents),'pre_validation':pre_validation,'motion_validation':motion_validation}
        with self.connect() as db:
            db.execute('INSERT INTO authorities VALUES(?,?,?,?,?)',(authority_id,canonical(root).decode(),before['content_sha256'],before['state_version'],canonical(before).decode()))
        return {'authority_id':authority_id,'authority_sha256':sha(root),'head_hash':before['content_sha256'],'head_version':before['state_version']}
    def export(self,authority_id):
        require(isinstance(authority_id,str) and bool(authority_id),'INVALID_DATA','authority_id','local identifier required')
        with self.connect() as db:
            row=db.execute('SELECT root,head,version,state FROM authorities WHERE id=?',(authority_id,)).fetchone()
            require(row is not None,'AUTHORITY_REQUIRED','authority_id','trusted local authority missing')
            events=db.execute('SELECT receipt FROM events WHERE authority=? ORDER BY id',(authority_id,)).fetchall()
        return {'authority_id':authority_id,'root':json.loads(row[0]),'authority_sha256':sha(json.loads(row[0])),'head_hash':row[1],'head_version':row[2],'state':json.loads(row[3]),'events':[json.loads(x[0]) for x in events]}
    def apply(self,authority_id,event,expected_hash,expected_version,*,_before_commit=None):
        require(isinstance(authority_id,str) and bool(authority_id),'INVALID_DATA','authority_id','local identifier required')
        require(isinstance(expected_hash,str) and len(expected_hash)==64 and all(c in '0123456789abcdef' for c in expected_hash),'INVALID_DATA','expected_head_hash','SHA-256 required')
        require(type(expected_version) is int and 0<expected_version<2**63,'INVALID_DATA','expected_head_version','bounded integer required')
        require(isinstance(event,EventEnvelope),'INVALID_DATA','event','typed pending event required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT root,head,version,state FROM authorities WHERE id=?',(authority_id,)).fetchone()
            require(row is not None,'AUTHORITY_REQUIRED','authority_id','trusted local authority missing')
            prior=db.execute('SELECT digest,receipt FROM events WHERE authority=? AND id=?',(authority_id,event.event_id)).fetchone()
            digest=sha(event.to_dict())
            if prior:
                require(prior[0]==digest,'EVENT_CONFLICT','event','same event ID has different canonical payload')
                return json.loads(prior[1])
            root=json.loads(row[0]);initial=DecisionState.from_dict(root['initial']);before=json.loads(row[3])
            require(row[1]==expected_hash and row[2]==expected_version,'STALE_HEAD','expected_head_hash','CAS head mismatch')
            pending={e.event_id:e for e in initial.pending_events}
            require(event.event_id in pending and event.to_dict()==pending[event.event_id].to_dict(),'EVENT_BINDING','event','event differs from receipt-gated initial source')
            require(initial.scenario_id in ('S2','S3'),'UNSUPPORTED','scenario_id','Step5 only S2/S3')
            try:
                current_time=_instant(before['current_time'],'before.current_time')
                event_time=_instant(event.timestamp,'event.timestamp')
            except MotionContractError as e:raise DynamicError('INVALID_DATA',e.path,str(e)) from e
            if current_time<event_time:
                return {'status':'DEFERRED','code':'EVENT_NOT_DUE','post_event_state':None}
            require(current_time==event_time,'EVENT_ORDER','event.timestamp','exact BEFORE_NEW_ACTIONS event-time head required')
            after=deepcopy(before)
            after.update(schema_version=STATE_VERSION,scenario_id=initial.scenario_id,head_version=row[2]+1,
                authority_id=authority_id,authority_sha256=sha(root),before_state_sha256=row[1],
                event_sha256=digest,leader_policy_sha256=LEADER_POLICY_SHA256)
            after['state_version']=row[2]+1
            after['pending_events']=[e for e in before['pending_events'] if e['event_id']!=event.event_id]
            after['limits']={**before['limits'],'event_application':True,'dynamic_solver':False}
            after['lineage']={**before['lineage'],'event_transition_count':1,'motion_parent_sha256':row[1]}
            if initial.scenario_id=='S2':
                require(event.event_type=='URGENT_ORDER','EVENT_BINDING','event.event_type','S2 urgent order required')
                order=_order(event.to_dict()['payload'].get('orderPayload'),0,'event.orderPayload')
                require(order.order_id not in {o['order_id'] for o in after['orders']} and order.status=='WAITING','EVENT_ORDER','event.orderPayload.id','new WAITING order required')
                after['orders'].append({'order_id':order.order_id,'status':'WAITING','owner_vehicle_id':None,'picked_up_at':None,'delivered_at':None,'demand_kg':order.demand_kg,'provenance':'PINNED_EVENT_SYNTHETIC_READY_AT_DEPOT'})
                after['event_order']=order.to_dict()
            else:
                payload=event.to_dict()['payload'];vid=payload.get('vehicleId')
                require(event.event_type=='VEHICLE_UNAVAILABLE' and vid=='V1','EVENT_BINDING','event.payload.vehicleId','S3 V1 unavailable required')
                vehicle=next((v for v in after['vehicles'] if v['vehicle_id']==vid),None)
                require(vehicle is not None,'INVALID_DATA','vehicles','V1 missing')
                vehicle['availability']='UNAVAILABLE';vehicle['suspended_commitment']=vehicle['active_commitment'];vehicle['active_commitment']=None
                vehicle['suspension_reason']='VEHICLE_UNAVAILABLE_NO_RECOVERY';vehicle['activity']='IMMOBILIZED'
            after['applied_event_ids']=[event.event_id]
            after['assumptions']={'mode':'ROLLING_REPLAY_SYNTHETIC','policy_id':LEADER_POLICY_ID,'pickup_service_seconds':0.,'urgent_ready_at_event_is_synthetic':True,'no_unload_or_transfer':True}
            after['state_id']='dynamic-'+sha(after)[:24]
            after=finalize(after)
            DynamicState.from_dict(after)
            receipt={'schema_version':TRANSITION_VERSION,'status':'APPLIED','authority_id':authority_id,'authority_sha256':sha(root),'event_id':event.event_id,'event_sha256':digest,'before_state_sha256':row[1],'before_version':row[2],'after_state_sha256':after['content_sha256'],'after_version':row[2]+1,'decision_time':event.timestamp,'policy_id':LEADER_POLICY_ID,'policy_sha256':LEADER_POLICY_SHA256,'after_state':after}
            receipt['transaction_id']='tx-'+sha({k:v for k,v in receipt.items() if k!='after_state'})[:24]
            db.execute('INSERT INTO events VALUES(?,?,?,?)',(authority_id,event.event_id,digest,canonical(receipt).decode()))
            db.execute('UPDATE authorities SET head=?,version=?,state=? WHERE id=?',(after['content_sha256'],row[2]+1,canonical(after).decode(),authority_id))
            if _before_commit is not None:_before_commit()
        return receipt
