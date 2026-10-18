"""Independent whole-trajectory checker; imports no planner/applier/master.

Reads every selected and nonselected column from raw SQLite. Step4's separate
raw reconstruction checks the executed prefix; this checker independently
checks the event delta and fractional commitment plus action-by-action suffix.
"""
from copy import deepcopy
from decimal import Decimal,ROUND_CEILING,ROUND_HALF_UP
import math
import hashlib,json
from pathlib import Path
from typing import Mapping
from optimization.models.decision_state import OrderState
from optimization.models.member1_dynamic_state import DynamicError,DynamicState,DynamicResult,EventTransition,sha,finite_tree,require,STATE_VERSION,RESULT_VERSION
from optimization.models.motion_state import MotionContractError,MotionState,_instant
from optimization.integration.member1_s0_graph import RoadDataError
from optimization.integration.member1_decision_state_adapter import _order
from optimization.rolling_horizon.member1_motion_validation import validate_motion_state

VALIDATOR_VERSION='task02-m1-independent-dynamic-whole-trajectory-validator/2'

def offset(epoch,t):
    delta=_instant(t,'time')-_instant(epoch,'epoch')
    return (delta.days*86400+delta.seconds)*1000000+delta.microseconds
def us(value):return int((Decimal(str(value))*1000000).to_integral_value(rounding=ROUND_CEILING))
def close(actual,expected,path):
    if isinstance(expected,(dict,list)):
        require(actual==expected,'PHYSICAL_MISMATCH',path,'raw structure differs');return
    require(type(actual) in (int,float) and math.isfinite(float(actual)) and abs(actual-expected)<=max(1e-6,abs(expected)*1e-10),'PHYSICAL_MISMATCH',path,'raw metric differs')

