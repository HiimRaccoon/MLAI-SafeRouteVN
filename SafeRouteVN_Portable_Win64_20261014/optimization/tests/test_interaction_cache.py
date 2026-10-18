"""Warm byte certificates must never hide changed authority or shared objects."""
import json,sqlite3
from unittest.mock import patch
import pytest
from optimization.runtime import store as module
from optimization.runtime.protocol import RuntimeError,canonical
from optimization.runtime.store import Store,persisted,encode_payload
from optimization.tests.test_runtime_performance import seeded


def test_repeated_decode_returns_independent_objects_and_survives_payload_eviction():
    raw=encode_payload(canonical({'routes':list(range(25000))}))
    first=persisted(raw,'jobs.result');first['routes'][0]=-5
    with patch.object(module,'decode',wraps=module.decode) as strict:
        assert persisted(raw,'jobs.result')['routes'][0]==0
        with module._PAYLOAD_LOCK:
            module._STRICT_PAYLOADS.clear();module._STRICT_PAYLOAD_BYTES=0
        assert persisted(raw,'jobs.result')['routes'][0]==0
        assert strict.call_count==0


@pytest.mark.parametrize('raw',[
    '{"x":1,"x":2}', '{"x":NaN}', '{"x":9007199254740992}', '{"x":',
    '{"x":'+('['*100)+'0'+(']'*100)+'}',
])
def test_strict_evidence_never_accepts_changed_malformed_bytes(raw):
    persisted('{"x":1}','journal.body')
    with pytest.raises(RuntimeError):persisted(raw,'journal.body')


def test_request_shape_is_checked_even_when_same_bytes_were_decoded_in_another_role():
    raw='{"hello":"world"}'
    persisted(raw,'journal.body')
    with pytest.raises(RuntimeError,match='typed persisted submit'):persisted(raw,'jobs.request')


@pytest.mark.parametrize('field,value',[
    ('parent','1'*64),('digest','2'*64),('session','other-session'),('kind','DISPATCH'),
    ('body','{"root":{},"head":{},"version":"1","generation":"0"}'),
])
def test_warm_journal_certificates_reject_changed_chain_fields(tmp_path,field,value):
    store,_=seeded(tmp_path);assert store.verify();assert store.verify()
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE journal SET '+field+'=? WHERE seq=1',(value,))
    with pytest.raises(RuntimeError):store.verify()


@pytest.mark.parametrize('table,field,value',[
    ('sessions','head','{"context_version":"changed"}'),
    ('sessions','root','{"source_hashes":{"changed":"yes"}}'),
    ('jobs','status','FAILED'),('jobs','result','{"status":"ALTERED"}'),
    ('receipts','body','{"status":"ALTERED"}'),('outbox','ack',1),
])
def test_warm_verification_still_checks_all_materialized_rows(tmp_path,table,field,value):
    store,_=seeded(tmp_path);assert store.verify()
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE '+table+' SET '+field+'=? WHERE rowid=(SELECT MIN(rowid) FROM '+table+')',(value,))
    with pytest.raises(RuntimeError):store.verify()


def test_warm_verification_authenticates_a_legitimate_tail_from_another_connection(tmp_path):
    store,_=seeded(tmp_path);assert store.verify()
    other=Store(store.path,store.build)
    other.submit('session','another-submit',other.head('session')['basis'],'SAFER',60)
    assert store.verify()
    with sqlite3.connect(store.path) as db:
        db.execute('DELETE FROM journal WHERE seq=(SELECT MAX(seq) FROM journal)')
    with pytest.raises(RuntimeError):store.verify()
