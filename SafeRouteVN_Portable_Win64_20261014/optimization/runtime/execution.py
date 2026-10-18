"""Observed SIMULATED_REPLAY of persisted accepted actions, no solver/search.

Change Impact Analysis: additive runtime observation /2, not MotionState/1.
An activation has an immutable physical anchor. Direct/incremental replay is
always from that anchor, preserving the previously executed prefix exactly.
At a timestamp, completed actions occur; new zero-duration pickups wait until
the next microsecond (completion -> event -> new action policy).
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal,ROUND_CEILING
from fractions import Fraction
from optimization.rolling_horizon.member1_motion_replay import interpolate_directed_geometry
from optimization.rolling_horizon.member1_rain_source import source_edge_rates
from optimization.rolling_horizon.member1_rain_temporal import traverse
from optimization.models.member1_dynamic_state import sha as physical_sha
from .protocol import require,offset,instant,sha

VERSION='task02-m2-runtime-observation/2'
REPLAY_VERSION='task02-m2-causal-action-replay/3'
METRICS=('distance_m','relative_exposure_proxy','cost_vnd','travel_time_us','waiting_time_us','service_time_us')

def timestamp(epoch,us):return (instant(epoch,'epoch')+timedelta(microseconds=us)).isoformat()

def initial_observation(state):
    orders=[{'order_id':o.order_id,'status':o.status,'owner_vehicle_id':o.assigned_vehicle_id,'picked_up_at':o.picked_up_at,'delivered_at':o.delivered_at,'demand_kg':o.demand_kg} for o in state.orders]
    vehicles=[]
    for v in state.vehicles:
        vehicles.append({'vehicle_id':v.vehicle_id,'availability':v.availability,'capacity_kg':v.capacity_kg,'current_load_kg':v.current_load_kg,
          'onboard_order_ids':list(v.onboard_order_ids),'remaining_range_m':v.remaining_range_m,'position_timestamp':state.current_time,
          'position':{'kind':'AT_NODE','node_id':v.current_position_node_id,'coordinates':list(v.current_position_coordinates),'incoming_edge':None,'position_source':'SIMULATED'},
          'activity':'IDLE_AT_DEPOT','active_commitment':None,'explicit_committed_stop_id':v.committed_stop_id,'planned_suffix':list(v.onboard_order_ids),
          'executed_metrics':{k:0 for k in METRICS}})
    value={'schema_version':VERSION,'scenario_id':state.scenario_id,'current_time':state.current_time,'decision_epoch':state.decision_epoch,
           'orders':orders,'vehicles':vehicles,'execution_metrics':{k:0 for k in METRICS},'execution_history':[],
           'pending_events':[e.to_dict() for e in state.pending_events],'applied_event_ids':[], 'state_version':state.state_version,
           'source_versions':{'routing':state.routing_version,'features':state.features_version,'context':state.context_version},
           'execution_mode':'SIMULATED_REPLAY','real_world_observation':False,'snapshot_boundary':'BEFORE_NEW_ACTIONS'}
    value['content_sha256']=physical_sha(value);return value

def replay_actions(state,graph,anchor,routes,target,activation_id,*,model=None,overlay=None,previous_history=(),confirmations=None,dispatch_frame=None):
    from .delivery_day import effective_state,pickup_depot
    state=effective_state(state,anchor)
    catalog={o.order_id:o for o in state.orders}
    begin=offset(state.decision_epoch,anchor['current_time']);q=offset(state.decision_epoch,target)
    require(q>=begin,'TIME_REWIND','target_time','cannot rewrite accepted prefix')
    for e in anchor['pending_events']:require(q<=offset(state.decision_epoch,e['timestamp']),'EVENT_TRANSITION_REQUIRED','target_time','pending event barrier')
    out=deepcopy(anchor);out.pop('content_sha256',None)
    if 'delivery_day' in out:
        if dispatch_frame:
            out['delivery_day']=deepcopy(dispatch_frame['delivery_day'])
            known={o['order_id'] for o in out['orders']}
            out['orders'].extend(deepcopy(o) for o in dispatch_frame['orders'] if o['order_id'] not in known)
        out['delivery_day']['confirmations'].update(confirmations or {})
    orders={o['order_id']:o for o in out['orders']};vehicles={v['vehicle_id']:v for v in out['vehicles']}
    rates={v.vehicle_id:v.cost_per_km_vnd for v in state.vehicles};new_history=[];causal_ordinal={}
    for route in routes:
        v=vehicles[route['vehicle_id']];local={k:0 for k in METRICS};node=v['position'].get('node_id',v['position'].get('from_node'));incoming=v['position'].get('incoming_edge')
        cargo=set(v['onboard_order_ids']);load=v['current_load_kg'];active=None;position=None;activity='IDLE_AT_DEPOT';completed=[]
        if 'delivery_day' in anchor:v['awaiting_delivery']=None
        for ordinal,a in enumerate(route['actions']):
            start,end=a['start_us'],a['end_us'];kind=a['kind']
            if q<=start:break
            stop=min(q,end);elapsed=stop-start;done=q>=end
            aid=sha({'activation_id':activation_id,'vehicle_id':v['vehicle_id'],'ordinal':ordinal,'action':a})
            causal_ordinal[aid]=ordinal
            if kind=='EDGE':
                raw=graph._checked_edge(graph.edge(a['edge_id']));p=Fraction(a.get('fraction_start_exact',str(a['fraction_start'])))
                if overlay is not None:
                    edge_rates=source_edge_rates(state,graph,model,overlay,a['edge_id'])
                    part=traverse(edge_rates,start,progress=p,until_us=stop,incoming_edge=incoming,cost_per_km_vnd=rates[v['vehicle_id']]);progress=Fraction(part['fraction_end'])
                    distance,exposure=part['distance_m'],part['exposure'];segments=part['segments']
                else:
                    duration=int((Decimal(str(raw['travelTimeHours']))*3600*1000000).to_integral_value(rounding=ROUND_CEILING))
                    progress=Fraction(1) if done else p+Fraction(elapsed,duration)
                    distance=raw['lengthKm']*1000*float(progress-p);exposure=raw['relativeExposure']*float(progress-p);segments=[]
                local['distance_m']+=distance;local['relative_exposure_proxy']+=exposure;local['cost_vnd']+=distance/1000*rates[v['vehicle_id']];local['travel_time_us']+=elapsed
                geometry=[[point.longitude,point.latitude] for point in raw['points']]
                if done:node=raw['toNodeId'];incoming=a['edge_id']
                else:
                    position={'kind':'ON_EDGE','edge_id':a['edge_id'],'from_node':raw['fromNodeId'],'to_node':raw['toNodeId'],'incoming_edge':incoming,
                              'progress':float(progress),'progress_exact':str(progress),'coordinates':interpolate_directed_geometry(geometry,float(progress)),
                              'edge_entry_us':start,'expected_exit_us':end,'position_source':'SIMULATED'}
                    active={'kind':'EDGE','edge_id':a['edge_id'],'to_node':raw['toNodeId'],'until_us':end};activity='MOVING'
                if elapsed:new_history.append({'action_id':aid,'vehicle_id':v['vehicle_id'],'kind':'EDGE_PROGRESS','start_us':start,'end_us':stop,'completed':done,'fraction_end':str(progress),'temporal_segments':segments})
            elif kind=='PICKUP':
                o=orders[a['order_id']];origin=pickup_depot(state,catalog[o['order_id']])
                require(o['status']=='WAITING' and node==origin.graph_node_id,'CUSTODY_OWNER','actions','pickup at origin depot from WAITING only')
                cargo.add(o['order_id']);load=float(sum((Decimal(str(orders[x]['demand_kg'])) for x in sorted(cargo)),Decimal(0)));o.update(status='ONBOARD',owner_vehicle_id=v['vehicle_id'],picked_up_at=timestamp(state.decision_epoch,start))
                if 'delivery_day' in out:
                    visits=out['delivery_day']['pickup_visits'].setdefault(v['vehicle_id'],[])
                    if origin.location_id not in visits:visits.append(origin.location_id)
                new_history.append({'action_id':aid,'vehicle_id':v['vehicle_id'],'order_id':o['order_id'],'kind':'PICKUP','at_us':start})
            elif kind=='SERVICE':
                local['service_time_us']+=elapsed
                if done and 'delivery_day' in anchor and a['order_id'] not in out['delivery_day']['confirmations']:
                    v['awaiting_delivery']=a['order_id'];activity='AWAITING_DELIVERY'
                    active={'kind':'SERVICE','order_id':a['order_id'],'node_id':node,'until_us':end}
                    break
                if done:
                    o=orders[a['order_id']];require(o['order_id'] in cargo and o['owner_vehicle_id']==v['vehicle_id'],'CUSTODY_OWNER','actions','cannot transfer or create cargo')
                    cargo.remove(o['order_id']);load=float(sum((Decimal(str(orders[x]['demand_kg'])) for x in sorted(cargo)),Decimal(0)));o.update(status='DELIVERED',delivered_at=timestamp(state.decision_epoch,end));completed.append(o['order_id'])
                    new_history.append({'action_id':aid,'vehicle_id':v['vehicle_id'],'order_id':o['order_id'],'kind':'DELIVERY','at_us':end})
                else:active={'kind':'SERVICE','order_id':a['order_id'],'node_id':node,'until_us':end};activity='SERVICING'
            else:
                require(kind=='WAIT','INVALID_DATA','actions.kind','known accepted action required');local['waiting_time_us']+=elapsed
                if not done:activity='WAITING_AT_STOP'
            if not done:break
        if position is None:position={'kind':'AT_NODE','node_id':node,'coordinates':list(graph.node(node)),'incoming_edge':incoming,'position_source':'SIMULATED'}
        v.update(position=position,position_timestamp=target,activity=activity,active_commitment=active,current_load_kg=load,onboard_order_ids=sorted(cargo),
                 remaining_range_m=v['remaining_range_m']-local['distance_m'],planned_suffix=[o for o in route['order_sequence'] if o not in completed and orders[o]['status']!='DELIVERED'])
        for k in METRICS:v['executed_metrics'][k]+=local[k];out['execution_metrics'][k]+=local[k]
    # No route means stationary unavailable/depot vehicle, not disappearance.
    for v in out['vehicles']:v['position_timestamp']=target
    # Time orders independent vehicles; the accepted action index, NOT a hash
    # or action-kind priority, orders each vehicle's tied physical boundaries.
    ordered=sorted(new_history,key=lambda x:(x.get('at_us',x.get('end_us')),x['vehicle_id'],causal_ordinal[x['action_id']]))
    # A compatible install must not rewrite already observed, independently
    # valid commuting depot pickups from a historical activation. No arrival
    # or delivery is reordered and an invalid causal frame is never repaired.
    previous_rank={x['action_id']:i for i,x in enumerate(previous_history) if x.get('kind')=='PICKUP'}
    i=0
    while i<len(ordered):
        end=i+1
        if ordered[i]['kind']=='PICKUP':
            while end<len(ordered) and ordered[end]['kind']=='PICKUP' and (ordered[end]['vehicle_id'],ordered[end]['at_us'])==(ordered[i]['vehicle_id'],ordered[i]['at_us']):end+=1
            if all(x['action_id'] in previous_rank for x in ordered[i:end]):ordered[i:end]=sorted(ordered[i:end],key=lambda x:previous_rank[x['action_id']])
        i=end
    out['execution_history']+=ordered
    out.update(current_time=target,activation_id=activation_id,activation_anchor_sha256=anchor['content_sha256'])
    if 'delivery_day' in out:
        from .delivery_day import settle_recoveries
        settle_recoveries(out)
    out['content_sha256']=physical_sha(out);return out
