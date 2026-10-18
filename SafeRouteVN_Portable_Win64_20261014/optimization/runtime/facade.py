"""Server-configured runtime facade; never an unauthenticated HTTP endpoint.

Change Impact Analysis: additive observed/event runtime, legacy wires unchanged.
Release readiness remains evidence-gated, independent of implemented features.
"""
from contextlib import contextmanager
from pathlib import Path
from time import monotonic,sleep
import json,subprocess,sys,tempfile
from optimization.models.decision_state import DecisionState,StateContractError
from optimization.models.member1_dynamic_state import DynamicError
from optimization.models.motion_state import MotionContractError
from optimization.integration.member1_decision_state_adapter import load_pinned_initial_states
from optimization.integration.member1_static_runner import _verified_source,_sha
from optimization.integration.member1_s1_runner import _require_no_sqlite_sidecars,S1SidecarError
from optimization.integration.member1_s0_graph import Member1RoadGraph,RoadDataError
from optimization.integration.member1_profiles import load_profile_config
from optimization.rolling_horizon.member1_dynamic_runner import legacy_projection,make_acceptance
from optimization.rolling_horizon.member1_motion_replay import replay_motion,MotionReplayError,canonical_sha256
from optimization.rolling_horizon.member1_motion_validation import validate_motion_state
from .protocol import command,require,RuntimeError,response,sha,copy,wire_exact,instant,decode
from .store import Store
from .build import inventory,verify
from .validation import validate_initial_result
from .validation import validate_runtime_result
from .execution import initial_observation,replay_actions
from .execution_validation import validate_replay
from .events import apply_candidate,validate_event
from optimization.rolling_horizon.member1_rain_source import load_source_model
from .worker_v2 import WORKER_VERSION
from fractions import Fraction

def _source_pins(snapshot,state):
    """Narrow adapter for the legacy raw-pin reader's documented ValueError.

    No planner, engine, callback or programming exception is caught here.
    """
    try:return _verified_source(snapshot,state)
    except ValueError as error:raise RuntimeError('SOURCE_MISMATCH','snapshot_root',str(error)) from error

