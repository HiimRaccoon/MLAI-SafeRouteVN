"""Native local HTTP-contract/worker smoke. Creates new simulated sessions only."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--local-root',required=True,type=Path)
    args=parser.parse_args()
    project=Path(__file__).resolve().parents[2];sys.path.insert(0,str(project))
    from fastapi.testclient import TestClient
    from backend.services.settings import Settings
    from backend.services.compute_worker import ComputeWorker
    from backend.api.main import create_app
    local=args.local_root.resolve(strict=True);private=local/'backend'
    output=local/'receipts'/('performance_'+uuid4().hex);output.mkdir()
    settings=Settings(project,local/'installation.json',private/'auth_tokens.json',private/'metadata.sqlite',
                      private/'worker_heartbeat.json',compute_budget_seconds=120,runtime_persistent_bridge=True)
    token=json.loads((private/'dev_access.json').read_bytes())['credentials'][0]['token']
    headers={'Authorization':'Bearer '+token}
    report={'build_sha256':settings.installation()['expected_build_sha256'],'scope':'LOCAL_SIMULATED_NATIVE_HTTP_CONTRACT',
            'load_seconds':{},'comparisons':[]}
    # The SDK/native children enforce the same offline policy as the launcher.
    os.environ.update(SAFEROUTE_OFFLINE_MODE='loopback-only',SAFEROUTE_OFFLINE_AUDIT_DIR=str(output/'network-audit'))
    app=create_app(settings)
    with TestClient(app) as client:
        def request(method,path,body=None):
            response=client.request(method,path,headers=headers,json=body)
            if response.status_code>=400:raise RuntimeError(str(response.status_code)+': '+response.text)
            return response.json()['data']
        async def run():
            worker=ComputeWorker(settings)
            await worker.startup()
            try:
                assert worker.status=='READY'
                for scenario in ('S0','S1','S2'):
                    started=time.perf_counter()
                    data=await asyncio.to_thread(request,'POST','/api/scenarios/'+scenario+'/load',{'request_id':'perf-load-'+uuid4().hex})
                    sid=data['session']['session_id']
                    await asyncio.to_thread(request,'GET',f'/api/sessions/{sid}/world')
                    report['load_seconds'][scenario]=round(time.perf_counter()-started,3)
                    if scenario=='S0':session=sid
                pid=app.state.gateway._bridge.process.pid
                await asyncio.to_thread(request,'GET','/api/runtime/capabilities')
                assert app.state.gateway._bridge.process.pid==pid
                report['persistent_process_reused']=True
                for mode in ('cold','warm'):
                    started=time.perf_counter()
                    receipt=await asyncio.to_thread(request,'POST',f'/api/sessions/{session}/profiles/compare',{'request_id':'perf-compare-'+uuid4().hex})
                    for _ in range(30):
                        await worker.run_once()
                        value=await asyncio.to_thread(request,'GET',receipt['links']['poll'])
                        if value['status']=='COMPLETED':break
                        await asyncio.sleep(.05)
                    assert value['status']=='COMPLETED',value
                    assert value['outcome']['comparison']['status']=='COMPARABLE',value
                    assert all(m['view']['job_status']=='COMPLETED' and m['view']['validation']['valid'] is True for m in value['jobs'])
                    report['comparisons'].append({'mode':mode,'seconds':round(time.perf_counter()-started,3),
                                                  'comparison':value})
                    print(json.dumps({'mode':mode,'seconds':report['comparisons'][-1]['seconds']}),flush=True)
                jid=value['jobs'][0]['job_id']
                basis=value['input_basis']
                accepted=await asyncio.to_thread(request,'POST',f'/api/sessions/{session}/jobs/{jid}/accept',
                    {'request_id':'perf-accept-'+uuid4().hex,'expected_revision':{k:basis[k] for k in ('head_version','generation')}})
                report['accept_status']=accepted['receipt']['status']
                assert report['accept_status']=='ACCEPTED'
                ready=await asyncio.to_thread(request,'GET','/ready')
                assert ready['ready'] is True
                report['ready']=ready
                report['status']='PASS'
            finally:await worker.shutdown()
        try:asyncio.run(run())
        finally:(output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'status':report['status'],'load_seconds':report['load_seconds'],'receipt':str(output/'result.json')}))


if __name__=='__main__':main()
