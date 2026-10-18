"""Bounded profile-aware source paths with a shared turn-free lower bound.

Reverse distances ignore turn restrictions only for search ordering. Every
forward path respects directed edges and turns, and the normal raw witness
validator still certifies capacity, time windows, cargo and source geometry.
This finite proposal search makes no global optimality claim.
Wet proposal graphs may rank edges with frozen wet rates. Those bounds are
only for that proposal graph; temporal realization rechecks actual rates.
"""
import heapq
import math
from contextlib import contextmanager
from dataclasses import replace
from time import monotonic

from .protocol import require


class ProfilePaths:
    def paths(self, node, target, incoming, objective):
        if node == target:
            return [None]
        key = (node, target, incoming, objective, getattr(self, 'profile_cost_identity', None))
        if key in self.cache:
            return self.cache[key]
        config = self.profile_config
        weights = {'time': (1., 0.), 'balanced': (config['profiles']['BALANCED']['time'] / config['references']['travel_time_s'],
                     config['profiles']['BALANCED']['risk'] / config['references']['relative_exposure_proxy']), 'exposure': (0., 1.)}
        require(objective in weights, 'INVALID_DATA', 'objective', 'unknown path objective')
        deadline = min(self.deadline, monotonic() + self.limits.per_query_seconds)
        if not hasattr(self, '_reverse_edges'):
            self._reverse_edges = {}
            self._forward_edges = {}
            for index, (eid, origin, dest, hours, risk) in enumerate(self.graph.db.execute(
                    'SELECT e.edgeId,e.fromNodeId,e.toNodeId,f.travelTimeHours,f.relativeExposure '
                    'FROM edges e JOIN costs.features f USING(edgeId)')):
                rates = getattr(self, 'profile_edge_rates', {}).get(eid)
                if rates is not None:
                    hours, risk = rates['duration_us'] / 3_600_000_000., rates['relativeExposure']
                require(all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in (hours, risk)),
                        'SOURCE_INVALID', 'edge', 'invalid source path cost')
                self._reverse_edges.setdefault(dest, []).append((origin, hours * 3600., risk))
                self._forward_edges.setdefault(origin, []).append((eid, dest, hours * 3600., risk))
                if index % 1024 == 0 and monotonic() >= deadline:
                    del self._reverse_edges
                    return []
            self._potentials = {}
        time_weight, risk_weight = weights[objective]
        potential_key = (target, objective)
        if potential_key not in self._potentials:
            self._potentials[potential_key] = ({}, {target: 0.}, [(0., target)])
        potential, tentative, reverse_queue = self._potentials[potential_key]

        def lower_bound(origin_node):
            # Continue reverse Dijkstra only until this node is settled. Every
            # returned bound is exact in the turn-free graph. Later origins or
            # turn detours resume the same frontier; tentative distances are
            # never used as heuristics or as proof of unreachability.
            count = 0
            while reverse_queue and origin_node not in potential:
                if count % 256 == 0 and monotonic() >= deadline:return None
                distance, dest = heapq.heappop(reverse_queue)
                if distance != tentative[dest] or dest in potential:continue
                potential[dest] = distance
                count += 1
                for origin, seconds, risk in self._reverse_edges.get(dest, ()):
                    cost = distance + time_weight * seconds + risk_weight * risk
                    if cost < tentative.get(origin, math.inf):
                        tentative[origin] = cost; heapq.heappush(reverse_queue, (cost, origin))
            return potential.get(origin_node, math.inf)

        initial_bound = lower_bound(node)
        if initial_bound is None or math.isinf(initial_bound):return []
        initial = (node, incoming); best = {initial: (0., 0.)}; parents = {}
        queue = [(initial_bound, 0., 0., 0, initial)]; serial = settled = 0; paths = []
        reason = 'NO_PATH'
        while queue:
            if monotonic() >= deadline:reason = 'TIME_LIMIT';break
            _, cost, travel, _, state = heapq.heappop(queue)
            if (cost, travel) != best[state]:continue
            settled += 1
            if settled > self.limits.max_road_states:reason = 'SEARCH_LIMIT';break
            current, previous = state
            if current == target:
                chain = []; cursor = state
                while cursor != initial:
                    cursor, eid = parents[cursor]; chain.append(eid)
                # Hydrate/validate geometry and feature payload only for the
                # selected witness; search uses the same immutable SQL costs.
                paths = [self.graph.path_from_edge_ids(node, list(reversed(chain)), incoming_edge=incoming)];reason = 'WITNESS_FOUND';break
            for eid, dest, seconds, risk in self._forward_edges.get(current, ()):
                if (previous, eid) in self.graph.forbidden:continue
                bound = lower_bound(dest)
                if bound is None:reason = 'TIME_LIMIT';queue.clear();break
                if math.isinf(bound):continue
                next_cost = cost + time_weight * seconds + risk_weight * risk
                next_travel = travel + seconds; following = (dest, eid)
                if (next_cost, next_travel) >= best.get(following, (math.inf, math.inf)):continue
                if len(best) >= self.limits.max_road_labels:reason = 'LABEL_LIMIT';queue.clear();break
                best[following] = (next_cost, next_travel); parents[following] = (state, eid); serial += 1
                heapq.heappush(queue, (next_cost + bound, next_cost, next_travel, serial, following))
        self.traces.append({'origin': node, 'destination': target, 'incoming_edge': incoming,
                            'objective': objective, 'stage': 'PROFILE_SOURCE_ASTAR', 'status': reason,
                            'option_count': len(paths), 'settled_states': settled})
        if paths:self.cache[key] = paths
        return paths