def route_check(state,graph,route,orders,*,after=None,_temporal_edge_check=None):
    require(isinstance(route,Mapping),'INVALID_DATA','route','object required');finite_tree(route,'route')
    for key in ('start_node','end_node','start_us','return_us'):
        require(type(route.get(key)) is int and 0<=route[key]<2**63,'INVALID_DATA','route.'+key,'bounded integer required without bool/float aliases')
    seq=route.get('order_sequence')
    require(isinstance(seq,list) and all(isinstance(x,str) and bool(x) for x in seq) and len(seq)==len(set(seq)),'INVALID_DATA','route.order_sequence','unique order ID array required')
    vid=route.get('vehicle_id');vehicles={v.vehicle_id:v for v in state.vehicles}
    require(isinstance(vid,str) and vid in vehicles,'VEHICLE_ASSIGNMENT','route.vehicle_id','known vehicle required');v=vehicles[vid]
    snapshot=next((x for x in after['vehicles'] if x['vehicle_id']==vid),None) if after else None
    require(v.availability=='AVAILABLE' and (snapshot is None or snapshot['availability']=='AVAILABLE'),'AVAILABILITY','route.vehicle_id','unavailable vehicle has suffix')
    cargo=set(v.onboard_order_ids if snapshot is None else snapshot['onboard_order_ids']);load=v.current_load_kg if snapshot is None else snapshot['current_load_kg']
    depot=state.depot.graph_node_id
    node=depot if snapshot is None else snapshot['position'].get('node_id',snapshot['position'].get('from_node'))
    previous=None if snapshot is None else snapshot['position'].get('incoming_edge')
    begin=max(0,offset(state.decision_epoch,v.working_start),offset(state.decision_epoch,state.depot.opening_time)) if after is None else offset(state.decision_epoch,after['current_time'])
    upper=min(offset(state.decision_epoch,v.working_end),offset(state.decision_epoch,state.depot.closing_time))
    bound=v.remaining_range_m if snapshot is None else snapshot['remaining_range_m']
    actions=route.get('actions');require(isinstance(actions,list),'INVALID_DATA','route.actions','array required')
    for i,action in enumerate(actions):require(isinstance(action,Mapping),'INVALID_DATA',f'route.actions[{i}]','action object required before commitment lookup')
    totals={'total_distance_m':0.,'total_travel_time_s':0.,'total_exposure':0.,'total_cost_vnd':0.,'total_soft_lateness_s':0.}
    clock=begin;delivered=[];pickup=[];completion={}
    commitment=snapshot.get('active_commitment') if snapshot else None
    require(not commitment or bool(actions),'COMMITMENT','route.actions','unfinished available commitment requires its connector')
    if snapshot and snapshot.get('explicit_committed_stop_id') is not None:
        commit=snapshot['explicit_committed_stop_id'];fulfilled={o['order_id'] for o in after['orders'] if o['status']=='DELIVERED'}
        first=next((a.get('order_id') for a in actions if a.get('kind')=='SERVICE'),None)
        require(commit in fulfilled or first==commit,'COMMITMENT','route.order_sequence','an unfulfilled explicit committed stop must be the next delivery')
    for i,a in enumerate(actions):
        p=f'route.actions[{i}]';require(isinstance(a,Mapping),'INVALID_DATA',p,'object required')
        kind=a.get('kind');require(isinstance(kind,str) and kind in ('EDGE','WAIT','SERVICE','PICKUP'),'INVALID_DATA',p+'.kind','known action required')
        for key in (('from_node','to_node') if kind=='EDGE' else ('node_id',)):
            require(type(a.get(key)) is int and 0<a[key]<2**63,'INVALID_DATA',p+'.'+key,'positive graph node integer required')
        require(type(a.get('start_us')) is int and type(a.get('end_us')) is int and 0<=a['start_us']<=a['end_us']<2**63,'INVALID_DATA',p+'.start_us','bounded integer timeline required')
        require(a['start_us']==clock,'TIME_CONTINUITY',p+'.start_us','action gap or overlap');end=a['end_us']
        require(kind=='WAIT' or clock>=offset(state.decision_epoch,v.working_start),'WORKING_WINDOW',p+'.start_us','action before vehicle opens')
        if i==0 and commitment:
            require((kind=='EDGE' and commitment['kind']=='EDGE' and a.get('edge_id')==commitment['edge_id']) or (kind=='SERVICE' and commitment['kind']=='SERVICE' and a.get('order_id')==commitment['order_id']),'COMMITMENT',p,'active commitment must finish first')
            if kind!='EDGE' or _temporal_edge_check is None:
                require(end==commitment['until_us'],'COMMITMENT',p+'.end_us','active completion differs')
        if kind=='EDGE':
            eid=a.get('edge_id');require(isinstance(eid,str),'INVALID_DATA',p+'.edge_id','edge ID required')
            raw=graph.edge(eid);require(raw is not None,'EDGE_MISSING',p+'.edge_id','edge absent')
            edge=graph._checked_edge(raw)
            require(edge['fromNodeId']==node and a.get('from_node')==node and a.get('to_node')==edge['toNodeId'],'EDGE_CONTINUITY',p,'directed endpoint differs')
            require(a.get('incoming_edge')==previous and (previous,eid) not in graph.forbidden,'FORBIDDEN_TURN',p+'.incoming_edge','incoming state/turn differs')
            fraction=0.
            if i==0 and snapshot and snapshot['position']['kind']=='ON_EDGE':fraction=snapshot['position']['progress']
            close(a.get('fraction_start'),fraction,p+'.fraction_start');close(a.get('fraction_end'),1.,p+'.fraction_end')
            duration=us(Decimal(str(edge['travelTimeHours']))*3600)
            expected=duration-round(duration*fraction)
            temporal=None
            if _temporal_edge_check is not None:
                temporal=_temporal_edge_check(edge,a,clock,fraction,previous,p)
                expected=temporal['duration_us']
            require(end-clock==expected,'EDGE_TIME',p+'.end_us','raw conservative duration differs')
            geometry=[[point.longitude,point.latitude] for point in edge['points']]
            close(a.get('geometry'),geometry,p+'.geometry');close(a.get('feature_payload'),edge['payload'],p+'.feature_payload')
            distance=edge['lengthKm']*1000*(1-fraction);exposure=edge['relativeExposure']*(1-fraction)
            if temporal is not None:distance,exposure=temporal['distance_m'],temporal['exposure']
            close(a.get('distance_m'),distance,p+'.distance_m');close(a.get('exposure'),exposure,p+'.exposure')
            totals['total_distance_m']+=distance;totals['total_travel_time_s']+=(end-clock)/1e6;totals['total_exposure']+=exposure
            node=edge['toNodeId'];previous=eid
        elif kind=='WAIT':
            require(a.get('node_id')==node,'POSITION',p+'.node_id','wait position differs')
        else:
            oid=a.get('order_id');require(isinstance(oid,str) and oid in orders,'ORDER_ID',p+'.order_id','known order required');o=orders[oid]
            require(a.get('node_id')==node,'POSITION',p+'.node_id','action position differs')
            if kind=='PICKUP':
                from optimization.runtime.delivery_day import pickup_depot
                origin=pickup_depot(state,o)
                observed=next((x for x in after['orders'] if x['order_id']==oid),None) if after else None
                status=o.status if observed is None else observed['status']
                require(node==origin.graph_node_id and status=='WAITING' and oid not in cargo and oid not in pickup and oid not in delivered,'DEPOT_PICKUP',p,'WAITING pickup only at its origin depot')
                if after and 'delivery_day' in after:
                    require(oid not in after['delivery_day']['queue'],'DAY_QUEUE',p,'deferred goods cannot be picked up today')
                    day=after['delivery_day']
                    require(o.pickup_location_id not in day.get('pickup_visits',{}).get(vid,[]) or oid in day['urgent_ids'],'DAILY_PICKUP',p,'repeat pickup is allowed only for urgent goods')
                ready=0
                if after and oid not in {x.order_id for x in state.orders}:ready=max(ready,offset(state.decision_epoch,after['current_time']))
                require(end==clock and clock>=ready and clock>=offset(state.decision_epoch,state.depot.opening_time),'PICKUP_READY',p,'zero dwell cannot bypass ready time')
                cargo.add(oid);load+=o.demand_kg;pickup.append(oid)
            else:
                require(node==o.graph_node_id and oid in cargo and oid not in delivered,'CUSTODY_OWNER',p,'delivery requires own cargo at source node')
                continuation=i==0 and commitment is not None and commitment['kind']=='SERVICE'
                require(a.get('continuation') is continuation,'COMMITMENT',p+'.continuation','service continuation claim differs')
                require(end-clock==(commitment['until_us']-clock if continuation else us(o.service_time_seconds)),'SERVICE_TIME',p,'service duration differs')
                require(clock>=offset(state.decision_epoch,o.earliest) and end<=offset(state.decision_epoch,o.hard_deadline),'HARD_DEADLINE',p,'service completion exceeds hard deadline')
                cargo.remove(oid);load-=o.demand_kg;delivered.append(oid);completion[oid]=end
                totals['total_soft_lateness_s']+=max(0,(end-offset(state.decision_epoch,o.preferred_due))/1e6)
            close(a.get('load_after_kg'),load,p+'.load_after_kg')
        require(-1e-9<=load<=v.capacity_kg+1e-9,'CAPACITY',p,'action load outside capacity')
        require(end<=upper,'WORKING_WINDOW',p+'.end_us','action after close')
        clock=end
    require(node==depot,'DEPOT_RETURN','route.end_node','active available route must return')
    require(route.get('end_node')==depot and route.get('start_node')==(depot if snapshot is None else snapshot['position'].get('node_id',snapshot['position'].get('from_node'))),'POSITION','route.start_node','route anchor differs')
    require(route.get('start_us')==begin and route.get('return_us')==clock,'TIME_CONTINUITY','route.return_us','route timing differs')
    require(route.get('order_sequence')==delivered,'SERVICE_SEQUENCE','route.order_sequence','service order differs')
    if after and 'delivery_day' in after:
        urgent=set(after['delivery_day']['urgent_ids'])
        urgent_served=[oid for oid in delivered if oid in urgent]
        if urgent_served:
            require(all(oid in urgent for oid in delivered[:len(urgent_served)]),'URGENT_PRIORITY','route.order_sequence','assigned urgent orders must be delivered first')
            first_service=next((i for i,a in enumerate(actions) if a['kind']=='SERVICE'),len(actions))
            require(all(i<first_service for i,a in enumerate(actions) if a['kind']=='PICKUP' and a['order_id'] in urgent),'URGENT_PICKUP','route.actions','return for urgent before serving regular orders')
    require(totals['total_distance_m']<=bound+1e-7,'RANGE','route.total_distance_m','full suffix return exceeds remaining range')
    totals['total_cost_vnd']=totals['total_distance_m']/1000*v.cost_per_km_vnd
    close(route.get('return_load_kg'),load,'route.return_load_kg')
    for k,x in totals.items():close(route.get(k),x,'route.'+k)
    return totals,completion

