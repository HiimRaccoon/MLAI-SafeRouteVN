"""Correctness gates for the optimized authority and runtime boundaries."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from optimization.runtime.store import Store, committed_hash
from optimization.runtime.protocol import RuntimeError, VERSION, sha
from optimization.runtime.facade import Runtime


def seeded(tmp_path):
    store=Store(tmp_path/'authority.sqlite','a'*64)
    store._bootstrap('session',{'source_hashes':{}},{'context_version':'context'},'boot')
    basis=store.head('session')['basis']
    job=store.submit('session','submit',basis,'FASTEST',60)['job_id']
    lease=store.claim('session',job)
    store._publish('session',job,lease,{'status':'FEASIBLE','large':[1]*1000},{'valid':True})
    return store,job


def test_sqlite_busy_survives_sdk_serialization_and_reads_do_not_reserve_writer(tmp_path):
    store,job=seeded(tmp_path)
    store.busy_timeout_seconds=0
    runtime=Runtime.__new__(Runtime)
    runtime.repo=None;runtime.build={};runtime.store=store
    runtime.capabilities=lambda:store.submit('session','busy',store.head('session')['basis'],'FASTEST',60)
    with sqlite3.connect(store.path,isolation_level=None) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        assert store.head('session')['basis']['session_id']=='session'
        with patch('optimization.runtime.facade.verify'):
            response=runtime.execute({'schema_version':VERSION,'operation':'capabilities','command_id':'test'})
        assert response['diagnostics'][0]['code']=='RUNTIME_BUSY'
        blocker.rollback()
    assert store.verify()


def test_fast_hash_preserves_legacy_json_and_rejects_corrupt_payload(tmp_path):
    store,job=seeded(tmp_path)
    assert store.verify()
    with sqlite3.connect(store.path) as db:
        raw=db.execute('SELECT result FROM jobs WHERE id=?',(job,)).fetchone()[0]
        db.execute('UPDATE jobs SET result=? WHERE id=?',(json.dumps(json.loads(raw),indent=2),job))
    assert store.verify()
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE jobs SET result=? WHERE id=?',('{"broken":',job))
    with pytest.raises(RuntimeError):store.verify()


def test_fast_hash_detects_valid_json_tampering(tmp_path):
    store,job=seeded(tmp_path)
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE jobs SET result=? WHERE id=?',('{"status":"FEASIBLE"}',job))
    with pytest.raises(RuntimeError,match='published result'):store.verify()


def test_non_sqlite_text_is_not_classified_as_busy():
    from optimization.runtime.store import sqlite_failure_code
    assert sqlite_failure_code(sqlite3.OperationalError('database is locked'))=='STORE_INVALID'


def test_compressed_payload_retains_canonical_digest_and_detects_corruption():
    from optimization.runtime.store import encode_payload,persisted
    from optimization.runtime.protocol import canonical
    value={'routes':list(range(20000))}
    raw=canonical(value);packed=encode_payload(raw)
    assert isinstance(packed,bytes) and len(packed)<len(raw)
    assert persisted(packed,'jobs.result')==value
    assert committed_hash(packed,sha(value),'jobs.result')
    with pytest.raises(RuntimeError):persisted(packed[:-10],'jobs.result')


def test_batch_builds_domain_once_and_selects_three_profiles_concurrently(tmp_path):
    import threading
    from optimization.runtime.profile_batch import worker_batch
    barrier=threading.Barrier(3,timeout=5)
    domain={'content_sha256':'d'*64,'columns':[]};builds=[];seen=[]
    def fake(inp,checkpoint=None,*,domain_only=False,_raw_cache=None):
        if domain_only:
            builds.append(inp['profile']);return domain
        assert inp['cached_domain'] is domain
        seen.append(inp['profile']);barrier.wait()
        return {'telemetry':{}}
    jobs=[{'profile':p,'compute_seconds':30,'binding':{'job_id':'job-'+p}} for p in ('FASTEST','BALANCED','SAFER')]
    with patch('optimization.runtime.profile_batch.compute',fake):worker_batch({'batch_jobs':jobs},tmp_path)
    assert len(builds)==1 and len(seen)==3
    assert len(list(tmp_path.glob('*.json')))==3 and not list(tmp_path.glob('*.error'))


def test_scalar_fast_paths_keep_strict_json_boundaries():
    from types import MappingProxyType
    from optimization.runtime.protocol import tree
    from optimization.models.member1_dynamic_state import finite_tree, DynamicError
    for validate,error in ((tree,RuntimeError),(finite_tree,DynamicError)):
        validate(MappingProxyType({'ok':[True,None,'text',12,1.2]}))
        cyclic=[];cyclic.append(cyclic)
        for bad in (float('nan'),float('inf'),cyclic,{1:'non-string-key'},object()):
            with pytest.raises(error):validate(bad)
    with pytest.raises(RuntimeError):tree(2**53)
    with pytest.raises(DynamicError):finite_tree(10**400)


def test_raw_cache_serializes_concurrent_checks_and_never_caches_failure():
    from concurrent.futures import ThreadPoolExecutor
    from optimization.runtime.validation import RawValidationCache
    from time import sleep
    cache=RawValidationCache();calls=[]
    def produce():
        calls.append(True);sleep(.01);return ['checked']
    with ThreadPoolExecutor(max_workers=3) as pool:
        values=list(pool.map(lambda _:cache.checked(('same-domain','same-anchor'),produce),range(3)))
    assert len(calls)==1 and values==[['checked']]*3
    assert cache.checked(('same-domain','new-anchor'),produce)==['checked']
    assert len(calls)==2
    def invalid():raise ValueError('raw edge mismatch')
    with pytest.raises(ValueError):cache.checked(('invalid',),invalid)
    assert ('invalid',) not in cache.rows


def queued_batch(tmp_path):
    store=Store(tmp_path/'batch.sqlite','a'*64)
    store._bootstrap('session',{'source_hashes':{}},{'context_version':'context'},'boot')
    basis=store.head('session')['basis']
    jobs=[store.submit('session',p,basis,p,35)['job_id'] for p in ('FASTEST','BALANCED','SAFER')]
    return store,jobs


def test_batch_claim_checks_history_once_and_starts_all_leases_after_check(tmp_path):
    store,jobs=queued_batch(tmp_path);clock=[100.]
    verify=store._verify
    def slow_verify(db):
        value=verify(db);clock[0]+=12;return value
    with patch.object(store,'_verify',side_effect=slow_verify) as checked, patch('optimization.runtime.store.time.time',side_effect=lambda:clock[0]):
        leases=store._claim_batch('session',jobs)
    assert checked.call_count==1 and len(set(leases.values()))==3
    assert all(store.job('session',j)['expires']==147 for j in jobs)
    assert all(store.job('session',j)['status']=='RUNNING' for j in jobs)
    assert store.verify()


def test_batch_claim_is_atomic_when_one_member_is_not_queued(tmp_path):
    store,jobs=queued_batch(tmp_path);store.claim('session',jobs[1])
    with pytest.raises(RuntimeError,match='only queued'):
        store._claim_batch('session',jobs)
    assert [store.job('session',j)['status'] for j in jobs]==['QUEUED','RUNNING','QUEUED']
    assert store.verify()


def test_batch_claim_still_quarantines_corrupt_history(tmp_path):
    store,jobs=queued_batch(tmp_path)
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE journal SET digest=? WHERE seq=1',('b'*64,))
    with pytest.raises(RuntimeError):store._claim_batch('session',jobs)
    assert all(store.job('session',j)['status']=='QUEUED' for j in jobs)
