"""Producer piecewise progress evaluator. Validator integrates independently."""
from fractions import Fraction
from optimization.models.member1_rain import EdgeRates, POLICY, integer, number, fraction
from optimization.models.member1_dynamic_state import require


def ceiling(value):return -(-value.numerator//value.denominator)


def traverse(rates,start_us,*,progress=Fraction(0),until_us=None,incoming_edge=None,cost_per_km_vnd=0.):
    require(isinstance(rates,EdgeRates),'INVALID_DATA','rates','typed edge rates required')
    integer(start_us,'start_us');number(cost_per_km_vnd,'cost_per_km_vnd')
    require(incoming_edge is None or isinstance(incoming_edge,str) and bool(incoming_edge),'INVALID_DATA','incoming_edge','directed ID or null required')
    p=fraction(progress,'progress');initial=p;clock=start_us;segments=[]
    if until_us is not None:
        integer(until_us,'until_us');require(until_us>=start_us,'INVALID_DATA','until_us','observation cannot move backward')
    while p<1 and (until_us is None or clock<until_us):
        layer,duration,proxy,boundary=rates.layer(clock)
        stop=boundary if until_us is None else until_us if boundary is None else min(boundary,until_us)
        needed=(1-p)*duration
        full=stop is None or needed<=stop-clock
        elapsed=ceiling(needed) if full else stop-clock
        endpoint=clock+elapsed
        integer(endpoint,'end_us')
        delta=1-p if full else Fraction(elapsed,duration)
        target=p+delta
        distance=rates.distance_m*float(delta)
        segments.append({'edge_id':rates.edge_id,'from_node':rates.from_node,'to_node':rates.to_node,
            'incoming_edge':incoming_edge,'start_us':clock,'end_us':endpoint,'fraction_start':str(p),
            'fraction_end':str(target),'layer':layer,'full_duration_us':duration,'edge_proxy':proxy,
            'distance_m':distance,'exposure':distance/1000*proxy,'cost_vnd':distance/1000*cost_per_km_vnd})
        clock=endpoint;p=target
    return {'temporal_policy':POLICY,'edge_id':rates.edge_id,'start_us':start_us,'end_us':clock,
        'fraction_start':str(initial),'fraction_end':str(p),'segments':segments,
        'distance_m':sum(x['distance_m'] for x in segments),'exposure':sum(x['exposure'] for x in segments),
        'cost_vnd':sum(x['cost_vnd'] for x in segments),'travel_time_us':clock-start_us,'completed':p==1}
