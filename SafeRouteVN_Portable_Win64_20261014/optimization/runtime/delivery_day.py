"""Delivery-day policy. All timestamps belong to the simulated +07:00 clock.

The immutable source fixture remains a template. Dispatch metadata is journaled
with the observed physical head, never written back into benchmark fixtures.
"""
from copy import deepcopy
from datetime import timedelta
from .protocol import instant, require, identifier, number


def initialize(state, observation):
    out = deepcopy(observation)
    cutoff = instant(out['current_time'], 'time').replace(hour=8, minute=0, second=0, microsecond=0)
    live_weather=(state.source_authentication=='RUN_MANIFEST_VERIFIED'
                  and state.source_kind != 'IMPORTED_SCENARIO')
    if not live_weather and cutoff < instant(out['current_time'], 'time'):
        cutoff += timedelta(days=1)
    delta = timedelta(0) if live_weather else cutoff - instant(out['current_time'], 'time')
    if not live_weather:out['current_time'] = cutoff.isoformat()
    catalog = {o.order_id: o.to_dict() for o in state.orders}
    for row in catalog.values():
        for key in ('earliest', 'preferred_due', 'hard_deadline'):
            row[key] = (instant(row[key], key) + delta).isoformat()
    for event in out['pending_events']:
        event['timestamp'] = (instant(event['timestamp'], 'event') + delta).isoformat()
    for vehicle in out['vehicles']:
        vehicle['position_timestamp'] = out['current_time']
    out['delivery_day'] = {
        'date': cutoff.date().isoformat(), 'cutoff': cutoff.isoformat(),
        'recovery_minutes': 30, 'catalog': catalog,
        'depots': [{**state.depot.to_dict(),'opening_time':cutoff.isoformat(),'closing_time':(cutoff+timedelta(hours=24)).isoformat()}], 'customers': {},
        'queue': {}, 'recoveries': [], 'confirmations': {}, 'before_event': None,
        'pickup_visits': {}, 'urgent_ids': [], 'history': [], 'event_shift_seconds': delta.total_seconds(),
    }
    for oid in catalog:
        out['delivery_day']['customers'][oid] = {'name': '', 'phone': '', 'arrived_at': cutoff.isoformat()}
    return out


def complete(observation):
    day = observation['delivery_day']
    return all(o['status'] == 'DELIVERED' or o['order_id'] in day['queue'] for o in observation['orders'])


def settle_recoveries(observation):
    day = observation['delivery_day']
    now = instant(observation['current_time'], 'time')
    for recovery in day['recoveries']:
        if instant(recovery['ready_at'], 'ready_at') > now:
            continue
        vehicle = next(v for v in observation['vehicles'] if v['vehicle_id'] == recovery['vehicle_id'])
        order = next(o for o in observation['orders'] if o['order_id'] == recovery['order_id'])
        if order['status']=='ONBOARD' and order['owner_vehicle_id']==vehicle['vehicle_id']:
            require(order['order_id'] in vehicle['onboard_order_ids'],'CUSTODY_OWNER','recovery','recovery cargo missing from original owner')
            vehicle['onboard_order_ids'].remove(order['order_id'])
            vehicle['current_load_kg'] = sum(o['demand_kg'] for o in observation['orders'] if o['order_id'] in vehicle['onboard_order_ids'])
            order.update(status='WAITING', owner_vehicle_id=None, picked_up_at=None, delivered_at=None)
        if recovery['status']=='RECOVERING':
            recovery['status'] = 'RECOVERED'
            day['history'].append({'kind': 'DEPOT_RECEIPT', **recovery})
        entry=day['queue'].get(order['order_id'])
        if entry and entry['date']<=day['date']:
            del day['queue'][order['order_id']]
            start=instant(day['cutoff'],'cutoff')
            day['catalog'][order['order_id']].update(earliest=max(start,now).isoformat(),preferred_due=(max(start,now)+timedelta(hours=4)).isoformat(),hard_deadline=(max(start,now)+timedelta(hours=12)).isoformat())


