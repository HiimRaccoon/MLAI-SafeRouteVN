"""Public Python execution-view checker; not a VRP/source authenticator.

Change Impact Analysis: new supplemental /2 with legacy /1 reader. Exact µs
use safe integers or int64 strings; rational components are decimal strings.
"""
from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path
from jsonschema import Draft202012Validator
import json
from .protocol import require,RuntimeError,tree,basis,instant,identifier,number,int64
from .trajectory_contract import trajectory

def validate_view(v):
    issues=[]
    try:
        require(isinstance(v,Mapping),'INVALID_DATA','$','object required');tree(v)
        require(v.get('schema_version') in ('task02-m2-execution-view/1','task02-m2-execution-view/2') if isinstance(v.get('schema_version'),str) else False,'VERSION_MISMATCH','schema_version','supported view required')
        basis(v.get('basis'));instant(v.get('current_time'),'current_time')
        require(v.get('execution_mode')=='SIMULATED_REPLAY' and v.get('real_world_observation') is False,'INVALID_DATA','execution_mode','simulation label required')
        require(v.get('metric_scope')=='OBSERVED_PREFIX_ONLY','INVALID_DATA','metric_scope','scope required')
        for k in ('order_ids','delivered_prefix','planned_served_suffix','unserved','vehicles','pending_event_ids'):require(isinstance(v.get(k),list),'INVALID_DATA',k,'array required')
        for k in ('order_ids','delivered_prefix','planned_served_suffix','pending_event_ids'):
            for x in v[k]:identifier(x,k)
            require(len(v[k])==len(set(v[k])),'INVALID_DATA',k,'unique IDs required')
        ids=v['delivered_prefix']+v['planned_served_suffix']
        for i,u in enumerate(v['unserved']):
            p=f'unserved[{i}]';require(isinstance(u,Mapping),'INVALID_DATA',p,'object required');identifier(u.get('order_id'),p+'.order_id');require(isinstance(u.get('reason'),str) and bool(u['reason']),'INVALID_DATA',p+'.reason','meaningful reason required');ids.append(u['order_id'])
        require(len(ids)==len(set(ids)) and set(ids)==set(v['order_ids']),'COVERAGE','order_ids','disjoint complete prefix/suffix/unserved required')
        if v.get('active_job_id') is not None:identifier(v['active_job_id'],'active_job_id')
        vids=[]
        for i,x in enumerate(v['vehicles']):
            p=f'vehicles[{i}]';require(isinstance(x,Mapping),'INVALID_DATA',p,'object required');vids.append(identifier(x.get('vehicle_id'),p+'.vehicle_id'))
            require(isinstance(x.get('availability'),str) and x['availability'] in ('AVAILABLE','UNAVAILABLE'),'INVALID_DATA',p+'.availability','known enum required')
            number(x.get('current_load_kg'),p+'.current_load_kg')
            if 'capacity_kg' in x:number(x['capacity_kg'],p+'.capacity_kg');require(x['current_load_kg']<=x['capacity_kg']+1e-9,'INVALID_DATA',p+'.current_load_kg','capacity exceeded')
            if 'remaining_range_m' in x:number(x['remaining_range_m'],p+'.remaining_range_m')
            cargo=x.get('onboard_order_ids');require(isinstance(cargo,list),'INVALID_DATA',p+'.onboard_order_ids','array required')
            for o in cargo:identifier(o,p+'.onboard_order_ids')
            require(len(cargo)==len(set(cargo)),'INVALID_DATA',p+'.onboard_order_ids','unique IDs required')
            position=x.get('position');require(isinstance(position,Mapping),'INVALID_DATA',p+'.position','object required')
            require(isinstance(position.get('kind'),str) and position['kind'] in ('AT_NODE','ON_EDGE'),'INVALID_DATA',p+'.position.kind','known position required')
            xy=position.get('coordinates');require(isinstance(xy,list) and len(xy)==2 and all(type(n) in (int,float) for n in xy),'INVALID_DATA',p+'.position.coordinates','WGS84 [lon,lat] required')
            from math import isfinite
            require(all(isfinite(n) for n in xy) and abs(xy[0])<=180 and abs(xy[1])<=90,'INVALID_DATA',p+'.position.coordinates','WGS84 bounds')
            if position['kind']=='ON_EDGE':
                identifier(position.get('edge_id'),p+'.position.edge_id');number(position.get('progress'),p+'.position.progress');require(position['progress']<=1,'INVALID_DATA',p+'.position.progress','progress bound')
            if 'progress_exact' in position:
                r=position['progress_exact'];require(isinstance(r,Mapping) and set(r)=={'numerator','denominator'},'INVALID_DATA',p+'.position.progress_exact','rational object required')
                n,d=int64(r['numerator'],p+'.position.progress_exact.numerator'),int64(r['denominator'],p+'.position.progress_exact.denominator');require(d>0 and 0<=n<=d,'INVALID_DATA',p+'.position.progress_exact','bounded progress')
            if 'position_timestamp' in x:instant(x['position_timestamp'],p+'.position_timestamp')
        require(len(vids)==len(set(vids)),'INVALID_DATA','vehicles.vehicle_id','duplicate vehicle')
        for key in ('observed_metrics','planned_suffix_metrics','projected_whole_metrics'):
            metrics=v.get(key);require(metrics is None or isinstance(metrics,Mapping),'INVALID_DATA',key,'null or metric object required')
            if metrics:
                for k,n in metrics.items():
                    if k.endswith('_us') and isinstance(n,str):require(int64(n,key+'.'+k)>=0,'INVALID_DATA',key+'.'+k,'nonnegative duration')
                    else:number(n,key+'.'+k)
        if v['schema_version'].endswith('/2'):
            trajectory(v.get('accepted_trajectory'))
            schema=json.loads(Path(__file__).with_name('execution_view.schema.json').read_bytes())
            errors=list(Draft202012Validator(schema).iter_errors(v));require(not errors,'SCHEMA_INVALID','.'.join(str(x) for x in errors[0].path) if errors else '$',errors[0].message if errors else '')
    except RuntimeError as e:issues.append(e.diagnostic())
    return issues

