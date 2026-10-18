"""Internal Step5 DTOs. No changes to DS2/Motion1/Event1/public API v1."""
from dataclasses import dataclass
import hashlib
import json
import math
from collections.abc import Mapping
from optimization.models.common import freeze_json, to_json_value, ContractValidationError
from optimization.models.motion_state import _instant,_validate_position,MotionContractError

STATE_VERSION='task02-m1-dynamic-state/1'
TRANSITION_VERSION='task02-m1-event-transition/1'
RESULT_VERSION='task02-m1-dynamic-result/2'
OBJECTIVE_VERSION='task02-m1-dynamic-objective/1'
SOLVER_VERSION='task02-m1-dynamic-turn-route-columns/2'
SUPPORTED_SOLVERS=('task02-m1-dynamic-turn-route-columns/1',SOLVER_VERSION)

class DynamicError(ValueError):
    def __init__(self,code,path,message):
        super().__init__(message);self.code,self.path=code,path
    def diagnostic(self):
        return {'severity':'ERROR','code':self.code,'path':self.path,'message':str(self)}

def finite_tree(value,path='$',_active=None,_depth=0):
    if _depth>128:raise DynamicError('INVALID_DATA',path,'JSON nesting exceeds bound')
    # Most leaves are scalars. Avoid ABC checks and allocating sets for them.
    if value is None or type(value) in (str,bool):return
    if type(value) in (int,float):
        try:n=float(value)
        except OverflowError as e:raise DynamicError('INVALID_DATA',path,'numeric overflow') from e
        if not math.isfinite(n):raise DynamicError('INVALID_DATA',path,'finite JSON numbers required')
        return
    active=set() if _active is None else _active
    if type(value) is dict or isinstance(value,Mapping):
        if id(value) in active:raise DynamicError('INVALID_DATA',path,'cyclic object is not JSON')
        active.add(id(value))
        for k,v in value.items():
            if not isinstance(k,str):raise DynamicError('INVALID_DATA',path,'string JSON keys required')
            finite_tree(v,f'{path}.{k}' if path else k,active,_depth+1)
        active.remove(id(value))
    elif isinstance(value,(list,tuple)):
        if id(value) in active:raise DynamicError('INVALID_DATA',path,'cyclic array is not JSON')
        active.add(id(value))
        for i,v in enumerate(value):finite_tree(v,f'{path}[{i}]',active,_depth+1)
        active.remove(id(value))
    elif type(value) in (int,float):
        try:n=float(value)
        except OverflowError as e:raise DynamicError('INVALID_DATA',path,'numeric overflow') from e
        if not math.isfinite(n):raise DynamicError('INVALID_DATA',path,'finite JSON numbers required')
    elif value is not None and type(value) not in (str,bool):raise DynamicError('INVALID_DATA',path,'JSON value required')

