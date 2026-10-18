"""Native Step5 runner. Own preplans, actual replay, transition and suffixes.

No historical solver result is relabeled. Public API v1 remains untouched.
An isolated M2 local authority SQLite is runtime only, never packaged.
"""
import argparse
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
import hashlib,json,platform,sys
from pathlib import Path
from time import monotonic
from optimization.models.decision_state import DecisionState,StateContractError
from optimization.models.member1_dynamic_state import DynamicError,sha,require,SOLVER_VERSION
from optimization.integration.member1_decision_state_adapter import load_pinned_initial_states
from optimization.integration.member1_static_runner import _verified_source,_sha
from optimization.integration.member1_s1_runner import _require_no_sqlite_sidecars
from optimization.integration.member1_s0_graph import Member1RoadGraph,RoadDataError
from optimization.integration.member1_profiles import load_profile_config
from optimization.rolling_horizon.member1_motion_replay import replay_motion,canonical_sha256,ACCEPTANCE_SCHEMA_VERSION,LEADER_POLICY_ID,LEADER_POLICY_SHA256
from optimization.rolling_horizon.member1_motion_validation import validate_motion_state
from optimization.rolling_horizon.member1_dynamic_planner import pre_domain,post_domain,solve_domain,DynamicLimits
from optimization.rolling_horizon.member1_dynamic_validation import validate_dynamic,VALIDATOR_VERSION
from optimization.rolling_horizon.member1_dynamic_transition import LocalAuthority

VERSION='task02-m1-dynamic-run-manifest/2'
MODULES=['optimization/models/member1_dynamic_state.py','optimization/rolling_horizon/member1_dynamic_transition.py','optimization/rolling_horizon/member1_dynamic_planner.py','optimization/rolling_horizon/member1_dynamic_validation.py','optimization/rolling_horizon/member1_dynamic_runner.py','optimization/solver/member1_dynamic_master.py','optimization/solver/member1_profile_master.py','optimization/models/decision_state.py','optimization/models/motion_state.py','optimization/models/common.py','optimization/rolling_horizon/member1_motion_replay.py','optimization/rolling_horizon/member1_motion_validation.py','optimization/integration/member1_decision_state_adapter.py','optimization/integration/member1_mapping_audit.py','optimization/integration/member1_s0_graph.py','optimization/integration/member1_s0_runner.py','optimization/integration/member1_s1_runner.py','optimization/integration/member1_static_runner.py','optimization/integration/member1_static_validation.py','optimization/integration/member1_profiles.py','optimization/integration/member1_path_foundation.py']

def data(value):return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()

def legacy_projection(state,result,graph,run_id):
    routes=[]
    for route in result['vehicle_routes']:
        actions=route['actions'];nodes=[state.depot.graph_node_id];legs=[];stops=[];edges=[];incoming=None;arrival=None
        for a in actions:
            if a['kind']=='EDGE':edges.append(a['edge_id']);arrival=a['end_us']/1e6
            elif a['kind']=='SERVICE':
                require(bool(edges),'PREPLAN_SHAPE','legs','zero-road preplan leg not supported in this benchmark')
                path=graph.path_from_edge_ids(nodes[-1],edges,incoming_edge=incoming)
                legs.append(path.to_leg(incoming));incoming=path.final_edge;nodes.append(a['node_id']);edges=[]
                stops.append({'order_id':a['order_id'],'node_id':a['node_id'],'arrival_s':arrival,'service_start_s':a['start_us']/1e6,'waiting_s':a['start_us']/1e6-arrival,'completion_s':a['end_us']/1e6,'load_after_delivery_kg':a['load_after_kg']})
        require(bool(edges),'PREPLAN_SHAPE','legs','preplan must return by a physical path')
        path=graph.path_from_edge_ids(nodes[-1],edges,incoming_edge=incoming);legs.append(path.to_leg(incoming));nodes.append(state.depot.graph_node_id)
        routes.append({**{k:route[k] for k in ('vehicle_id','order_sequence','total_distance_m','total_travel_time_s','total_exposure','total_cost_vnd','total_soft_lateness_s')},'node_sequence':nodes,'legs':legs,'stops':stops,'departure_s':route['start_us']/1e6,'return_s':route['return_us']/1e6,'pickup_service_s':0.,'load_before_pickup_kg':next(v.current_load_kg for v in state.vehicles if v.vehicle_id==route['vehicle_id'])})
    return {'schema_version':'task02-m1-own-pre-event-plan/1','solver_version':SOLVER_VERSION,'solution_id':'pre-'+run_id,'scenario_id':state.scenario_id,'status':result['status'],'served_orders':result['served_orders'],'unserved_orders':result['unserved_orders'],'profile':{'name':'BALANCED'},'vehicle_routes':routes,'search':result['search'],'source_initial_state_sha256':sha(state.to_dict())}