def validate_dynamic(state,graph,domain,result,config,*,plan=None,accepted=None,before=None,transition=None,authority=None):
    issues=[]
    try:
        require(isinstance(result,Mapping),'INVALID_DATA','$','object required')
        DynamicResult.from_dict(result)
        config_path=Path(__file__).resolve().parents[2]/'configs/member1_profiles_step3.json'
        raw_config=config_path.read_bytes()
        require(hashlib.sha256(raw_config).hexdigest()=='6d2dbd9b743a735e35a9f895ccfb653c26622893ef85bd4dffc914ce801438a8' and config==json.loads(raw_config),'CONFIG_BINDING','profile_config','locked common normalization/profile config differs')
        require(isinstance(domain,Mapping),'INVALID_DATA','domain','domain object required');finite_tree(domain,'domain')
        require(result.get('schema_version')==RESULT_VERSION,'VERSION_MISMATCH','schema_version','internal dynamic /2 required')
        require(state.routing_version==graph.routing_version and state.features_version==graph.features_version and state.context_version==graph.context_version,'SOURCE_BINDING','source_versions','raw SQLite reader/source state differs')
        require(result.get('scenario_id')==state.scenario_id and result.get('initial_state_sha256')==sha(state.to_dict()),'SOURCE_BINDING','initial_state_sha256','own initial state differs')
        material=deepcopy(domain);material.pop('content_sha256',None)
        require(domain.get('content_sha256')==sha(material) and result.get('domain_sha256')==domain['content_sha256'],'DOMAIN_BINDING','domain_sha256','domain differs')
        require(domain.get('source_hashes')==graph.source_hashes and domain.get('initial_state_sha256')==sha(state.to_dict()),'SOURCE_BINDING','domain.source_hashes','domain source differs')
        require(domain.get('source_versions')=={'routing':state.routing_version,'features':state.features_version,'context':state.context_version} and domain.get('schema_version')=='task02-m1-dynamic-physical-domain/1','SOURCE_BINDING','domain.source_versions','domain version/context differs')
        require(domain.get('planner_version')==result['solver_version'],'VERSION_MISMATCH','domain.planner_version','domain and result method must agree')
        after=None;actual=[];orders={o.order_id:o for o in state.orders}
        if before is not None:
            require(isinstance(authority,Mapping),'TRUSTED_AUTHORITY_REQUIRED','authority','persisted independent authority required')
            root=authority.get('root');require(isinstance(root,Mapping),'TRUSTED_AUTHORITY_REQUIRED','authority.root','persisted root missing')
            require(authority.get('authority_sha256')==sha(root),'AUTHORITY_BINDING','authority.authority_sha256','persisted root digest differs')
            require(root.get('initial')==state.to_dict() and root.get('plan')==plan and root.get('accepted')==accepted and root.get('before')==before,'AUTHORITY_BINDING','authority.root','root differs from persisted trusted input')
            parents=root.get('parents',[]);require(isinstance(parents,list),'INVALID_DATA','authority.root.parents','trusted parent array required')
            parent_models=[MotionState.from_dict(p) for p in parents]
            check=validate_motion_state(state,plan,accepted,graph,before,trusted_parent=parent_models[-1] if parent_models else None,trusted_parent_chain=parent_models[:-1])
            require(check['valid'] is True,'PREFIX_INVALID','before_state',str(check['diagnostics']))
            require(isinstance(authority.get('events'),list),'INVALID_DATA','authority.events','ledger array required')
            require(isinstance(transition,Mapping) and transition in authority['events'],'TRANSITION_AUTHORITY','transition','receipt not in persisted exactly-once ledger')
            EventTransition.from_dict(transition)
            after=transition['after_state'];DynamicState.from_dict(after)
            require(transition.get('authority_id')==authority.get('authority_id') and transition.get('authority_sha256')==authority['authority_sha256'] and after.get('authority_sha256')==authority['authority_sha256'],'AUTHORITY_BINDING','transition.authority_sha256','receipt authority differs')
            require(domain.get('anchor_state_sha256')==after['content_sha256'],'DOMAIN_BINDING','domain.anchor_state_sha256','dynamic domain uses a different physical anchor')
            require(transition.get('before_state_sha256')==before['content_sha256'] and transition.get('after_version')==before['state_version']+1 and result.get('before_state_sha256')==before['content_sha256'] and result.get('after_state_sha256')==after['content_sha256'],'TRANSITION_BINDING','transition','before/after/lineage differs')
            event=state.pending_events[0]
            policy='70ee2b3977578cc7f69a561e92b2f92a836977f1d30afaa496ad9eefdd14aed6'
            require(transition.get('policy_id')=='task02-m1-event-policy/1' and transition.get('policy_sha256')==policy and after.get('leader_policy_sha256')==policy,'POLICY_BINDING','transition.policy_sha256','locked Leader policy differs')
            require(after.get('before_state_sha256')==before['content_sha256'] and after.get('head_version')==transition['after_version'] and after.get('state_version')==transition['after_version'],'TRANSITION_BINDING','after_state.head_version','head/version differs')
            for key in ('current_time','decision_epoch','plan_origin_epoch','cost_epoch','source_versions','root_initial_state','accepted_plan','snapshot_boundary'):
                require(after.get(key)==before.get(key),'FRAME_BINDING','after_state.'+key,'event changed source/physical frame')
            require(after.get('pending_events')==[e for e in before['pending_events'] if e['event_id']!=event.event_id],'EVENT_BINDING','after_state.pending_events','applied event must leave pending set')
            require(transition.get('event_sha256')==sha(event.to_dict()),'EVENT_BINDING','transition.event_sha256','pending event differs')
            require(after['execution_history']==before['execution_history'] and after['execution_metrics']==before['execution_metrics'] and after['applied_event_ids']==[event.event_id],'PREFIX_CHANGED','after_state','executed prefix/applied ledger differs')
            expected_orders=deepcopy(before['orders']);expected_vehicles=deepcopy(before['vehicles'])
            if state.scenario_id=='S2':
                o=_order(event.to_dict()['payload']['orderPayload'],0,'event.orderPayload');orders[o.order_id]=o
                expected_orders.append({'order_id':o.order_id,'status':'WAITING','owner_vehicle_id':None,'picked_up_at':None,'delivered_at':None,'demand_kg':o.demand_kg,'provenance':'PINNED_EVENT_SYNTHETIC_READY_AT_DEPOT'})
                require(after.get('event_order')==o.to_dict(),'EVENT_BINDING','after_state.event_order','raw birth data differs')
            else:
                v=next(x for x in expected_vehicles if x['vehicle_id']=='V1')
                v.update(availability='UNAVAILABLE',suspended_commitment=v['active_commitment'],active_commitment=None,suspension_reason='VEHICLE_UNAVAILABLE_NO_RECOVERY',activity='IMMOBILIZED')
            require(after['orders']==expected_orders and after['vehicles']==expected_vehicles,'EVENT_PHYSICS','after_state','custody/position/event delta differs')
            actual=sorted(o['order_id'] for o in before['orders'] if o['status']=='DELIVERED')
        if result['status'] in ('SEARCH_LIMIT','TIME_LIMIT'):
            require(result['vehicle_routes']==[] and result['served_orders']==[] and result['planned_served']==[],'STATUS_COVERAGE','status','no-witness limit cannot carry a plan')
            return {'validator_version':VALIDATOR_VERSION,'validation_status':'NOT_RUN','valid':None,'diagnostics':[{'severity':'INFO','code':'NO_WITNESS','path':'status','message':'search limit is not an infeasibility certificate'}],'scope':'NO_WITNESS; NOT_FEASIBILITY_PROOF'}
        columns=domain.get('columns');require(isinstance(columns,list),'INVALID_DATA','domain.columns','array required')
        normalized=[]
        for i,c in enumerate(columns):
            totals,ends=route_check(state,graph,c,orders,after=after)
            weights=config['profiles'][result['profile']];refs=config['references']
            score=Decimal(str(weights['time']))*Decimal(str(totals['total_travel_time_s']))/Decimal(str(refs['travel_time_s']))+Decimal(str(weights['distance']))*Decimal(str(totals['total_distance_m']))/Decimal(str(refs['distance_m']))+Decimal(str(weights['risk']))*Decimal(str(totals['total_exposure']))/Decimal(str(refs['relative_exposure_proxy']))
            normalized.append({'served':len(ends),'priority':sum(orders[x].priority for x in ends),'late':math.ceil(totals['total_soft_lateness_s']*1000),'profile':int((score*config['score_scale']).quantize(Decimal(1),rounding=ROUND_HALF_UP)),'time':math.ceil(totals['total_travel_time_s']*1000),'distance':math.ceil(totals['total_distance_m']*1000),'risk':math.ceil(totals['total_exposure']*1e6),'plan_key':i+1})
        search=result.get('search');require(isinstance(search,Mapping),'INVALID_DATA','search','metadata required')
        require(search.get('search_complete') is False and search.get('optimality_proven') is False and search.get('truncated') is True,'GLOBAL_CLAIM','search','finite domain cannot prove global completeness/optimality')
        master=search.get('master');require(isinstance(master,Mapping),'INVALID_DATA','search.master','master metadata required')
        phases=master.get('phases');require(isinstance(phases,list),'INVALID_DATA','search.master.phases','phase array required')
        tiers=[('served',True),('priority',True),('late',False),('profile',False)]+[(x,False) for x in config['profiles'][result['profile']]['tie_break']]+[('plan_key',False)]
        expected_master='task02-m1-dynamic-route-column-master/'+result['solver_version'].rsplit('/',1)[1]
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
            require(isinstance(k,str) and k in ('served','priority','late','profile','time','distance','risk','plan_key'),'MASTER_BINDING','search.master.phases.objective_key','objective key required')
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
        planned=sorted(x for r in picked for x in r['order_sequence']);served=sorted(actual+planned)
        require(len(served)==len(set(served)),'DUPLICATE_SERVICE','served_orders','prefix redelivery/duplicate suffix')
        require(result.get('actual_delivered')==actual and result.get('planned_served')==planned and result.get('served_orders')==served,'COVERAGE','served_orders','whole trajectory coverage differs')
        unserved=result.get('unserved_orders');require(isinstance(unserved,list),'INVALID_DATA','unserved_orders','array required')
        ids=[x.get('order_id') for x in unserved if isinstance(x,Mapping)]
        require(len(ids)==len(unserved) and all(isinstance(x,str) and bool(x) for x in ids),'INVALID_DATA','unserved_orders.order_id','string identifiers required')
        require(len(ids)==len(set(ids)) and set(ids)==set(orders)-set(served),'COVERAGE','unserved_orders','partition lost/duplicate/unknown order')
        for i,item in enumerate(unserved):
            reason=item.get('reason');p=f'unserved_orders[{i}].reason'
            require(isinstance(reason,str) and reason in ('CUSTODY_BLOCKED','SEARCH_INCOMPLETE','NOT_SERVED_BY_FOUND_WITNESS'),'UNSERVED_REASON',p,'unsupported reason')
            if reason=='CUSTODY_BLOCKED':
                require(after is not None,'CUSTODY_PROOF',p,'custody requires post-event state')
                o=next(x for x in after['orders'] if x['order_id']==item['order_id'])
                require(o['status']=='ONBOARD' and item.get('owner_vehicle_id')==o['owner_vehicle_id'] and any(v['vehicle_id']==o['owner_vehicle_id'] and v['availability']=='UNAVAILABLE' for v in after['vehicles']),'CUSTODY_PROOF',p,'owner/unavailable evidence missing')
            elif after:
                o=next(x for x in after['orders'] if x['order_id']==item['order_id'])
                require(item.get('owner_vehicle_id')==(o['owner_vehicle_id'] if o['status']=='ONBOARD' else None),'CUSTODY_OWNER',f'unserved_orders[{i}].owner_vehicle_id','observed ownership cannot disappear from report')
        status=result.get('status')
        no_service=status=='NO_SERVICE' and not served and len(ids)==len(orders) and all(x['reason']=='CUSTODY_BLOCKED' for x in unserved)
        if no_service:
            require(result.get('no_service_certificate')=={'kind':'ALL_REMAINING_CUSTODY_BLOCKED','order_ids':sorted(orders),'after_state_sha256':after['content_sha256']},'NO_SERVICE_CERTIFICATE','no_service_certificate','independent all-custody-blocked certificate required')
        require(isinstance(status,str) and (status=='FEASIBLE' and len(served)==len(orders) and not ids or status=='PARTIAL' and bool(served) and bool(ids) or no_service),'STATUS_COVERAGE','status','witness status incompatible with coverage')
        totals={k:sum(r[k] for r in picked) for k in ('total_distance_m','total_travel_time_s','total_exposure','total_cost_vnd','total_soft_lateness_s')}
        for k,v in totals.items():close(result.get('metrics',{}).get(k),v,'metrics.'+k)
        if before:
            require(result.get('prefix_metrics')==before['execution_metrics'],'PREFIX_CHANGED','prefix_metrics','prefix metric differs')
            require(isinstance(result.get('whole_trajectory_metrics'),Mapping),'INVALID_DATA','whole_trajectory_metrics','whole metric object required')
            mapping={'total_distance_m':('distance_m',1),'total_travel_time_s':('travel_time_us',1000000),'total_exposure':('relative_exposure_proxy',1),'total_cost_vnd':('cost_vnd',1)}
            for k,(v,scale) in mapping.items():close(result.get('whole_trajectory_metrics',{}).get(k),totals[k]+before['execution_metrics'][v]/scale,'whole_trajectory_metrics.'+k)
            late=sum(max(0,(offset(state.decision_epoch,o['delivered_at'])-offset(state.decision_epoch,orders[o['order_id']].preferred_due))/1e6) for o in before['orders'] if o['status']=='DELIVERED')
            close(result.get('whole_trajectory_metrics',{}).get('total_soft_lateness_s'),totals['total_soft_lateness_s']+late,'whole_trajectory_metrics.total_soft_lateness_s')
    except (DynamicError,MotionContractError,RoadDataError) as e:
        issues.append({'severity':'ERROR','code':getattr(e,'code','RAW_SOURCE_INVALID'),'path':getattr(e,'path','$'),'message':str(e)})
    return {'validator_version':VALIDATOR_VERSION,'valid':not issues,'diagnostics':issues,'scope':'RAW_PREFIX_EVENT_COMMITMENT_SUFFIX_WITNESS; NOT_GLOBAL_OPTIMALITY'}