def validate_job_view(v):
    """Portable lifecycle semantics; no persistence/source authentication."""
    issues=[]
    try:
        require(isinstance(v,Mapping),'INVALID_DATA','$','job-view object required');tree(v)
        keys={'schema_version','job_id','job_status','input_basis','business_status','internal_status','diagnostics','validation','coverage_evaluated','served_orders','unserved_orders','plan_available','execution_view_required','public_api_v1_dynamic_plan_available'}
        require(set(v)==keys,'INVALID_DATA','$','exact job-view fields required')
        require(v['schema_version']=='task02-m2-runtime-job-view/1','VERSION_MISMATCH','schema_version','job-view/1 required');identifier(v['job_id'],'job_id');basis(v['input_basis'])
        require(isinstance(v['job_status'],str) and v['job_status'] in ('QUEUED','RUNNING','COMPLETED','FAILED'),'INVALID_DATA','job_status','known lifecycle')
        require(v['execution_view_required'] is True and v['public_api_v1_dynamic_plan_available'] is False,'INVALID_DATA','execution_view_required','supplemental scope required')
        for k in ('diagnostics','served_orders','unserved_orders'):require(isinstance(v[k],list),'INVALID_DATA',k,'array required')
        ids=[]
        for i,o in enumerate(v['served_orders']):ids.append(identifier(o,f'served_orders[{i}]'))
        for i,u in enumerate(v['unserved_orders']):
            p=f'unserved_orders[{i}]';require(isinstance(u,Mapping),'INVALID_DATA',p,'object required');ids.append(identifier(u.get('order_id'),p+'.order_id'));require(isinstance(u.get('reason'),str) and bool(u['reason']),'INVALID_DATA',p+'.reason','meaningful reason required')
        require(len(ids)==len(set(ids)),'INVALID_DATA','served_orders','duplicate or overlapping coverage')
        for k in ('coverage_evaluated','plan_available'):require(type(v[k]) is bool,'INVALID_DATA',k,'boolean required')
        validation=v['validation'];require(isinstance(validation,Mapping),'INVALID_DATA','validation','validation object required')
        for i,d in enumerate(v['diagnostics']):
            p=f'diagnostics[{i}]';require(isinstance(d,Mapping),'INVALID_DATA',p,'diagnostic object required')
            for k in ('code','path','message'):require(isinstance(d.get(k),str) and bool(d[k]),'INVALID_DATA',p+'.'+k,'meaningful diagnostic required')
        if v['plan_available']:
            require(v['job_status']=='COMPLETED' and v['internal_status'] in ('FEASIBLE','PARTIAL','RETURN_ONLY') and v['coverage_evaluated'] is True,'INVALID_DATA','plan_available','completed witness required')
            require(validation.get('status')=='VALIDATED' and validation.get('valid') is True and isinstance(validation.get('validator_version'),str),'INVALID_DATA','validation','witness validation required')
            require(v['business_status']==('UNSUPPORTED' if v['internal_status']=='RETURN_ONLY' else v['internal_status']),'INVALID_DATA','business_status','public mapping differs')
            if v['internal_status']=='FEASIBLE':require(bool(v['served_orders']) and not v['unserved_orders'],'INVALID_DATA','served_orders','full witness required')
            if v['internal_status']=='PARTIAL':require(bool(v['served_orders']) and bool(v['unserved_orders']),'INVALID_DATA','unserved_orders','partial requires both sets')
        else:
            require(v['coverage_evaluated'] is False and v['served_orders']==v['unserved_orders']==[],'INVALID_DATA','coverage_evaluated','no-witness is not dropped-order coverage')
            require(validation=={'status':'NOT_RUN','valid':None},'INVALID_DATA','validation','no-witness validation not run')
            if v['job_status'] in ('QUEUED','RUNNING','FAILED'):require(v['business_status'] is None and v['internal_status'] is None,'INVALID_DATA','business_status','lifecycle is not solver status')
            if v['job_status'] in ('COMPLETED','FAILED'):require(bool(v['diagnostics']),'DIAGNOSTIC_REQUIRED','diagnostics','terminal reason required')
            if v['job_status']=='COMPLETED':
                require(isinstance(v['internal_status'],str) and v['internal_status'] in ('NO_SERVICE','SEARCH_LIMIT','TIME_LIMIT','UNSUPPORTED','INVALID_DATA'),'INVALID_DATA','internal_status','known no-witness status required')
                require(v['business_status']==('UNSUPPORTED' if v['internal_status']=='NO_SERVICE' else v['internal_status']),'INVALID_DATA','business_status','public mapping differs')
    except RuntimeError as e:issues.append(e.diagnostic())
    return issues
