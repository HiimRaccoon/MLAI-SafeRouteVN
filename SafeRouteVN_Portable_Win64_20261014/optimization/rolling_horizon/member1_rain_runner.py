"""Native S4 only: own preplan, immutable replay, rain CAS, physical CP-SAT.

Source CLI acceptance is separate and must be checked by the evidence gate.
Runner success is not a whole Step6 READY label. No historical run is reused.
"""
import argparse
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from pathlib import Path
from time import monotonic
import hashlib,json,platform,sys
from optimization.models.decision_state import DecisionState,StateContractError
from optimization.models.member1_dynamic_state import require,sha,DynamicError
from optimization.models.member1_rain import POLICY
from optimization.integration.member1_decision_state_adapter import load_pinned_initial_states
from optimization.integration.member1_static_runner import _verified_source,_sha
from optimization.integration.member1_s1_runner import _require_no_sqlite_sidecars,S1SidecarError
from optimization.integration.member1_s0_graph import Member1RoadGraph,RoadDataError
from optimization.integration.member1_profiles import load_profile_config
from optimization.rolling_horizon.member1_dynamic_runner import legacy_projection,make_acceptance,data,MODULES as CORE_MODULES
from optimization.rolling_horizon.member1_dynamic_planner import pre_domain,solve_domain,DynamicLimits
from optimization.rolling_horizon.member1_dynamic_validation import validate_dynamic
from optimization.rolling_horizon.member1_motion_replay import replay_motion
from optimization.rolling_horizon.member1_motion_validation import validate_motion_state
from optimization.rolling_horizon.member1_rain_source import load_source_model,build_overlay,source_edge_rates,PINS
from optimization.rolling_horizon.member1_rain_temporal import traverse
from optimization.rolling_horizon.member1_rain_transition import RainAuthority
from optimization.rolling_horizon.member1_rain_planner import temporal_domain,solve_rain,RainColumns
from optimization.rolling_horizon.member1_rain_validation import validate_rain,check_temporal_route,validate_forecast_sample,VALIDATOR_VERSION
from optimization.rolling_horizon.member1_rain_replay import sample_forecast

VERSION='task02-m1-temporal-rain-run-manifest/1'
MODULES=CORE_MODULES+['optimization/models/member1_rain.py','optimization/rolling_horizon/member1_rain_source.py',
 'optimization/rolling_horizon/member1_rain_temporal.py','optimization/rolling_horizon/member1_rain_transition.py',
 'optimization/rolling_horizon/member1_rain_planner.py','optimization/rolling_horizon/member1_rain_validation.py',
 'optimization/rolling_horizon/member1_column_validation.py','optimization/rolling_horizon/member1_rain_runner.py','optimization/rolling_horizon/member1_rain_replay.py',
 'configs/member1_rain_step6.json','configs/member1_profiles_step3.json','configs/member1_dynamic_step5.json',
 'optimization/integration/member1_trusted_receipt.json']


def probes(state,graph,model,overlay):
    affected=overlay['affected_edge_ids'][0]
    # Stream one unaffected raw edge. No second selection of affected polygon.
    unaffected=next(row[0] for row in graph.db.execute('SELECT edgeId FROM edges ORDER BY edgeId') if row[0] not in overlay['cost_table'])
    results=[]
    for eid in (affected,unaffected):
        rates=source_edge_rates(state,graph,model,overlay,eid)
        for boundary in (overlay['start_us'],overlay['end_us']):
            for delta in (-1,0,1):
                results.append({'edge_id':eid,'affected':eid==affected,'boundary_us':boundary,'delta_us':delta,
                    'ingredients':model.ingredients(graph,eid),'evaluation':traverse(rates,boundary+delta)})
    return {'schema_version':'task02-m1-raw-temporal-edge-probes/1','scope':'RAW_EDGE_PROBES_NOT_VRP_TRAJECTORY','probes':results}


