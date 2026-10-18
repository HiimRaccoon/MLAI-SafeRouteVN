"""Independent raw-SQLite prefix/overlay/trajectory and phase checker.

No producer evaluator, planner, transition, master or scoring imports. Shared
dependencies: pure DTO guards, pinned primary M1 formula reader, old raw motion
and physical checks, independent finite-domain phase primitive. The rational
integration below is a second implementation checked against hand oracles.
"""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from typing import Mapping
import hashlib,json
from optimization.models.member1_dynamic_state import require,sha,finite_tree,DynamicError
from optimization.models.member1_rain import (POLICY,OVERLAY_VERSION,STATE_VERSION,TRANSITION_VERSION,DOMAIN_VERSION,RESULT_VERSION,SOLVER_VERSION,integer,number,fraction,check_state)
from optimization.models.motion_state import MotionContractError
from optimization.models.decision_state import DecisionState
from optimization.integration.member1_s0_graph import RoadDataError
from optimization.rolling_horizon.member1_rain_source import SourceModel,event_interval
from optimization.rolling_horizon.member1_motion_validation import validate_motion_state
from optimization.rolling_horizon.member1_dynamic_validation import route_check,close,offset
from optimization.rolling_horizon.member1_column_validation import check_selection

VALIDATOR_VERSION='task02-m1-independent-temporal-rain-validator/2'


def check_execution_metrics(value,path):
    """Typed copied ledger; preserve valid native int/float aggregate wire.

    Change Impact Analysis: reject numeric bool aliases and malformed copied
    ledgers before digest/equality; no change to producer, physics or policy.
    """
    require(isinstance(value,Mapping),'INVALID_DATA',path,'metric object required')
    for key in ('distance_m','relative_exposure_proxy','cost_vnd',
                'travel_time_us','waiting_time_us','service_time_us'):
        number(value.get(key),path+'.'+key)


def independent_segments(row,overlay,edge_id,start,progress,incoming):
    """Integrate remaining distance in time layers; no producer calls."""
    integer(start,'edge.start_us');p=fraction(progress,'edge.progress');clock=start;out=[]
    affected=edge_id in overlay['cost_table']
    while p<1:
        active=affected and overlay['start_us']<=clock<overlay['end_us']
        identity='WET' if active else 'BASELINE' if clock<overlay['end_us'] else 'BASELINE_AFTER_EXPIRY'
        spec=row['wet' if active else 'baseline'];duration=spec['duration_us']
        boundary=overlay['start_us'] if clock<overlay['start_us'] else overlay['end_us'] if clock<overlay['end_us'] else None
        remainder=(1-p)*duration
        if boundary is None or remainder<=boundary-clock:
            endpoint=clock+(remainder.numerator+remainder.denominator-1)//remainder.denominator
            q=Fraction(1)
        else:endpoint=boundary;q=p+Fraction(boundary-clock,duration)
        integer(endpoint,'edge.end_us');distance=row['length_m']*float(q-p)
        out.append({'edge_id':edge_id,'from_node':row['from_node'],'to_node':row['to_node'],'incoming_edge':incoming,
            'start_us':clock,'end_us':endpoint,'fraction_start':str(p),'fraction_end':str(q),
            'layer':identity,'full_duration_us':duration,'edge_proxy':spec['edgeProxy'],
            'distance_m':distance,'exposure':distance/1000*spec['edgeProxy'],'cost_vnd':0.})
        p=q;clock=endpoint
    return out