@contextmanager
def time_domain_budget(builder):
    """Reserve time, proposals and column slots for both risk-aware passes."""
    limits, deadline = builder.limits, builder.deadline
    builder.limits = replace(limits, max_proposals=max(1, limits.max_proposals // 3),
                            max_columns=max(1, limits.max_columns // 3))
    builder.deadline = min(deadline, monotonic() + max(0., deadline - monotonic()) * .6)
    try:
        yield builder.limits
    finally:
        builder.limits, builder.deadline = limits, deadline


def enrich_profile_domain(builder, state, anchor=None):
    """Add genuine time/risk tradeoffs for the already feasible service tours."""
    from optimization.solver.member1_dynamic_master import select_columns

    grouped = {}
    for c in sorted(builder.columns, key=lambda c: (-len(c['order_sequence']), c['total_soft_lateness_s'], c['total_travel_time_s'])):
        sequence = tuple(c['order_sequence'])
        tours = grouped.setdefault(c['vehicle_id'], [])
        if sequence not in tours:
            tours.append(sequence)
    # One tour per vehicle per round, so the first vehicle cannot monopolize
    # the bounded profile passes. Prefer service tours over empty returns.
    tours = [(vid, sequences[i]) for i in range(max(map(len, grouped.values()), default=0))
             for vid, sequences in grouped.items() if i < len(sequences)]
    # Refine a jointly feasible fleet first. Refining individually attractive
    # tours can waste the entire pass on overlapping order assignments.
    remaining = builder.deadline - monotonic()
    if builder.columns and remaining > .1:
        seed = select_columns(builder.columns, {oid: o.priority for oid, o in builder.orders.items()},
                              [v.vehicle_id for v in state.vehicles], 'FASTEST', builder.profile_config,
                              seconds=min(.25, remaining * .05),
                              urgent_order_ids=(anchor or {}).get('delivery_day', {}).get('urgent_ids', []))
        preferred = [(builder.columns[i]['vehicle_id'], tuple(builder.columns[i]['order_sequence']))
                     for i in seed['selected_route_indexes']]
        tours = preferred + [tour for tour in tours if tour not in preferred]
    limits, deadline = builder.limits, builder.deadline
    try:
        for index, objective in enumerate(('balanced', 'exposure')):
            remaining_passes = 2 - index
            builder.deadline = min(deadline, monotonic() + max(0., deadline - monotonic()) / remaining_passes)
            builder.limits = replace(limits,
                max_proposals=builder.proposals + max(0, limits.max_proposals - builder.proposals) // remaining_passes,
                max_columns=len(builder.columns) + max(0, limits.max_columns - len(builder.columns)) // remaining_passes)
            for vid, sequence in tours:
                if (monotonic() >= builder.deadline or builder.proposals >= builder.limits.max_proposals
                        or len(builder.columns) >= builder.limits.max_columns):
                    break
                vehicle = next(v for v in state.vehicles if v.vehicle_id == vid)
                if anchor is None:
                    builder.realize(vehicle, list(sequence), pickup_ids=[oid for oid in sequence if builder.orders[oid].status == 'WAITING'], objective=objective)
                else:
                    observed = next(v for v in anchor['vehicles'] if v['vehicle_id'] == vid)
                    waiting = {o['order_id'] for o in anchor['orders'] if o['status'] == 'WAITING'}
                    builder.realize(vehicle, list(sequence), snapshot=observed, decision_time=anchor['current_time'],
                                    fulfilled_ids=[o['order_id'] for o in anchor['orders'] if o['status'] == 'DELIVERED'],
                                    pickup_ids=[oid for oid in sequence if oid in waiting], objective=objective)
    finally:
        builder.limits, builder.deadline = limits, deadline