def counterfactual(state,graph,model,overlay,before,pre_result,limits):
    """Keep accepted directed sequence, only re-time future physical actions."""
    builder=RainColumns(state,graph,limits,model,overlay);orders={o.order_id:o for o in state.orders};routes=[]
    from optimization.rolling_horizon.member1_dynamic_validation import offset,us
    event_us=offset(state.decision_epoch,before['current_time'])
    for old in pre_result['vehicle_routes']:
        snapshot=next(v for v in before['vehicles'] if v['vehicle_id']==old['vehicle_id'])
        if snapshot['activity']=='RETURNED':continue
        actions=[];clock=event_us;load=snapshot['current_load_kg'];incoming=snapshot['position'].get('incoming_edge')
        for a in old['actions']:
            if a['end_us']<=event_us:continue
            if a['kind']=='EDGE':
                progress=builder.initial_edge_progress(snapshot,clock) if not actions and snapshot['position']['kind']=='ON_EDGE' else 0.
                item=builder.edge_action(a['edge_id'],clock,fraction=progress,incoming=incoming);incoming=a['edge_id'];clock=item['end_us'];actions.append(item)
            elif a['kind']=='WAIT':
                end=max(clock,a['end_us'])
                if end>clock:actions.append({**a,'start_us':clock,'end_us':end});clock=end
            elif a['kind']=='SERVICE':
                o=orders[a['order_id']];start=max(clock,offset(state.decision_epoch,o.earliest))
                if start>clock:actions.append({'kind':'WAIT','node_id':o.graph_node_id,'start_us':clock,'end_us':start})
                continuation=not actions and snapshot['active_commitment'] is not None and snapshot['active_commitment']['kind']=='SERVICE'
                end=snapshot['active_commitment']['until_us'] if continuation else start+us(o.service_time_seconds)
                load-=o.demand_kg;actions.append({**a,'start_us':start,'end_us':end,'continuation':continuation,'load_after_kg':load});clock=end
            else:
                load+=orders[a['order_id']].demand_kg;actions.append({**a,'start_us':clock,'end_us':clock,'load_after_kg':load})
        v=next(x for x in state.vehicles if x.vehicle_id==old['vehicle_id'])
        distance=sum(a.get('distance_m',0.) for a in actions)
        route={'vehicle_id':old['vehicle_id'],'order_sequence':[a['order_id'] for a in actions if a['kind']=='SERVICE'],'actions':actions,
            'start_us':event_us,'return_us':clock,'start_node':snapshot['position'].get('node_id',snapshot['position'].get('from_node')),
            'end_node':state.depot.graph_node_id,'return_load_kg':load,'total_distance_m':distance,
            'total_travel_time_s':sum((a['end_us']-a['start_us'])/1e6 for a in actions if a['kind']=='EDGE'),
            'total_exposure':sum(a.get('exposure',0.) for a in actions),'total_cost_vnd':distance/1000*v.cost_per_km_vnd,
            'total_soft_lateness_s':sum(max(0,(a['end_us']-offset(state.decision_epoch,orders[a['order_id']].preferred_due))/1e6) for a in actions if a['kind']=='SERVICE')}
        routes.append(route)
    return {'schema_version':'task02-m1-rain-counterfactual/1','scope':'FORECAST_NO_REPLAN_FROM_SAME_OBSERVED_HEAD',
            'vehicle_routes':routes,'feasibility':'NOT_YET_CHECKED','not_a_solver_result':True}


