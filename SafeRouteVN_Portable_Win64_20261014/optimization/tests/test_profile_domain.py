"""Profile tradeoffs must survive bounded domain generation and fleet selection."""
from types import SimpleNamespace
from pathlib import Path

import pytest

from optimization.integration.member1_profiles import load_profile_config
from optimization.rolling_horizon.member1_dynamic_planner import DynamicLimits, pre_domain, post_domain
from optimization.runtime.profile_paths import time_domain_budget, enrich_profile_domain
from optimization.solver.member1_dynamic_master import select_columns


def config():
    return load_profile_config(Path(__file__).resolve().parents[2] / 'configs/member1_profiles_step3.json')


def column(vid, oid, seconds, risk, late=0):
    return dict(vehicle_id=vid, order_sequence=[oid], total_travel_time_s=seconds,
                total_exposure=risk, total_distance_m=100., total_soft_lateness_s=late)


def selected(columns, profile):
    result = select_columns(columns, {'o': 1}, ['v'], profile, config(), seconds=1.)
    return result['selected_route_indexes']


def test_profiles_select_distinct_real_tradeoffs_under_production_weights():
    # A 60/40 blend on the fixed benchmark references has an interior winner.
    refs = config()['references']
    columns = [column('v', 'o', t * refs['travel_time_s'], r * refs['relative_exposure_proxy'])
               for t, r in ((.1, 1.), (.3, .4), (.6, .1))]
    assert [selected(columns, p) for p in ('FASTEST', 'BALANCED', 'SAFER')] == [[0], [1], [2]]


def test_common_winner_is_not_forced_apart_and_lateness_keeps_precedence():
    dominated = [column('v', 'o', 100., 1.), column('v', 'o', 200., 2.)]
    late_safer = [column('v', 'o', 100., 10.), column('v', 'o', 200., .1, late=1.)]
    for columns in (dominated, late_safer):
        assert [selected(columns, p) for p in ('FASTEST', 'BALANCED', 'SAFER')] == [[0]] * 3


def test_seed_caps_leave_both_profile_passes_time_and_proposals(monkeypatch):
    now = [100.]
    monkeypatch.setattr('optimization.runtime.profile_paths.monotonic', lambda: now[0])
    limits = DynamicLimits(domain_seconds=30., max_proposals=12, max_columns=12)
    state = SimpleNamespace(vehicles=[SimpleNamespace(vehicle_id=v) for v in ('a', 'b')])
    value = SimpleNamespace(limits=limits, deadline=130., proposals=0, columns=[],
                            orders={o: SimpleNamespace(status='WAITING', priority=1) for o in ('x', 'y')},
                            profile_config=config())
    with time_domain_budget(value) as seed_limits:
        assert seed_limits.max_proposals == 4 and seed_limits.max_columns == 4
        now[0] = value.deadline  # Even a fully exhausted seed pass leaves room.
        value.proposals = seed_limits.max_proposals
        value.columns = [column('a', 'x', 100., 1.), column('b', 'y', 100., 1.)]
    calls = []
    def realize(vehicle, sequence, **kwargs):
        assert now[0] < value.deadline and value.proposals < value.limits.max_proposals
        calls.append((kwargs['objective'], vehicle.vehicle_id, sequence, kwargs['pickup_ids']))
        value.proposals += 1
        now[0] += 2.
    value.realize = realize
    enrich_profile_domain(value, state)
    assert [(mode, vid) for mode, vid, _, _ in calls] == [
        ('balanced', 'a'), ('balanced', 'b'), ('exposure', 'a'), ('exposure', 'b')]
    assert all(sequence == pickups for _, _, sequence, pickups in calls)
    assert value.limits is limits and value.deadline == 130.


def test_budget_restores_original_limits_after_error():
    limits = DynamicLimits()
    value = SimpleNamespace(limits=limits, deadline=1e20)
    with pytest.raises(ValueError), time_domain_budget(value):
        raise ValueError('source failure')
    assert value.limits is limits and value.deadline == 1e20


def test_seed_proposals_interleave_vehicles_under_shared_time_limit():
    vehicles = [SimpleNamespace(vehicle_id=v, capacity_kg=10.) for v in ('a', 'b')]
    orders = {o: SimpleNamespace(order_id=o, demand_kg=1.) for o in ('x', 'y')}
    state = SimpleNamespace(vehicles=vehicles)
    anchor = dict(current_time='now', orders=[dict(order_id=o, status='WAITING') for o in orders],
                  vehicles=[dict(vehicle_id=v.vehicle_id, availability='AVAILABLE', onboard_order_ids=[], planned_suffix=[])
                            for v in vehicles])
    calls = []
    value = SimpleNamespace(orders=orders, proposals=0, columns=[], deadline=1e20)
    def realize(vehicle, sequence, **kwargs):
        calls.append((vehicle.vehicle_id, sequence))
        value.proposals += 1
    value.realize = realize
    value.domain = lambda before: {'columns': value.columns}
    post_domain(state, None, anchor, DynamicLimits(max_proposals=6), _builder=value, _fair_share=True)
    assert [vid for vid, _ in calls] == ['a', 'b', 'a', 'b', 'a', 'b']
    assert calls[2][1] == calls[3][1] == ['x', 'y']


