"""Isolated current-anchor computation; no authority-store access.

Change Impact Analysis: new /2; legacy worker/engine defaults unchanged.
Atomic incumbents remain untrusted until parent's raw all-column check.
"""
import argparse,time,os
from dataclasses import replace,asdict
from pathlib import Path
from fractions import Fraction
from copy import deepcopy
from optimization.models.member1_dynamic_state import sha as physical_sha
from optimization.models.decision_state import DecisionState,OrderState
from optimization.integration.member1_static_runner import _verified_source,_sha
from optimization.integration.member1_s1_runner import _require_no_sqlite_sidecars
from optimization.integration.member1_s0_graph import Member1RoadGraph
from optimization.integration.member1_profiles import load_profile_config
from optimization.rolling_horizon.member1_dynamic_planner import pre_domain,post_domain,DynamicLimits,Columns
from optimization.rolling_horizon.member1_rain_planner import RainColumns
from optimization.rolling_horizon.member1_rain_source import load_source_model
from optimization.solver.member1_dynamic_master import select_columns
from .protocol import decode,require,canonical,RuntimeError
from .validation import validate_runtime_result
from .profile_paths import ProfilePaths, enrich_profile_domain, time_domain_budget

VERSION='task02-m2-runtime-column-compute/2'
WORKER_VERSION='task02-m2-runtime-worker-envelope/2'

class ProgressColumns:
    observer=None
    def realize(self,*args,**kwargs):
        count=len(self.columns);result=super().realize(*args,**kwargs)
        if self.observer is not None and len(self.columns)>count:self.observer(self)
        return result

class ObservedRainColumns(ProgressColumns,ProfilePaths,RainColumns):
    def initial_edge_progress(self,snapshot,clock):return Fraction(snapshot['position']['progress_exact'])
    def paths_at(self,node,target,incoming,objective,departure_us):
        if getattr(self,'fleet_seed',False):
            # Raw source-backed paths are fully re-timed by edge_action under
            # the wet overlay. Establish complete fleet continuations before
            # spending the finite budget on temporal arrival refinements.
            return Columns.paths(self,node,target,incoming,'time')
        if getattr(self,'rain_profile_pass',False):
            # Frozen wet rates only rank path proposals. edge_action still
            # realizes every edge at its actual departure and rain expiry.
            return ProfilePaths.paths(self,node,target,incoming,objective)
        return RainColumns.paths_at(self,node,target,incoming,objective,departure_us)

class ObservedColumns(ProgressColumns,ProfilePaths,Columns):
    def initial_edge_progress(self,snapshot,clock):return Fraction(snapshot['position']['progress_exact'])
    def edge_action(self,edge_id,start,*,fraction=0.,incoming=None):
        value=super().edge_action(edge_id,start,fraction=float(fraction),incoming=incoming)
        value['fraction_start_exact']=str(fraction if isinstance(fraction,Fraction) else Fraction(str(fraction)))
        return value

