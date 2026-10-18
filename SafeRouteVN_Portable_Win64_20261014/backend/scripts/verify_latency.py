"""Real SDK + Ollama latency gate; isolated stores, no existing credentials."""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
from backend.api.main import create_app
from backend.services.compute_worker import ComputeWorker
from backend.services.settings import Settings


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--local-root', required=True, type=Path)
    args = parser.parse_args()
    output = ROOT/'outputs'/('latency-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    output.mkdir()
    install = json.loads((args.local_root/'installation.json').read_bytes())
    install['authority_store_parent'] = str(output/'authority')
    (output/'installation.json').write_text(json.dumps(install), encoding='utf8')
    token = uuid4().hex
    (output/'auth.json').write_text(json.dumps({'schema_version':'saferoute-m3-auth/1','tokens':[
        {'actor_id':'latency-test','role':'dispatcher','token_sha256':hashlib.sha256(token.encode()).hexdigest()}]}), encoding='utf8')
    settings = Settings(ROOT, output/'installation.json', output/'auth.json', output/'metadata.sqlite',
                        output/'heartbeat.json', runtime_persistent_bridge=True)
    report = {'status':'RUNNING','cases':[], 'build':install['expected_build_sha256']}
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        worker = ComputeWorker(settings, gateway=app.state.gateway, catalog=app.state.catalog)
        await worker.startup()
        async def work():
            while True:
                if not await worker.run_once():await asyncio.sleep(.1)
        task = asyncio.create_task(work())
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                    headers={'Authorization':'Bearer '+token}, timeout=90) as client:
                async def call(method, path, body=None):
                    r = await client.request(method, path, json=body)
                    r.raise_for_status()
                    return r.json()['data']
                for scenario in ('S4', 'S0'):
                    loaded = await call('POST',f'/api/scenarios/{scenario}/load',{'request_id':'load-'+scenario})
                    sid = loaded['session']['session_id']
                    if scenario == 'S4':
                        basis = loaded['execution_view']['basis']
                        await call('POST', f'/api/sessions/{sid}/delivery-day', {'request_id':'enable-day',
                            'expected_revision':{k:basis[k] for k in ('head_version','generation')},
                            'basis':basis, 'action':'enable', 'data':{}})
                    for mode in ('cold', 'warm'):
                        started = time.perf_counter()
                        submission = await call('POST', f'/api/sessions/{sid}/profiles/compare',
                                                {'request_id':scenario+'-'+mode})
                        prefix = submission['links']['poll']
                        while True:
                            group = await call('GET',prefix)
                            if group['status'] in ('COMPLETED','FAILED','CANCELLED'):break
                            if task.done():await task
                            assert time.perf_counter()-started < 60, 'Comparison exceeded 60 seconds'
                            await asyncio.sleep(.2)
                        (output/f'{scenario}-{mode}-comparison.json').write_text(json.dumps(group),encoding='utf8')
                        assert group['outcome']['comparison'] and group['outcome']['comparison']['status']=='COMPARABLE', group['outcome']
                        comparison_seconds = time.perf_counter()-started
                        # Include the trajectory reads the browser needs, not only scalar cards.
                        await asyncio.gather(*(call('GET',f"/api/sessions/{sid}/jobs/{m['job_id']}/forecast") for m in group['jobs']))
                        while True:
                            explanation = await call('GET',prefix+'/explanations')
                            if explanation['status']!='PENDING':break
                            await asyncio.sleep(.2)
                        seconds = time.perf_counter()-started
                        row = {'scenario':scenario,'mode':mode,'comparison_seconds':round(comparison_seconds,3),
                               'total_seconds':round(seconds,3),'llm_status':explanation['status'],
                               'profiles':[{ 'profile':m['profile'], 'status':m['view']['job_status'],
                                    'served':len(m['view']['served_orders'])} for m in group['jobs']]}
                        report['cases'].append(row)
                        (output/f'{scenario}-{mode}-explanation.json').write_text(json.dumps(explanation,ensure_ascii=False,indent=2),encoding='utf8')
                        print(json.dumps(row),flush=True)
                        assert explanation['status']=='READY',explanation['message']
                        assert seconds <= 60, f'End-to-end exceeded 60 seconds: {seconds}'
                        expected = 8 if scenario=='S4' else 3
                        assert all(r['served']==expected and r['status']=='COMPLETED' for r in row['profiles'])
                report['status']='PASS'
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            await worker.shutdown()
            (output/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
            print(str(output/'result.json'),flush=True)


if __name__=='__main__':asyncio.run(main())
