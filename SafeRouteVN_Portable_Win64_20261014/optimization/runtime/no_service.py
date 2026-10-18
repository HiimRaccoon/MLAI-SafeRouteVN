"""Narrow independent no-service proof from trusted custody/no-split facts.

No road search, finite-domain absence, engine result or timeout is a proof.
This certificate is not a witness; no physical plan may be activated from it.
"""
from optimization.models.decision_state import OrderState
from .protocol import require,sha

def certificate(state,anchor=None):
    orders={o.order_id:o for o in state.orders}
    if anchor and anchor.get('event_order'):
        o=OrderState.from_dict(anchor['event_order']);orders[o.order_id]=o
    observed={o['order_id']:o for o in anchor['orders']} if anchor else {}
    vehicles={v.vehicle_id:v for v in state.vehicles};availability={v.vehicle_id:v.availability for v in state.vehicles}
    if anchor:availability={v['vehicle_id']:v['availability'] for v in anchor['vehicles']}
    reasons=[]
    for oid,o in orders.items():
        current=observed.get(oid,{'status':o.status,'owner_vehicle_id':o.assigned_vehicle_id})
        if current['status']=='DELIVERED':continue
        owner=current.get('owner_vehicle_id')
        if current['status']=='ONBOARD' and owner and availability.get(owner)=='UNAVAILABLE':reason='CUSTODY_BLOCKED'
        elif current['status']=='WAITING' and all(o.demand_kg>v.capacity_kg for v in vehicles.values()):reason='CAPACITY_EXCEEDS_ALL_VEHICLES_NO_SPLIT'
        else:return None
        reasons.append({'order_id':oid,'reason':reason,'owner_vehicle_id':owner})
    if not reasons:return None  # No pending work is not proof that orders failed.
    return {'schema_version':'task02-m2-no-service-certificate/1','initial_state_sha256':sha(state.to_dict()),
            'anchor_sha256':anchor['content_sha256'] if anchor else None,'reasons':reasons,'scope':'TRUSTED_NO_SPLIT_CAPACITY_OR_UNAVAILABLE_CUSTODY_ONLY','global_infeasibility_proven':False}

def validate_certificate(state,anchor,value):
    expected=certificate(state,anchor)
    require(expected is not None and value==expected,'NO_SERVICE_PROOF','no_service_certificate','independent source/custody facts do not support this claim')
    return {'status':'SCOPED_PROOF_VERIFIED','scope':expected['scope'],'not_a_feasible_plan':True}