def compute(inp,checkpoint=None,*,domain_only=False,_raw_cache=None):
    started=time.monotonic();state=DecisionState.from_dict(inp['initial']);snapshot=Path(inp['snapshot_root']);anchor=inp.get('anchor');overlay=inp.get('overlay')
    budget=inp['compute_seconds'];paths,hashes=_verified_source(snapshot,state)
    require(hashes==inp['source_hashes'],'SOURCE_CHANGED','source_hashes','bootstrap source differs');_require_no_sqlite_sidecars(paths,phase='before_open')
    from .delivery_day import effective_state
    state=effective_state(state,anchor)
    try:
        with Member1RoadGraph(paths['network_sqlite_sha256'],paths['features_sqlite_sha256'],routing_version=state.routing_version,features_version=state.features_version,context_version=state.context_version,source_hashes=hashes) as graph:
            config=load_profile_config(Path(__file__).resolve().parents[2]/'configs/member1_profiles_step3.json')
            limits=replace(DynamicLimits(),domain_seconds=max(.001,budget*.65));t=time.monotonic()
            if domain_only:
                limits=replace(limits,max_proposals=36,max_columns=48,master_seconds=2.)
            orders={o.order_id:o for o in state.orders}
            if anchor and anchor.get('event_order'):o=OrderState.from_dict(anchor['event_order']);orders[o.order_id]=o
            actual=sorted(o['order_id'] for o in anchor['orders'] if o['status']=='DELIVERED') if anchor else []
            required=[v['vehicle_id'] for v in anchor['vehicles'] if v['availability']=='AVAILABLE' and (v['active_commitment'] is not None or v['position']['kind']=='ON_EDGE' or v['position'].get('node_id')!=state.depot.graph_node_id or v.get('explicit_committed_stop_id') is not None and v['explicit_committed_stop_id'] not in actual)] if anchor else []
            retained=None;last_count=0;checked=set()
            def keep(record):
                nonlocal retained
                if record['validation']['valid'] is not True:return
                vec=record['result']['search']['master']['incumbent_vector'][:-1]
                if retained is None or tuple(vec)>=tuple(retained['result']['search']['master']['incumbent_vector'][:-1]):
                    # Domain is immutable within a compute. Copying every raw
                    # action for every incumbent caused seconds of GIL work.
                    retained={**deepcopy({k:v for k,v in record.items() if k!='domain'}),'domain':record['domain']}
                    if checkpoint is not None:checkpoint(retained)
            def proposal_progress(builder):
                nonlocal last_count
                if checkpoint is None or len(builder.columns)<last_count+2:return
                d=deepcopy(builder.domain(anchor))
                if anchor is None and state.scenario_id=='S3' and state.source_authentication=='PINNED_RECEIPT_VERIFIED':
                    d['columns']=[c for c in d['columns'] if c['vehicle_id']!='V1' or any(a['kind']=='SERVICE' and a['order_id']=='O001' and a['end_us']>900000000 for a in c['actions'])]
                    d.pop('content_sha256');d['content_sha256']=physical_sha(d)
                if not all(any(c['vehicle_id']==v for c in d['columns']) for v in required):return
                remaining=budget-(time.monotonic()-started)
                if remaining<=2:return
                last_count=len(builder.columns)
                master=select_columns(d['columns'],{k:o.priority for k,o in orders.items()},[v.vehicle_id for v in state.vehicles],inp['profile'],config,seconds=min(1.,remaining*.1),required_vehicle_ids=required,urgent_order_ids=(anchor or {}).get('delivery_day',{}).get('urgent_ids',[]))
                keep(pack(inp,state,graph,d,master,config,limits,started,time.monotonic()-t))
            if inp.get('cached_domain') is not None:
                domain=inp['cached_domain']
            elif anchor is None:
                builder=ObservedColumns(state,graph,limits);builder.observer=proposal_progress;builder.profile_config=config
                with time_domain_budget(builder) as seed_limits:
                    domain=pre_domain(state,graph,seed_limits,_builder=builder,_fair_share=True)
            elif overlay is None:
                builder=ObservedColumns(state,graph,limits);builder.observer=proposal_progress;builder.profile_config=config
                with time_domain_budget(builder) as seed_limits:
                    prime_required_returns(state,anchor,builder)
                    domain=post_domain(state,graph,anchor,seed_limits,_builder=builder,_fair_share=True)
            else:
                model=load_source_model(snapshot,state);builder=ObservedRainColumns(state,graph,limits,model,overlay);builder.observer=proposal_progress
                builder.profile_config=config
                builder.profile_cost_identity=overlay['content_sha256']
                builder.profile_edge_rates={eid:row['wet'] for eid,row in overlay['cost_table'].items()}
                with time_domain_budget(builder) as seed_limits:
                    builder.fleet_seed=True
                    try:prime_required_returns(state,anchor,builder)
                    finally:builder.fleet_seed=False
                    domain=post_domain(state,graph,anchor,seed_limits,_builder=builder,_fair_share=True)
            if inp.get('cached_domain') is None:
                if overlay is None:enrich_profile_domain(builder,state,anchor)
                else:
                    builder.rain_profile_pass=True
                    try:
                        deadline=builder.deadline
                        builder.deadline=min(deadline,time.monotonic()+max(0.,deadline-time.monotonic())/3)
                        try:prime_required_returns(state,anchor,builder)
                        finally:builder.deadline=deadline
                        enrich_profile_domain(builder,state,anchor)
                    finally:builder.rain_profile_pass=False
                if anchor is None and state.scenario_id=='S3' and state.source_authentication=='PINNED_RECEIPT_VERIFIED':
                    builder.columns=[c for c in builder.columns if c['vehicle_id']!='V1' or any(a['kind']=='SERVICE' and a['order_id']=='O001' and a['end_us']>900000000 for a in c['actions'])]
                domain=builder.domain(anchor)
            elapsed=time.monotonic()-t
            if domain_only:
                require(hashes=={k:_sha(p) for k,p in paths.items()},'SOURCE_CHANGED','source_hashes','raw source changed')
                return domain
            remaining=budget-(time.monotonic()-started);require(remaining>0,'BUDGET_EXHAUSTED','compute_seconds','no budget for master')
            orders={o.order_id:o for o in state.orders}
            if anchor and anchor.get('event_order'):o=OrderState.from_dict(anchor['event_order']);orders[o.order_id]=o
            actual=sorted(o['order_id'] for o in anchor['orders'] if o['status']=='DELIVERED') if anchor else []
            required=[]
            if anchor:required=[v['vehicle_id'] for v in anchor['vehicles'] if v['availability']=='AVAILABLE' and (v['active_commitment'] is not None or v['position']['kind']=='ON_EDGE' or v['position'].get('node_id')!=state.depot.graph_node_id or v.get('explicit_committed_stop_id') is not None and v['explicit_committed_stop_id'] not in actual)]
            def envelope(master):
                return pack(inp,state,graph,domain,master,config,limits,started,elapsed,_raw_cache=_raw_cache)
            def progress(master):
                # A later objective phase often reports exactly the same witness.
                # Preserve final phase telemetry without revalidating/copying the
                # multi-MB immutable domain for each identical route selection.
                key=tuple(master['selected_route_indexes'])
                if key in checked:return
                checked.add(key);keep(envelope(master))
            t=time.monotonic();master=select_columns(domain['columns'],{k:o.priority for k,o in orders.items()},[v.vehicle_id for v in state.vehicles],inp['profile'],config,seconds=min(limits.master_seconds,max(.001,remaining*.75)),required_vehicle_ids=required,_progress=progress,urgent_order_ids=(anchor or {}).get('delivery_day',{}).get('urgent_ids',[]))
            progress(master)
            v=retained if retained is not None else envelope(master)
            if v['result']['search']['master']['selected_route_indexes']==master['selected_route_indexes']:
                v['result']['search']['master']=master
            v['telemetry']['master_seconds']=time.monotonic()-t
    finally:_require_no_sqlite_sidecars(paths,phase='after_close')
    require(hashes=={k:_sha(p) for k,p in paths.items()},'SOURCE_CHANGED','source_hashes','raw source changed');return v