def validate_overlay(state,graph,model,overlay):
    require(isinstance(overlay,Mapping),'INVALID_DATA','overlay','overlay object required');finite_tree(overlay,'overlay')
    require(overlay.get('schema_version')==OVERLAY_VERSION and overlay.get('temporal_policy')==POLICY,'VERSION_MISMATCH','overlay.schema_version','known overlay/policy required')
    body=dict(overlay);body.pop('content_sha256',None)
    require(overlay.get('content_sha256')==sha(body),'OVERLAY_BINDING','overlay.content_sha256','integrity differs')
    event,edges,start,end=event_interval(state)
    expected={'scenario_id':state.scenario_id,'initial_state_sha256':sha(state.to_dict()),'event_id':event.event_id,'event_sha256':sha(event.to_dict()),
        'start_us':start,'end_us':end,'affected_edge_ids':edges,'affected_edges_sha256':sha(edges),'edge_count':len(edges),
        'source_pins':model.source_pins,'source_hashes':graph.source_hashes,
        'source_versions':{'routing':state.routing_version,'features':state.features_version,'context':state.context_version},
        'fixture_raw_sha256':state.fixture_raw_sha256,'catalog_raw_sha256':state.catalog_raw_sha256,'receipt_sha256':state.receipt_sha256,
        'traffic_policy':'frozen-decision-epoch','synthetic':True,'expiry_policy':'SYNTHETIC_RETURN_TO_PINNED_BASELINE',
        'units':{'distance':'m','duration':'signed us','exposure':'relative exposure proxy (km × edgeProxy)','rain':'absolute mm per hour'}}
    require(overlay.get('synthetic') is True,'INVALID_DATA','overlay.synthetic','boolean synthetic flag required')
    for key in ('start_us','end_us','edge_count'):integer(overlay.get(key),'overlay.'+key)
    for k,v in expected.items():require(overlay.get(k)==v,'OVERLAY_BINDING','overlay.'+k,'trusted source/event overlay differs')
    table=overlay.get('cost_table');require(isinstance(table,Mapping) and set(table)==set(edges),'OVERLAY_BINDING','overlay.cost_table','exact directed edge set required')
    for eid in edges:
        row=model.ingredients(graph,eid)
        wanted={k:deepcopy(row[k]) for k in ('baseline','wet','length_m','from_node','to_node','pre_weather_travel_hours','time_bucket','feature_flags')}
        wanted['expired']=deepcopy(row['baseline'])
        require(table[eid]==wanted,'OVERLAY_FORMULA','overlay.cost_table.'+eid,'raw pinned formula/flags differ (self-rehash is not authority)')


def check_temporal_route(state,graph,route,orders,after,model,overlay):
    def edge_checker(edge,a,start,progress,incoming,path):
        row=model.ingredients(graph,edge['edgeId']);exact=fraction(progress,path+'.fraction_start')
        snapshot=next(v for v in after['vehicles'] if v['vehicle_id']==route['vehicle_id'])
        position=snapshot['position']
        if start==offset(state.decision_epoch,after['current_time']) and position['kind']=='ON_EDGE':
            entry=integer(position.get('edge_entry_us'),'position.edge_entry_us');exit_us=integer(position.get('expected_exit_us'),'position.expected_exit_us')
            require(exit_us>entry and entry<=start<exit_us,'TEMPORAL_PROGRESS','position','active edge anchors required')
            exact=Fraction(start-entry,exit_us-entry)
        wanted=independent_segments(row,overlay,edge['edgeId'],start,exact,incoming)
        require(a.get('temporal_policy')==POLICY and a.get('overlay_sha256')==overlay['content_sha256'],'TEMPORAL_BINDING',path+'.overlay_sha256','edge temporal identity differs')
        require(a.get('fraction_start_exact')==str(exact),'TEMPORAL_PROGRESS',path+'.fraction_start_exact','exact observed progress differs')
        close(a.get('temporal_segments'),wanted,path+'.temporal_segments')
        return {'duration_us':wanted[-1]['end_us']-start,'distance_m':sum(x['distance_m'] for x in wanted),'exposure':sum(x['exposure'] for x in wanted)}
    return route_check(state,graph,route,orders,after=after,_temporal_edge_check=edge_checker)


