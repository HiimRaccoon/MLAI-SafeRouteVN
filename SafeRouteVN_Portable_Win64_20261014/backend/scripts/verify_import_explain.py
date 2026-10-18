"""Native import -> optimize -> Qwen3 smoke in an isolated metadata/authority store.

Run from repository root with Python 3.12. Does not read production bearer tokens.
"""
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import httpx
from backend.api.main import create_app
from backend.services.settings import Settings
from backend.services.compute_worker import ComputeWorker


async def main():
    selected=json.loads((ROOT.parent/'.local/saferoute-active.json').read_bytes())
    installation=json.loads((Path(selected['local_root'])/'installation.json').read_bytes())
    output=ROOT/'outputs'/('import-explain-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True)
    installation['authority_store_parent']=str(output/'authority')
    (output/'installation.json').write_text(json.dumps(installation),encoding='utf8')
    token=uuid4().hex+uuid4().hex
    (output/'auth.json').write_text(json.dumps({'schema_version':'saferoute-m3-auth/1','tokens':[
        {'actor_id':'native-import-test','role':'dispatcher','token_sha256':hashlib.sha256(token.encode()).hexdigest()}]}),encoding='utf8')
    settings=Settings(ROOT,output/'installation.json',output/'auth.json',output/'metadata.sqlite',output/'heartbeat.json',
        compute_budget_seconds=30, runtime_timeout_seconds=180,runtime_bootstrap_timeout_seconds=180,
        runtime_persistent_bridge=True)
    app=create_app(settings);report={}
    async with app.router.lifespan_context(app):
        worker=ComputeWorker(settings,gateway=app.state.gateway,catalog=app.state.catalog)
        await worker.startup()
        async def work():
            while True:
                if not await worker.run_once():await asyncio.sleep(.1)
        task=asyncio.create_task(work())
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',headers={'Authorization':'Bearer '+token},timeout=240) as client:
                async def call(method,path,body=None):
                    response=await client.request(method,path,json=body)
                    if response.status_code>=400:raise RuntimeError(f'{path}: {response.text}')
                    return response.json()['data']
                fixture=await call('GET','/api/scenarios/import/template')
                fixture['initialState']['orders'][0]['demandKg']=2.75
                body={'request_id':'native-import','name':'Imported three-order scenario','fixture':fixture}
                run=await call('POST','/api/scenarios/import',body)
                assert (await call('POST','/api/scenarios/import',body))['run_id']==run['run_id']
                report['run_id']=run['run_id'];print('IMPORT PASS',flush=True)
                loaded=await call('POST',f"/api/runs/{run['run_id']}/load",{'request_id':'load-import'})
                sid=loaded['session']['session_id'];basis=loaded['execution_view']['basis'];report['session_id']=sid
                assert len(loaded['execution_view']['order_ids'])==3
                print('BOOTSTRAP PASS',flush=True)
                submission=await call('POST',f'/api/sessions/{sid}/profiles/compare',{'request_id':'compare-import','expected_revision':{k:basis[k] for k in ('head_version','generation')}})
                cid=submission['comparison_id'];prefix=f'/api/sessions/{sid}/profiles/comparisons/{cid}'
                deadline=time.monotonic()+300
                while time.monotonic()<deadline:
                    group=await call('GET',prefix)
                    if group['status'] in ('COMPLETED','FAILED','CANCELLED'):break
                    if task.done():await task
                    await asyncio.sleep(.5)
                assert group['status']=='COMPLETED',group
                assert group['outcome']['comparison']['status']=='COMPARABLE',group['outcome']
                report['comparison_id']=cid;print('OPTIMIZE COMPARABLE PASS',flush=True)
                started=time.monotonic()
                while time.monotonic()-started<120:
                    explanation=await call('GET',prefix+'/explanations')
                    if explanation['status']!='PENDING':break
                    await asyncio.sleep(.5)
                report['llm_status']=explanation['status'];report['llm_wait_seconds']=round(time.monotonic()-started,2)
                (output/'explanation.json').write_text(json.dumps(explanation,ensure_ascii=False,indent=2),encoding='utf8')
                assert explanation['status']=='READY',explanation['message']
                started=time.monotonic();cached=await call('GET',prefix+'/explanations')
                report['cached_read_ms']=round((time.monotonic()-started)*1000,2)
                assert cached==explanation
                report['status']='PASS';print('QWEN + CACHE PASS',flush=True)
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True);await worker.shutdown()
            (output/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
            print(str(output/'result.json'),flush=True)

if __name__=='__main__':asyncio.run(main())
