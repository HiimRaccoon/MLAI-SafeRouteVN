"""Warm simulation must still reject changed bytes and physical inputs."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from optimization.runtime import store as store_module
from optimization.runtime.store import Store,committed_hash,encode_payload
from optimization.runtime.protocol import canonical,sha,RuntimeError
from optimization.runtime.facade import Runtime
from optimization.runtime.build import production_inventory


def test_verified_compressed_bytes_are_reused_but_changed_bytes_are_rejected():
    value={'large':list(range(30000))};packed=encode_payload(canonical(value));expected=sha(value)
    with patch.object(store_module,'payload_bytes',wraps=store_module.payload_bytes) as inflate:
        assert committed_hash(packed,expected,'jobs.result')
        first=inflate.call_count
        assert committed_hash(packed,expected,'jobs.result')
        assert inflate.call_count==first
        changed=encode_payload(canonical({'large':list(range(30001))}))
        assert not committed_hash(changed,expected,'jobs.result')
        with pytest.raises(RuntimeError):committed_hash(packed[:-1],expected,'jobs.result')


def test_cached_import_scan_still_checks_new_dependencies_and_changed_source(tmp_path):
    package=tmp_path/'optimization';package.mkdir()
    (tmp_path/'requirements-runtime.lock.txt').write_text('locked',encoding='utf8')
    module=package/'__init__.py';module.write_text('import optimization.tests.hidden\n',encoding='utf8')
    original=production_inventory(tmp_path)
    assert production_inventory(tmp_path)==original
    excluded=package/'tests';excluded.mkdir();(excluded/'hidden.py').write_text('',encoding='utf8')
    with pytest.raises(RuntimeError,match='local dependency absent'):production_inventory(tmp_path)
    module.write_text('import optimization.other\n',encoding='utf8')
    updated=production_inventory(tmp_path)
    assert original['optimization/__init__.py']!=updated['optimization/__init__.py']


def fixture():
    runtime=Runtime.__new__(Runtime);runtime.build={'build_sha256':'build'};runtime.snapshot='snapshot'
    state=SimpleNamespace(to_dict=lambda:{'orders':['O1']})
    graph=SimpleNamespace(source_hashes={'network':'source'})
    result={'domain':{'columns':[{'order':'O1'}]},'validation_anchor':{'time':'08:00'},'overlay':None}
    config={'profile':'FASTEST'}
    return runtime,state,graph,result,config


@pytest.mark.parametrize('field',['domain','anchor','overlay','source','config','state','build'])
def test_replay_cache_rechecks_every_changed_input(field):
    runtime,state,graph,result,config=fixture()
    with patch('optimization.runtime.facade.validate_runtime_result',return_value={'valid':True}) as validate:
        assert runtime._check_replay_witness(state,graph,result,config)['valid']
        assert runtime._check_replay_witness(state,graph,deepcopy(result),deepcopy(config))['valid']
        assert validate.call_count==1
        if field=='domain':result['domain']['columns'][0]['order']='O2'
        elif field=='anchor':result['validation_anchor']['time']='08:01'
        elif field=='overlay':result['overlay']={'rain':'active'}
        elif field=='source':graph.source_hashes['network']='changed'
        elif field=='config':config['profile']='SAFER'
        elif field=='state':state.to_dict=lambda:{'orders':['O2']}
        else:runtime.build['build_sha256']='changed'
        runtime._check_replay_witness(state,graph,result,config)
        assert validate.call_count==2


def test_replay_cache_does_not_remember_failed_checks_and_is_bounded():
    runtime,state,graph,result,config=fixture()
    with patch('optimization.runtime.facade.validate_runtime_result',return_value={'valid':False}) as validate:
        for _ in range(2):runtime._check_replay_witness(state,graph,result,config)
        assert validate.call_count==2 and not runtime._replay_checks
    with patch('optimization.runtime.facade.validate_runtime_result',return_value={'valid':True}):
        for i in range(5):
            config['revision']=i;runtime._check_replay_witness(state,graph,result,config)
        assert len(runtime._replay_checks)==2


def test_raw_source_guard_failure_discards_replay_checks():
    runtime,_,_,_,_=fixture();runtime._replay_checks={'previous':{'valid':True}}
    root={'initial':{},'source_hashes':{'network':'before'}}
    with patch('optimization.runtime.facade.DecisionState.from_dict',return_value=SimpleNamespace(
        routing_version='r',features_version='f',context_version='c')),\
        patch('optimization.runtime.facade._source_pins',return_value=({'network':'file'},{'network':'before'})),\
        patch('optimization.runtime.facade._require_no_sqlite_sidecars'),\
        patch('optimization.runtime.facade.Member1RoadGraph'),\
        patch('optimization.runtime.facade._sha',return_value='changed'):
        with pytest.raises(RuntimeError,match='raw bytes changed'):
            with runtime.source(root):pass
    assert runtime._replay_checks=={}


def test_notification_batch_verifies_one_snapshot_and_preserves_session_partition(tmp_path):
    store=Store(tmp_path/'outbox.sqlite','a'*64)
    for sid in ('first','second','third'):
        store._bootstrap(sid,{'source_hashes':{}},{'context_version':'context'},'boot')
    expected={sid:store.notifications(sid) for sid in ('first','second')}
    with patch.object(store,'_verify',wraps=store._verify) as verify:
        assert store.notifications_many(['first','second'])==expected
        assert verify.call_count==1
    first=expected['first'][0]['event_id']
    store.acknowledge('first','ack',first)
    assert first not in [v['event_id'] for v in store.notifications_many(['first'])['first']]
    with pytest.raises(RuntimeError,match='unknown authority session'):
        store.notifications_many(['first','missing'])
    with store.transaction() as db:
        db.execute('UPDATE outbox SET ack=1 WHERE ack=0')
    with pytest.raises(RuntimeError,match='acknowledgement differs'):
        store.notifications_many(['first','second'])
