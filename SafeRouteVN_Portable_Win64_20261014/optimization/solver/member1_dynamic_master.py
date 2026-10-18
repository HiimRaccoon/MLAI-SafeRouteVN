"""OR-Tools selection over one authenticated dynamic route-column domain.

Objective /1: coverage, priority, lateness, fixed Step3 profile, tie breaks.
Retained seeds and engine incumbents use the same integer lexicographic tuple.
No phase locks a merely FEASIBLE value out of a stronger known witness.
"""
from itertools import product
from typing import Mapping
from time import monotonic
import math
from optimization.solver.member1_profile_master import scaled_profile_score,INT64_MAX
from optimization.models.member1_dynamic_state import DynamicError,require,finite_tree

VERSION='task02-m1-dynamic-route-column-master/2'

def select_columns(columns,order_priorities,vehicle_ids,profile,config,*,seconds=30,required_vehicle_ids=(),_progress=None,urgent_order_ids=()):
    # Change Impact Analysis: private optional runtime checkpoint observer.
    # Default None preserves all reviewed legacy output/objective/phase behavior.
    import ortools
    from ortools.sat.python import cp_model
    require(type(seconds) in (int,float),'INVALID_DATA','master_seconds','positive finite budget')
    try:valid_seconds=math.isfinite(float(seconds)) and seconds>0
    except OverflowError:valid_seconds=False
    require(valid_seconds,'INVALID_DATA','master_seconds','positive finite budget')
    deadline=monotonic()+seconds
    require(isinstance(columns,list) and all(isinstance(c,Mapping) for c in columns),'INVALID_DATA','columns','column object array required')
    require(isinstance(order_priorities,Mapping) and all(isinstance(k,str) and bool(k) and type(v) is int and 0<v<=INT64_MAX for k,v in order_priorities.items()),'INVALID_DATA','order_priorities','bounded typed priority map required')
    require(isinstance(vehicle_ids,list) and all(isinstance(x,str) and bool(x) for x in vehicle_ids) and len(vehicle_ids)==len(set(vehicle_ids)),'INVALID_DATA','vehicle_ids','unique vehicle IDs required')
    require(isinstance(required_vehicle_ids,(list,tuple)) and all(isinstance(x,str) and x in vehicle_ids for x in required_vehicle_ids) and len(required_vehicle_ids)==len(set(required_vehicle_ids)),'INVALID_DATA','required_vehicle_ids','unique known required vehicle IDs needed')
    required=set(required_vehicle_ids)
    urgent=set(urgent_order_ids)
    for vid in required:require(any(c.get('vehicle_id')==vid for c in columns),'REQUIRED_RETURN_NOT_FOUND','columns','no physical column for active vehicle '+vid)
    require(isinstance(profile,str) and profile in ('FASTEST','BALANCED','SAFER'),'INVALID_DATA','profile','locked profile required')
    ties=config['profiles'][profile]['tie_break']
    normalized=[]
    for i,c in enumerate(columns):
        finite_tree(c,f'columns[{i}]')
        require(isinstance(c.get('vehicle_id'),str) and c['vehicle_id'] in vehicle_ids,'DOMAIN_BINDING',f'columns[{i}].vehicle_id','known vehicle required')
        for key,scale in [('total_soft_lateness_s',1000),('total_travel_time_s',1000),('total_distance_m',1000),('total_exposure',1000000)]:
            value=c.get(key)
            require(type(value) in (int,float) and 0<=value<=INT64_MAX/scale,'OBJECTIVE_OVERFLOW',f'columns[{i}].{key}','nonnegative int64-scalable coefficient required')
        seq=c.get('order_sequence');require(isinstance(seq,list) and all(isinstance(x,str) and bool(x) for x in seq),'INVALID_DATA',f'columns[{i}].order_sequence','order ID array required')
        require(len(seq)==len(set(seq)) and all(x in order_priorities for x in seq),'DOMAIN_BINDING',f'columns[{i}].order_sequence','unique known orders required')
        n={'served':len(seq),'priority':sum(order_priorities[x] for x in seq),'late':math.ceil(c['total_soft_lateness_s']*1000),
            'profile':scaled_profile_score(c,config['profiles'][profile],config['references'],config['score_scale']),
            'time':math.ceil(c['total_travel_time_s']*1000),'distance':math.ceil(c['total_distance_m']*1000),
            'risk':math.ceil(c['total_exposure']*1e6),'plan_key':i+1}
        if urgent:n['urgent']=len(set(seq)&urgent)
        require(all(type(x) is int and 0<=x<=INT64_MAX for x in n.values()),'OBJECTIVE_OVERFLOW',f'columns[{i}]','int64 coefficients required')
        normalized.append(n)
    tiers=[('served',True),('priority',True),('late',False),('profile',False)]+[(x,False) for x in ties]+[('plan_key',False)]
    if urgent:tiers.insert(0,('urgent',True))
    for key,_ in tiers:require(sum(n[key] for n in normalized)<=INT64_MAX,'OBJECTIVE_OVERFLOW',key,'aggregate coefficient overflow')
    def vector(indexes):return tuple(sum(normalized[i][k] for i in indexes)*(1 if maximize else -1) for k,maximize in tiers)
    best=None;bestvec=None;seed_count=0;seed_truncated=False
    opts=[([] if v in required else [None])+[i for i,c in enumerate(columns) if c['vehicle_id']==v] for v in sorted(vehicle_ids)]
    for choices in product(*opts):
        seed_count+=1
        if seed_count>300000 or seed_count%256==0 and monotonic()>=deadline:seed_truncated=True;break
        indexes=sorted(i for i in choices if i is not None)
        served=[o for i in indexes for o in columns[i]['order_sequence']]
        if len(served)!=len(set(served)):continue
        v=vector(indexes)
        if bestvec is None or v>bestvec:best,bestvec=indexes,v
    model=cp_model.CpModel();selected=[model.NewBoolVar(f'column_{i}') for i in range(len(columns))]
    for vid in vehicle_ids:
        count=sum(selected[i] for i,c in enumerate(columns) if c['vehicle_id']==vid)
        model.Add(count==1 if vid in required else count<=1)
    for oid in order_priorities:model.Add(sum(selected[i] for i,c in enumerate(columns) if oid in c['order_sequence'])<=1)
    phases=[];origin='AUTHENTICATED_DOMAIN_SEED'
    def record():
        return {'schema_version':VERSION,'engine':'Google OR-Tools CP-SAT','ortools_version':ortools.__version__,
        'effective_parameters':{'num_search_workers':1,'random_seed':0},
        'selected_route_indexes':best or [],'incumbent_origin':origin,'incumbent_vector':list(bestvec or ()),
        'phases':__import__('copy').deepcopy(phases),'seed_combinations_checked':seed_count,'seed_truncated':seed_truncated,
        'global_search_complete':False,'global_optimality_proven':False,
        'termination_reason':'RESTRICTED_PHASES_OPTIMAL' if len(phases)==len(tiers) and all(p['engine_status']=='OPTIMAL' for p in phases) else 'BEST_KNOWN_INCUMBENT_RETAINED'}
    if _progress is not None and best is not None:_progress(record())
    for pi,(key,maximize) in enumerate(tiers):
        remaining=deadline-monotonic()
        if remaining<=0:break
        expression=sum(n[key]*selected[i] for i,n in enumerate(normalized))
        if best is not None:
            val=sum(normalized[i][key] for i in best)
            model.Add(expression>=val if maximize else expression<=val)
            model.ClearHints()
            for i,var in enumerate(selected):model.AddHint(var,int(i in best))
        model.Maximize(expression) if maximize else model.Minimize(expression)
        solver=cp_model.CpSolver();solver.parameters.max_time_in_seconds=remaining/(len(tiers)-pi)
        solver.parameters.num_search_workers=1;solver.parameters.random_seed=0
        status=solver.Solve(model);native=solver.StatusName(status)
        phase={'objective_key':key,'maximize':maximize,'engine_status':native,'selected_route_indexes':None,'objective_value':None,'time_limit_seconds':solver.parameters.max_time_in_seconds,'engine_wall_time_seconds':solver.WallTime() if hasattr(solver,'WallTime') else None}
        phases.append(phase)
        if status not in (cp_model.FEASIBLE,cp_model.OPTIMAL):
            if _progress is not None and best is not None:_progress(record())
            break
        found=[i for i,x in enumerate(selected) if solver.Value(x)]
        phase.update(selected_route_indexes=found,objective_value=int(solver.Value(expression)))
        if bestvec is None or vector(found)>bestvec:best,bestvec=found,vector(found);origin=f'ENGINE_PHASE:{key}'
        if status==cp_model.OPTIMAL:model.Add(expression==phase['objective_value'])
        if _progress is not None and best is not None:_progress(record())
    require(best is not None or not required,'SEARCH_INCOMPLETE','selected_route_indexes','no complete required fleet retained in finite domain; not an infeasibility proof')
    return {'schema_version':VERSION,'engine':'Google OR-Tools CP-SAT','ortools_version':ortools.__version__,
        'effective_parameters':{'num_search_workers':1,'random_seed':0},
        'selected_route_indexes':best or [],'incumbent_origin':origin,'incumbent_vector':list(bestvec or ()),
        'phases':phases,'seed_combinations_checked':seed_count,'seed_truncated':seed_truncated,
        'global_search_complete':False,'global_optimality_proven':False,
        'termination_reason':'RESTRICTED_PHASES_OPTIMAL' if len(phases)==len(tiers) and all(p['engine_status']=='OPTIMAL' for p in phases) else 'BEST_KNOWN_INCUMBENT_RETAINED'}
