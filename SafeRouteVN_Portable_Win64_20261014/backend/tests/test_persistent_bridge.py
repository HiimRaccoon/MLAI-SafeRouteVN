import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from backend.services.persistent_bridge import PersistentBridge


@pytest.fixture
def transport(tmp_path):
    script=tmp_path/'echo.py'
    script.write_text('''import json,os,sys,time
for line in sys.stdin:
    request=json.loads(line)
    if request['operation']=='sleep':time.sleep(10)
    if request['operation']=='exit':sys.exit(1)
    print(json.dumps({'pid':os.getpid(),'fields':request['fields']}),flush=True)
''')
    bridge=PersistentBridge()
    def call(operation='echo',fields=None,timeout=3,binding='a'):
        return json.loads(bridge.request([sys.executable,'-B',str(script)],operation,fields or {},
                          cwd=tmp_path,env=dict(os.environ),timeout=timeout,binding=binding).stdout)
    yield bridge,call
    bridge.close()


def test_reuses_process_serializes_concurrent_calls_and_restarts_on_binding(transport):
    bridge,call=transport
    first=call()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda i:call(fields={'i':i}),range(6)))
    assert all(r['pid']==first['pid'] for r in results)
    assert [r['fields']['i'] for r in results]==list(range(6))
    assert call(binding='b')['pid']!=first['pid']


def test_timeout_stops_process_before_return_without_replaying(transport):
    bridge,call=transport
    call();process=bridge.process
    with pytest.raises(subprocess.TimeoutExpired):call('sleep',timeout=.1)
    assert process.poll() is not None and bridge.process is None
    assert call()['pid']!=process.pid


def test_broken_pipe_resets_transport_for_next_explicit_request(transport):
    bridge,call=transport
    with pytest.raises(ValueError,match='stopped during request'):call('exit')
    assert bridge.process is None
    assert call()['pid']>0