def run(snapshot_root,output_root):
    import ortools
    repo=Path(__file__).resolve().parents[2];snapshot=Path(snapshot_root).resolve()
    config=load_profile_config(repo/'configs/member1_profiles_step3.json');limits=DynamicLimits()
    temporal_config=json.loads((repo/'configs/member1_rain_step6.json').read_bytes())
    require(temporal_config.get('limits')==asdict(limits) and temporal_config.get('temporal_policy')==POLICY,'CONFIG_BINDING','temporal_config','locked limits/policy differ')
    require(_sha(repo/'configs/member1_profiles_step3.json')==temporal_config['profile_config_sha256'],'CONFIG_BINDING','profile_config','locked common profile differs')
    execution_code={p:_sha(repo/p) for p in MODULES}
    batch=load_pinned_initial_states(snapshot)
    require(batch.get('status')=='INITIAL_STATE_READY' and batch.get('source_gate')=='M1_SOURCE_CONTRACT_GATE_PASS','SOURCE_GATE','snapshot_root',str(batch.get('diagnostics')))
    state=DecisionState.from_dict(next(s for s in batch['states'] if s['scenario_id']=='S4'))
    paths,before_source=_verified_source(snapshot,state);model=load_source_model(snapshot,state)
    paths={**paths,**{'primary:'+rel:snapshot/rel for rel in PINS}}
    before_source={k:_sha(p) for k,p in paths.items()}
    run_id='S4_RAIN_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory=Path(output_root).resolve()/run_id;directory.mkdir(parents=True,exist_ok=False)
    started=monotonic();payloads={'initial_state.json':state.to_dict()};stages={};targets={};gate='GATE_BLOCKED';sidecar_before=False;sidecar_after=False
    try:
        _require_no_sqlite_sidecars(paths,phase='before_open');sidecar_before=True
        with Member1RoadGraph(paths['network_sqlite_sha256'],paths['features_sqlite_sha256'],routing_version=state.routing_version,features_version=state.features_version,context_version=state.context_version,source_hashes=before_source) as graph:
            tick=monotonic();pre=pre_domain(state,graph,limits);pre_result=solve_domain(state,pre,config,'BALANCED');pre_validation=validate_dynamic(state,graph,pre,pre_result,config)
            payloads.update({'pre_domain.json':pre,'pre_result.json':pre_result,'pre_validation.json':pre_validation});stages['preplan']={'elapsed_seconds':monotonic()-tick,'executed':True}
            require(pre_validation['valid'] is True and pre_result['status']=='FEASIBLE' and len(pre_result['served_orders'])==8,'OWN_PREPLAN_REQUIRED','pre_result','own valid full S4 pre-event plan required')
            plan=legacy_projection(state,pre_result,graph,run_id);accepted=make_acceptance(state,plan,pre_validation,run_id,before_source)
            event=state.pending_events[0]
            before=replay_motion(state,plan,accepted,graph,event.timestamp);motion_validation=validate_motion_state(state,plan,accepted,graph,before)
            require(motion_validation['valid'] is True,'PREFIX_INVALID','before',str(motion_validation['diagnostics']))
            overlay=build_overlay(state,graph,model);require(overlay['edge_count']==2253,'OVERLAY_BINDING','overlay.edge_count','exact raw M1 edge count required')
            store=RainAuthority(repo/'outputs/member1_rain_runtime'/f'{run_id}.sqlite')
            trusted=store.register(run_id,state,plan,accepted,before,pre_validation,motion_validation,overlay=overlay)
            tx=store.apply(run_id,event,before['content_sha256'],before['state_version'])
            require(tx==store.apply(run_id,event,before['content_sha256'],before['state_version']),'IDEMPOTENCY','transition','retry differs')
            authority=store.export(run_id);after=tx['after_state']
            payloads.update({'pre_plan.json':plan,'accepted_plan_receipt.json':accepted,'before_state.json':before,'before_validation.json':motion_validation,
                'overlay.json':overlay,'event.json':event.to_dict(),'transition.json':tx,'authority_root_receipt.json':trusted,'authority_export.json':authority,
                'raw_edge_probes.json':probes(state,graph,model,overlay)})
            minus=(datetime.fromisoformat(event.timestamp)-timedelta(microseconds=1)).isoformat()
            prior=replay_motion(state,plan,accepted,graph,minus);prior_validation=validate_motion_state(state,plan,accepted,graph,prior)
            require(prior_validation['valid'] is True,'PREFIX_INVALID','before_minus',str(prior_validation['diagnostics']))
            payloads['before_minus_state.json']=prior;payloads['before_minus_validation.json']=prior_validation
            # Raw probes are explicit, separate evidence if VRP does not cross
            # an affected boundary. Never describe them as route coverage.
            cf=counterfactual(state,graph,model,overlay,before,pre_result,limits)
            cf_issues=[]
            for route in cf['vehicle_routes']:
                try:check_temporal_route(state,graph,route,{o.order_id:o for o in state.orders},after,load_source_model(snapshot,state),overlay)
                except (DynamicError,RoadDataError) as error:cf_issues.append({'code':getattr(error,'code','RAW_SOURCE_INVALID'),'path':getattr(error,'path','route'),'message':str(error)})
            cf['feasibility']='INVALID' if cf_issues else 'VALIDATED_FIXED_SEQUENCE_FORECAST';cf['diagnostics']=cf_issues
            payloads['counterfactual.json']=cf
            tick=monotonic();domain=temporal_domain(state,graph,after,limits,model,overlay);payloads['temporal_domain.json']=domain;stages['domain']={'elapsed_seconds':monotonic()-tick,'executed':True}
            for profile in ('FASTEST','BALANCED','SAFER'):
                tick=monotonic();result=solve_rain(state,domain,config,profile,before=before,after=after)
                # Checker uses a fresh source ingredient cache, not the producer's
                # memoized metrics or weather/overlay output as authority.
                checker_model=load_source_model(snapshot,state)
                validation=validate_rain(state,graph,domain,result,config,model=checker_model,overlay=overlay,
                    plan=plan,accepted=accepted,before=before,transition=tx,authority=authority)
                payloads[profile.lower()+'_solution.json']=result;payloads[profile.lower()+'_validation.json']=validation
                if validation['valid'] is True:
                    times=sorted({overlay['start_us'],overlay['start_us']+1,overlay['end_us']-1,overlay['end_us'],overlay['end_us']+1,
                        max((r['return_us'] for r in result['vehicle_routes']),default=overlay['start_us'])})
                    samples=[sample_forecast(state,graph,model,overlay,after,result,t) for t in times]
                    sample_checks=[validate_forecast_sample(state,graph,checker_model,overlay,after,result,s) for s in samples]
                    payloads[profile.lower()+'_forecast_samples.json']={'samples':samples,'validations':sample_checks}
                    if not all(v['valid'] is True for v in sample_checks):validation['valid']=False;validation['diagnostics'].append({'code':'FORECAST_SAMPLE_INVALID','path':'forecast_samples','message':str(sample_checks)})
                targets[profile]={'status':result['status'],'served_orders':result['served_orders'],'unserved_orders':result['unserved_orders'],
                    'validation_valid':validation['valid'],'diagnostics':validation['diagnostics'],'metrics':result.get('whole_trajectory_metrics'),
                    'domain_sha256':domain['content_sha256'],'elapsed_seconds':monotonic()-tick}
            gate='S4_TEMPORAL_WITNESSES_VALIDATED' if all(t['validation_valid'] is True for t in targets.values()) and len(targets)==3 else 'GATE_BLOCKED'
            comparison={'schema_version':'task02-m1-rain-whole-trajectory-comparison/1','scope':'WHOLE_TRAJECTORY_FROM_SAME_OWN_ACCEPTED_PREPLAN',
                'observed_prefix_sha256':before['content_sha256'],'actual_delivered_at_event':sorted(o['order_id'] for o in before['orders'] if o['status']=='DELIVERED'),
                'baseline_accepted_forecast':{'metrics':pre_result['metrics'],'validation_valid':pre_validation['valid'],'served_orders':pre_result['served_orders']},
                'counterfactual_continue_without_replanning':{'feasibility':cf['feasibility'],'diagnostics':cf['diagnostics'],'not_an_optimized_result':True},
                'replanned_profiles':targets,'limitations':['FORECAST_NOT_OBSERVED','EXPOSURE_PROXY_NOT_ACCIDENT_PROBABILITY','DUPLICATE_PROFILES_ALLOWED','FINITE_DOMAIN']}
            cf_metrics={k:sum(r[k] for r in cf['vehicle_routes']) for k in ('total_distance_m','total_travel_time_s','total_exposure','total_cost_vnd','total_soft_lateness_s')}
            mapping={'total_distance_m':('distance_m',1),'total_travel_time_s':('travel_time_us',1e6),'total_exposure':('relative_exposure_proxy',1),'total_cost_vnd':('cost_vnd',1)}
            for k,(v,scale) in mapping.items():cf_metrics[k]+=before['execution_metrics'][v]/scale
            from optimization.rolling_horizon.member1_dynamic_validation import offset
            cf_metrics['total_soft_lateness_s']+=sum(max(0,(offset(state.decision_epoch,o['delivered_at'])-offset(state.decision_epoch,next(x.preferred_due for x in state.orders if x.order_id==o['order_id'])))/1e6) for o in before['orders'] if o['status']=='DELIVERED')
            comparison['counterfactual_continue_without_replanning']['metrics']=cf_metrics
            for profile in targets:
                result=payloads[profile.lower()+'_solution.json'];actions=[a for r in result['vehicle_routes'] for a in r['actions']]
                detail={'whole_wait_s':before['execution_metrics']['waiting_time_us']/1e6+sum((a['end_us']-a['start_us'])/1e6 for a in actions if a['kind']=='WAIT'),
                    'whole_service_s':before['execution_metrics']['service_time_us']/1e6+sum((a['end_us']-a['start_us'])/1e6 for a in actions if a['kind']=='SERVICE'),
                    'final_return_us':max((r['return_us'] for r in result['vehicle_routes']),default=overlay['start_us']),
                    'affected_suffix_edges':sum(a['kind']=='EDGE' and a['edge_id'] in overlay['cost_table'] for a in actions)}
                segments=[s for a in actions if a['kind']=='EDGE' for s in a['temporal_segments']]
                detail['suffix_layer_metrics']={layer:{'segments':sum(s['layer']==layer for s in segments),
                    'distance_m':sum(s['distance_m'] for s in segments if s['layer']==layer),
                    'travel_us':sum(s['end_us']-s['start_us'] for s in segments if s['layer']==layer),
                    'exposure_proxy':sum(s['exposure'] for s in segments if s['layer']==layer)} for layer in ('BASELINE','WET','BASELINE_AFTER_EXPIRY')}
                comparison['replanned_profiles'][profile]={**targets[profile],'timeline_breakdown':detail}
            comparison['duplicate_profile_routes']=[[a,b] for i,a in enumerate(targets) for b in list(targets)[i+1:] if payloads[a.lower()+'_solution.json']['vehicle_routes']==payloads[b.lower()+'_solution.json']['vehicle_routes']]
            payloads['comparison.json']=comparison
    except (DynamicError,StateContractError,RoadDataError,OSError,S1SidecarError) as error:
        payloads['failure.json']={'code':getattr(error,'code','RAW_SOURCE_INVALID'),'path':getattr(error,'path','$'),'message':str(error)}
        if hasattr(error,'traces'):payloads['failed_queries.json']=error.traces
    finally:
        try:_require_no_sqlite_sidecars(paths,phase='after_close');sidecar_after=True
        except S1SidecarError as error:payloads['sidecar_failure.json']=error.to_diagnostic();gate='GATE_BLOCKED'
    after_source={k:_sha(p) for k,p in paths.items()};source_same=before_source==after_source
    current_code={p:_sha(repo/p) for p in MODULES};code_same=execution_code==current_code
    if not source_same or not code_same:gate='GATE_BLOCKED'
    records={}
    for name,value in payloads.items():
        raw=data(value);(directory/name).write_bytes(raw);records[name]={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    manifest={'schema_version':VERSION,'run_id':run_id,'scenario_id':'S4','gate':gate,'snapshot_verified':source_same and sidecar_before and sidecar_after,
        'source_hashes_before':before_source,'source_hashes_after':after_source,'code_sha256':execution_code,'code_sha256_after':current_code,
        'code_bytes':{p:(repo/p).stat().st_size for p in MODULES},'files':records,'targets':targets,'stages':stages,'limits':asdict(limits),
        'temporal_config_sha256':_sha(repo/'configs/member1_rain_step6.json'),'profile_config_sha256':_sha(repo/'configs/member1_profiles_step3.json'),
        'source_versions':{'routing':state.routing_version,'features':state.features_version,'context':state.context_version},
        'overlay_sha256':payloads.get('overlay.json',{}).get('content_sha256'),'elapsed_seconds':monotonic()-started,
        'sidecars':{'before_open_absent':sidecar_before,'after_close_absent':sidecar_after},
        'platform':{'system':platform.system(),'python':platform.python_version(),'interpreter':sys.executable,'optimized_mode':sys.flags.optimize>0},
        'ortools_version':ortools.__version__,'validator_version':VALIDATOR_VERSION,
        'source_cli_acceptance':'NOT_CERTIFIED_BY_RUNNER','general_m1_validated':False,'e4_run':False,'production_calibrated':False,
        'scope':'SYNTHETIC_S4_TEMPORAL_FORECAST_WITNESS; FINITE_DOMAIN; NOT_ATOMIC_SNAPSHOT'}
    (directory/'manifest.json').write_bytes(data(manifest));return manifest,directory


def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot-root',required=True);p.add_argument('--output-root',required=True);a=p.parse_args()
    try:
        manifest,path=run(a.snapshot_root,a.output_root)
        print(json.dumps({'status':'VALIDATED' if manifest['gate']=='S4_TEMPORAL_WITNESSES_VALIDATED' else 'BLOCKED',
            'gate':manifest['gate'],'run_id':manifest['run_id'],'output':str(path),'targets':manifest['targets']}))
        return 0 if manifest['gate']=='S4_TEMPORAL_WITNESSES_VALIDATED' else 2
    except (DynamicError,StateContractError,RoadDataError,OSError,ValueError) as error:
        print(json.dumps({'status':'FAIL','diagnostic':{'code':getattr(error,'code','RAIN_RUNNER'),'path':getattr(error,'path','$'),'message':str(error)}}),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