def mutate(state, before, action, data):
    """Validated operational commands; caller supplies CAS and durable idempotency."""
    out = deepcopy(before)
    if action == 'enable':
        require('delivery_day' not in out, 'DAY_ALREADY_STARTED', 'action', 'delivery day already enabled')
        require(not out['execution_history'], 'DAY_ALREADY_MOVING', 'action', 'start a delivery day before replay')
        out = initialize(state, out)
    require('delivery_day' in out, 'DELIVERY_DAY_REQUIRED', 'action', 'start delivery-day mode first')
    day = out['delivery_day']; now = instant(out['current_time'], 'time')
    orders = {o['order_id']: o for o in out['orders']}
    if action == 'add_order':
        from optimization.models.decision_state import OrderState
        template = day['catalog'].get(data.get('template_id'))
        require(template is not None, 'ORDER_TEMPLATE', 'template_id', 'choose a known delivery address')
        oid = identifier(data.get('order_id'), 'order_id')
        require(oid not in orders, 'ORDER_EXISTS', 'order_id', 'order ID already exists')
        depot = data.get('depot_id', 'DEPOT')
        require(any(d['location_id'] == depot for d in day['depots']), 'DEPOT_ID', 'depot_id', 'unknown depot')
        urgent = data.get('urgent', False)
        require(type(urgent) is bool, 'INVALID_DATA', 'urgent', 'boolean required')
        name, phone = data.get('name', ''), data.get('phone', '')
        require(isinstance(name, str) and 0 < len(name.strip()) <= 120 and isinstance(phone, str) and 0 < len(phone.strip()) <= 40,
                'CUSTOMER_REQUIRED', 'customer', 'customer name and phone required')
        row = {**template, 'order_id': oid, 'pickup_location_id': depot,
               'status': 'WAITING', 'assigned_vehicle_id': None, 'picked_up_at': None, 'delivered_at': None,
               'earliest': now.isoformat(), 'preferred_due': (now + timedelta(hours=2)).isoformat(),
               'hard_deadline': (now + timedelta(hours=8)).isoformat(), 'priority': 10 if urgent else 1}
        OrderState.from_dict(row)
        day['catalog'][oid] = row
        day['customers'][oid] = {'name': name.strip(), 'phone': phone.strip(), 'arrived_at': now.isoformat()}
        out['orders'].append({'order_id': oid, 'status': 'WAITING', 'owner_vehicle_id': None,
                              'picked_up_at': None, 'delivered_at': None, 'demand_kg': row['demand_kg']})
        if urgent:
            day['urgent_ids'].append(oid)
            # A service can be interrupted; directed edge motion cannot teleport.
            for v in out['vehicles']:
                if v.get('active_commitment') and v['active_commitment']['kind'] == 'SERVICE':
                    v['active_commitment'] = None
                v['awaiting_delivery'] = None
        elif now > instant(day['cutoff'], 'cutoff'):
            day['queue'][oid] = {'date': (now.date() + timedelta(days=1)).isoformat(), 'reason': 'ARRIVED_AFTER_0800', 'depot_id': depot}
    elif action == 'unavailable':
        vehicle = next((v for v in out['vehicles'] if v['vehicle_id'] == data.get('vehicle_id')), None)
        require(vehicle is not None and vehicle['availability'] == 'AVAILABLE', 'VEHICLE_UNAVAILABLE', 'vehicle_id', 'available vehicle required')
        minutes = data.get('recovery_minutes', day['recovery_minutes'])
        number(minutes, 'recovery_minutes', positive=True)
        require(minutes <= 1440, 'INVALID_DATA', 'recovery_minutes', 'recovery must finish within 24 hours')
        vehicle.update(availability='UNAVAILABLE', activity='IMMOBILIZED', suspended_commitment=vehicle.get('active_commitment'), active_commitment=None)
        out['pending_events']=[e for e in out['pending_events'] if not (e['event_type']=='VEHICLE_UNAVAILABLE' and e.get('payload',{}).get('vehicleId')==vehicle['vehicle_id'])]
        for oid in vehicle['onboard_order_ids']:
            depot = day['catalog'][oid]['pickup_location_id']
            day['queue'][oid] = {'date': (now.date() + timedelta(days=1)).isoformat(), 'reason': 'VEHICLE_UNAVAILABLE', 'depot_id': depot}
            day['recoveries'].append({'order_id': oid, 'vehicle_id': vehicle['vehicle_id'], 'depot_id': depot,
                                      'started_at': now.isoformat(), 'ready_at': (now + timedelta(minutes=minutes)).isoformat(), 'status': 'RECOVERING'})
        vehicle['planned_suffix'] = []
    elif action == 'deliver':
        order = orders.get(data.get('order_id'))
        vehicle = next((v for v in out['vehicles'] if v['vehicle_id'] == data.get('vehicle_id')), None)
        require(order is not None and vehicle is not None and vehicle['availability'] == 'AVAILABLE', 'DELIVERY_NOT_READY', 'order_id', 'available owner required')
        require(order['status'] == 'ONBOARD' and order['owner_vehicle_id'] == vehicle['vehicle_id'] and order['order_id'] in vehicle['onboard_order_ids'], 'CUSTODY_OWNER', 'order_id', 'only the owning driver may confirm')
        require(vehicle['position'].get('node_id') == day['catalog'][order['order_id']]['graph_node_id'] and vehicle.get('awaiting_delivery') == order['order_id'], 'DELIVERY_NOT_READY', 'position', 'reach the delivery stop before confirming')
        order.update(status='DELIVERED', delivered_at=now.isoformat())
        vehicle['onboard_order_ids'].remove(order['order_id'])
        vehicle['current_load_kg'] = sum(o['demand_kg'] for o in out['orders'] if o['order_id'] in vehicle['onboard_order_ids'])
        vehicle['awaiting_delivery'] = None; vehicle['active_commitment'] = None
        day['confirmations'][order['order_id']] = now.isoformat()
    elif action == 'defer':
        order = orders.get(data.get('order_id'))
        require(order is not None and order['status'] == 'WAITING', 'CUSTODY_OWNER', 'order_id', 'only goods still at depot may be deferred')
        day['queue'][order['order_id']] = {'date': (now.date() + timedelta(days=1)).isoformat(), 'reason': 'DISPATCHER_DEFERRED', 'depot_id': day['catalog'][order['order_id']]['pickup_location_id']}
    elif action == 'wait_recovery':
        pending=[r for r in day['recoveries'] if r['status']=='RECOVERING']
        require(bool(pending), 'NO_RECOVERY_PENDING', 'recoveries', 'no pending recovery')
        require(not any(v['availability']=='AVAILABLE' and (v.get('active_commitment') or v['position']['kind']=='ON_EDGE') for v in out['vehicles']), 'PAUSE_PLAN_REQUIRED','vehicles','moving vehicles must finish their connector before skipping recovery time')
        out['current_time']=max(out['current_time'],min(r['ready_at'] for r in pending))
        future=[e['timestamp'] for e in out['pending_events'] if e['timestamp']>=before['current_time']]
        if future:out['current_time']=min(out['current_time'],min(future))
        for v in out['vehicles']:v['position_timestamp']=out['current_time']
    elif action == 'next_day':
        require(complete(out), 'DAY_NOT_COMPLETE', 'orders', 'deliver or explicitly defer remaining orders first')
        start = (instant(day['cutoff'], 'cutoff') + timedelta(days=1)).replace(hour=8)
        require(start > now, 'DAY_BOUNDARY', 'time', 'next 08:00 must follow the current clock')
        out['current_time'] = start.isoformat(); day['date'] = start.date().isoformat(); day['cutoff'] = start.isoformat()
        if out['pending_events']:day['history'].append({'kind':'UNTRIGGERED_EVENTS_CLOSED','events':deepcopy(out['pending_events'])})
        out['pending_events']=[]
        for depot in day['depots']:depot.update(opening_time=start.isoformat(),closing_time=(start+timedelta(hours=24)).isoformat())
        settle_recoveries(out)
        recovering = {r['order_id'] for r in day['recoveries'] if r['status'] == 'RECOVERING'}
        for oid, entry in list(day['queue'].items()):
            if entry['date'] <= day['date'] and oid not in recovering:
                del day['queue'][oid]
                row = day['catalog'][oid]
                row.update(earliest=start.isoformat(), preferred_due=(start + timedelta(hours=4)).isoformat(), hard_deadline=(start + timedelta(hours=12)).isoformat())
        day['pickup_visits'] = {}; day['urgent_ids'] = []; day['before_event'] = None
        for v in out['vehicles']:
            commitment=v.get('active_commitment')
            if commitment and commitment['kind']=='EDGE':
                delay=int((start-now).total_seconds()*1_000_000)
                commitment['until_us']+=delay
                for field in ('edge_entry_us','expected_exit_us'):
                    if field in v['position']:v['position'][field]+=delay
            else:v['active_commitment'] = None
            # Keep the actual position and unavailable state. No overnight teleport.
            v['position_timestamp'] = out['current_time']
    elif action == 'add_depot':
        from optimization.models.decision_state import DepotState
        oid = data.get('location_order_id'); row = day['catalog'].get(oid)
        require(row is not None, 'DEPOT_LOCATION', 'location_order_id', 'choose a verified graph location')
        depot_id = identifier(data.get('depot_id'), 'depot_id')
        require(not any(d['location_id'] == depot_id for d in day['depots']), 'DEPOT_EXISTS', 'depot_id', 'duplicate depot')
        depot = {**state.depot.to_dict(), 'location_id': depot_id, 'graph_node_id': row['graph_node_id'], 'coordinates': row['coordinates']}
        DepotState.from_dict(depot); day['depots'].append(depot)
    elif action == 'assign_depot':
        oid = data.get('order_id'); depot = data.get('depot_id')
        require(oid in orders and orders[oid]['status'] == 'WAITING', 'CUSTODY_OWNER', 'order_id', 'goods already picked up cannot change origin')
        require(any(d['location_id'] == depot for d in day['depots']), 'DEPOT_ID', 'depot_id', 'unknown depot')
        day['catalog'][oid]['pickup_location_id'] = depot
        if oid in day['queue']:day['queue'][oid]['depot_id']=depot
    elif action == 'customer':
        oid=data.get('order_id');name=data.get('name');phone=data.get('phone')
        require(oid in orders,'ORDER_ID','order_id','known order required')
        require(isinstance(name,str) and 0<len(name.strip())<=120 and isinstance(phone,str) and 0<len(phone.strip())<=40,'CUSTOMER_REQUIRED','customer','name and phone required')
        day['customers'][oid]={**day['customers'].get(oid,{}),'name':name.strip(),'phone':phone.strip()}
    else:
        require(action == 'enable', 'UNKNOWN_ACTION', 'action', 'unknown delivery-day action')
    if action in ('wait_recovery','next_day'):
        settle_recoveries(out)
    day['history'].append({'kind': action, 'at': out['current_time'], 'data': deepcopy(data)})
    return out