def canonical(value):
    finite_tree(value)
    try:return json.dumps(to_json_value(value),sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    except (ValueError,OverflowError,TypeError) as e:raise DynamicError('INVALID_DATA','$','canonical JSON failed') from e

def sha(value):return hashlib.sha256(canonical(value)).hexdigest()
def require(ok,code,path,msg):
    if not ok:raise DynamicError(code,path,msg)

def finalize(value):
    body=dict(value);body.pop('content_sha256',None);body['content_sha256']=sha(body);return body

def observed_fields(value):
    """Typed observed fields only; raw/event/authority certification is separate."""
    orders={};vehicles={}
    for key in ('orders','vehicles'):
        require(isinstance(value.get(key),list),'INVALID_DATA',key,'object array required')
        for i,item in enumerate(value[key]):
            p=f'{key}[{i}]';require(isinstance(item,Mapping),'INVALID_DATA',p,'object required')
            name='order_id' if key=='orders' else 'vehicle_id';identity=item.get(name)
            target=orders if key=='orders' else vehicles
            require(isinstance(identity,str) and bool(identity),'INVALID_DATA',p+'.'+name,'nonempty identifier required')
            require(identity not in target,'INVALID_DATA',p+'.'+name,'duplicate identifier');target[identity]=item
            numeric=('demand_kg',) if key=='orders' else ('current_load_kg','capacity_kg','remaining_range_m')
            for n in numeric:require(type(item.get(n)) in (int,float) and item[n]>=0,'INVALID_DATA',p+'.'+n,'nonnegative finite number required')
            if key=='orders':
                status=item.get('status');require(isinstance(status,str) and status in ('WAITING','ONBOARD','DELIVERED'),'INVALID_DATA',p+'.status','known status required')
                if status=='WAITING':require(all(item.get(n) is None for n in ('owner_vehicle_id','picked_up_at','delivered_at')),'INVALID_DATA',p,'WAITING cannot have custody history')
                else:
                    require(isinstance(item.get('owner_vehicle_id'),str) and bool(item['owner_vehicle_id']),'INVALID_DATA',p+'.owner_vehicle_id','custody owner required')
                    for n in (('picked_up_at','delivered_at') if status=='DELIVERED' else ('picked_up_at',)):
                        try:_instant(item.get(n),p+'.'+n)
                        except MotionContractError as e:raise DynamicError(e.code,e.path,str(e)) from e
                    if status=='ONBOARD':require(item.get('delivered_at') is None,'INVALID_DATA',p+'.delivered_at','ONBOARD is not delivered')
            else:
                availability=item.get('availability');require(isinstance(availability,str) and availability in ('AVAILABLE','UNAVAILABLE'),'INVALID_DATA',p+'.availability','known availability required')
                try:_validate_position(item.get('position'),p+'.position');_instant(item.get('position_timestamp'),p+'.position_timestamp')
                except MotionContractError as e:raise DynamicError(e.code,e.path,str(e)) from e
                for n in ('onboard_order_ids','planned_suffix','completed_stops'):
                    ids=item.get(n);require(isinstance(ids,list) and all(isinstance(x,str) and bool(x) for x in ids) and len(ids)==len(set(ids)),'INVALID_DATA',p+'.'+n,'unique string identifiers required')
                commit=item.get('active_commitment')
                if commit is not None:
                    require(isinstance(commit,Mapping),'INVALID_DATA',p+'.active_commitment','commitment object required')
                    require(isinstance(commit.get('kind'),str) and commit['kind'] in ('EDGE','SERVICE'),'INVALID_DATA',p+'.active_commitment.kind','known commitment kind required')
                    n='edge_id' if commit['kind']=='EDGE' else 'order_id'
                    require(isinstance(commit.get(n),str) and bool(commit[n]),'INVALID_DATA',p+'.active_commitment.'+n,'committed identity required')
                    require(type(commit.get('until_us')) is int and 0<=commit['until_us']<2**63,'INVALID_DATA',p+'.active_commitment.until_us','bounded integer required')
                commit=item.get('explicit_committed_stop_id')
                require(commit is None or isinstance(commit,str) and bool(commit),'INVALID_DATA',p+'.explicit_committed_stop_id','null or nonempty stop identifier required')
    for i,v in enumerate(value['vehicles']):
        ids={oid for oid,o in orders.items() if o['status']=='ONBOARD' and o['owner_vehicle_id']==v['vehicle_id']}
        require(set(v['onboard_order_ids'])==ids,'CUSTODY_OWNER',f'vehicles[{i}].onboard_order_ids','observed cargo and owner differ')
        require(abs(v['current_load_kg']-sum(orders[oid]['demand_kg'] for oid in ids))<=1e-9 and v['current_load_kg']<=v['capacity_kg']+1e-9,'CAPACITY',f'vehicles[{i}].current_load_kg','observed load/capacity differs from cargo')
    require(all(o['owner_vehicle_id'] in vehicles for o in orders.values() if o['status']!='WAITING'),'CUSTODY_OWNER','orders.owner_vehicle_id','unknown owner')

@dataclass(frozen=True)
class DynamicState:
    payload:Mapping
    @classmethod
    def from_dict(cls,value):
        require(isinstance(value,Mapping),'INVALID_DATA','$','object required');finite_tree(value)
        require(value.get('schema_version')==STATE_VERSION,'VERSION_MISMATCH','schema_version','dynamic state /1 required')
        require(type(value.get('head_version')) is int and 0<value['head_version']<2**63,'INVALID_DATA','head_version','bounded integer required')
        require(isinstance(value.get('scenario_id'),str) and value['scenario_id'] in ('S2','S3'),'INVALID_DATA','scenario_id','S2/S3 required')
        for k in ('orders','vehicles','execution_history','applied_event_ids'):
            require(isinstance(value.get(k),list),'INVALID_DATA',k,'array required')
        try:_instant(value.get('current_time'),'current_time')
        except ValueError as e:raise DynamicError('INVALID_DATA','current_time',str(e)) from e
        material=dict(value);material.pop('content_sha256',None)
        require(value.get('content_sha256')==sha(material),'STATE_DIGEST','content_sha256','state content differs')
        observed_fields(value)
        require(type(value.get('state_version')) is int and value['state_version']==value['head_version'],'INVALID_DATA','state_version','observed and head version must agree')
        ids=value['applied_event_ids'];require(all(isinstance(x,str) and bool(x) for x in ids) and len(ids)==len(set(ids)),'INVALID_DATA','applied_event_ids','unique event IDs required')
        try:return cls(freeze_json(value))
        except ContractValidationError as e:raise DynamicError('INVALID_DATA','$',str(e)) from e
    def to_dict(self):return to_json_value(self.payload)

@dataclass(frozen=True)
class EventTransition:
    payload:Mapping
    @classmethod
    def from_dict(cls,value):
        require(isinstance(value,Mapping),'INVALID_DATA','transition','object required');finite_tree(value,'transition')
        require(value.get('schema_version')==TRANSITION_VERSION,'VERSION_MISMATCH','transition.schema_version','transition /1 required')
        require(value.get('status')=='APPLIED','INVALID_DATA','transition.status','applied receipt required')
        for key in ('before_version','after_version'):
            require(type(value.get(key)) is int and 0<value[key]<2**63,'INVALID_DATA','transition.'+key,'bounded version required')
        require(value['after_version']==value['before_version']+1,'TRANSITION_BINDING','transition.after_version','one atomic increment required')
        for key in ('authority_id','event_id','transaction_id','policy_id'):
            require(isinstance(value.get(key),str) and bool(value[key]),'INVALID_DATA','transition.'+key,'nonempty identifier required')
        for key in ('authority_sha256','event_sha256','before_state_sha256','after_state_sha256','policy_sha256'):
            digest(value.get(key),'transition.'+key)
        DynamicState.from_dict(value.get('after_state'))
        require(value['after_state_sha256']==value['after_state']['content_sha256'],'TRANSITION_BINDING','transition.after_state_sha256','content differs')
        return cls(freeze_json(value))
    def to_dict(self):return to_json_value(self.payload)

def digest(value,path):
    require(isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value),'INVALID_DATA',path,'SHA-256 required')

