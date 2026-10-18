"""S4 exactly-once temporal transition. Base authority storage is reused.

Only a runner with receipt-gated source and independent preplan/motion checks
may bootstrap this local trust store. A JSON root claim is not authentication.
"""
from copy import deepcopy
import json
from typing import Mapping
from optimization.models.decision_state import DecisionState,EventEnvelope
from optimization.models.member1_dynamic_state import require,sha,canonical,finalize
from optimization.models.member1_rain import POLICY,STATE_VERSION,TRANSITION_VERSION,check_state,integer
from optimization.rolling_horizon.member1_dynamic_transition import LocalAuthority
from optimization.rolling_horizon.member1_motion_replay import LEADER_POLICY_ID,LEADER_POLICY_SHA256


class RainAuthority(LocalAuthority):
    def register(self,authority_id,initial,plan,accepted,before,pre_validation,motion_validation,*,overlay,parents=()):
        require(isinstance(overlay,Mapping) and overlay.get('initial_state_sha256')==sha(initial.to_dict()),'OVERLAY_BINDING','overlay','own initial overlay required')
        super().register(authority_id,initial,plan,accepted,before,pre_validation,motion_validation,parents=parents)
        with self.connect() as db:
            row=db.execute('SELECT root FROM authorities WHERE id=?',(authority_id,)).fetchone()
            root=json.loads(row[0]);root['overlay']=deepcopy(overlay)
            db.execute('UPDATE authorities SET root=? WHERE id=?',(canonical(root).decode(),authority_id))
        return {'authority_id':authority_id,'authority_sha256':sha(root),'head_hash':before['content_sha256'],'head_version':before['state_version']}

    def apply(self,authority_id,event,expected_hash,expected_version,*,_before_commit=None):
        require(isinstance(authority_id,str) and bool(authority_id),'INVALID_DATA','authority_id','local ID required')
        require(isinstance(expected_hash,str) and len(expected_hash)==64 and all(c in '0123456789abcdef' for c in expected_hash),'INVALID_DATA','expected_head_hash','SHA-256 required')
        integer(expected_version,'expected_head_version',positive=True)
        require(isinstance(event,EventEnvelope),'INVALID_DATA','event','typed event required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT root,head,version,state FROM authorities WHERE id=?',(authority_id,)).fetchone()
            require(row is not None,'AUTHORITY_REQUIRED','authority_id','persisted local authority missing')
            prior=db.execute('SELECT digest,receipt FROM events WHERE authority=? AND id=?',(authority_id,event.event_id)).fetchone()
            digest=sha(event.to_dict())
            if prior:
                require(prior[0]==digest,'EVENT_CONFLICT','event','same ID with changed event')
                return json.loads(prior[1])
            root=json.loads(row[0]);initial=DecisionState.from_dict(root['initial']);before=json.loads(row[3]);overlay=root['overlay']
            require(row[1]==expected_hash and row[2]==expected_version,'STALE_HEAD','expected_head_hash','CAS mismatch')
            require(initial.scenario_id=='S4' and len(initial.pending_events)==1 and initial.pending_events[0].to_dict()==event.to_dict(),'EVENT_BINDING','event','own pinned S4 pending event required')
            require(event.event_type=='LOCAL_RAIN_WHAT_IF','EVENT_BINDING','event.event_type','rain event required')
            require(before['current_time']==event.timestamp and before['snapshot_boundary']=='BEFORE_NEW_ACTIONS','EVENT_ORDER','before.current_time','exact event-time observed head required')
            require(overlay['event_sha256']==digest and overlay['initial_state_sha256']==sha(initial.to_dict()),'OVERLAY_BINDING','overlay','registered overlay source differs')
            after=deepcopy(before)
            after.update(schema_version=STATE_VERSION,scenario_id=initial.scenario_id,head_version=row[2]+1,state_version=row[2]+1,
                authority_id=authority_id,authority_sha256=sha(root),before_state_sha256=row[1],
                event_sha256=digest,leader_policy_sha256=LEADER_POLICY_SHA256,overlay_sha256=overlay['content_sha256'],
                temporal_policy=POLICY,forecast_materialized=False)
            after['pending_events']=[e for e in before['pending_events'] if e['event_id']!=event.event_id]
            after['applied_event_ids']=[event.event_id]
            after['limits']={**before['limits'],'event_application':True,'dynamic_solver':False}
            after['lineage']={**before['lineage'],'event_transition_count':1,'motion_parent_sha256':row[1]}
            # Preserve every observed vehicle commitment and physical byte. The
            # suffix has its own temporal ETA, not a rewritten baseline until_us.
            after['temporal_commitment_policy']='HELD_EDGE_PROGRESS; RESIDUAL_ETA_RECOMPUTED_IN_FORECAST'
            after['state_id']='rain-'+sha(after)[:24];after=finalize(after);check_state(after)
            receipt={'schema_version':TRANSITION_VERSION,'status':'APPLIED','authority_id':authority_id,
                'authority_sha256':sha(root),'event_id':event.event_id,'event_sha256':digest,
                'before_state_sha256':row[1],'before_version':row[2],'after_state_sha256':after['content_sha256'],
                'after_version':row[2]+1,'decision_time':event.timestamp,'policy_id':LEADER_POLICY_ID,
                'policy_sha256':LEADER_POLICY_SHA256,'overlay_sha256':overlay['content_sha256'],
                'temporal_policy':POLICY,'after_state':after}
            receipt['transaction_id']='rain-tx-'+sha({k:v for k,v in receipt.items() if k!='after_state'})[:24]
            db.execute('INSERT INTO events VALUES(?,?,?,?)',(authority_id,event.event_id,digest,canonical(receipt).decode()))
            db.execute('UPDATE authorities SET head=?,version=?,state=? WHERE id=?',(after['content_sha256'],row[2]+1,canonical(after).decode(),authority_id))
            if _before_commit is not None:_before_commit()
        return receipt
