import sqlite3
from types import SimpleNamespace
from time import monotonic

from optimization.runtime.profile_paths import ProfilePaths


def builder():
    db=sqlite3.connect(':memory:');db.execute("ATTACH ':memory:' AS costs")
    db.execute('CREATE TABLE edges(edgeId TEXT,fromNodeId INTEGER,toNodeId INTEGER)')
    db.execute('CREATE TABLE costs.features(edgeId TEXT,travelTimeHours REAL,relativeExposure REAL)')
    edges=[{'edgeId':name,'fromNodeId':1,'toNodeId':2,'travelTimeHours':seconds/3600.,'relativeExposure':risk}
           for name,seconds,risk in [('fast',1,10),('balanced',3,4),('safe',6,1)]]
    for edge in edges:
        db.execute('INSERT INTO edges VALUES(?,?,?)',(edge['edgeId'],1,2))
        db.execute('INSERT INTO costs.features VALUES(?,?,?)',(edge['edgeId'],edge['travelTimeHours'],edge['relativeExposure']))
    value=ProfilePaths();value.graph=SimpleNamespace(db=db,forbidden=set(),_outgoing=lambda node:edges if node==1 else [],
        path_from_edge_ids=lambda node,chain,**kw:tuple(chain))
    value.profile_config={'references':{'travel_time_s':10.,'relative_exposure_proxy':10.},'profiles':{'BALANCED':{'time':.6,'risk':.4}}}
    value.cache={};value.traces=[];value.deadline=monotonic()+10
    value.limits=SimpleNamespace(per_query_seconds=5.,max_road_states=100,max_road_labels=100)
    return value


def test_three_objectives_search_genuine_distinct_source_paths():
    value=builder()
    try:
        assert value.paths(1,2,None,'time')==[('fast',)]
        assert value.paths(1,2,None,'balanced')==[('balanced',)]
        assert value.paths(1,2,None,'exposure')==[('safe',)]
    finally:value.graph.db.close()


def test_wet_proposal_uses_a_dry_bypass_without_changing_source_edges():
    value=builder();value.profile_cost_identity='rain'
    value.profile_edge_rates={'fast':{'duration_us':10_000_000,'relativeExposure':20}}
    # Existing baseline Columns cache must not mask the weather proposal.
    value.cache[(1,2,None,'time')]=[('fast',)]
    try:
        assert value.paths(1,2,None,'time')==[('balanced',)]
        assert value.paths(1,2,None,'exposure')==[('safe',)]
        assert value.graph.db.execute("SELECT travelTimeHours FROM costs.features WHERE edgeId='fast'").fetchone()[0]==1/3600.
    finally:value.graph.db.close()


def test_wet_proposal_still_obeys_incoming_turn_restrictions():
    value=builder();value.profile_cost_identity='rain'
    value.profile_edge_rates={'fast':{'duration_us':10_000_000,'relativeExposure':20}}
    value.graph.forbidden={('entry','balanced')}
    try:assert value.paths(1,2,'entry','time')==[('safe',)]
    finally:value.graph.db.close()


def test_turn_restrictions_are_enforced_despite_turn_free_lower_bound():
    value=builder();value.graph.forbidden={('incoming','fast')}
    try:assert value.paths(1,2,'incoming','time')==[('balanced',)]
    finally:value.graph.db.close()


def test_expired_budget_never_fabricates_a_path():
    value=builder();value.deadline=monotonic()-1
    try:assert value.paths(1,2,None,'time')==[]
    finally:value.graph.db.close()


def test_reverse_frontier_resumes_for_a_farther_origin():
    value=builder()
    value.graph.db.execute('INSERT INTO edges VALUES(?,?,?)',('approach',3,1))
    value.graph.db.execute('INSERT INTO costs.features VALUES(?,?,?)',('approach',2/3600.,0.))
    try:
        assert value.paths(1,2,None,'time')==[('fast',)]
        assert value.paths(3,2,None,'time')==[('approach','fast')]
        assert value.paths(4,2,None,'time')==[]
    finally:value.graph.db.close()


def test_turn_detour_can_expand_beyond_the_initial_reverse_frontier():
    value=builder()
    for name,origin,dest,seconds in [('detour',1,3,1.),('finish',3,2,2.)]:
        value.graph.db.execute('INSERT INTO edges VALUES(?,?,?)',(name,origin,dest))
        value.graph.db.execute('INSERT INTO costs.features VALUES(?,?,?)',(name,seconds/3600.,0.))
    value.graph.forbidden={('entry',name) for name in ('fast','balanced','safe')}
    try:
        assert value.paths(1,2,'entry','time')==[('detour','finish')]
    finally:value.graph.db.close()
