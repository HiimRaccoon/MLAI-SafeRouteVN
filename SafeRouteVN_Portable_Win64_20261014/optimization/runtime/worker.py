"""Isolated native initial-state computation, no authority-store access.

Change Impact Analysis: unchanged legacy planners/master are reused. Only
initial dry states are accepted here; post-event/temporal runtime is not silently
delegated to a one-shot historical runner. Parent supervisor owns hard budget.
"""
import argparse,json,time
from dataclasses import replace,asdict
from pathlib import Path
from optimization.models.decision_state import DecisionState
from optimization.models.member1_dynamic_state import finalize
from optimization.integration.member1_static_runner import _verified_source,_sha
from optimization.integration.member1_s1_runner import _require_no_sqlite_sidecars
from optimization.integration.member1_s0_graph import Member1RoadGraph
from optimization.integration.member1_profiles import load_profile_config
from optimization.rolling_horizon.member1_dynamic_planner import pre_domain,DynamicLimits
from optimization.solver.member1_dynamic_master import select_columns
from .protocol import decode,require,sha
from .validation import validate_initial_result

VERSION='task02-m2-initial-runtime-column-compute/1'

def compute(input_value):
    started=time.monotonic();state=DecisionState.from_dict(input_value['initial']);snapshot=Path(input_value['snapshot_root'])
    require(input_value['head_time']==state.decision_epoch,'RUNTIME_SCOPE_BLOCKED','head.current_time','post-activation replanning is not yet certified by this worker')
    budget=input_value['compute_seconds'];paths,hashes=_verified_source(snapshot,state)
    require(hashes==input_value['source_hashes'],'SOURCE_CHANGED','source_hashes','bootstrap source differs')
    _require_no_sqlite_sidecars(paths,phase='before_open')
    try:
        with Member1RoadGraph(paths['network_sqlite_sha256'],paths['features_sqlite_sha256'],routing_version=state.routing_version,features_version=state.features_version,context_version=state.context_version,source_hashes=hashes) as graph:
            config=load_profile_config(Path(__file__).resolve().parents[2]/'configs/member1_profiles_step3.json')
            limits=replace(DynamicLimits(),domain_seconds=max(.001,budget*.6))
            t=time.monotonic();domain=pre_domain(state,graph,limits);domain_elapsed=time.monotonic()-t
            remaining=budget-(time.monotonic()-started)
            require(remaining>0,'BUDGET_EXHAUSTED','compute_seconds','no budget for master/validation')
            t=time.monotonic();master=select_columns(domain['columns'],{o.order_id:o.priority for o in state.orders},[v.vehicle_id for v in state.vehicles],input_value['profile'],config,seconds=min(limits.master_seconds,max(.001,remaining*.6)))
            master_elapsed=time.monotonic()-t
            routes=[domain['columns'][i] for i in master['selected_route_indexes']]
            served=sorted(o for r in routes for o in r['order_sequence']);unserved=[{'order_id':o.order_id,'reason':'NOT_SERVED_BY_FOUND_WITNESS'} for o in state.orders if o.order_id not in served]
            status='FEASIBLE' if not unserved else 'PARTIAL' if served else 'SEARCH_LIMIT'
            result={'schema_version':'task02-m2-runtime-witness/1','solver_version':VERSION,'scenario_id':state.scenario_id,'profile':input_value['profile'],
                    'status':status,'domain_sha256':domain['content_sha256'],'source_hashes':hashes,'served_orders':served,'unserved_orders':unserved,'vehicle_routes':routes,
                    'forecast':True,'post_event_state':None,'search':{'search_complete':False,'optimality_proven':False,'truncated':True,'master':master},
                    'metrics':{k:sum(r[k] for r in routes) for k in ('total_distance_m','total_travel_time_s','total_exposure','total_cost_vnd','total_soft_lateness_s')}}
            t=time.monotonic()
            if served:validation=validate_initial_result(state,graph,domain,result,config)
            else:
                result.update(vehicle_routes=[],served_orders=[],unserved_orders=[],coverage_evaluated=False)
                validation={'validator_version':'task02-m2-runtime-raw-witness-validator/1','valid':None,'validation_status':'NOT_RUN','diagnostics':[]}
            validation_elapsed=time.monotonic()-t
    finally:_require_no_sqlite_sidecars(paths,phase='after_close')
    require(hashes=={k:_sha(p) for k,p in paths.items()},'SOURCE_CHANGED','source_hashes','raw source changed')
    return {'result':result,'validation':validation,'domain':domain,'telemetry':{'solver_executed':True,'road_search_executed':True,'domain_seconds':domain_elapsed,'master_seconds':master_elapsed,'validation_seconds':validation_elapsed,'elapsed_seconds':time.monotonic()-started,'limits':asdict(limits)}}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--input',required=True,type=Path);parser.add_argument('--output',required=True,type=Path);args=parser.parse_args()
    v=compute(decode(args.input.read_bytes()))
    with args.output.open('x',encoding='utf8') as f:json.dump(v,f,ensure_ascii=False,sort_keys=True,allow_nan=False)
    return 0
if __name__=='__main__':raise SystemExit(main())
