"""Raw-SQLite feasibility and phase checks for new runtime job results.

No planner, master, replay producer or forecast sampler imports. Legacy
independent physical/phase primitives are reused without changing defaults.
"""
from collections.abc import Mapping
from optimization.models.member1_dynamic_state import DynamicError,finite_tree
from optimization.models.motion_state import MotionContractError
from optimization.integration.member1_s0_graph import RoadDataError
from optimization.rolling_horizon.member1_dynamic_validation import route_check,close
from optimization.rolling_horizon.member1_column_validation import check_selection
from .protocol import require,RuntimeError
from optimization.models.member1_dynamic_state import sha as domain_sha
from optimization.models.decision_state import OrderState
from fractions import Fraction
from threading import Lock
from optimization.rolling_horizon.member1_rain_validation import independent_segments,validate_overlay
from optimization.rolling_horizon.member1_rain_source import load_source_model

VERSION='task02-m2-runtime-raw-witness-validator/1'

class RawValidationCache:
    """Operation-local raw checks, never supplied by a worker/transport.

    The caller still checks the actual domain digest on every validation.
    Profile phase, selection, coverage and metrics are always checked anew.
    """
    def __init__(self):
        self.lock = Lock()
        self.rows = {}

    def checked(self, key, producer):
        with self.lock:
            if key not in self.rows:self.rows[key] = producer()
            return self.rows[key]