class Runtime:
    def __init__(self,snapshot_root,store_path,expected_build_sha256,*,repo_root=None):
        self.repo=Path(repo_root or Path(__file__).resolve().parents[2]).resolve();self.snapshot=Path(snapshot_root).resolve()
        self.build=inventory(self.repo);verify(self.repo,self.build,expected_build_sha256)
        from .environment import attest
        self.environment=attest(self.repo)
        self.store=Store(store_path,expected_build_sha256)

    def capabilities(self):
        return {'schema_version':'task02-m2-runtime-capabilities/1','release_status':'IMPLEMENTATION_COMPLETE_RELEASE_EVIDENCE_SEPARATE',
                'default_profile':'BALANCED','profiles':['FASTEST','BALANCED','SAFER'],
                'implemented_operations':['bootstrap','resolve','submit','compute','get_job','cancel','apply_event','accept','advance','get_head','recover','capabilities','dispatch'],
                'compute_scope':'INITIAL_OR_CURRENT_OBSERVED_ANCHOR','observed_scope':'ACCEPTED_ACTIONS_DRY_OR_TEMPORAL',
                'unsupported_operations':[],
                'build_sha256':self.build['build_sha256'],'execution_mode':'SIMULATED_REPLAY','real_world_observation':False,
                'production_calibration':'UNCONFIGURED','general_m1_validated':False,'e4_run':False,'online_target_seconds':30,'online_target_is_sla':False}

    @contextmanager
    def source(self,root):
        state=DecisionState.from_dict(root['initial']);paths,hashes=_source_pins(self.snapshot,state)
        require(hashes==root['source_hashes'],'SOURCE_CHANGED','source_hashes','current source differs from bootstrap')
        _require_no_sqlite_sidecars(paths,phase='before_open')
        try:
            with Member1RoadGraph(paths['network_sqlite_sha256'],paths['features_sqlite_sha256'],routing_version=state.routing_version,features_version=state.features_version,context_version=state.context_version,source_hashes=hashes) as g:yield state,g
        finally:
            try:
                _require_no_sqlite_sidecars(paths,phase='after_close')
                require(hashes=={k:_sha(p) for k,p in paths.items()},'SOURCE_CHANGED','source_hashes','raw bytes changed during operation')
            except BaseException:
                self._replay_checks={}
                raise

    def _check_replay_witness(self,state,graph,result,config):
        """Reuse certified immutable plans after fresh raw source checks.

        Every input is hashed again, including the entire persisted domain,
        anchor, overlay and profile configuration. Physical replay and its
        independent validator still run on every tick. Source guard failures
        clear this process-local, bounded cache before another operation.
        """
        key=sha({'initial':state.to_dict(),'result':result,'config':config,
                 'source':graph.source_hashes,'build':self.build['build_sha256']})
        checks=getattr(self,'_replay_checks',None)
        if checks is None:checks=self._replay_checks={}
        if key in checks:return checks[key]
        check=validate_runtime_result(state,graph,result['domain'],result,config,
            anchor=result['validation_anchor'],snapshot=self.snapshot,overlay=result['overlay'])
        if check['valid'] is True:
            checks[key]=check
            if len(checks)>2:del checks[next(iter(checks))]
        return check

    def bootstrap(self,c):
        batch=load_pinned_initial_states(self.snapshot)
        require(batch.get('status')=='INITIAL_STATE_READY' and batch.get('source_gate')=='M1_SOURCE_CONTRACT_GATE_PASS','SOURCE_GATE','snapshot_root',str(batch.get('diagnostics')))
        initial=next(x for x in batch['states'] if x['scenario_id']==c['scenario_id']);state=DecisionState.from_dict(initial)
        paths,hashes=_source_pins(self.snapshot,state);_require_no_sqlite_sidecars(paths,phase='before_open')
        try:
            orders=[{'order_id':o.order_id,'status':o.status,'owner_vehicle_id':o.assigned_vehicle_id,'picked_up_at':o.picked_up_at,'delivered_at':o.delivered_at,'demand_kg':o.demand_kg} for o in state.orders]
            head={'current_time':state.current_time,'context_version':state.context_version,'overlay_sha256':None,'orders':orders,
                  'pending_events':[e.to_dict() for e in state.pending_events],'execution_mode':'SIMULATED_REPLAY','real_world_observation':False,'observation':None}
        finally:_require_no_sqlite_sidecars(paths,phase='after_close')
        require(hashes=={k:_sha(p) for k,p in paths.items()},'SOURCE_CHANGED','snapshot_root','source changed while bootstrapping')
        root={'initial':initial,'source_hashes':hashes,'receipt_gated':True}
        return self.store._bootstrap(c['session_id'],root,head,c['command_id'])

    def compute(self,c):
        # Corruption is quarantine, not an ordinary failed computation. The
        # transactional Store.fail guard also covers corruption discovered
        # during cleanup, including a concurrent mutation after this read.
        self.store.verify()
        session,job=c['session_id'],c['job_id'];record=self.store.job(session,job)
        if record['status']=='COMPLETED':return record
        head=self.store.head(session);require(record['basis']==head['basis'],'JOB_STALE','job_id','compute cannot run against a stale input basis')
        lease=self.store.claim(session,job)
        failure=None
        try:return self._compute_claimed(c,record,head,lease)
        except (RuntimeError,DynamicError,StateContractError,MotionContractError,RoadDataError,S1SidecarError,OSError) as error:
            failure=error
            raise
        finally:
            # Cleanup only, never masks a programming exception or overwrites
            # a newer lease/COMPLETED result. Known errors are mapped by execute.
            if not isinstance(failure,RuntimeError) or failure.code not in ('RUNTIME_BUSY','STORE_INVALID','JOURNAL_CORRUPT','RECEIPT_CORRUPT','OUTBOX_CORRUPT','BUILD_FENCED','STORE_VERSION','BUILD_UPGRADE_REQUIRED'):
                active=sys.exception()
                try:self.store.fail(session,job,getattr(failure,'code','COMPUTE_ABORTED'),str(failure) if failure is not None else 'compute ended without a certified publish',expected_lease=lease)
                except RuntimeError:
                    # Do not turn a programming exception into a data outcome;
                    # guarded cleanup rolled back without modifying corruption.
                    if active is not None and failure is None:raise active
                    raise

    def _compute_claimed(self,c,record,head,lease):
        session,job=c['session_id'],c['job_id']
        budget=record['request']['budget_seconds'];reserve=min(30.,budget*.3);t=monotonic();deadline=t+budget-reserve
        inp={'initial':head['root']['initial'],'source_hashes':head['root']['source_hashes'],'snapshot_root':str(self.snapshot),
             'head_time':head['head']['current_time'],'profile':record['request']['profile'],'compute_seconds':max(.001,budget-reserve),
             'anchor':head['head']['observation'],'overlay':head['head'].get('overlay'),
             'binding':{'job_id':job,'basis':record['basis'],'lease':lease,'build_sha256':self.store.build}}
        cached=self.store.cached_domain(session,record['basis'])
        if cached:
            with self.source(head['root']) as (state,graph):
                prior=cached['result'];check=validate_runtime_result(state,graph,prior['domain'],prior,load_profile_config(self.repo/'configs/member1_profiles_step3.json'),anchor=inp['anchor'],snapshot=self.snapshot,overlay=inp['overlay'])
                require(check['valid'] is True,'CACHE_INVALID','cached_domain',str(check['diagnostics']))
            inp['cached_domain']=prior['domain'];inp['cached_domain_origin_job_id']=cached['job_id']
        # Native subprocess isolates CP-SAT/SQLite and supports hard cancellation;
        # temporary files remain local and are never part of a runtime package.
        with tempfile.TemporaryDirectory(prefix='m2-worker-',dir=self.store.path.parent) as temp:
            p=Path(temp);(p/'input.json').write_text(json.dumps(inp,allow_nan=False),encoding='utf8')
            with (p/'stdout.log').open('wb') as out,(p/'stderr.log').open('wb') as err:
                proc=subprocess.Popen([sys.executable,'-m','optimization.runtime.worker_v2','--input',str(p/'input.json'),'--output',str(p/'output.json'),'--incumbent',str(p/'incumbent.json')],cwd=self.repo,stdout=out,stderr=err)
                why=None
                try:
                    while proc.poll() is None:
                        if self.store.job(session,job)['status']!='RUNNING':why='JOB_CANCELLED';break
                        if monotonic()>=deadline:why='BUDGET_EXHAUSTED';break
                        sleep(.05)
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        try:proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=2)
                if why and (why=='JOB_CANCELLED' or not (p/'incumbent.json').is_file()):
                    self.store.fail(session,job,why,'isolated worker stopped; no witness was certified before the reserve',expected_lease=lease)
                    return self.store.job(session,job)
            chosen=p/'incumbent.json' if why=='BUDGET_EXHAUSTED' else p/'output.json'
            if (not why and proc.returncode!=0) or not chosen.is_file():
                self.store.fail(session,job,'WORKER_FAILED',(p/'stderr.log').read_text(encoding='utf8')[-4000:],expected_lease=lease);return self.store.job(session,job)
            envelope=decode(chosen.read_bytes())
            require(isinstance(envelope,dict) and set(envelope)=={'schema_version','binding','result','validation','domain','telemetry'},'WORKER_OUTPUT_INVALID','worker.output','typed complete worker envelope required')
            require(envelope['schema_version']==WORKER_VERSION and envelope['binding']==inp['binding'],'WORKER_BINDING','worker.binding','output basis/lease/build differs')
            for name in ('result','validation','domain','telemetry'):require(isinstance(envelope[name],dict),'WORKER_OUTPUT_INVALID','worker.output.'+name,'object required')
            result=envelope['result'];domain=envelope['domain']
            require(result.get('schema_version')=='task02-m2-runtime-witness/2' and result.get('solver_version')=='task02-m2-runtime-column-compute/2','WORKER_OUTPUT_INVALID','worker.output.result.schema_version','known witness version required')
            require(result.get('profile')==record['request']['profile'] and result.get('source_hashes')==head['root']['source_hashes'],'WORKER_BINDING','worker.output.result','profile/source differs')
            require(isinstance(result.get('status'),str) and result['status'] in ('FEASIBLE','PARTIAL','RETURN_ONLY','NO_SERVICE','SEARCH_LIMIT','TIME_LIMIT'),'WORKER_OUTPUT_INVALID','worker.output.result.status','known business/supplemental status required')
            # Re-read raw source in the trusted parent; worker's valid=true is
            # not the authority. No transaction is held during this check.
            with self.source(head['root']) as (state,graph):
                if result['status'] in ('FEASIBLE','PARTIAL','RETURN_ONLY'):
                    validation=validate_runtime_result(state,graph,domain,result,load_profile_config(self.repo/'configs/member1_profiles_step3.json'),anchor=inp['anchor'],snapshot=self.snapshot,overlay=inp['overlay'])
                    require(validation['valid'] is True,'WITNESS_INVALID','validation',str(validation['diagnostics']))
                else:
                    require(all(result.get(k)==[] for k in ('vehicle_routes','served_orders','unserved_orders')) and result.get('coverage_evaluated') is False,'WORKER_OUTPUT_INVALID','worker.output.result','no witness cannot carry plan or order absence claims')
                    validation={'validator_version':'task02-m2-runtime-raw-witness-validator/2','valid':None,'validation_status':'NOT_RUN','diagnostics':[]}
                    if result['status']=='NO_SERVICE':
                        from .no_service import validate_certificate
                        validation['no_service_proof']=validate_certificate(state,inp['anchor'],result.get('no_service_certificate'))
            verify(self.repo,self.build,self.store.build)
            require(monotonic()-t<budget,'BUDGET_EXHAUSTED','budget_seconds','parent validation exceeded the job budget; physical head is unchanged')
            result={**result,'domain':domain,'validation_anchor':inp['anchor'],'activation_anchor':inp['anchor'] or initial_observation(DecisionState.from_dict(inp['initial'])),'overlay':inp['overlay'],
              'telemetry':{**envelope['telemetry'],'parent_total_seconds':monotonic()-t,'budget_seconds':budget,'validation_commit_reserve_seconds':reserve,'retained_worker_incumbent':why=='BUDGET_EXHAUSTED'}}
            return self.store._publish(session,job,lease,result,validation)

    def _replay(self,record,job,target):
        require(record['active_job']==job['id'],'PLAN_NOT_ACTIVE','job_id','observed replay requires current activated job')
        state=DecisionState.from_dict(record['root']['initial']);origin=state.decision_epoch
        require(instant(target,'target_time')>=instant(record['head']['current_time'],'head.current_time'),'TIME_REWIND','target_time','observed time cannot go backwards')
        for e in record['head']['pending_events']:
            require(instant(target,'target_time')<=instant(e['timestamp'],'event.timestamp'),'EVENT_TRANSITION_REQUIRED','target_time','cannot cross an unapplied event')
        if target==record['head']['current_time']:return record['head']
        with self.source(record['root']) as (state,graph):
            result=job['result'];domain=result['domain'];config=load_profile_config(self.repo/'configs/member1_profiles_step3.json')
            if result['schema_version']=='task02-m2-runtime-witness/2':
                check=self._check_replay_witness(state,graph,result,config)
                require(check['valid'] is True,'WITNESS_INVALID','result',str(check['diagnostics']))
                model=load_source_model(self.snapshot,state) if result['overlay'] else None
                prior=record['head'].get('observation')
                if prior and 'delivery_day' in prior:
                    barrier=self._execution_view(record,result).get('delivery_barrier')
                    require(barrier is None or instant(target,'target')<=instant(barrier,'barrier'),'DELIVERY_CONFIRMATION_REQUIRED','target_time','cannot pass a delivery awaiting driver confirmation')
                confirmations=prior.get('delivery_day',{}).get('confirmations') if prior else None
                observed=replay_actions(state,graph,result['activation_anchor'],result['vehicle_routes'],target,job['id'],model=model,overlay=result['overlay'],previous_history=prior['execution_history'] if prior else (),confirmations=confirmations,dispatch_frame=prior)
                rawcheck=validate_replay(state,graph,result['activation_anchor'],result['vehicle_routes'],target,job['id'],observed,model=model,overlay=result['overlay'],confirmations=confirmations,dispatch_frame=prior)
                require(rawcheck['valid'] is True,'OBSERVATION_INVALID','observation',str(rawcheck['diagnostics']))
                return {**record['head'],'current_time':target,'orders':observed['orders'],'observation':observed,'observation_validation':rawcheck,'pending_events':observed['pending_events']}
            check=validate_initial_result(state,graph,domain,result,config)
            require(check['valid'] is True,'WITNESS_INVALID','result',str(check['diagnostics']))
            plan=legacy_projection(state,result,graph,job['id'])
            # This is an internal shape conversion, not a static solver run.
            plan['solver_version']=result['solver_version'];plan['profile']={'name':result['profile']}
            accepted=make_acceptance(state,plan,check,job['id'],record['root']['source_hashes'])
            accepted['selected_profile']=result['profile']
            accepted['acceptance_sha256']=None;accepted['acceptance_sha256']=canonical_sha256(accepted)
            observed=replay_motion(state,plan,accepted,graph,target)
            rawcheck=validate_motion_state(state,plan,accepted,graph,observed)
            require(rawcheck['valid'] is True,'OBSERVATION_INVALID','observation',str(rawcheck['diagnostics']))
            return {**record['head'],'current_time':target,'orders':observed['orders'],'observation':observed,'observation_validation':rawcheck}

    def accept(self,c):
        payload={'operation':'accept','job_id':c['job_id'],'basis':c['basis']}
        prior=self.store.receipt(c['session_id'],c['command_id'],payload)
        if prior:return prior
        record=self.store.head(c['session_id']);job=self.store.job(c['session_id'],c['job_id'])
        require(record['basis']==c['basis'],'STALE_HEAD','basis','activation input differs from current authority')
        require(job['status']=='COMPLETED' and job['result'] is not None,'WITNESS_REQUIRED','job_id','completed result required')
        # Acceptance revalidates source/build now, not just the earlier worker.
        with self.source(record['root']) as (state,graph):
            result=job['result'];check=validate_runtime_result(state,graph,result['domain'],result,load_profile_config(self.repo/'configs/member1_profiles_step3.json'),anchor=result.get('validation_anchor'),snapshot=self.snapshot,overlay=result.get('overlay'))
            require(check['valid'] is True,'WITNESS_INVALID','validation',str(check['diagnostics']))
        verify(self.repo,self.build,self.store.build)
        return self.store._accept(c['session_id'],c['command_id'],c['job_id'],c['basis'])

    def execution_view(self,session):
        record=self.store.head(session)
        result=self.store.job(session,record['active_job'])['result'] if record['active_job'] else None
        return self._execution_view(record,result)

    def _execution_view(self,record,result):
        head=record['head'];initial=DecisionState.from_dict(record['root']['initial'])
        actual=sorted(o['order_id'] for o in head['orders'] if o['status']=='DELIVERED');planned=[];unserved=[]
        if record['active_job']:
            planned=sorted(set(result['served_orders'])-set(actual));unserved=copy(result['unserved_orders'])
        else:unserved=[{'order_id':o['order_id'],'reason':'NO_ACCEPTED_PLAN'} for o in head['orders'] if o['order_id'] not in actual]
        covered=set(actual+planned+[x['order_id'] for x in unserved])
        unserved.extend({'order_id':o['order_id'],'reason':'QUEUED_NEXT_DAY'} for o in head['orders'] if o['order_id'] not in covered and o['order_id'] in (head.get('observation') or {}).get('delivery_day',{}).get('queue',{}))
        universe=sorted(o['order_id'] for o in head['orders'])
        require(not set(actual)&set(planned) and set(actual+planned+[x['order_id'] for x in unserved])==set(universe),'COVERAGE','execution_view','prefix/suffix/unserved accounting differs')
        observation=head['observation']
        vehicles=copy(observation['vehicles']) if observation else [{'vehicle_id':v.vehicle_id,'availability':v.availability,'current_load_kg':v.current_load_kg,'onboard_order_ids':list(v.onboard_order_ids),'capacity_kg':v.capacity_kg,'remaining_range_m':v.remaining_range_m,'position':{'kind':'AT_NODE','node_id':v.current_position_node_id,'coordinates':list(v.current_position_coordinates)}} for v in initial.vehicles]
        for v in vehicles:
            if 'progress_exact' in v['position']:v['position']['progress_exact']=wire_exact(Fraction(v['position']['progress_exact']))
        suffix_metrics=None;whole_metrics=None;trajectory=None
        if record['active_job']:
            anchor=result.get('activation_anchor');suffix_metrics={}
            if anchor:
                current=observation['execution_metrics'] if observation else anchor['execution_metrics']
                mapping={'total_distance_m':('distance_m',1),'total_travel_time_s':('travel_time_us',1000000),'total_exposure':('relative_exposure_proxy',1),'total_cost_vnd':('cost_vnd',1)}
                for k,(field,scale) in mapping.items():suffix_metrics[k]=max(0,result['metrics'][k]-(current[field]-anchor['execution_metrics'][field])/scale)
                whole_metrics={k:current[field]/scale+suffix_metrics[k] for k,(field,scale) in mapping.items()}
            trajectory={'job_id':record['active_job'],'profile':result['profile'],'forecast':True,'domain_sha256':result['domain_sha256'],'vehicle_routes':wire_exact(result['vehicle_routes'])}
        value={'schema_version':'task02-m2-execution-view/2','basis':record['basis'],'execution_mode':'SIMULATED_REPLAY','real_world_observation':False,
                'current_time':head['current_time'],'order_ids':universe,'delivered_prefix':actual,'planned_served_suffix':planned,'unserved':unserved,
                'metric_scope':'OBSERVED_PREFIX_ONLY','observed_metrics':observation['execution_metrics'] if observation else None,
                'vehicles':wire_exact(vehicles),
                'active_job_id':record['active_job'],'pending_event_ids':[e['event_id'] for e in head['pending_events']],
                'planned_suffix_metrics':suffix_metrics,'projected_whole_metrics':whole_metrics,'accepted_trajectory':trajectory}
        if observation and 'delivery_day' in observation:
            from .delivery_day import complete
            value['delivery_day']={**copy(observation['delivery_day']),'before_event':head.get('before_event',observation['delivery_day']['before_event']),'complete':complete(observation),'pending_events':copy(observation['pending_events'])}
            stops=[]
            if trajectory:
                for route in trajectory['vehicle_routes']:
                    for a in route['actions']:
                        if a['kind']=='SERVICE' and a['order_id'] not in actual:
                            stops.append((instant(initial.decision_epoch,'epoch')+__import__('datetime').timedelta(microseconds=int(a['end_us']))).isoformat())
                            break
            value['delivery_barrier']=min(stops) if stops else None
        from .contracts import validate_view
        issues=validate_view(value);require(not issues,'EXECUTION_VIEW_INVALID','execution_view',str(issues));return value

    def apply_event(self,c):
        payload={'operation':'apply_event','basis':c['basis'],'event_id':c['event_id']};prior=self.store.receipt(c['session_id'],c['command_id'],payload)
        if prior:return prior
        record=self.store.head(c['session_id']);require(record['basis']==c['basis'],'STALE_HEAD','basis','event basis differs')
        before=record['head']['observation'];require(before is not None,'ACCEPTED_REPLAY_REQUIRED','head.observation','event requires own accepted replay first')
        with self.source(record['root']) as (state,graph):
            after,overlay,proof=apply_candidate(state,graph,self.snapshot,before,c['event_id'])
            check=validate_event(state,graph,self.snapshot,before,after,proof,overlay)
        updated={**record['head'],'orders':after['orders'],'observation':after,'pending_events':after['pending_events'],
                 'overlay_sha256':overlay['content_sha256'] if overlay else record['head'].get('overlay_sha256'),
                 'overlay':overlay or record['head'].get('overlay'),'event_validation':check}
        if 'delivery_day' in after:updated['before_event']=self.execution_view(c['session_id'])['projected_whole_metrics']
        verify(self.repo,self.build,self.store.build)
        return self.store._event(c['session_id'],c['command_id'],c['basis'],c['event_id'],updated,proof)

    def dispatch(self,c):
        from .delivery_day import mutate
        from optimization.models.member1_dynamic_state import sha as physical_sha
        payload={k:c[k] for k in ('operation','basis','action','data')}
        prior=self.store.receipt(c['session_id'],c['command_id'],payload)
        if prior:return prior
        record=self.store.head(c['session_id'])
        require(record['basis']==c['basis'],'STALE_HEAD','basis','dispatch basis differs')
        state=DecisionState.from_dict(record['root']['initial'])
        before=record['head']['observation'] or initial_observation(state)
        if c['action']=='wait_recovery':
            require(record['active_job'] is None,'PAUSE_PLAN_REQUIRED','action','finish or suspend active routes before skipping to recovery')
        after=mutate(state,before,c['action'],c['data'])
        if c['action'] in ('add_order','unavailable') and record['active_job']:
            after['delivery_day']['before_event']=self.execution_view(c['session_id'])['projected_whole_metrics']
        after.pop('content_sha256',None);after['content_sha256']=physical_sha(after)
        updated={**record['head'],'current_time':after['current_time'],'orders':after['orders'],'observation':after,'pending_events':after['pending_events']}
        if c['action'] in ('add_order','unavailable','next_day'):updated['before_event']=after['delivery_day']['before_event']
        keep=c['action'] in ('deliver','customer','add_depot') or c['action']=='add_order' and c['data'].get('order_id') in after['delivery_day']['queue']
        return self.store.dispatch(c['session_id'],c['command_id'],c['basis'],c['action'],c['data'],updated,keep_active=keep)

    def validate_session(self,session):
        """Administrative independent bound validation of every committed head.

        Store journal/root are the trusted binding, not a caller-provided hash.
        No solve, search, apply-candidate or replay producer is called here.
        """
        record=self.store.head(session);events=self.store.journal(session);current=None;active=None;count=0
        with self.source(record['root']) as (state,graph):
            config=load_profile_config(self.repo/'configs/member1_profiles_step3.json')
            for entry in events:
                kind,b=entry['kind'],entry['body']
                if kind=='BOOTSTRAP':
                    require(b['root']==record['root'] and b['root']['initial']==state.to_dict(),'ROOT_BINDING','journal.BOOTSTRAP','pinned root differs')
                    require(b['head']['orders']==initial_observation(state)['orders'] and b['head']['observation'] is None,'ROOT_BINDING','journal.BOOTSTRAP.head','initial physical frame differs');current=b['head']
                elif kind=='ACTIVATE':
                    job=self.store.job(session,b['job_id']);result=job['result']
                    require(b['head']==current and result.get('validation_anchor')==current['observation'],'ACTIVATION_BINDING','journal.ACTIVATE','accepted result belongs to a different physical head')
                    expected=current['observation'] or initial_observation(state)
                    require(result.get('activation_anchor')==expected,'ACTIVATION_BINDING','result.activation_anchor','accepted prefix differs')
                    check=validate_runtime_result(state,graph,result['domain'],result,config,anchor=result['validation_anchor'],snapshot=self.snapshot,overlay=result['overlay'])
                    require(check['valid'] is True,'WITNESS_INVALID','journal.ACTIVATE',str(check['diagnostics']));active=job;count+=1
                elif kind=='ADVANCE':
                    require(active is not None,'ACCEPTED_PLAN_REQUIRED','journal.ADVANCE','observation without accepted witness')
                    result=active['result'];observed=b['head']['observation'];model=load_source_model(self.snapshot,state) if result['overlay'] else None
                    check=validate_replay(state,graph,result['activation_anchor'],result['vehicle_routes'],b['head']['current_time'],active['id'],observed,model=model,overlay=result['overlay'],confirmations=current['observation'].get('delivery_day',{}).get('confirmations') if current['observation'] else None,dispatch_frame=current['observation'])
                    require(check['valid'] is True,'OBSERVATION_INVALID','journal.ADVANCE',str(check['diagnostics']))
                    require(b['head']['orders']==observed['orders'] and b['head']['pending_events']==observed['pending_events'],'OBSERVATION_BINDING','journal.ADVANCE.head','head/observation differs');current=b['head'];count+=1
                elif kind=='APPLY_EVENT':
                    require(current is not None and current['observation'] is not None,'EVENT_BINDING','journal.APPLY_EVENT','event before observed pre-plan')
                    validate_event(state,graph,self.snapshot,current['observation'],b['head']['observation'],b['proof'],b['head'].get('overlay'))
                    require(b['head']['orders']==b['head']['observation']['orders'] and b['head']['pending_events']==b['head']['observation']['pending_events'],'EVENT_BINDING','journal.APPLY_EVENT.head','event summary differs')
                    current=b['head'];active=None;count+=1
                elif kind=='DISPATCH':
                    from .delivery_day import mutate
                    from optimization.models.member1_dynamic_state import sha as physical_sha
                    before=current['observation'] or initial_observation(state)
                    expected=mutate(state,before,b['action'],b['data'])
                    if b['action'] in ('add_order','unavailable') and active:
                        expected['delivery_day']['before_event']=b['head']['observation']['delivery_day']['before_event']
                    expected.pop('content_sha256',None);expected['content_sha256']=physical_sha(expected)
                    require(b['head']['observation']==expected and b['head']['orders']==expected['orders'],'DISPATCH_BINDING','journal.DISPATCH','operational transition differs')
                    current=b['head'];active=active if b['active_job'] else None;count+=1
            require(current==record['head'],'JOURNAL_CORRUPT','head','current bound head differs')
        return {'validator_version':'task02-m2-bound-session-validator/2','valid':True,'checked_physical_mutations':count,'historical_execution_builds_preserved':True,'current_checker_build_sha256':self.build['build_sha256'],'scope':'TRUSTED_LOG_RAW_PREFIX_EVENT_SUFFIX; SIMULATED_REPLAY; NOT_OPTIMALITY'}

    def execute(self,value):
        try:
            c=command(value);op=c['operation'];session=c.get('session_id');key=c['command_id']
            verify(self.repo,self.build,self.store.build)
            if op=='capabilities':result=self.capabilities()
            elif op=='bootstrap':result=self.bootstrap(c)
            elif op in ('resolve','get_head'):result=self.execution_view(session)
            elif op=='submit':result=self.store.submit(session,key,c['basis'],c.get('profile','BALANCED'),c.get('budget_seconds',30))
            elif op=='compute':result=self.compute(c)
            elif op=='get_job':result=self.store.job(session,c['job_id'])
            elif op=='cancel':result=self.store.cancel(session,key,c['job_id'])
            elif op=='recover':result=self.store.recover(session)
            elif op=='accept':result=self.accept(c)
            elif op=='apply_event':result=self.apply_event(c)
            elif op=='dispatch':result=self.dispatch(c)
            elif op=='advance':
                payload={'operation':'advance','basis':c['basis'],'target_time':c['target_time']}
                prior=self.store.receipt(session,key,payload)
                if prior:return response(key,'OK',prior)
                record=self.store.head(session);require(record['active_job'] is not None,'ACCEPTED_PLAN_REQUIRED','active_job_id','no observed movement without an activated witness')
                # Reject stale input before expensive validation, then CAS again
                # inside the short commit transaction after source work.
                require(record['basis']==c['basis'],'STALE_HEAD','basis','stale replay request')
                verified=self._replay(record,self.store.job(session,record['active_job']),c['target_time'])
                result=self.store._advance(session,key,c['basis'],c['target_time'],verified)
            else:raise RuntimeError('RUNTIME_EVENT_ACTIVATION_NOT_CERTIFIED','operation','generic post-event authority/replay is not implemented; historical Step5/6 receipts cannot substitute')
            return response(key,'OK',result)
        except (RuntimeError,DynamicError,StateContractError,MotionContractError,MotionReplayError,RoadDataError,S1SidecarError,OSError) as e:
            return response(value.get('command_id') if isinstance(value,dict) else None,'FAIL',diagnostics=[{'severity':'ERROR','code':getattr(e,'code','RUNTIME_SOURCE_ERROR'),'path':getattr(e,'path','$'),'message':str(e)}])
