"""S4 temporal realization using the locked Step5 column/master core.

Baseline proposal IDs are reusable; temporal metrics are not. Refinement keeps
different arrival times even when dry time/distance dominate: expiry can change
the exposure of continuation. Every cutoff is evidence, not an absence proof.
"""
from copy import deepcopy
import heapq
from fractions import Fraction
from time import monotonic
from optimization.models.member1_dynamic_state import require,sha,finalize,DynamicError
from optimization.models.member1_rain import POLICY,DOMAIN_VERSION,RESULT_VERSION,SOLVER_VERSION
from optimization.rolling_horizon.member1_dynamic_planner import Columns,post_domain,solve_domain
from optimization.rolling_horizon.member1_rain_source import source_edge_rates
from optimization.rolling_horizon.member1_rain_temporal import traverse


class RainColumns(Columns):
    def __init__(self,state,graph,limits,model,overlay):
        super().__init__(state,graph,limits);self.model=model;self.overlay=overlay;self.temporal_cache={}

    def edge_action(self,edge_id,start,*,fraction=0.,incoming=None):
        rates=source_edge_rates(self.state,self.graph,self.model,self.overlay,edge_id)
        result=traverse(rates,start,progress=fraction,incoming_edge=incoming)
        row=self.model.ingredients(self.graph,edge_id)
        return {'kind':'EDGE','edge_id':edge_id,'from_node':rates.from_node,'to_node':rates.to_node,
            'incoming_edge':incoming,'start_us':start,'end_us':result['end_us'],
            'fraction_start':float(result['fraction_start'] if '/' not in result['fraction_start'] else __import__('fractions').Fraction(result['fraction_start'])),
            'fraction_start_exact':result['fraction_start'],'fraction_end':1.,'distance_m':result['distance_m'],
            'exposure':result['exposure'],'geometry':row['geometry'],'feature_payload':row['feature_payload'],
            'temporal_segments':result['segments'],'temporal_policy':POLICY,'overlay_sha256':self.overlay['content_sha256']}

    def finish_committed_edge(self,action,commitment):
        # Keep held edge/entry/progress/incoming, not obsolete dry residual ETA.
        return action
    def initial_edge_progress(self,snapshot,clock):
        position=snapshot['position']
        elapsed=clock-position['edge_entry_us'];duration=position['expected_exit_us']-position['edge_entry_us']
        require(duration>0 and 0<=elapsed<duration,'TEMPORAL_PROGRESS','position','trusted active edge anchor required')
        return Fraction(elapsed,duration)

    def _refine(self,node,target,incoming,departure,objective,deadline):
        serial=0;heap=[(0.,0, node,incoming,departure,0.,0.,())];found=[];settled=0;labels=1;pruned=0;frontiers={}
        coords=self.graph.node(target)
        while heap:
            if monotonic()>=deadline:break
            _,_,local,prev,clock,distance,exposure,chain=heapq.heappop(heap);settled+=1
            if settled>self.limits.max_road_states:break
            if local==target and chain:
                found.append(self.graph.path_from_edge_ids(node,chain,incoming_edge=incoming))
                if len(found)>=self.limits.max_arrival_options:break
                continue
            for raw in self.graph._outgoing(local):
                eid=raw['edgeId']
                if (prev,eid) in self.graph.forbidden or eid in chain:continue
                a=self.edge_action(eid,clock,incoming=prev);now=a['end_us'];dist=distance+a['distance_m'];risk=exposure+a['exposure']
                # Identical arrival/turn is not enough under our finite path
                # no-repeat-edge policy: history determines legal continuation.
                # Compare resources only for equal used-directed-edge sets.
                # Store the existing chain tuple by reference, not a large set
                # per label; compute set equality only on a state collision.
                state=(a['to_node'],eid,now);existing=frontiers.get(state,[]);new_chain=chain+(eid,)
                same=lambda history:set(history)==set(new_chain)
                if any(same(h) and d<=dist and r<=risk for d,r,h in existing):pruned+=1;continue
                frontiers[state]=[(d,r,h) for d,r,h in existing if not (same(h) and dist<=d and risk<=r)]+[(dist,risk,new_chain)]
                labels+=1
                if labels>self.limits.max_road_labels:break
                serial+=1;priority={'time':now-departure,'distance':dist,'exposure':risk}[objective]
                # Queue ordering only, never hard feasibility pruning. Bounded
                # searches do not certify shortest paths or disconnection.
                if objective=='time':
                    position=self.graph.node(a['to_node'])
                    priority+=((position[0]-coords[0])**2+(position[1]-coords[1])**2)**.5*111000/12.5*1e6
                heapq.heappush(heap,(priority,serial,a['to_node'],eid,now,dist,risk,new_chain))
            if labels>self.limits.max_road_labels:break
        reason='LABEL_LIMIT' if labels>self.limits.max_road_labels else 'SEARCH_LIMIT' if settled>self.limits.max_road_states else 'TIME_LIMIT' if monotonic()>=deadline else 'OPTION_LIMIT' if heap else None
        self.traces.append({'origin':node,'destination':target,'incoming_edge':incoming,'departure_us':departure,'objective':objective,
            'stage':'TEMPORAL_ARRIVAL_REFINEMENT','option_count':len(found),'limit':reason,'settled_states':settled,'generated_labels':labels,
            'dominated_prunes':pruned,'dominance':'IDENTICAL_ARRIVAL_AND_USED_EDGE_SET','cycle_policy':'NO_REPEATED_DIRECTED_EDGE_IN_FINITE_PROPOSAL'})
        return found

    def paths_at(self,node,target,incoming,objective,departure_us):
        if node==target:return [None]
        key=(node,target,incoming,departure_us,objective,self.overlay['content_sha256'],POLICY,sha(self.graph.source_hashes))
        if key in self.temporal_cache:return self.temporal_cache[key]
        # Retain a source-backed seed even if refinement reaches a cap. Source
        # ERROR always aborts; an incumbent cannot hide broken source data.
        options=list(super().paths(node,target,incoming,'time'))
        deadline=min(self.deadline,monotonic()+self.limits.per_query_seconds)
        if monotonic()<deadline:
            for mode in ('time','distance','exposure'):
                if monotonic()>=deadline:break
                try:refined=self._refine(node,target,incoming,departure_us,mode,min(deadline,monotonic()+2.))
                except ValueError as error:
                    failure=DynamicError('PATH_SOURCE_INVALID','domain.queries',str(error));failure.traces=deepcopy(self.traces);raise failure from error
                for p in refined:
                    if all(p.edge_ids!=o.edge_ids for o in options):options.append(p)
        else:self.traces.append({'origin':node,'destination':target,'incoming_edge':incoming,'departure_us':departure_us,'stage':'TEMPORAL_ARRIVAL_REFINEMENT','status':'NOT_STARTED_GLOBAL_DEADLINE'})
        def actual(p):
            clock=departure_us;distance=exposure=0.;prev=incoming
            for eid in p.edge_ids:
                a=self.edge_action(eid,clock,incoming=prev);clock=a['end_us'];distance+=a['distance_m'];exposure+=a['exposure'];prev=eid
            return clock-departure_us,distance,exposure,p.edge_ids
        measured=[(actual(p),p) for p in options]
        # Union of each objective's bounded choices; do not relabel identical
        # time paths as independent distance/exposure alternatives.
        chosen=[]
        for axis in (0,1,2):
            for _,p in sorted(measured,key=lambda pair:(pair[0][axis],pair[0]))[:self.limits.max_arrival_options]:
                if all(p.edge_ids!=o.edge_ids for o in chosen):chosen.append(p)
        self.temporal_cache[key]=chosen;return chosen

    def domain(self,before=None):
        value=super().domain(before);value.pop('content_sha256',None)
        value.update(schema_version=DOMAIN_VERSION,planner_version=SOLVER_VERSION,overlay_sha256=self.overlay['content_sha256'],
            temporal_policy=POLICY,proposal_semantics='BASELINE_IDS_PLUS_BOUNDED_TEMPORAL_REFINEMENT; REALIZED_AT_ACTUAL_DEPARTURE')
        return finalize(value)


