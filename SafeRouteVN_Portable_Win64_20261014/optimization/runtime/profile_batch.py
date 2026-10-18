"""One immutable physical domain, bounded parallel selectors, one authority writer.

The isolated worker has no Store. The parent validates every witness and
publishes with that job's original basis/lease; completed/cancelled jobs survive
partial failure and retries. External commands cannot supply a cached domain.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import monotonic, sleep
import json
import os
import subprocess
import sys
import tempfile

from .protocol import RuntimeError, require, decode, canonical
from .worker_v2 import compute, WORKER_VERSION, VERSION
from .validation import validate_runtime_result, RawValidationCache
from .build import verify
from .execution import initial_observation
from optimization.models.decision_state import DecisionState
from optimization.integration.member1_profiles import load_profile_config


def atomic_envelope(path, value):
    temporary=path.with_suffix('.pending')
    temporary.write_bytes(canonical(value));os.replace(temporary,path)


def worker_batch(payload, output):
    jobs=payload['batch_jobs'];started=monotonic()
    require(1<=len(jobs)<=3,'INVALID_DATA','batch_jobs','bounded profile batch required')
    # The coordinator binds every input to one basis before starting this child.
    domain=payload.get('cached_domain')
    if domain is None:domain=compute(jobs[0],domain_only=True)
    domain_seconds=monotonic()-started
    raw_cache=RawValidationCache()
    def solve(inp):
        jid=inp['binding']['job_id']
        target=output/jid
        def checkpoint(value):atomic_envelope(target.with_suffix('.incumbent'),value)
        remaining=inp['compute_seconds']-domain_seconds
        require(remaining>0,'BUDGET_EXHAUSTED','profile','domain exhausted batch budget')
        local={**inp,'compute_seconds':remaining,'cached_domain':domain,
               'cached_domain_origin_job_id':payload.get('cache_origin_job_id')}
        value=compute(local,checkpoint,_raw_cache=raw_cache)
        value['telemetry'].update(shared_domain=True,batch_size=len(jobs),
                                  batch_domain_seconds=domain_seconds,
                                  road_search_executed=payload.get('cached_domain') is None,
                                  cache_hit=payload.get('cached_domain') is not None)
        atomic_envelope(target.with_suffix('.json'),value)
    with ThreadPoolExecutor(max_workers=len(jobs),thread_name_prefix='profile') as pool:
        futures={pool.submit(solve,inp):inp['binding']['job_id'] for inp in jobs}
        for future in as_completed(futures):
            try:future.result()
            except Exception as error:
                atomic_envelope((output/futures[future]).with_suffix('.error'),
                                {'code':getattr(error,'code','WORKER_FAILED')})


def compute_profiles(runtime, session, job_ids):
    require(isinstance(job_ids,list) and 1<=len(job_ids)<=3 and len(set(job_ids))==len(job_ids),
            'INVALID_DATA','job_ids','unique bounded jobs required')
    runtime.store.verify()
    records=[runtime.store.job(session,jid) for jid in job_ids]
    head=runtime.store.head(session)
    require(len({r['request']['profile'] for r in records})==len(records),'INVALID_DATA','profiles','unique profiles required')
    require(all(r['basis']==records[0]['basis'] for r in records),'JOB_STALE','basis','shared basis required')
    pending=[r for r in records if r['status'] not in ('COMPLETED','FAILED')]
    if not pending:return records
    require(all(r['basis']==head['basis'] for r in pending),'JOB_STALE','basis','batch basis changed')
    require(all(r['status']=='QUEUED' for r in pending),'JOB_LIFECYCLE','batch','recover orphaned leases before retry')
    raw_cache=RawValidationCache()
    cached=runtime.store.cached_domain(session,head['basis'])
    if cached:
        with runtime.source(head['root']) as (state,graph):
            prior=cached['result']
            check=validate_runtime_result(state,graph,prior['domain'],prior,load_profile_config(runtime.repo/'configs/member1_profiles_step3.json'),
                                          anchor=head['head']['observation'],snapshot=runtime.snapshot,overlay=head['head'].get('overlay'),_raw_cache=raw_cache)
            require(check['valid'] is True,'CACHE_INVALID','domain','cached domain failed independent check')
    claims=[];failure=None
    try:
        leases=runtime.store._claim_batch(session,[r['id'] for r in pending])
        # Search starts after the atomic history check/claim, alongside the
        # leases, rather than losing time to three sequential history scans.
        started=monotonic()
        for record in pending:
            lease=leases[record['id']];budget=record['request']['budget_seconds']
            reserve=min(18.,budget*.45)
            inp={'initial':head['root']['initial'],'source_hashes':head['root']['source_hashes'],
                 'snapshot_root':str(runtime.snapshot),'head_time':head['head']['current_time'],
                 'profile':record['request']['profile'],'compute_seconds':max(.001,budget-reserve),
                 'anchor':head['head']['observation'],'overlay':head['head'].get('overlay'),
                 'binding':{'job_id':record['id'],'basis':record['basis'],'lease':lease,'build_sha256':runtime.store.build}}
            claims.append((record,inp,reserve))
        with tempfile.TemporaryDirectory(prefix='m2-profile-batch-',dir=runtime.store.path.parent) as folder:
            path=Path(folder);out=path/'results';out.mkdir()
            payload={'batch_jobs':[inp for _,inp,_ in claims]}
            if cached:payload.update(cached_domain=cached['result']['domain'],cache_origin_job_id=cached['job_id'])
            (path/'input.json').write_text(json.dumps(payload,allow_nan=False),encoding='utf8')
            deadline=started+min(inp['compute_seconds'] for _,inp,_ in claims)
            with (path/'stdout.log').open('wb') as stdout,(path/'stderr.log').open('wb') as stderr:
                proc=subprocess.Popen([sys.executable,'-m','optimization.runtime.worker_v2','--input',str(path/'input.json'),'--output',str(out)],cwd=runtime.repo,stdout=stdout,stderr=stderr)
                timed_out=False
                try:
                    while proc.poll() is None:
                        active=[runtime.store.job(session,r['id'])['status']=='RUNNING' for r,_,_ in claims]
                        if not any(active):break
                        if monotonic()>=deadline:timed_out=True;break
                        sleep(.1)
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        try:proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=2)
            domain_hash=None
            for record,inp,reserve in claims:
                jid=record['id'];lease=inp['binding']['lease'];current=runtime.store.job(session,jid)
                if current['status']!='RUNNING' or current['lease']!=lease:continue
                output=(out/jid).with_suffix('.json')
                retained=not output.is_file()
                if retained:output=(out/jid).with_suffix('.incumbent')
                if not output.is_file():
                    runtime.store.fail(session,jid,'BUDGET_EXHAUSTED' if timed_out else 'WORKER_FAILED',
                                       'batch worker produced no certified witness',expected_lease=lease)
                    continue
                envelope=decode(output.read_bytes())
                require(set(envelope)=={'schema_version','binding','result','validation','domain','telemetry'} and envelope['schema_version']==WORKER_VERSION
                        and envelope['binding']==inp['binding'],'WORKER_BINDING','batch','worker binding differs')
                result=envelope['result'];domain=envelope['domain']
                require(result.get('schema_version')=='task02-m2-runtime-witness/2' and result.get('solver_version')==VERSION
                        and result.get('profile')==record['request']['profile'] and result.get('source_hashes')==head['root']['source_hashes'],
                        'WORKER_BINDING','result','batch result differs from input')
                require(domain_hash is None or domain_hash==domain['content_sha256'],'WORKER_BINDING','domain','batch domains differ')
                domain_hash=domain['content_sha256']
                with runtime.source(head['root']) as (state,graph):
                    if result.get('status') in ('FEASIBLE','PARTIAL','RETURN_ONLY'):
                        validation=validate_runtime_result(state,graph,domain,result,load_profile_config(runtime.repo/'configs/member1_profiles_step3.json'),
                                                           anchor=inp['anchor'],snapshot=runtime.snapshot,overlay=inp['overlay'],_raw_cache=raw_cache)
                        require(validation['valid'] is True,'WITNESS_INVALID','validation','batch witness failed independent check')
                    else:
                        require(result.get('status') in ('NO_SERVICE','SEARCH_LIMIT','TIME_LIMIT') and all(result.get(k)==[] for k in ('vehicle_routes','served_orders','unserved_orders'))
                                and result.get('coverage_evaluated') is False,'WORKER_OUTPUT_INVALID','result','invalid no-witness result')
                        validation={'validator_version':'task02-m2-runtime-raw-witness-validator/2','valid':None,'validation_status':'NOT_RUN','diagnostics':[]}
                        if result['status']=='NO_SERVICE':
                            from .no_service import validate_certificate
                            validation['no_service_proof']=validate_certificate(state,inp['anchor'],result.get('no_service_certificate'))
                verify(runtime.repo,runtime.build,runtime.store.build)
                if monotonic()-started>=record['request']['budget_seconds']:
                    runtime.store.fail(session,jid,'BUDGET_EXHAUSTED','batch validation exceeded budget',expected_lease=lease)
                    continue
                result={**result,'domain':domain,'validation_anchor':inp['anchor'],
                        'activation_anchor':inp['anchor'] or initial_observation(DecisionState.from_dict(inp['initial'])),'overlay':inp['overlay'],
                        'telemetry':{**envelope['telemetry'],'parent_total_seconds':monotonic()-started,'budget_seconds':record['request']['budget_seconds'],
                                     'validation_commit_reserve_seconds':reserve,'retained_worker_incumbent':retained}}
                try:runtime.store._publish(session,jid,lease,result,validation)
                except RuntimeError as error:
                    if error.code!='WORKER_FENCED':raise
        return [runtime.store.job(session,jid) for jid in job_ids]
    except BaseException as error:
        failure=error
        raise
    finally:
        # BUSY/corruption leave leases for verified recovery. Never overwrite a
        # successful publish or a cancellation committed by another request.
        if failure is not None and (not isinstance(failure,RuntimeError) or not failure.code.startswith(('RUNTIME_BUSY','STORE','JOURNAL','RECEIPT','OUTBOX','BUILD'))):
            for record,inp,_ in claims:
                runtime.store.fail(session,record['id'],getattr(failure,'code','COMPUTE_ABORTED'),'batch interrupted',expected_lease=inp['binding']['lease'])