def validate_forecast_sample(state,graph,model,overlay,head,result,sample):
    """Raw rational segment prefix reconstruction, not producer replay."""
    issues=[]
    try:
        require(isinstance(sample,Mapping),'INVALID_DATA','sample','forecast sample object required')
        require(isinstance(sample.get('schema_version'),str) and sample['schema_version']=='task02-m1-temporal-forecast-sample/1','VERSION_MISMATCH','sample.schema_version','supported temporal forecast sample /1 required')
        require(isinstance(sample.get('temporal_policy'),str) and sample['temporal_policy']==POLICY,'TEMPORAL_BINDING','sample.temporal_policy','pinned temporal policy required')
        require(isinstance(head,Mapping),'INVALID_DATA','head','observed head object required')
        check_execution_metrics(head.get('execution_metrics'),'head.execution_metrics')
        finite_tree(sample,'sample')
        require(sample.get('forecast') is True and sample.get('observed_state') is False and sample.get('authority_advanced') is False,'FORECAST_OBSERVATION','sample','a forecast is not an observation')
        require(sample.get('head_sha256')==head['content_sha256'] and sample.get('forecast_sha256')==sha(result) and sample.get('overlay_sha256')==overlay['content_sha256'],'FORECAST_BINDING','sample.head_sha256','sample binding differs')
        q=integer(sample.get('query_us'),'sample.query_us');require(q>=offset(state.decision_epoch,head['current_time']),'INVALID_DATA','sample.query_us','cannot rewrite prefix')
        totals=deepcopy(head['execution_metrics']);deliveries=[];vehicles=[]
        for v in head['vehicles']:
            route=next((r for r in result['vehicle_routes'] if r['vehicle_id']==v['vehicle_id']),None)
            local={'distance_m':0.,'relative_exposure_proxy':0.,'cost_vnd':0.,'travel_time_us':0,'waiting_time_us':0,'service_time_us':0}
            load=v['current_load_kg'];cargo=list(v['onboard_order_ids']);active=None
            rate=next(x.cost_per_km_vnd for x in state.vehicles if x.vehicle_id==v['vehicle_id'])
            if route:
                for a in route['actions']:
                    elapsed=max(0,min(q,a['end_us'])-a['start_us'])
                    if a['kind']=='EDGE' and elapsed:
                        row=model.ingredients(graph,a['edge_id'])
                        segments=independent_segments(row,overlay,a['edge_id'],a['start_us'],Fraction(a['fraction_start_exact']),a['incoming_edge'])
                        progress=Fraction(a['fraction_start_exact'])
                        for segment in segments:
                            if q<=segment['start_us']:break
                            delta=min(Fraction(segment['fraction_end'])-Fraction(segment['fraction_start']),Fraction(min(q,segment['end_us'])-segment['start_us'],segment['full_duration_us']))
                            distance=row['length_m']*float(delta);local['distance_m']+=distance;local['relative_exposure_proxy']+=distance/1000*segment['edge_proxy'];local['cost_vnd']+=distance/1000*rate;progress+=delta
                        local['travel_time_us']+=elapsed
                        if q<a['end_us']:active={'kind':'EDGE','edge_id':a['edge_id'],'incoming_edge':a['incoming_edge'],'from_node':a['from_node'],'to_node':a['to_node'],'fraction':str(progress)}
                    elif a['kind']=='WAIT':local['waiting_time_us']+=elapsed
                    elif a['kind']=='SERVICE':
                        local['service_time_us']+=elapsed
                        if a['end_us']<=q and (a['start_us']<q or a['continuation']):load=a['load_after_kg'];cargo.remove(a['order_id']);deliveries.append(a['order_id'])
                        elif a['start_us']<q<a['end_us']:active={'kind':'SERVICE','order_id':a['order_id'],'node_id':a['node_id']}
                    elif a['kind']=='PICKUP' and a['start_us']<q:load=a['load_after_kg'];cargo.append(a['order_id'])
            vehicles.append({'vehicle_id':v['vehicle_id'],'predicted_load_kg':load,'predicted_onboard_order_ids':sorted(cargo),
                'predicted_remaining_range_m':v['remaining_range_m']-local['distance_m'],'active_forecast_action':active,'observed_head_position':v['position']})
            for k,n in local.items():totals[k]+=n
        require(sample.get('predicted_new_deliveries')==sorted(deliveries) and sample.get('actual_delivered_at_head')==sorted(o['order_id'] for o in head['orders'] if o['status']=='DELIVERED'),'COVERAGE','sample.predicted_new_deliveries','forecast/prefix deliveries differ')
        require(isinstance(sample.get('vehicles'),list) and len(sample['vehicles'])==len(vehicles),'INVALID_DATA','sample.vehicles','vehicle forecast ledger required')
        for i,wanted in enumerate(vehicles):
            actual=sample['vehicles'][i];require(isinstance(actual,Mapping),'INVALID_DATA',f'sample.vehicles[{i}]','forecast object required')
            for k,n in wanted.items():
                if type(n) in (int,float):close(actual.get(k),n,f'sample.vehicles[{i}].{k}')
                else:require(actual.get(k)==n,'PHYSICAL_MISMATCH',f'sample.vehicles[{i}].{k}','forecast structure differs')
        require(isinstance(sample.get('whole_metrics_to_query'),Mapping),'INVALID_DATA','sample.whole_metrics_to_query','scoped metric object required')
        for k,n in totals.items():close(sample['whole_metrics_to_query'].get(k),n,'sample.whole_metrics_to_query.'+k)
    except (DynamicError,RoadDataError) as e:issues.append({'code':getattr(e,'code','RAW_SOURCE_INVALID'),'path':getattr(e,'path','sample'),'message':str(e)})
    return {'validator_version':VALIDATOR_VERSION,'valid':not issues,'diagnostics':issues,'scope':'FORECAST_SAMPLE_ONLY_NOT_OBSERVED_AUTHORITY'}


