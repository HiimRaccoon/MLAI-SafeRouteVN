"""An urgent insertion must retain complementary tours for the loaded fleet."""
from types import SimpleNamespace

import pytest

from optimization.rolling_horizon.member1_dynamic_planner import DynamicLimits, post_domain
from optimization.runtime.profile_paths import time_domain_budget
from optimization.runtime.worker_v2 import prime_required_returns
from optimization.solver.member1_dynamic_master import select_columns
from optimization.tests.test_profile_domain import config


def fleet():
    vehicles = [SimpleNamespace(vehicle_id=f'V{i}', capacity_kg=30.) for i in range(1, 6)]
    groups = {v.vehicle_id: [f'O{i:03}' for i in range(index * 5 + 1, min(index * 5 + 6, 25))]
              for index, v in enumerate(vehicles)}
    orders = {oid: SimpleNamespace(order_id=oid, demand_kg=1., priority=1,
                                  status='WAITING', pickup_location_id='DEPOT')
              for ids in groups.values() for oid in ids}
    orders['URGENT'] = SimpleNamespace(order_id='URGENT', demand_kg=1., priority=10,
                                      status='WAITING', pickup_location_id='DEPOT')
    anchor = dict(current_time='now', orders=[dict(order_id=oid, status='ONBOARD') for oid in orders if oid != 'URGENT']
                  + [dict(order_id='URGENT', status='WAITING')],
                  vehicles=[dict(vehicle_id=v.vehicle_id, availability='AVAILABLE',
                                 onboard_order_ids=groups[v.vehicle_id],
                                 planned_suffix=groups[v.vehicle_id], current_load_kg=len(groups[v.vehicle_id]))
                            for v in vehicles],
                  delivery_day=dict(urgent_ids=['URGENT'], pickup_visits={}, queue={}))
    state = SimpleNamespace(vehicles=vehicles)
    builder = SimpleNamespace(orders=orders, proposals=0, columns=[], deadline=1e20,
                              limits=DynamicLimits(max_proposals=36, max_columns=48))

    def realize(vehicle, sequence, **kwargs):
        builder.proposals += 1
        if builder.proposals > builder.limits.max_proposals or len(builder.columns) >= builder.limits.max_columns:
            return []
        row = dict(vehicle_id=vehicle.vehicle_id, order_sequence=list(sequence),
                   total_travel_time_s=100. + len(sequence), total_exposure=1.,
                   total_distance_m=100., total_soft_lateness_s=0.)
        if row not in builder.columns:
            builder.columns.append(row)
        return [row]

    builder.realize = realize
    builder.domain = lambda before: dict(columns=builder.columns)
    return state, anchor, builder, groups


@pytest.mark.parametrize('profile', ['FASTEST', 'BALANCED', 'SAFER'])
def test_25_orders_five_vehicles_keep_cargo_tours_when_urgent_consumes_seed_budget(profile):
    state, anchor, builder, groups = fleet()
    with time_domain_budget(builder) as limits:
        prime_required_returns(state, anchor, builder)
        domain = post_domain(state, None, anchor, limits, _builder=builder, _fair_share=True)
    master = select_columns(domain['columns'], {oid: o.priority for oid, o in builder.orders.items()},
                            list(groups), profile, config(), seconds=1.,
                            required_vehicle_ids=list(groups), urgent_order_ids=['URGENT'])
    routes = [domain['columns'][i] for i in master['selected_route_indexes']]
    served = [oid for route in routes for oid in route['order_sequence']]
    assert len(served) == len(set(served)) == 25
    assert set(served) == set(builder.orders)
    for route in routes:
        assert set(groups[route['vehicle_id']]).issubset(route['order_sequence'])
    assert sum('URGENT' in r['order_sequence'] for r in routes) == 1


def test_continuation_uses_owned_cargo_only_and_keeps_planned_order():
    state, anchor, builder, groups = fleet()
    observed = anchor['vehicles'][0]
    observed['planned_suffix'] = ['O003', 'DELIVERED', 'O001', 'FOREIGN']
    anchor['vehicles'][-1]['availability'] = 'UNAVAILABLE'
    prime_required_returns(state, anchor, builder)
    first = builder.columns[0]
    assert first['vehicle_id'] == 'V1'
    assert first['order_sequence'] == ['O003', 'O001', 'O002', 'O004', 'O005']
    assert not any(c['vehicle_id'] == 'V5' for c in builder.columns)


def test_continuation_keeps_explicit_stop_and_empty_vehicles_can_return():
    state, anchor, builder, groups = fleet()
    anchor['vehicles'][0]['explicit_committed_stop_id'] = 'O005'
    anchor['vehicles'][1].update(onboard_order_ids=[], planned_suffix=['O006'])
    prime_required_returns(state, anchor, builder)
    assert builder.columns[0]['order_sequence'] == ['O005', 'O001', 'O002', 'O003', 'O004']
    assert builder.columns[1]['vehicle_id'] == 'V2'
    assert builder.columns[1]['order_sequence'] == []