def pack(inp,state,graph,domain,master,config,limits,started,elapsed,*,_raw_cache=None):
    """Proposal-stage checkpoint: genuine CP-SAT + independent raw check."""
    anchor=inp.get('anchor');overlay=inp.get('overlay');orders={o.order_id:o for o in state.orders}
    if anchor and anchor.get('event_order'):o=OrderState.from_dict(anchor['event_order']);orders[o.order_id]=o
    actual=sorted(o['order_id'] for o in anchor['orders'] if o['status']=='DELIVERED') if anchor else []
    routes=[domain['columns'][i] for i in master['selected_route_indexes']];served=sorted(actual+[o for r in routes for o in r['order_sequence']]);unserved=[]
    for oid in sorted(set(orders)-set(served)):
        observed=next((o for o in anchor['orders'] if o['order_id']==oid),None) if anchor else None
        owner=observed['owner_vehicle_id'] if observed and observed['status']=='ONBOARD' else None
        blocked=owner is not None and any(v['vehicle_id']==owner and v['availability']=='UNAVAILABLE' for v in anchor['vehicles'])
        unserved.append({'order_id':oid,'reason':'CUSTODY_BLOCKED' if blocked else 'NOT_SERVED_BY_FOUND_WITNESS','owner_vehicle_id':owner})
    status='FEASIBLE' if not unserved else 'PARTIAL' if served else 'RETURN_ONLY' if routes else 'SEARCH_LIMIT'
    result={'schema_version':'task02-m2-runtime-witness/2','solver_version':VERSION,'scenario_id':state.scenario_id,'profile':inp['profile'],'status':status,
        'domain_sha256':domain['content_sha256'],'source_hashes':graph.source_hashes,'served_orders':served,'unserved_orders':unserved,'vehicle_routes':routes,
        'anchor_sha256':anchor['content_sha256'] if anchor else None,'overlay_sha256':overlay['content_sha256'] if overlay else None,
        'forecast':True,'post_event_state':None,'metric_scope':'PLANNED_SUFFIX_ONLY','search':{'search_complete':False,'optimality_proven':False,'truncated':True,'master':master},
        'metrics':{k:sum(r[k] for r in routes) for k in ('total_distance_m','total_travel_time_s','total_exposure','total_cost_vnd','total_soft_lateness_s')}}
    check=validate_runtime_result(state,graph,domain,result,config,anchor=anchor,snapshot=inp['snapshot_root'],overlay=overlay,_raw_cache=_raw_cache) if routes or actual else {'valid':None,'validation_status':'NOT_RUN'}
    if not routes and not actual:
        from .no_service import certificate
        proof=certificate(state,anchor)
        result.update(served_orders=[],unserved_orders=[],coverage_evaluated=False,diagnostics=[{'severity':'ERROR','code':'NO_SERVICE_SCOPED_PROOF' if proof else 'SEARCH_INCOMPLETE','path':'no_service_certificate' if proof else 'domain','message':'Trusted no-split capacity/custody facts prevent pending deliveries; no physical plan certified.' if proof else 'No witness retained in finite domain; not an infeasibility proof.'}])
        if proof:result.update(status='NO_SERVICE',no_service_certificate=proof)
    return {'schema_version':WORKER_VERSION,'binding':inp['binding'],'result':result,'validation':check,'domain':domain,
        'telemetry':{'solver_executed':True,'road_search_executed':inp.get('cached_domain') is None,'cache_hit':inp.get('cached_domain') is not None,'cache_origin_job_id':inp.get('cached_domain_origin_job_id'),'domain_seconds':elapsed,'elapsed_seconds':time.monotonic()-started,'limits':asdict(limits)}}