@dataclass(frozen=True)
class DynamicResult:
    payload:Mapping
    @classmethod
    def from_dict(cls,value):
        require(isinstance(value,Mapping),'INVALID_DATA','$','result object required');finite_tree(value,'')
        require(value.get('schema_version')==RESULT_VERSION,'VERSION_MISMATCH','schema_version','dynamic result /2 required')
        require(isinstance(value.get('status'),str) and value['status'] in ('FEASIBLE','PARTIAL','NO_SERVICE','SEARCH_LIMIT','TIME_LIMIT'),'INVALID_DATA','status','internal business status required')
        require(isinstance(value.get('profile'),str) and value['profile'] in ('FASTEST','BALANCED','SAFER'),'INVALID_DATA','profile','locked profile required')
        for key,expected in [('solver_version',SUPPORTED_SOLVERS),('objective_version',(OBJECTIVE_VERSION,))]:
            require(isinstance(value.get(key),str),'INVALID_DATA',key,'version string required')
            require(value[key] in expected,'VERSION_MISMATCH',key,'unsupported declared version')
        require('post_event_state' in value and value['post_event_state'] is None,'FORECAST_OBSERVATION','post_event_state','forecast must not claim an observed post-event state')
        require(isinstance(value.get('metric_scope'),str) and value['metric_scope']=='SUFFIX_ONLY','METRIC_SCOPE_BINDING','metric_scope','metrics represent the physical suffix; whole metrics are separate')
        if 'whole_trajectory_metrics' in value:require(isinstance(value['whole_trajectory_metrics'],Mapping),'INVALID_DATA','whole_trajectory_metrics','whole metric object required')
        for key in ('served_orders','actual_delivered','planned_served','unserved_orders','vehicle_routes'):
            require(isinstance(value.get(key),list),'INVALID_DATA',key,'array required')
        for key in ('served_orders','actual_delivered','planned_served'):
            ids=value[key]
            require(all(isinstance(x,str) and bool(x) for x in ids),'INVALID_DATA',key,'string identifiers required')
            require(len(ids)==len(set(ids)),'COVERAGE',key,'duplicate identifiers')
        for key in ('domain_sha256','initial_state_sha256'):digest(value.get(key),key)
        require(isinstance(value.get('search'),Mapping),'INVALID_DATA','search','search metadata required')
        require(isinstance(value.get('metrics'),Mapping),'INVALID_DATA','metrics','metrics object required')
        return cls(freeze_json(value))
    def to_dict(self):return to_json_value(self.payload)