def effective_state(state, anchor, *, rain_event=False):
    """Typed planning input derived exclusively from the persisted day catalog."""
    if not anchor or 'delivery_day' not in anchor:
        return state
    if state.depots and state.depot.opening_time == anchor['delivery_day']['cutoff']:
        return _rain_state(state) if rain_event or anchor.get('overlay_sha256') else state
    from dataclasses import replace
    from optimization.models.decision_state import OrderState, DepotState
    day = anchor['delivery_day']; start = instant(day['cutoff'], 'cutoff')
    end = (start + timedelta(hours=24)).isoformat()
    orders = tuple(OrderState.from_dict({**o, 'status': 'WAITING', 'assigned_vehicle_id': None, 'picked_up_at': None, 'delivered_at': None}) for o in day['catalog'].values())
    depots = tuple(DepotState.from_dict({**d, 'opening_time': day['cutoff'], 'closing_time': end}) for d in day['depots'])
    from dataclasses import replace as event_replace
    events=tuple(event_replace(e,timestamp=(instant(e.timestamp,'event')+timedelta(seconds=day.get('event_shift_seconds',0))).isoformat()) for e in state.pending_events)
    effective = replace(state, orders=orders, depot=depots[0], depots=depots, pending_events=events,
                   vehicles=tuple(replace(v, onboard_order_ids=(), current_load_kg=0, working_start=day['cutoff'], working_end=end) for v in state.vehicles))
    # Preserve the exact dry planning input used by previously accepted tours.
    # Only rain application and wet planning need the translated wet interval.
    return _rain_state(effective) if rain_event or anchor.get('overlay_sha256') else effective


def _rain_state(state):
    from dataclasses import replace
    from optimization.models.decision_state import EventEnvelope
    from .protocol import sha
    events = []
    for event in state.pending_events:
        if event.event_type != 'LOCAL_RAIN_WHAT_IF':
            events.append(event)
            continue
        row = event.to_dict()
        payload = row['payload']
        delta = instant(event.timestamp, 'event') - instant(payload['timestamp'], 'event.payload.timestamp')
        if delta:
            for field in ('timestamp', 'startTime', 'endTime'):
                payload[field] = (instant(payload[field], field) + delta).isoformat()
            row['payload_canonical_sha256'] = sha(payload)
            event = EventEnvelope.from_dict(row)
        events.append(event)
    return replace(state, pending_events=tuple(events))


def pickup_depot(state, order):
    return next(d for d in (state.depots or (state.depot,)) if d.location_id == order.pickup_location_id)
