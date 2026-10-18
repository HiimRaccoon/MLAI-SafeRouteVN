"""Pure independent finite-domain phase/fleet validation reused for temporal /1.

Ported from the locked Step5 /2 checker. No producer, solver or evaluator
imports. Caller supplies all-column totals recomputed by raw physical checks.
"""
from decimal import Decimal, ROUND_HALF_UP
import math
from typing import Mapping
from optimization.models.member1_dynamic_state import require


def check_selection(state,domain,result,config,orders,raw_totals,*,after=None):
    columns=domain.get('columns');require(isinstance(columns,list),'INVALID_DATA','domain.columns','array required')
    normalized=[]
    urgent=set((after or {}).get('delivery_day',{}).get('urgent_ids',[]))
    for i,c in enumerate(columns):
        totals,ends=raw_totals[i]
        weights=config['profiles'][result['profile']];refs=config['references']
        score=Decimal(str(weights['time']))*Decimal(str(totals['total_travel_time_s']))/Decimal(str(refs['travel_time_s']))+Decimal(str(weights['distance']))*Decimal(str(totals['total_distance_m']))/Decimal(str(refs['distance_m']))+Decimal(str(weights['risk']))*Decimal(str(totals['total_exposure']))/Decimal(str(refs['relative_exposure_proxy']))
        normalized.append({'served':len(ends),'priority':sum(orders[x].priority for x in ends),'late':math.ceil(totals['total_soft_lateness_s']*1000),'profile':int((score*config['score_scale']).quantize(Decimal(1),rounding=ROUND_HALF_UP)),'time':math.ceil(totals['total_travel_time_s']*1000),'distance':math.ceil(totals['total_distance_m']*1000),'risk':math.ceil(totals['total_exposure']*1e6),'plan_key':i+1})
    search=result.get('search');require(isinstance(search,Mapping),'INVALID_DATA','search','metadata required')
    if urgent:
        for i,n in enumerate(normalized):n['urgent']=len(set(raw_totals[i][1])&urgent)
    require(search.get('search_complete') is False and search.get('optimality_proven') is False and search.get('truncated') is True,'GLOBAL_CLAIM','search','finite domain cannot prove global completeness/optimality')
    master=search.get('master');require(isinstance(master,Mapping),'INVALID_DATA','search.master','master metadata required')
    phases=master.get('phases');require(isinstance(phases,list),'INVALID_DATA','search.master.phases','phase array required')
    tiers=[('served',True),('priority',True),('late',False),('profile',False)]+[(x,False) for x in config['profiles'][result['profile']]['tie_break']]+[('plan_key',False)]
    if urgent:tiers.insert(0,('urgent',True))
    expected_master='task02-m1-dynamic-route-column-master/2'
    require(master.get('schema_version')==expected_master and master.get('engine')=='Google OR-Tools CP-SAT' and master.get('ortools_version')=='9.15.6755','MASTER_BINDING','search.master','engine/version differs')
    parameters=master.get('effective_parameters')
    require(isinstance(parameters,Mapping) and all(type(parameters.get(k)) is int for k in ('num_search_workers','random_seed')),'MASTER_TYPE','search.master.effective_parameters','native parameters require integer types without bool/float aliases')
    require(master.get('effective_parameters')=={'num_search_workers':1,'random_seed':0},'MASTER_BINDING','search.master.effective_parameters','effective CP-SAT parameters differ')
    require(type(master.get('seed_combinations_checked')) is int and 0<master['seed_combinations_checked']<2**63 and type(master.get('seed_truncated')) is bool,'MASTER_TYPE','search.master.seed_combinations_checked','typed bounded seed telemetry required')
    require(master.get('global_search_complete') is False and master.get('global_optimality_proven') is False,'GLOBAL_CLAIM','search.master','restricted domain is not globally complete')
    required_vehicles=set()
    if after:
        delivered={o['order_id'] for o in after['orders'] if o['status']=='DELIVERED'}
        required_vehicles={v['vehicle_id'] for v in after['vehicles'] if v['availability']=='AVAILABLE' and (v['active_commitment'] is not None or v['position']['kind']=='ON_EDGE' or v['position'].get('node_id')!=state.depot.graph_node_id or v.get('explicit_committed_stop_id') is not None and v['explicit_committed_stop_id'] not in delivered)}
    def fleet(indexes,path):
        require(isinstance(indexes,list) and all(type(i) is int and 0<=i<len(columns) for i in indexes) and len(indexes)==len(set(indexes)),'MASTER_BINDING',path,'unique column indexes required')
        picked=[columns[i] for i in indexes];vids=[c['vehicle_id'] for c in picked];ids=[o for c in picked for o in c['order_sequence']]
        require(len(vids)==len(set(vids)) and len(ids)==len(set(ids)),'FLEET_DUPLICATE',path,'vehicle/order duplication')
        require(required_vehicles.issubset(vids),'REQUIRED_RETURN_MISSING',path,'active fleet connector/return cannot disappear')
        return picked
    indexes=master.get('selected_route_indexes');picked=fleet(indexes,'search.master.selected_route_indexes')
    require(result.get('vehicle_routes')==picked,'MASTER_BINDING','vehicle_routes','selected incumbent payload differs')
    if after:
        for v in after['vehicles']:
            required=v['availability']=='AVAILABLE' and (v['active_commitment'] is not None or v['position']['kind']=='ON_EDGE' or v['position'].get('node_id')!=state.depot.graph_node_id)
            require(not required or v['vehicle_id'] in {c['vehicle_id'] for c in picked},'DEPOT_RETURN','vehicle_routes','an active available vehicle cannot silently lose its connector/return')
    locks={};known_vectors=[]
    require(len(phases)<=len(tiers),'MASTER_BINDING','search.master.phases','too many phases')
    for pi,p in enumerate(phases):
        require(isinstance(p,Mapping) and isinstance(p.get('engine_status'),str) and p['engine_status'] in ('OPTIMAL','FEASIBLE','UNKNOWN','INFEASIBLE','MODEL_INVALID'),'MASTER_TYPE','search.master.phases','native phase status required')
        for key in ('time_limit_seconds','engine_wall_time_seconds'):
            n=p.get(key)
            require(key in p and (key=='engine_wall_time_seconds' and n is None or type(n) in (int,float) and math.isfinite(float(n)) and (n>0 if key=='time_limit_seconds' else n>=0)),'MASTER_TYPE',f'search.master.phases[{pi}].{key}','typed finite native duration required')
        require((p.get('objective_key'),p.get('maximize'))==tiers[pi] and type(p.get('maximize')) is bool,'MASTER_BINDING','search.master.phases','phase ordering/direction differs')
        if p['engine_status'] not in ('OPTIMAL','FEASIBLE'):
            require(p.get('selected_route_indexes') is None and p.get('objective_value') is None,'MASTER_BINDING','search.master.phases','non-solution phase cannot claim a fleet or objective')
            require(pi==len(phases)-1,'PHASE_HISTORY','search.master.phases','native engine stops at its first non-solution phase')
            continue
        ii=p.get('selected_route_indexes');fleet(ii,'search.master.phases.selected_route_indexes');k=p.get('objective_key')
        require(isinstance(k,str) and k in ('urgent','served','priority','late','profile','time','distance','risk','plan_key'),'MASTER_BINDING','search.master.phases.objective_key','objective key required')
        require(type(p.get('objective_value')) is int and p['objective_value']==sum(normalized[i][k] for i in ii),'OBJECTIVE_BINDING','search.master.phases.objective_value','raw integer phase value differs')
        for locked,val in locks.items():require(sum(normalized[i][locked] for i in ii)==val,'PHASE_LOCK','search.master.phases','prior optimal objective changed')
        known_vectors.append(tuple(sum(normalized[i][key] for i in ii)*(1 if maximize else -1) for key,maximize in tiers))
        if p['engine_status']=='OPTIMAL':locks[k]=p['objective_value']
    vector=[sum(normalized[i][k] for i in indexes)*(1 if maximize else -1) for k,maximize in tiers]
    claimed=master.get('incumbent_vector')
    require(isinstance(claimed,list) and len(claimed)==len(tiers) and all(type(x) is int and -(2**63-1)<=x<=2**63-1 for x in claimed),'INCUMBENT_BINDING','search.master.incumbent_vector','bounded integer vector required')
    require(claimed==vector,'INCUMBENT_BINDING','search.master.incumbent_vector','raw selected vector differs')
    require(all(tuple(vector)>=known for known in known_vectors),'INCUMBENT_REGRESSION','search.master.incumbent_vector','retained witness is weaker than a recorded feasible phase')
    origin=master.get('incumbent_origin')
    require(isinstance(origin,str) and (origin=='AUTHENTICATED_DOMAIN_SEED' or origin in {'ENGINE_PHASE:'+k for k,_ in tiers}),'INCUMBENT_BINDING','search.master.incumbent_origin','origin required')
    if origin.startswith('ENGINE_PHASE:'):
        require(any(p['objective_key']==origin.split(':',1)[1] and p['engine_status'] in ('OPTIMAL','FEASIBLE') and p['selected_route_indexes']==indexes for p in phases),'INCUMBENT_BINDING','search.master.incumbent_origin','engine origin must reference the retained recorded fleet')
    for k,val in locks.items():require(sum(normalized[i][k] for i in indexes)==val,'PHASE_LOCK','search.master.selected_route_indexes','retained selection differs from optimal phase')
    termination='RESTRICTED_PHASES_OPTIMAL' if len(phases)==len(tiers) and all(p['engine_status']=='OPTIMAL' for p in phases) else 'BEST_KNOWN_INCUMBENT_RETAINED'
    require(master.get('termination_reason')==termination,'TERMINATION_BINDING','search.master.termination_reason','termination inconsistent')
    return picked