def validate_rain(state,graph,domain,result,config,*,model=None,overlay=None,plan=None,accepted=None,before=None,transition=None,authority=None):
    issues=[]
    try:
        require(isinstance(authority,Mapping),'TRUSTED_AUTHORITY_REQUIRED','authority','server-persisted source-bound root required, not self-declared provenance')
        require(isinstance(state,DecisionState),'INVALID_DATA','initial_state','validated DecisionState/2 required')
        # Typed guard precedes finite-tree/canonical digests so diagnostics keep
        # the semantic field path, and bool never aliases a copied numeric zero.
        require(isinstance(result,Mapping),'INVALID_DATA','result','object required')
        require(isinstance(before,Mapping),'INVALID_DATA','before','object required')
        require(isinstance(transition,Mapping),'INVALID_DATA','transition','object required')
        after=transition.get('after_state')
        require(isinstance(after,Mapping),'INVALID_DATA','after_state','object required')
        check_execution_metrics(result.get('prefix_metrics'),'prefix_metrics')
        check_execution_metrics(before.get('execution_metrics'),'before.execution_metrics')
        check_execution_metrics(after.get('execution_metrics'),'after_state.execution_metrics')
        for key,value in [('authority',authority),('domain',domain),('result',result),('before',before),('transition',transition)]:
            require(isinstance(value,Mapping),'INVALID_DATA',key,'object required');finite_tree(value,key)
        require(isinstance(model,SourceModel),'TRUSTED_SOURCE_REQUIRED','source_model','independent pinned raw source ingredients required')
        require(state.scenario_id=='S4' and state.routing_version==graph.routing_version and state.features_version==graph.features_version and state.context_version==graph.context_version,'SOURCE_BINDING','source_versions','own S4 reader/state versions differ')
        raw_config=(Path(__file__).resolve().parents[2]/'configs/member1_profiles_step3.json').read_bytes()
        require(hashlib.sha256(raw_config).hexdigest()=='6d2dbd9b743a735e35a9f895ccfb653c26622893ef85bd4dffc914ce801438a8' and config==json.loads(raw_config),'CONFIG_BINDING','profile_config','frozen common references/weights differ')
        validate_overlay(state,graph,model,overlay)
        root=authority.get('root');require(isinstance(root,Mapping),'TRUSTED_AUTHORITY_REQUIRED','authority.root','persisted root required')
        require(authority.get('authority_sha256')==sha(root),'AUTHORITY_BINDING','authority.authority_sha256','root digest differs')
        require(root.get('initial')==state.to_dict() and root.get('plan')==plan and root.get('accepted')==accepted and root.get('before')==before and root.get('overlay')==overlay,'AUTHORITY_BINDING','authority.root','persisted evidence differs from input')
        prefix=validate_motion_state(state,plan,accepted,graph,before)
        require(prefix['valid'] is True,'PREFIX_INVALID','before',str(prefix['diagnostics']))
        require(isinstance(plan,Mapping) and isinstance(plan.get('served_orders'),list) and all(isinstance(x,str) for x in plan['served_orders']),'INVALID_DATA','pre_plan.served_orders','typed order IDs required')
        require(plan.get('scenario_id')=='S4' and plan.get('status')=='FEASIBLE' and set(plan['served_orders'])=={o.order_id for o in state.orders} and len(plan['served_orders'])==len(state.orders) and plan.get('unserved_orders')==[],'PREPLAN_BINDING','pre_plan','own full initial accepted plan required')
        event=state.pending_events[0]
        require(transition.get('schema_version')==TRANSITION_VERSION and transition.get('status')=='APPLIED','VERSION_MISMATCH','transition.schema_version','rain transition /1 required')
        for key in ('before_version','after_version'):integer(transition.get(key),'transition.'+key,positive=True)
        require(isinstance(authority.get('events'),list) and authority['events']==[transition],'TRANSITION_AUTHORITY','authority.events','exactly one persisted event required')
        require(transition.get('event_id')==event.event_id and transition.get('event_sha256')==sha(event.to_dict()),'EVENT_BINDING','transition.event_sha256','receipt event differs')
        after=transition.get('after_state');check_state(after)
        for k,v in {'authority_id':authority.get('authority_id'),'authority_sha256':authority['authority_sha256'],'before_state_sha256':before['content_sha256'],
                    'before_version':before['state_version'],'after_version':before['state_version']+1,'after_state_sha256':after['content_sha256'],
                    'decision_time':event.timestamp,'temporal_policy':POLICY,'overlay_sha256':overlay['content_sha256'],
                    'policy_id':'task02-m1-event-policy/1','policy_sha256':'70ee2b3977578cc7f69a561e92b2f92a836977f1d30afaa496ad9eefdd14aed6'}.items():
            require(transition.get(k)==v,'TRANSITION_BINDING','transition.'+k,'trusted transition binding differs')
        require(before['current_time']==event.timestamp and before['snapshot_boundary']=='BEFORE_NEW_ACTIONS','EVENT_ORDER','before.current_time','exact before-new-actions head required')
        require(authority.get('head_hash')==after['content_sha256'] and authority.get('head_version')==after['state_version'] and authority.get('state')==after,'AUTHORITY_BINDING','authority.head_hash','persisted head differs')
        # Event changes temporal context, not observed physics/history/custody.
        for key in ('orders','vehicles','execution_history','execution_metrics','current_time','decision_epoch','plan_origin_epoch','cost_epoch','source_versions','root_initial_state','accepted_plan','snapshot_boundary'):
            same=sha(after[key])==sha(before[key]) if key=='execution_metrics' else after.get(key)==before.get(key)
            require(same,'PREFIX_CHANGED','after_state.'+key,'rain rewrote observed typed frame/prefix')
        for key,value in {'before_state_sha256':before['content_sha256'],'authority_id':authority['authority_id'],'authority_sha256':authority['authority_sha256'],
            'overlay_sha256':overlay['content_sha256'],'event_sha256':sha(event.to_dict()),'leader_policy_sha256':transition['policy_sha256'],
            'head_version':transition['after_version'],'state_version':transition['after_version'],'applied_event_ids':[event.event_id],
            'pending_events':[],'temporal_commitment_policy':'HELD_EDGE_PROGRESS; RESIDUAL_ETA_RECOMPUTED_IN_FORECAST'}.items():
            require(after.get(key)==value,'TRANSITION_BINDING','after_state.'+key,'rain state delta differs')
        expected_versions={'schema_version':RESULT_VERSION,'solver_version':SOLVER_VERSION,'objective_version':'task02-m1-dynamic-objective/1','scenario_id':'S4','temporal_policy':POLICY,'overlay_sha256':overlay['content_sha256'],
            'initial_state_sha256':sha(state.to_dict()),'before_state_sha256':before['content_sha256'],'after_state_sha256':after['content_sha256'],'forecast':True,'post_event_state':None,'metric_scope':'SUFFIX_ONLY'}
        for k,v in expected_versions.items():require(k in result and result[k]==v,'RESULT_BINDING','result.'+k,'forecast binding differs')
        require(result.get('forecast') is True,'FORECAST_OBSERVATION','forecast','boolean forecast required')
        require(isinstance(result.get('profile'),str) and result['profile'] in config['profiles'],'INVALID_DATA','profile','locked profile required')
        require(isinstance(result.get('status'),str) and result['status'] in ('FEASIBLE','PARTIAL','SEARCH_LIMIT','TIME_LIMIT'),'INVALID_DATA','status','witness or honest limit status required')
        body=dict(domain);body.pop('content_sha256',None)
        require(domain.get('content_sha256')==sha(body) and result.get('domain_sha256')==domain['content_sha256'],'DOMAIN_BINDING','domain.content_sha256','domain integrity differs')
        for k,v in {'schema_version':DOMAIN_VERSION,'planner_version':SOLVER_VERSION,'scenario_id':'S4','initial_state_sha256':sha(state.to_dict()),'anchor_state_sha256':after['content_sha256'],
            'overlay_sha256':overlay['content_sha256'],'temporal_policy':POLICY,'source_hashes':graph.source_hashes,'source_versions':{'routing':state.routing_version,'features':state.features_version,'context':state.context_version},'coverage_complete':False,'finite_path_domain':True}.items():
            require(domain.get(k)==v,'DOMAIN_BINDING','domain.'+k,'finite temporal domain source/anchor differs')
        queries=domain.get('queries');require(isinstance(queries,list) and all(isinstance(q,Mapping) for q in queries),'INVALID_DATA','domain.queries','query records required')
        for i,q in enumerate(queries):
            for key in ('status','limit','objective','stage'):
                require(q.get(key) is None or isinstance(q.get(key),str),'INVALID_DATA',f'domain.queries[{i}].{key}','trace label must be string/null')
            for key in ('option_count','settled_states','generated_labels','dominated_prunes'):
                if key in q:integer(q[key],f'domain.queries[{i}].{key}');require(q[key]>=0,'INVALID_DATA',f'domain.queries[{i}].{key}','nonnegative native count required')
        require(domain.get('coverage_complete') is False and domain.get('finite_path_domain') is True,'GLOBAL_CLAIM','domain.coverage_complete','bounded domain flags must be boolean')
        require(not any(q.get('status')=='PATH_SOURCE_INVALID' for q in queries),'PATH_SOURCE_INVALID','domain.queries','source ERROR cannot be hidden by another candidate')
        columns=domain.get('columns');require(isinstance(columns,list),'INVALID_DATA','domain.columns','columns required')
        orders={o.order_id:o for o in state.orders}
        raw_totals=[check_temporal_route(state,graph,c,orders,after,model,overlay) for c in columns]
        search=result.get('search');require(isinstance(search,Mapping),'INVALID_DATA','search','finite engine telemetry object required')
        require(search.get('optimality_proven') is False and search.get('search_complete') is False,'GLOBAL_CLAIM','search','finite temporal pool cannot certify global completion/optimality')
        if result['status'] in ('SEARCH_LIMIT','TIME_LIMIT'):
            require(result.get('vehicle_routes')==[] and result.get('served_orders')==[] and result.get('planned_served')==[],'STATUS_COVERAGE','status','no witness may not carry a plan')
            require(result.get('coverage_evaluated') is False,'STATUS_COVERAGE','coverage_evaluated','no witness is not evaluated order coverage')
            return {'validator_version':VALIDATOR_VERSION,'valid':None,'validation_status':'NOT_RUN','diagnostics':[{'severity':'INFO','code':'NO_WITNESS','path':'status','message':'finite search is not infeasibility proof'}]}
        picked=check_selection(state,domain,result,config,orders,raw_totals,after=after)
        actual=sorted(o['order_id'] for o in before['orders'] if o['status']=='DELIVERED');planned=sorted(x for r in picked for x in r['order_sequence']);served=sorted(actual+planned)
        require(len(served)==len(set(served)),'DUPLICATE_SERVICE','served_orders','prefix cannot be delivered twice')
        for k,v in [('actual_delivered',actual),('planned_served',planned),('served_orders',served)]:require(result.get(k)==v,'COVERAGE',k,'raw prefix/suffix coverage differs')
        unserved=result.get('unserved_orders');require(isinstance(unserved,list),'INVALID_DATA','unserved_orders','array required')
        ids=[]
        for i,item in enumerate(unserved):
            p=f'unserved_orders[{i}]';require(isinstance(item,Mapping) and isinstance(item.get('order_id'),str),'INVALID_DATA',p,'typed order reason required');ids.append(item['order_id'])
            require(isinstance(item.get('reason'),str) and item['reason'] in ('SEARCH_INCOMPLETE','NOT_SERVED_BY_FOUND_WITNESS'),'UNSERVED_REASON',p+'.reason','only honest non-proof reasons supported')
            observed=next((o for o in before['orders'] if o['order_id']==item['order_id']),None)
            require(observed is not None,'COVERAGE',p+'.order_id','unknown order')
            require(item.get('owner_vehicle_id')==(observed['owner_vehicle_id'] if observed['status']=='ONBOARD' else None),'CUSTODY_OWNER',p+'.owner_vehicle_id','unserved custody cannot disappear')
        require(len(ids)==len(set(ids)) and set(ids)==set(orders)-set(served),'COVERAGE','unserved_orders','partition lost or duplicates orders')
        require(result['status']=='FEASIBLE' and not ids and len(served)==len(orders) or result['status']=='PARTIAL' and bool(served) and bool(ids),'STATUS_COVERAGE','status','full/partial semantics differ')
        totals={k:sum(t[k] for i,(t,_) in enumerate(raw_totals) if i in result['search']['master']['selected_route_indexes']) for k in ('total_distance_m','total_travel_time_s','total_exposure','total_cost_vnd','total_soft_lateness_s')}
        require(isinstance(result.get('metrics'),Mapping) and isinstance(result.get('whole_trajectory_metrics'),Mapping),'INVALID_DATA','metrics','scoped metrics required')
        for k,v in totals.items():close(result['metrics'].get(k),v,'metrics.'+k)
        require(sha(result['prefix_metrics'])==sha(before['execution_metrics']),'PREFIX_CHANGED','prefix_metrics','typed canonical prefix values differ')
        mapping={'total_distance_m':('distance_m',1),'total_travel_time_s':('travel_time_us',1000000),'total_exposure':('relative_exposure_proxy',1),'total_cost_vnd':('cost_vnd',1)}
        for k,(v,scale) in mapping.items():close(result['whole_trajectory_metrics'].get(k),totals[k]+before['execution_metrics'][v]/scale,'whole_trajectory_metrics.'+k)
        late=sum(max(0,(offset(state.decision_epoch,o['delivered_at'])-offset(state.decision_epoch,orders[o['order_id']].preferred_due))/1e6) for o in before['orders'] if o['status']=='DELIVERED')
        close(result['whole_trajectory_metrics'].get('total_soft_lateness_s'),totals['total_soft_lateness_s']+late,'whole_trajectory_metrics.total_soft_lateness_s')
    except (DynamicError,MotionContractError,RoadDataError) as error:
        issues.append({'severity':'ERROR','code':getattr(error,'code','RAW_SOURCE_INVALID'),'path':getattr(error,'path','$'),'message':str(error)})
    return {'validator_version':VALIDATOR_VERSION,'valid':not issues,'diagnostics':issues,
        'scopes':{'overlay_formula':not issues,'trusted_observed_prefix_and_forecast':not issues,'finite_domain_metadata':not issues,'native_source_acceptance':False},
        'scope':'RAW_SQLITE_PREFIX_OVERLAY_ALL_COLUMNS_FORECAST_PHASES; NOT_WEATHER_OBSERVATION_OR_GLOBAL_OPTIMALITY'}