def validate_runtime_result(state,graph,domain,result,config,*,anchor=None,snapshot=None,overlay=None,_raw_cache=None):
    """New runtime /2 witness checker, preserving the historical /1 reader.

    Validates ALL domain columns and exact selected phase/fleet from raw source.
    Return-only is a supplemental action plan, not NO_SERVICE/public API v1.
    """
    from .delivery_day import effective_state
    state=effective_state(state,anchor)
    if isinstance(result,Mapping) and result.get('schema_version')=='task02-m2-runtime-witness/1':return validate_initial_result(state,graph,domain,result,config)
    issues=[]
    try:
        require(isinstance(result,Mapping),'INVALID_DATA','result','object required');finite_tree(result)
        require(isinstance(result.get('metrics'),Mapping),'INVALID_DATA','metrics','metric object required')
        require(isinstance(domain,Mapping),'INVALID_DATA','domain','object required')
        require(anchor is None or isinstance(anchor,Mapping),'INVALID_DATA','anchor','trusted physical frame required')
        require((state.routing_version,state.features_version,state.context_version)==(graph.routing_version,graph.features_version,graph.context_version),'SOURCE_BINDING','source_versions','state and raw source versions differ')
        require(result.get('schema_version')=='task02-m2-runtime-witness/2' and result.get('solver_version')=='task02-m2-runtime-column-compute/2','VERSION_MISMATCH','schema_version','runtime /2 required')
        require(result.get('scenario_id')==state.scenario_id and result.get('source_hashes')==graph.source_hashes,'SOURCE_BINDING','source_hashes','initial/source identity differs')
        material=dict(domain);material.pop('content_sha256',None)
        require(domain.get('content_sha256')==domain_sha(material)==result.get('domain_sha256'),'DOMAIN_BINDING','domain_sha256','domain content differs')
        require(domain.get('initial_state_sha256')==domain_sha(state.to_dict()) and domain.get('scenario_id')==state.scenario_id and domain.get('source_hashes')==graph.source_hashes,'DOMAIN_BINDING','domain.initial_state_sha256','domain initial/source differs')
        require(domain.get('source_versions')=={'routing':graph.routing_version,'features':graph.features_version,'context':graph.context_version},'SOURCE_BINDING','domain.source_versions','domain raw context differs')
        require(domain.get('anchor_state_sha256')==(anchor.get('content_sha256') if anchor else None),'DOMAIN_BINDING','domain.anchor_state_sha256','physical anchor differs')
        require(domain.get('schema_version') in ('task02-m1-dynamic-physical-domain/1','task02-m1-temporal-rain-domain/1') if isinstance(domain.get('schema_version'),str) else False,'VERSION_MISMATCH','domain.schema_version','reviewed physical domain required')
        require(domain.get('finite_path_domain') is True and domain.get('coverage_complete') is False,'GLOBAL_CLAIM','domain.coverage_complete','finite domain is not globally complete')
        require(result.get('anchor_sha256')==(anchor.get('content_sha256') if anchor else None) and result.get('overlay_sha256')==(overlay.get('content_sha256') if overlay else None),'ANCHOR_BINDING','anchor_sha256','result anchor/overlay differs')
        columns=domain.get('columns');require(isinstance(columns,list),'INVALID_DATA','domain.columns','array required')
        known_vehicles={v.vehicle_id for v in state.vehicles}
        for i,c in enumerate(columns):
            path=f'domain.columns[{i}]';require(isinstance(c,Mapping),'INVALID_DATA',path,'object required')
            require(isinstance(c.get('vehicle_id'),str) and c['vehicle_id'] in known_vehicles,'INVALID_DATA',path+'.vehicle_id','known source vehicle ID required')
        require(isinstance(result.get('profile'),str) and result['profile'] in config['profiles'],'INVALID_DATA','profile','locked profile required')
        orders={o.order_id:o for o in state.orders}
        if anchor and anchor.get('event_order'):o=OrderState.from_dict(anchor['event_order']);orders[o.order_id]=o
        model=None
        if overlay is not None:model=load_source_model(snapshot,state);validate_overlay(state,graph,model,overlay)
        def raw_column(column):
            def temporal(edge,a,start,progress,incoming,path):
                exact=Fraction(a.get('fraction_start_exact','0'))
                wanted_progress=Fraction('0')
                if start==offset_local and vehicle_anchor['position']['kind']=='ON_EDGE':wanted_progress=Fraction(vehicle_anchor['position']['progress_exact'])
                require(exact==wanted_progress,'TEMPORAL_PROGRESS',path+'.fraction_start_exact','exact physical anchor differs')
                wanted=independent_segments(model.ingredients(graph,edge['edgeId']),overlay,edge['edgeId'],start,exact,incoming)
                require(a.get('overlay_sha256')==overlay['content_sha256'],'OVERLAY_BINDING',path,'temporal overlay differs')
                close(a.get('temporal_segments'),wanted,path+'.temporal_segments')
                return {'duration_us':wanted[-1]['end_us']-start,'distance_m':sum(s['distance_m'] for s in wanted),'exposure':sum(s['exposure'] for s in wanted)}
            from .protocol import offset
            offset_local=offset(state.decision_epoch,anchor['current_time']) if anchor else 0
            vehicle_anchor=next(v for v in anchor['vehicles'] if v['vehicle_id']==column['vehicle_id']) if anchor else None
            return route_check(state,graph,column,orders,after=anchor,_temporal_edge_check=temporal if overlay is not None else None)
        raw_key=(domain['content_sha256'],domain_sha(state.to_dict()),domain_sha(anchor),domain_sha(overlay),domain_sha(graph.source_hashes))
        raw=([raw_column(c) for c in columns] if _raw_cache is None else
             _raw_cache.checked(raw_key,lambda:[raw_column(c) for c in columns]))
        picked=check_selection(state,domain,result,config,orders,raw,after=anchor)
        actual=sorted(o['order_id'] for o in anchor['orders'] if o['status']=='DELIVERED') if anchor else []
        suffix=[o for r in picked for o in r['order_sequence']]
        require(len(suffix)==len(set(suffix)) and not set(suffix)&set(actual),'COVERAGE','vehicle_routes','duplicate already-delivered service')
        served=sorted(actual+suffix);require(result.get('served_orders')==served,'COVERAGE','served_orders','prefix/suffix served differs')
        unserved=result.get('unserved_orders');require(isinstance(unserved,list),'INVALID_DATA','unserved_orders','array required')
        ids=[]
        for i,item in enumerate(unserved):
            p=f'unserved_orders[{i}]';require(isinstance(item,Mapping),'INVALID_DATA',p,'object required')
            oid=item.get('order_id');require(isinstance(oid,str) and oid in orders,'INVALID_DATA',p+'.order_id','known order required');ids.append(oid)
            reason=item.get('reason');require(isinstance(reason,str) and reason in ('CUSTODY_BLOCKED','SEARCH_INCOMPLETE','NOT_SERVED_BY_FOUND_WITNESS'),'UNSERVED_REASON',p+'.reason','scoped allowed reason required')
            if reason=='CUSTODY_BLOCKED':
                o=next(x for x in anchor['orders'] if x['order_id']==oid)
                require(o['status']=='ONBOARD' and item.get('owner_vehicle_id')==o['owner_vehicle_id'] and any(v['vehicle_id']==o['owner_vehicle_id'] and v['availability']=='UNAVAILABLE' for v in anchor['vehicles']),'CUSTODY_PROOF',p+'.reason','owner/load cannot be transferred')
        require(len(ids)==len(set(ids)) and not set(ids)&set(served) and set(ids)|set(served)==set(orders),'COVERAGE','unserved_orders','complete disjoint partition required')
        status='FEASIBLE' if not ids else 'PARTIAL' if served else 'RETURN_ONLY'
        require(result.get('status')==status and bool(picked or actual),'STATUS_COVERAGE','status','witness coverage differs')
        totals={k:sum(c[k] for c in picked) for k in ('total_distance_m','total_travel_time_s','total_exposure','total_cost_vnd','total_soft_lateness_s')}
        for k,v in totals.items():close(result['metrics'].get(k),v,'metrics.'+k)
        require(result.get('forecast') is True and result.get('post_event_state') is None and result.get('metric_scope')=='PLANNED_SUFFIX_ONLY','FORECAST_OBSERVATION','metric_scope','plan not observation')
    except (RuntimeError,DynamicError,RoadDataError,MotionContractError) as e:issues.append({'severity':'ERROR','code':getattr(e,'code','RAW_SOURCE_INVALID'),'path':getattr(e,'path','$'),'message':str(e)})
    return {'validator_version':'task02-m2-runtime-raw-witness-validator/3','valid':not issues,'diagnostics':issues,'scope':'RAW_ALL_COLUMN_ANCHOR_FLEET_PHASE_AND_SUFFIX; NOT_GLOBAL_OPTIMALITY'}