def test_initial_seed_tries_complementary_loads_before_overlapping_subsets():
    vehicles = [SimpleNamespace(vehicle_id=v, capacity_kg=2.) for v in ('a', 'b')]
    orders = [SimpleNamespace(order_id=o, demand_kg=1., coordinates=(i, i), status='WAITING')
              for i, o in enumerate(('w', 'x', 'y', 'z'))]
    state = SimpleNamespace(vehicles=vehicles, orders=orders, scenario_id='test',
                            depot=SimpleNamespace(coordinates=(0, 0)))
    value = SimpleNamespace(orders={o.order_id: o for o in orders}, proposals=0, columns=[], deadline=1e20)
    def realize(vehicle, sequence, **kwargs):
        value.proposals += 1
        value.columns.append(dict(vehicle_id=vehicle.vehicle_id, order_sequence=sequence))
    value.realize = realize
    value.domain = lambda before=None: {'columns': value.columns}
    domain = pre_domain(state, None, DynamicLimits(max_proposals=6), _builder=value, _fair_share=True)
    assert domain['stop_reason'] == 'FULL_INITIAL_WITNESS_POOL_FOUND'
    assert value.proposals <= 5
    assert set(value.columns[0]['order_sequence']).isdisjoint(value.columns[-1]['order_sequence'])


@pytest.mark.parametrize('count,fleet_size', [(18, 4), (22, 5), (25, 5), (100, 20)])
def test_large_initial_fleet_covers_orders_without_power_set(monkeypatch, count, fleet_size):
    def forbidden_power_set(*args):
        raise AssertionError('Large fleet search must not enumerate combinations')
    monkeypatch.setattr('optimization.rolling_horizon.member1_dynamic_planner.combinations', forbidden_power_set)
    fleet = [SimpleNamespace(vehicle_id=f'V{i}', capacity_kg=15., availability='AVAILABLE')
             for i in range(fleet_size)]
    orders = [SimpleNamespace(order_id=f'O{i:03d}', demand_kg=1.5,
                              coordinates=(106.7+(i%5)*.01, 10.8+(i//5)*.005),
                              status='WAITING', assigned_vehicle_id=None) for i in range(count)]
    # Existing cargo must retain its original vehicle even on the scalable path.
    orders[0].status = 'ONBOARD'; orders[0].assigned_vehicle_id = 'V0'
    state = SimpleNamespace(vehicles=fleet, orders=orders, scenario_id='test',
                            depot=SimpleNamespace(coordinates=(106.7, 10.8)))
    value = SimpleNamespace(orders={o.order_id: o for o in orders}, proposals=0,
                            columns=[], deadline=1e20)
    def realize(vehicle, sequence, **kwargs):
        value.proposals += 1
        assert sum(value.orders[oid].demand_kg for oid in sequence) <= vehicle.capacity_kg
        assert set(kwargs['pickup_ids']) == {oid for oid in sequence if value.orders[oid].status == 'WAITING'}
        if orders[0].order_id in sequence:
            assert vehicle.vehicle_id == 'V0'
        value.columns.append(dict(vehicle_id=vehicle.vehicle_id, order_sequence=sequence))
    value.realize = realize
    value.domain = lambda before=None: {'columns': value.columns}
    domain = pre_domain(state, None, DynamicLimits(max_proposals=40), _builder=value, _fair_share=True)
    assigned = [oid for c in domain['columns'] for oid in c['order_sequence']]
    assert len(assigned) == len(set(assigned)) == count
    assert {c['vehicle_id'] for c in domain['columns']} == {v.vehicle_id for v in fleet}
    assert value.proposals == fleet_size


@pytest.mark.parametrize('count,fleet_size', [(18,4), (22,5), (25,5), (100,20)])
def test_large_fresh_delivery_day_assigns_complementary_tours(monkeypatch, count, fleet_size):
    def no_subsets(*args):
        raise AssertionError('Fresh delivery day must distribute the fleet before subset search')
    monkeypatch.setattr('optimization.rolling_horizon.member1_dynamic_planner.combinations', no_subsets)
    fleet = [SimpleNamespace(vehicle_id=f'V{i}', capacity_kg=15., availability='AVAILABLE') for i in range(fleet_size)]
    orders = [SimpleNamespace(order_id=f'O{i:03d}', demand_kg=1.5, status='WAITING',
                             coordinates=(106.7+(i%5)*.01,10.8+(i//5)*.005)) for i in range(count)]
    state = SimpleNamespace(vehicles=fleet, orders=orders, depot=SimpleNamespace(graph_node_id=1, coordinates=(106.7,10.8)))
    anchor = {'current_time':'2026-09-28T08:00:00+07:00',
              'orders':[{'order_id':o.order_id,'status':'WAITING'} for o in orders],
              'vehicles':[{'vehicle_id':v.vehicle_id,'availability':'AVAILABLE','onboard_order_ids':[],
                           'current_load_kg':0,'position':{'kind':'AT_NODE','node_id':1}} for v in fleet],
              'delivery_day':{'queue':{},'pickup_visits':{}}}
    builder = SimpleNamespace(orders={o.order_id:o for o in orders},proposals=0,columns=[],deadline=1e20)
    def realize(vehicle, sequence, **kwargs):
        builder.proposals += 1
        assert kwargs['decision_time'] == anchor['current_time']
        assert kwargs['snapshot']['vehicle_id'] == vehicle.vehicle_id
        assert set(kwargs['pickup_ids']) == set(sequence)
        assert sum(builder.orders[oid].demand_kg for oid in sequence) <= vehicle.capacity_kg
        builder.columns.append({'vehicle_id':vehicle.vehicle_id,'order_sequence':sequence})
    builder.realize = realize
    builder.domain = lambda observed: {'columns':builder.columns,'observed':observed}
    domain = post_domain(state,None,anchor,DynamicLimits(max_proposals=40),_builder=builder,_fair_share=True)
    assigned = [oid for c in domain['columns'] for oid in c['order_sequence']]
    assert len(assigned) == len(set(assigned)) == count
    assert {c['vehicle_id'] for c in domain['columns']} == {v.vehicle_id for v in fleet}
    assert domain['observed'] is anchor