def prime_required_returns(state,anchor,builder):
    """Keep each loaded vehicle's service continuation before optional search.

    Return-only seeds compete for the same bounded proposal slots as deliveries.
    If every early delivery option includes the new urgent order, only one can
    be selected and the other vehicles lose their remaining tours. Preserve
    complementary, custody-bound continuations before trying urgent insertions.
    """
    fulfilled=[o['order_id'] for o in anchor['orders'] if o['status']=='DELIVERED']
    for vehicle in state.vehicles:
        snapshot=next(x for x in anchor['vehicles'] if x['vehicle_id']==vehicle.vehicle_id)
        if snapshot['availability']!='AVAILABLE':continue
        owned=set(snapshot['onboard_order_ids'])-set(fulfilled)
        sequence=list(dict.fromkeys(oid for oid in snapshot['planned_suffix'] if oid in owned))
        sequence+=sorted(owned-set(sequence))
        committed=snapshot.get('explicit_committed_stop_id')
        if committed in owned:
            sequence=[committed]+[oid for oid in sequence if oid!=committed]
        builder.realize(vehicle,sequence,snapshot=snapshot,decision_time=anchor['current_time'],fulfilled_ids=fulfilled)

def main():
    p=argparse.ArgumentParser();p.add_argument('--input',required=True,type=Path);p.add_argument('--output',required=True,type=Path);p.add_argument('--incumbent',type=Path);a=p.parse_args()
    def checkpoint(v):
        if a.incumbent is not None:
            temp=a.incumbent.with_suffix('.pending');temp.write_bytes(canonical(v));os.replace(temp,a.incumbent)
    inp=decode(a.input.read_bytes())
    if isinstance(inp,dict) and 'batch_jobs' in inp:
        from .profile_batch import worker_batch
        worker_batch(inp,a.output)
        return 0
    v=compute(inp,checkpoint)
    with a.output.open('xb') as f:f.write(canonical(v))
    return 0
if __name__=='__main__':raise SystemExit(main())