def validate_initial_result(state,graph,domain,result,config):
    issues=[]
    try:
        require(isinstance(result,Mapping),'INVALID_DATA','result','object required');finite_tree(result)
        require(result.get('scenario_id')==state.scenario_id,'SOURCE_BINDING','scenario_id','source scenario differs')
        require(result.get('source_hashes')==graph.source_hashes,'SOURCE_BINDING','source_hashes','raw source differs')
        require(isinstance(domain,Mapping),'INVALID_DATA','domain','physical domain object required')
        material=dict(domain);material.pop('content_sha256',None)
        require(domain.get('content_sha256')==domain_sha(material),'DOMAIN_BINDING','domain.content_sha256','domain content digest differs')
        require(domain.get('initial_state_sha256')==domain_sha(state.to_dict()) and domain.get('scenario_id')==state.scenario_id,'DOMAIN_BINDING','domain.initial_state_sha256','domain belongs to a different initial state')
        require(domain.get('source_hashes')==graph.source_hashes and domain.get('source_versions')=={'routing':graph.routing_version,'features':graph.features_version,'context':graph.context_version},'SOURCE_BINDING','domain.source_versions','domain uses different source/version')
        require(domain.get('anchor_state_sha256') is None,'DOMAIN_BINDING','domain.anchor_state_sha256','initial worker cannot use a post-event physical anchor')
        require(result.get('status') in ('FEASIBLE','PARTIAL') if isinstance(result.get('status'),str) else False,'STATUS_COVERAGE','status','witness status required')
        require(result.get('domain_sha256')==domain.get('content_sha256'),'DOMAIN_BINDING','domain_sha256','physical domain differs')
        profile=result.get('profile');require(isinstance(profile,str) and profile in config['profiles'],'INVALID_DATA','profile','locked profile required')
        columns=domain.get('columns');require(isinstance(columns,list),'INVALID_DATA','domain.columns','column array required')
        orders={o.order_id:o for o in state.orders}
        raw=[route_check(state,graph,c,orders) for c in columns]
        picked=check_selection(state,domain,result,config,orders,raw)
        served=[o for r in picked for o in r['order_sequence']]
        require(len(served)==len(set(served)) and result.get('served_orders')==sorted(served),'COVERAGE','served_orders','unique served identities differ')
        unserved=result.get('unserved_orders');require(isinstance(unserved,list),'INVALID_DATA','unserved_orders','array required')
        ids=[]
        for i,o in enumerate(unserved):
            p=f'unserved_orders[{i}]';require(isinstance(o,Mapping),'INVALID_DATA',p,'object required')
            oid=o.get('order_id');reason=o.get('reason')
            require(isinstance(oid,str) and oid in orders,'INVALID_DATA',p+'.order_id','known order required');ids.append(oid)
            require(reason=='NOT_SERVED_BY_FOUND_WITNESS','UNSERVED_REASON',p+'.reason','initial runtime currently makes only weak absence claims')
        require(len(ids)==len(set(ids)) and not set(ids)&set(served) and set(ids)|set(served)==set(orders),'COVERAGE','unserved_orders','partition must cover all orders')
        require(result['status']==('FEASIBLE' if not ids else 'PARTIAL') and bool(served),'STATUS_COVERAGE','status','full/partial witness coverage differs')
        totals={k:sum(c[k] for c in picked) for k in ('total_distance_m','total_travel_time_s','total_exposure','total_cost_vnd','total_soft_lateness_s')}
        require(isinstance(result.get('metrics'),Mapping),'INVALID_DATA','metrics','metric object required')
        for k,v in totals.items():close(result['metrics'].get(k),v,'metrics.'+k)
        require(result.get('post_event_state') is None and result.get('forecast') is True,'FORECAST_OBSERVATION','post_event_state','a computed plan is not observed execution')
    except (RuntimeError,DynamicError,RoadDataError,MotionContractError) as e:
        issues.append({'severity':'ERROR','code':getattr(e,'code','RAW_SOURCE_INVALID'),'path':getattr(e,'path','$'),'message':str(e)})
    return {'validator_version':VERSION,'valid':not issues,'diagnostics':issues,'scope':'INITIAL_RAW_ALL_COLUMN_FEASIBILITY_AND_PHASE_CONSISTENCY; NOT_GLOBAL_OPTIMALITY'}