def temporal_domain(state,graph,after,limits,model,overlay):
    return post_domain(state,graph,after,limits,_builder=RainColumns(state,graph,limits,model,overlay))


def solve_rain(state,domain,config,profile,*,before,after):
    # Actual Step5 CP-SAT selection, not a relabeled greedy replay.
    result=solve_domain(state,domain,config,profile,before=before,after=after)
    result.update(schema_version=RESULT_VERSION,solver_version=SOLVER_VERSION,overlay_sha256=domain['overlay_sha256'],
                  temporal_policy=POLICY,forecast=True)
    if result['status'] in ('SEARCH_LIMIT','TIME_LIMIT'):
        # A physically valid return-only column does not certify an order
        # service witness or a NO_SERVICE proof. Keep it in domain evidence,
        # never publish it as a no-witness plan.
        result['vehicle_routes']=[];result['planned_served']=[];result['served_orders']=[]
        result['coverage_evaluated']=False
        result['diagnostics']=[{'severity':'ERROR','code':'NO_SERVICE_WITNESS_IN_FINITE_DOMAIN','path':'status','message':'no service witness; no global infeasibility claim'}]
        result['metrics']={k:0. for k in result['metrics']}
        prefix=before['execution_metrics']
        result['whole_trajectory_metrics']={k:prefix[v]/scale for k,(v,scale) in {
            'total_distance_m':('distance_m',1),'total_travel_time_s':('travel_time_us',1e6),
            'total_exposure':('relative_exposure_proxy',1),'total_cost_vnd':('cost_vnd',1)}.items()}
        result['whole_trajectory_metrics']['total_soft_lateness_s']=0.
    return result
