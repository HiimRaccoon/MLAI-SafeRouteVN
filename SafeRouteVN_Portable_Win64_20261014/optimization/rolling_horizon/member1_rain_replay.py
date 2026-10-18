"""Forecast samples, NOT observed authority or a generic rolling scheduler."""
from copy import deepcopy
from fractions import Fraction
from optimization.models.member1_dynamic_state import require,sha
from optimization.models.member1_rain import integer,POLICY
from optimization.rolling_horizon.member1_rain_source import source_edge_rates
from optimization.rolling_horizon.member1_rain_temporal import traverse
from optimization.rolling_horizon.member1_dynamic_validation import offset


def sample_forecast(state,graph,model,overlay,head,result,query_us):
    integer(query_us,'query_us');origin=offset(state.decision_epoch,head['current_time'])
    require(query_us>=origin,'INVALID_DATA','query_us','forecast cannot rewrite pre-event observation')
    require(result.get('forecast') is True and result.get('after_state_sha256')==head['content_sha256'],'FORECAST_BINDING','result','validated forecast/head binding required')
    metrics=deepcopy(head['execution_metrics']);deliveries=[];vehicles=[]
    for snapshot in head['vehicles']:
        route=next((r for r in result['vehicle_routes'] if r['vehicle_id']==snapshot['vehicle_id']),None)
        local={'distance_m':0.,'relative_exposure_proxy':0.,'cost_vnd':0.,'travel_time_us':0,'waiting_time_us':0,'service_time_us':0}
        load=snapshot['current_load_kg'];active=None;cargo=list(snapshot['onboard_order_ids'])
        if route:
            vehicle=next(v for v in state.vehicles if v.vehicle_id==snapshot['vehicle_id'])
            for action in route['actions']:
                begin,end=action['start_us'],action['end_us']
                elapsed=max(0,min(query_us,end)-begin)
                if action['kind']=='EDGE' and elapsed:
                    rates=source_edge_rates(state,graph,model,overlay,action['edge_id'])
                    part=traverse(rates,begin,progress=Fraction(action['fraction_start_exact']),until_us=min(query_us,end),incoming_edge=action['incoming_edge'],cost_per_km_vnd=vehicle.cost_per_km_vnd)
                    local['distance_m']+=part['distance_m'];local['relative_exposure_proxy']+=part['exposure'];local['cost_vnd']+=part['cost_vnd'];local['travel_time_us']+=elapsed
                    if query_us<end:active={'kind':'EDGE','edge_id':action['edge_id'],'incoming_edge':action['incoming_edge'],'from_node':action['from_node'],'to_node':action['to_node'],'fraction':part['fraction_end']}
                elif action['kind']=='WAIT':local['waiting_time_us']+=elapsed
                elif action['kind']=='SERVICE':
                    local['service_time_us']+=elapsed
                    if end<=query_us and (begin<query_us or action['continuation']):
                        load=action['load_after_kg'];cargo.remove(action['order_id']);deliveries.append(action['order_id'])
                    elif begin<query_us<end:active={'kind':'SERVICE','order_id':action['order_id'],'node_id':action['node_id']}
                elif action['kind']=='PICKUP' and begin<query_us:
                    load=action['load_after_kg'];cargo.append(action['order_id'])
        remaining=snapshot['remaining_range_m']-local['distance_m']
        require(remaining>=-1e-7,'RANGE','forecast.remaining_range_m','forecast exceeds trusted head remaining range')
        vehicles.append({'vehicle_id':snapshot['vehicle_id'],'predicted_load_kg':load,'predicted_onboard_order_ids':sorted(cargo),
            'predicted_remaining_range_m':remaining,'active_forecast_action':active,'observed_head_position':snapshot['position']})
        for key,value in local.items():metrics[key]+=value
    return {'schema_version':'task02-m1-temporal-forecast-sample/1','forecast':True,'observed_state':False,
        'query_us':query_us,'temporal_policy':POLICY,'head_sha256':head['content_sha256'],'forecast_sha256':sha(result),
        'overlay_sha256':overlay['content_sha256'],'predicted_new_deliveries':sorted(deliveries),
        'actual_delivered_at_head':sorted(o['order_id'] for o in head['orders'] if o['status']=='DELIVERED'),
        'whole_metrics_to_query':metrics,'vehicles':vehicles,'authority_advanced':False}