def make_acceptance(state,plan,validation,run_id,source):
    a={'schema_version':ACCEPTANCE_SCHEMA_VERSION,'acceptance_id':'accept-'+run_id,'acceptance_type':'SYNTHETIC_LEADER_OWN_VALIDATED_PREPLAN','acceptance_sha256':None,'accepted_at':state.decision_epoch,'execution_start':state.decision_epoch,'leader_policy_id':LEADER_POLICY_ID,'leader_policy_sha256':LEADER_POLICY_SHA256,'scenario_id':state.scenario_id,'run_id':run_id,'solution_id':plan['solution_id'],'selected_profile':'BALANCED','initial_state_sha256':canonical_sha256(state.to_dict()),'initial_state_schema_version':state.schema_version,'plan_sha256':hashlib.sha256(data(plan)).hexdigest(),'plan_canonical_sha256':canonical_sha256(plan),'manifest_sha256':sha({'run_id':run_id,'source':source,'plan':canonical_sha256(plan),'validation':sha(validation)}),'validation_sha256':hashlib.sha256(data(validation)).hexdigest(),'fixture_raw_sha256':state.fixture_raw_sha256,'receipt_sha256':state.receipt_sha256,'routing_version':state.routing_version,'features_version':state.features_version,'context_version':state.context_version,'source_hashes':source,'solver_rerun':True}
    a['acceptance_sha256']=canonical_sha256(a);return a

