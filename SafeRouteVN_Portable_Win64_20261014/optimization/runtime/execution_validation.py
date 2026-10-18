"""Independent observed ledger reconstruction. No producer/search imports.

Change Impact Analysis: runtime-only validator /2. Source physical primitives
are independent Step5/6 checkers, not producer playback/forecast routines.
"""
from copy import deepcopy
from decimal import Decimal,ROUND_CEILING
from fractions import Fraction
from optimization.models.member1_dynamic_state import sha as physical_sha,DynamicError
from optimization.integration.member1_s0_graph import RoadDataError
from optimization.rolling_horizon.member1_dynamic_validation import close
from optimization.rolling_horizon.member1_rain_validation import independent_segments
from optimization.rolling_horizon.member1_motion_validation import _point
from .protocol import require,RuntimeError,offset,sha,instant,tree,identifier,number

VERSION='task02-m2-observed-action-validator/3'

def validate_replay(state,graph,anchor,routes,target,activation_id,observed,*,model=None,overlay=None,confirmations=None,dispatch_frame=None):
    from .delivery_day import effective_state,pickup_depot
    state=effective_state(state,anchor)
    catalog={o.order_id:o for o in state.orders}
    issues=[]
    try:
        require(isinstance(observed,dict),'INVALID_DATA','observation','object required')
        tree(observed);require(isinstance(anchor,dict),'INVALID_DATA','anchor','trusted physical anchor required');tree(anchor)
        for frame,path in ((anchor,'anchor'),(observed,'observation')):
            for key in ('orders','vehicles','pending_events','applied_event_ids','execution_history'):require(isinstance(frame.get(key),list),'INVALID_DATA',path+'.'+key,'array required')
            require(isinstance(frame.get('execution_metrics'),dict),'INVALID_DATA',path+'.execution_metrics','metric object required')
            keys={'distance_m','relative_exposure_proxy','cost_vnd','travel_time_us','waiting_time_us','service_time_us'}
            require(set(frame['execution_metrics'])==keys,'INVALID_DATA',path+'.execution_metrics','exact metric scope required')
            for k,n in frame['execution_metrics'].items():number(n,path+'.execution_metrics.'+k)
            ids=[]
            for i,o in enumerate(frame['orders']):
                p=f'{path}.orders[{i}]';require(isinstance(o,dict),'INVALID_DATA',p,'order object required');ids.append(identifier(o.get('order_id'),p+'.order_id'))
                require(isinstance(o.get('status'),str) and o['status'] in ('WAITING','ONBOARD','DELIVERED'),'INVALID_DATA',p+'.status','known custody status required')
            require(len(ids)==len(set(ids)),'INVALID_DATA',path+'.orders','unique orders required')
            for i,v in enumerate(frame['vehicles']):
                p=f'{path}.vehicles[{i}]';require(isinstance(v,dict),'INVALID_DATA',p,'vehicle object required');identifier(v.get('vehicle_id'),p+'.vehicle_id')
                require(isinstance(v.get('position'),dict) and isinstance(v.get('executed_metrics'),dict) and isinstance(v.get('onboard_order_ids'),list),'INVALID_DATA',p,'physical fields required')
                require(set(v['executed_metrics'])==keys,'INVALID_DATA',p+'.executed_metrics','exact metrics required')
                for k,n in v['executed_metrics'].items():number(n,p+'.executed_metrics.'+k)
                number(v.get('current_load_kg'),p+'.current_load_kg');number(v.get('capacity_kg'),p+'.capacity_kg');number(v.get('remaining_range_m'),p+'.remaining_range_m')
        require(observed.get('schema_version')==anchor.get('schema_version')=='task02-m2-runtime-observation/2' and observed.get('scenario_id')==anchor.get('scenario_id')==state.scenario_id and observed.get('decision_epoch')==anchor.get('decision_epoch')==state.decision_epoch,'OBSERVATION_BINDING','scenario_id','physical identity/version differs')
        body=dict(observed);body.pop('content_sha256',None)
        require(observed.get('content_sha256')==physical_sha(body),'OBSERVATION_BINDING','content_sha256','observed digest differs')
        q=offset(state.decision_epoch,target);require(q>=offset(state.decision_epoch,anchor['current_time']),'TIME_REWIND','target_time','rewind')
        require(observed.get('current_time')==target and observed.get('execution_mode')=='SIMULATED_REPLAY' and observed.get('real_world_observation') is False,'OBSERVATION_BINDING','current_time','observed identity differs')
        require(observed.get('activation_anchor_sha256')==anchor['content_sha256'] and observed.get('activation_id')==activation_id,'ACTIVATION_BINDING','activation_id','accepted anchor differs')
        for e in anchor['pending_events']:require(q<=offset(state.decision_epoch,e['timestamp']),'EVENT_BARRIER','current_time','unapplied event crossed')
        for key in ('pending_events','applied_event_ids','source_versions','state_version'):require(observed.get(key)==anchor.get(key),'PREFIX_CHANGED',key,'replay changed immutable frame')
        orders=deepcopy({o['order_id']:o for o in anchor['orders']});expected=deepcopy(anchor['execution_metrics']);history=deepcopy(anchor['execution_history']);extra=[]
        if dispatch_frame and 'delivery_day' in anchor:
            for o in dispatch_frame['orders']:
                if o['order_id'] not in orders:orders[o['order_id']]=deepcopy(o)
        byvid={v['vehicle_id']:v for v in observed['vehicles']}
        require(len(byvid)==len(observed['vehicles']) and set(byvid)=={v['vehicle_id'] for v in anchor['vehicles']},'VEHICLE_ID','vehicles','vehicle inventory differs')
        rates={v.vehicle_id:v.cost_per_km_vnd for v in state.vehicles}
        for original in anchor['vehicles']:
            vid=original['vehicle_id'];v=byvid[vid];route=next((r for r in routes if r['vehicle_id']==vid),None)
            require(v.get('capacity_kg')==original['capacity_kg'] and v.get('position_timestamp')==target,'POSITION','vehicles.position_timestamp','source capacity/timestamp differs')
            for field in ('explicit_committed_stop_id','suspended_commitment','suspension_reason'):require(v.get(field)==original.get(field),'COMMITMENT','vehicles.'+field,'immutable vehicle frame differs')
            cargo=set(original['onboard_order_ids']);load=original['current_load_kg'];local={k:0 for k in expected};node=original['position'].get('node_id',original['position'].get('from_node'));incoming=original['position'].get('incoming_edge');active=None;edgepos=None
            if route:
                for ordinal,a in enumerate(route['actions']):
                    start,end=a['start_us'],a['end_us']
                    if start>=q:break
                    stop=min(q,end);elapsed=stop-start;done=end<=q;kind=a['kind'];aid=sha({'activation_id':activation_id,'vehicle_id':vid,'ordinal':ordinal,'action':a})
                    if kind=='EDGE':
                        raw=graph._checked_edge(graph.edge(a['edge_id']));p=Fraction(a.get('fraction_start_exact',str(a['fraction_start'])))
                        require(raw['fromNodeId']==node and (incoming,a['edge_id']) not in graph.forbidden,'FORBIDDEN_TURN','routes.actions','directed chain differs')
                        if overlay is None:
                            duration=int((Decimal(str(raw['travelTimeHours']))*3600*1000000).to_integral_value(rounding=ROUND_CEILING));progress=Fraction(1) if done else p+Fraction(elapsed,duration)
                            distance=raw['lengthKm']*1000*float(progress-p);exposure=raw['relativeExposure']*float(progress-p);segments=[]
                        else:
                            row=model.ingredients(graph,a['edge_id']);segments=[];progress=p;distance=exposure=0.
                            for s in independent_segments(row,overlay,a['edge_id'],start,p,incoming):
                                if s['start_us']>=stop:break
                                ending=min(stop,s['end_us']);delta=Fraction(s['fraction_end'])-Fraction(s['fraction_start']) if ending==s['end_us'] else Fraction(ending-s['start_us'],s['full_duration_us'])
                                progress=Fraction(s['fraction_start'])+delta;dist=row['length_m']*float(delta);risk=dist/1000*s['edge_proxy']
                                entry={**s,'end_us':ending,'fraction_end':str(progress),'distance_m':dist,'exposure':risk,'cost_vnd':dist/1000*rates[vid]};segments.append(entry);distance+=dist;exposure+=risk
                        local['distance_m']+=distance;local['relative_exposure_proxy']+=exposure;local['cost_vnd']+=distance/1000*rates[vid];local['travel_time_us']+=elapsed
                        if done:node=raw['toNodeId'];incoming=a['edge_id']
                        else:
                            edgepos=(a['edge_id'],str(progress),incoming)
                            close(v['position'].get('coordinates'),_point([[p.longitude,p.latitude] for p in raw['points']],float(progress)),'vehicles.position.coordinates')
                            active={'kind':'EDGE','edge_id':a['edge_id'],'to_node':raw['toNodeId'],'until_us':end}
                        if elapsed:extra.append({'action_id':aid,'vehicle_id':vid,'kind':'EDGE_PROGRESS','start_us':start,'end_us':stop,'completed':done,'fraction_end':str(progress),'temporal_segments':segments})
                    elif kind=='PICKUP':
                        o=orders[a['order_id']];require(node==pickup_depot(state,catalog[o['order_id']]).graph_node_id and o['status']=='WAITING','CUSTODY_OWNER','routes.actions','pickup not WAITING at origin depot')
                        cargo.add(o['order_id']);load=float(sum((Decimal(str(orders[x]['demand_kg'])) for x in sorted(cargo)),Decimal(0)));o.update(status='ONBOARD',owner_vehicle_id=vid,picked_up_at=(instant(state.decision_epoch,'epoch')+__import__('datetime').timedelta(microseconds=start)).isoformat())
                        extra.append({'action_id':aid,'vehicle_id':vid,'order_id':o['order_id'],'kind':'PICKUP','at_us':start})
                    elif kind=='SERVICE':
                        local['service_time_us']+=elapsed
                        if done and 'delivery_day' in anchor and a['order_id'] not in (confirmations or anchor['delivery_day']['confirmations']):
                            require(v.get('awaiting_delivery')==a['order_id'],'DELIVERY_CONFIRMATION','vehicle','driver confirmation required')
                            active={'kind':'SERVICE','order_id':a['order_id'],'node_id':node,'until_us':end}
                            break
                        if done:
                            o=orders[a['order_id']];require(o['owner_vehicle_id']==vid and o['order_id'] in cargo,'CUSTODY_OWNER','routes.actions','cargo owner differs')
                            cargo.remove(o['order_id']);load=float(sum((Decimal(str(orders[x]['demand_kg'])) for x in sorted(cargo)),Decimal(0)));o.update(status='DELIVERED',delivered_at=(instant(state.decision_epoch,'epoch')+__import__('datetime').timedelta(microseconds=end)).isoformat());extra.append({'action_id':aid,'vehicle_id':vid,'order_id':o['order_id'],'kind':'DELIVERY','at_us':end})
                        else:active={'kind':'SERVICE','order_id':a['order_id'],'node_id':node,'until_us':end}
                    else:require(kind=='WAIT','INVALID_DATA','actions.kind','unknown action');local['waiting_time_us']+=elapsed
                    require(-1e-9<=load<=original['capacity_kg']+1e-9,'CAPACITY','current_load_kg','load bound')
                    if not done:break
            if 'delivery_day' in anchor:
                for recovery in (dispatch_frame or anchor)['delivery_day']['recoveries']:
                    oid=recovery['order_id']
                    if recovery['vehicle_id']==vid and oid in cargo and instant(recovery['ready_at'],'recovery')<=instant(target,'target'):
                        cargo.remove(oid)
                        orders[oid].update(status='WAITING',owner_vehicle_id=None,picked_up_at=None,delivered_at=None)
                load=float(sum(Decimal(str(orders[oid]['demand_kg'])) for oid in cargo))
            close(v['current_load_kg'],load,'vehicles.current_load_kg');require(v['onboard_order_ids']==sorted(cargo),'CUSTODY_OWNER','vehicles.onboard_order_ids','cargo differs')
            close(v['remaining_range_m'],original['remaining_range_m']-local['distance_m'],'vehicles.remaining_range_m');require(v['remaining_range_m']>=-1e-7,'RANGE','vehicles.remaining_range_m','range bound')
            require(v['availability']==original['availability'] and v['active_commitment']==(active if route else original['active_commitment']),'COMMITMENT','vehicles.active_commitment','commitment/availability differs')
            if edgepos:
                pos=v['position'];require(pos.get('kind')=='ON_EDGE' and (pos.get('edge_id'),pos.get('progress_exact'),pos.get('incoming_edge'))==edgepos,'POSITION','vehicles.position','edge progress differs')
                close(pos.get('progress'),float(Fraction(edgepos[1])),'vehicles.position.progress')
            elif route:
                require(v['position'].get('kind')=='AT_NODE' and v['position'].get('node_id')==node and v['position'].get('incoming_edge')==incoming,'POSITION','vehicles.position','node/turn state differs')
                close(v['position'].get('coordinates'),list(graph.node(node)),'vehicles.position.coordinates')
            else:require(v['position']==original['position'],'POSITION','vehicles.position','inactive vehicle moved')
            for k,n in local.items():close(v['executed_metrics'][k],original['executed_metrics'][k]+n,'vehicles.executed_metrics.'+k);expected[k]+=n
        require(observed['orders']==list(orders.values()),'CUSTODY_OWNER','orders','observed order states differ')
        # Independent partial-order check: exact prefix and record set, with
        # each vehicle's sequence reconstructed from raw accepted actions.
        # Cross-vehicle ties have no fabricated physical dependency. Do not
        # import or reproduce the producer's global sorting comparator.
        actual=observed['execution_history']
        require(actual[:len(history)]==history,'CAUSAL_HISTORY','execution_history','accepted prefix rewritten')
        suffix=actual[len(history):];expected_by_id={x['action_id']:x for x in extra}
        require(len(expected_by_id)==len(extra) and len(suffix)==len(extra),'CAUSAL_HISTORY','execution_history','missing or duplicate action')
        seen=set();previous=None;vehicle_order={}
        for i,x in enumerate(suffix):
            p=f'execution_history[{len(history)+i}]'
            require(isinstance(x,dict) and isinstance(x.get('action_id'),str),'CAUSAL_HISTORY',p,'typed executed action required')
            if 'at_us' in x:require(type(x['at_us']) is int,'CAUSAL_HISTORY',p+'.at_us','exact physical timestamp required')
            if x.get('kind')=='EDGE_PROGRESS':
                require(type(x.get('start_us')) is int and type(x.get('end_us')) is int and type(x.get('completed')) is bool,'CAUSAL_HISTORY',p,'typed edge progress/timestamp required')
            aid=x['action_id'];require(aid not in seen and expected_by_id.get(aid)==x,'CAUSAL_HISTORY',p,'unknown, repeated or altered physical action');seen.add(aid)
            at=x.get('at_us',x.get('end_us'));require(previous is None or at>=previous,'CAUSAL_HISTORY',p,'execution time rewound');previous=at
            vehicle_order.setdefault(x['vehicle_id'],[]).append(aid)
        for vid in byvid:
            wanted=[x for x in extra if x['vehicle_id']==vid];actual_ids=vehicle_order.get(vid,[]);i=0
            while i<len(wanted):
                end=i+1
                # Independent simultaneous depot pickups commute. This keeps
                # causally valid historical frames readable without imposing
                # a new ordering on records that have no physical dependency.
                if wanted[i]['kind']=='PICKUP':
                    while end<len(wanted) and wanted[end]['kind']=='PICKUP' and wanted[end]['at_us']==wanted[i]['at_us']:end+=1
                require(set(actual_ids[i:end])=={x['action_id'] for x in wanted[i:end]},'CAUSAL_HISTORY','execution_history','arrival/pickup/delivery order differs from physical route');i=end
        for k,v in expected.items():close(observed['execution_metrics'][k],v,'execution_metrics.'+k)
        if 'delivery_day' in anchor:
            day=deepcopy((dispatch_frame or anchor)['delivery_day'])
            day['confirmations'].update(confirmations or {})
            for entry in extra:
                if entry['kind']=='PICKUP':
                    dep=catalog[entry['order_id']].pickup_location_id
                    visits=day['pickup_visits'].setdefault(entry['vehicle_id'],[])
                    if dep not in visits:visits.append(dep)
            from .delivery_day import settle_recoveries
            shadow={'delivery_day':day,'current_time':target,'orders':deepcopy(list(orders.values())),'vehicles':deepcopy(observed['vehicles'])}
            settle_recoveries(shadow)
            require(observed.get('delivery_day')==day,'DAY_BINDING','delivery_day','replay altered dispatch policy or queue')
    except (RuntimeError,DynamicError,RoadDataError) as e:issues.append({'severity':'ERROR','code':getattr(e,'code','RAW_SOURCE_INVALID'),'path':getattr(e,'path','$'),'message':str(e)})
    return {'validator_version':VERSION,'valid':not issues,'diagnostics':issues,'scope':'RAW_ACCEPTED_ACTION_PREFIX; SIMULATED_NOT_GPS; NOT_OPTIMALITY'}