def run(snapshot_root,output_root,scenario_id,profiles,event_time,*,limits=None):
    import ortools
    repo=Path(__file__).resolve().parents[2];snapshot=Path(snapshot_root).resolve();limits=limits or DynamicLimits()
    execution_code={p:_sha(repo/p) for p in MODULES}
    dynamic_config=json.loads((repo/'configs/member1_dynamic_step5.json').read_bytes())
    require(dynamic_config['limits']==asdict(limits),'CONFIG_BINDING','limits','locked Step5 limit config differs')
    require(_sha(repo/'configs/member1_profiles_step3.json')==dynamic_config['profile_config_sha256'],'CONFIG_BINDING','profile_config','frozen Step3 config differs')
    batch=load_pinned_initial_states(snapshot)
    require(batch.get('status')=='INITIAL_STATE_READY' and batch.get('source_gate')=='M1_SOURCE_CONTRACT_GATE_PASS','SOURCE_GATE','snapshot_root',str(batch.get('diagnostics')))
    require(scenario_id in ('S2','S3'),'UNSUPPORTED','scenario_id','only S2/S3')
    state=DecisionState.from_dict(next(x for x in batch['states'] if x['scenario_id']==scenario_id))
    require(len(state.pending_events)==1 and event_time==state.pending_events[0].timestamp,'EVENT_TIME','event_time','exact pinned event required')
    paths,before_source=_verified_source(snapshot,state);_require_no_sqlite_sidecars(paths,phase='before_open')
    config=load_profile_config(repo/'configs/member1_profiles_step3.json')
    run_id=f'{scenario_id}_DYNAMIC_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory=Path(output_root).resolve()/run_id;directory.mkdir(parents=True,exist_ok=False)
    started=monotonic();payloads={'initial_state.json':state.to_dict()};stages={}
    try:
        with Member1RoadGraph(paths['network_sqlite_sha256'],paths['features_sqlite_sha256'],routing_version=state.routing_version,features_version=state.features_version,context_version=state.context_version,source_hashes=before_source) as graph:
            tick=monotonic();pre=pre_domain(state,graph,limits);pre_result=solve_domain(state,pre,config,'BALANCED');pre_validation=validate_dynamic(state,graph,pre,pre_result,config)
            payloads.update({'pre_domain.json':pre,'pre_result.json':pre_result,'pre_validation.json':pre_validation});stages['preplan']={'executed':True,'elapsed_seconds':monotonic()-tick}
            # Preserve genuine failed-stage evidence without certifying a run.
            for name in ('pre_domain.json','pre_result.json','pre_validation.json'):
                (directory/name).write_bytes(data(payloads[name]))
            require(pre_validation['valid'] and pre_result['status']=='FEASIBLE','OWN_PREPLAN_REQUIRED','pre_result','own independently valid full initial witness required')
            plan=legacy_projection(state,pre_result,graph,run_id);accepted=make_acceptance(state,plan,pre_validation,run_id,before_source)
            b=replay_motion(state,plan,accepted,graph,event_time);bv=validate_motion_state(state,plan,accepted,graph,b)
            require(bv['valid'],'BEFORE_STATE_INVALID','before_state',str(bv['diagnostics']))
            if scenario_id=='S3':require(next(x for x in b['orders'] if x['order_id']=='O001')['status']=='ONBOARD','CUSTODY_EXERCISE_REQUIRED','before_state.orders.O001','O001 must remain onboard at event')
            minus=(datetime.fromisoformat(event_time)-timedelta(microseconds=1)).isoformat()
            prior=replay_motion(state,plan,accepted,graph,minus);priorv=validate_motion_state(state,plan,accepted,graph,prior)
            require(priorv['valid'],'BEFORE_STATE_INVALID','before_state_minus',str(priorv['diagnostics']))
            store=LocalAuthority(repo/'outputs/member1_dynamic_runtime'/f'{run_id}.sqlite')
            trusted=store.register(run_id,state,plan,accepted,b,pre_validation,bv)
            transition=store.apply(run_id,state.pending_events[0],b['content_sha256'],b['state_version'])
            retry=store.apply(run_id,state.pending_events[0],b['content_sha256'],b['state_version']);require(retry==transition,'IDEMPOTENCY','transition','retry changed receipt')
            ledger=store.export(run_id);after=transition['after_state']
            stages['replay_transition']={'executed':True,'reused':False,'event_applications':1,'same_event_retries':1}
            payloads.update({'pre_plan.json':plan,'accepted_plan_receipt.json':accepted,'authority_record.json':trusted,'before_state.json':b,'before_validation.json':bv,'before_minus_state.json':prior,'before_minus_validation.json':priorv,'event.json':state.pending_events[0].to_dict(),'transition.json':transition,'ledger.json':ledger})
            tick=monotonic();domain=post_domain(state,graph,after,limits);payloads['dynamic_domain.json']=domain;stages['post_domain']={'executed':True,'elapsed_seconds':monotonic()-tick}
            targets={}
            for profile in profiles:
                tick=monotonic();result=solve_domain(state,domain,config,profile,before=b,after=after)
                validation=validate_dynamic(state,graph,domain,result,config,plan=plan,accepted=accepted,before=b,transition=transition,authority=ledger)
                payloads[profile.lower()+'_solution.json']=result;payloads[profile.lower()+'_validation.json']=validation
                targets[profile]={'status':result['status'],'served_orders':result['served_orders'],'unserved_orders':result['unserved_orders'],'validation_valid':validation['valid'],'diagnostics':validation['diagnostics'],'domain_sha256':domain['content_sha256'],'elapsed_seconds':monotonic()-tick}
            all_valid=all(t['validation_valid'] is True for t in targets.values())
            exercise=(any('O009' in t['served_orders'] for t in targets.values()) if scenario_id=='S2' else all(t['status']=='PARTIAL' and any(x['order_id']=='O001' and x['reason']=='CUSTODY_BLOCKED' for x in t['unserved_orders']) for t in targets.values()))
            gate=scenario_id+'_EVENT_DYNAMIC_VALIDATED' if all_valid and exercise and len(targets)==3 else 'GATE_BLOCKED'
    except (DynamicError,StateContractError,RoadDataError,OSError) as e:
        payloads['failure.json']={'code':getattr(e,'code','RAW_SOURCE_INVALID'),'path':getattr(e,'path','$'),'message':str(e)};gate='GATE_BLOCKED';targets={}
        if hasattr(e,'traces'):payloads['failed_queries.json']=e.traces
    finally:
        _require_no_sqlite_sidecars(paths,phase='after_close')
    after_source={k:_sha(p) for k,p in paths.items()};require(before_source==after_source,'SOURCE_CHANGED','snapshot_root','raw source changed')
    records={}
    for name,value in payloads.items():
        raw=data(value);(directory/name).write_bytes(raw);records[name]={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    current_code={p:_sha(repo/p) for p in MODULES}
    if execution_code!=current_code:gate='GATE_BLOCKED';payloads['code_drift']=True
    manifest={'schema_version':VERSION,'run_id':run_id,'scenario_id':scenario_id,'gate':gate,'snapshot_verified':True,'source_hashes_before':before_source,'source_hashes_after':after_source,'code_sha256':execution_code,'code_sha256_after':current_code,'code_bytes':{p:(repo/p).stat().st_size for p in MODULES},'dynamic_config_sha256':_sha(repo/'configs/member1_dynamic_step5.json'),'profile_config_sha256':_sha(repo/'configs/member1_profiles_step3.json'),'policy_sha256':LEADER_POLICY_SHA256,'files':records,'stages':stages,'limits':asdict(limits),'targets':targets,'elapsed_seconds':monotonic()-started,'platform':{'system':platform.system(),'interpreter':sys.executable,'python':platform.python_version(),'optimized_mode':sys.flags.optimize>0},'ortools_version':ortools.__version__,'validator_version':VALIDATOR_VERSION,'preplan_selection_rule':'capacity-balanced subsets; nearest/reverse; S3 O001-last completion > event; stop at full initial witness pool (synthetic exercise only)','scope':{'general_m1_validated':False,'e4_run':False,'production_calibrated':False,'api_v1_changed':False,'rain_overlay':False}}
    (directory/'manifest.json').write_bytes(data(manifest));return manifest,directory

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot-root',required=True);p.add_argument('--scenario-id',required=True,choices=['S2','S3']);p.add_argument('--profiles',nargs='+',choices=['FASTEST','BALANCED','SAFER'],default=['FASTEST','BALANCED','SAFER']);p.add_argument('--event-time',required=True);p.add_argument('--output-root',required=True)
    args=p.parse_args()
    try:
        require(len(args.profiles)==len(set(args.profiles)),'INVALID_DATA','profiles','duplicate profile')
        manifest,path=run(args.snapshot_root,args.output_root,args.scenario_id,args.profiles,args.event_time)
        print(json.dumps({'status':'READY' if manifest['gate']!='GATE_BLOCKED' else 'BLOCKED','gate':manifest['gate'],'run_id':manifest['run_id'],'output':str(path),'targets':manifest['targets']}));return 0 if manifest['gate']!='GATE_BLOCKED' else 2
    except (DynamicError,StateContractError,RoadDataError,OSError,ValueError) as e:
        print(json.dumps({'status':'FAIL','diagnostic':{'code':getattr(e,'code','DYNAMIC_RUNNER'),'path':getattr(e,'path','$'),'message':str(e)}}),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
