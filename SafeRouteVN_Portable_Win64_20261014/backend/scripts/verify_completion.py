"""Native acceptance for the remaining M3 features in a new private installation."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from verify_step4 import require
from verify_step5 import accept, revision
from verify_step8 import OfflineHarness


class Harness(OfflineHarness):
    def http(self, path, body=None, *, actor='member3', timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            result = super().http(path, body, actor=actor, timeout=max(.1, deadline-time.monotonic()))
            codes = {d.get('code') for d in result[1].get('diagnostics', [])}
            busy = result[0] == 503 and 'RUNTIME_BUSY' in codes
            capture_race = (result[0] == 409 and 'ARTIFACT_STATE_CHANGED' in codes
                and body is not None and path.endswith('/artifacts'))
            if not (busy or capture_race):
                return result
            self.poll_retries.append({'path': path, 'status': result[0],
                'code': 'RUNTIME_BUSY' if busy else 'ARTIFACT_STATE_CHANGED', 'same_body_retry': body is not None})
            if time.monotonic() + 1 >= deadline:
                return result
            time.sleep(1)

    def data(self, path, body=None, *, status=200):
        actual, response, _ = self.http(path, body)
        require(actual == status, 'Unexpected HTTP '+str(actual)+' at '+path+' codes '+str([d.get('code') for d in response.get('diagnostics', [])]))
        return response['data']

    def comparison(self, sid, body):
        receipt = self.data(f'/api/sessions/{sid}/profiles/compare', body, status=202)
        return receipt, f"/api/sessions/{sid}/profiles/comparisons/{receipt['comparison_id']}"

    def wait_comparison(self, path, timeout=1500):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            row = self.data(path)
            if row['status'] in ('COMPLETED', 'FAILED', 'CANCELLED'):
                return row
            require(self.worker is not None and self.worker.poll() is None, 'Worker stopped during comparison')
            time.sleep(1)
        raise TimeoutError('Profile comparison aggregate bound exceeded')

    def paused(self, sid, *, reason=None, timeout=600):
        deadline=time.monotonic()+timeout
        path=f'/api/sessions/{sid}/replay/playback'
        while time.monotonic()<deadline:
            value=self.data(path)
            if value['fully_paused']:
                require(reason is None or value['reason']==reason, 'Unexpected playback stop reason '+value['reason'])
                return value
            time.sleep(.5)
        raise TimeoutError('Playback did not settle and pause')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    project=Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(project))
    from backend.services.settings import Settings
    from backend.services.runtime_gateway import RuntimeGateway
    from backend.services.narrative_service import decision_narrative
    from backend.services.offline_guard import install_from_environment
    require(os.getenv('SAFEROUTE_OFFLINE_MODE')=='loopback-only', 'Offline process policy required')
    install_from_environment('native-http-verifier')
    settings=Settings.from_environment()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    harness=Harness(project, settings, args.output)
    harness.env['SAFEROUTE_COMPUTE_BUDGET_SECONDS']='120'
    harness.env['SAFEROUTE_REPLAY_STEP_SECONDS']='60'
    run='m3-complete-'+uuid4().hex
    report={'schema_version':'saferoute-m3-completion-native/1','status':'RUNNING',
        'started_at':datetime.now(timezone.utc).isoformat(), 'checks':{}, 'new_solver_calls':0,
        'runtime_build_sha256':settings.installation()['expected_build_sha256'],
        'simulation_step_seconds':60, 'frontend_e2e':'PENDING_M4_SOURCE',
        'execution_mode':'SIMULATED_REPLAY', 'real_world_observation':False}
    frames=[]
    def save(name,value):
        path=args.output.parent/(name+'.json')
        raw=json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False).encode('utf8')
        path.write_bytes(raw)
        frames.append({'name':name,'path':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
    failure=None
    try:
        harness.start_server()
        status, schema, _ = harness.http('/openapi.json', actor=None)
        routes = {
            '/api/sessions/{session_id}/jobs/{job_id}/accept': 'post',
            '/api/sessions/{session_id}/profiles/compare': 'post',
            '/api/sessions/{session_id}/replay/start': 'post',
            '/api/sessions/{session_id}/replay/playback/pause': 'post',
            '/api/sessions/{session_id}/narrative': 'get',
            '/api/sessions/{session_id}/artifacts': 'post',
        }
        require(status == 200 and schema['info']['version'] == '0.8.0'
            and all(method in schema['paths'].get(path, {}) for path, method in routes.items()),
            'Completion verifier route preflight failed')
        report['checks']['route_preflight'] = {'status': 'PASS', 'app_version': '0.8.0', 'routes': routes}
        report['checks']['initial_readiness']=harness.start_worker()
        sid=harness.load('S0',run+'-S0-load')
        before,_=harness.state(sid)
        save('S0_initial', before)
        request={'request_id':run+'-batch','expected_revision':revision(before['basis'])}
        receipt,path=harness.comparison(sid,request)
        require(harness.data(f'/api/sessions/{sid}/profiles/compare',request,status=202)==receipt, 'Batch retry receipt differs')
        batch=harness.wait_comparison(path)
        require(batch['status']=='COMPLETED', 'Batch did not complete')
        jobs={member['profile']:member for member in batch['jobs']}
        require(set(jobs)=={'FASTEST','BALANCED','SAFER'}, 'Profile members differ')
        require(all(member['view']['job_status']=='COMPLETED' and member['view']['validation']['valid'] is True for member in jobs.values()), 'Native three-profile witnesses not all certified')
        require(batch['outcome']['comparison']['status'] in ('COMPARABLE','NON_COMPARABLE'), 'SDK typed comparison absent')
        require(harness.state(sid)[0]==before, 'Profile batch altered full physical view')
        report['new_solver_calls']+=3
        save('profile_batch',batch)
        profile_text=harness.data(path+'/narrative')
        require(profile_text['comparison_status']==batch['outcome']['comparison']['status'], 'Trade-off narrative contradicted SDK verdict')
        save('profile_narrative',profile_text)
        report['checks']['profile_batch']={'status':'PASS','comparison_id':receipt['comparison_id'],
            'sdk_verdict':batch['outcome']['comparison']['status'],'full_physical_view_unchanged':True,'retry_exact':True}
        print('PROFILE_BATCH_PASS', flush=True)
        existing,existing_path=harness.comparison(sid, {'request_id':run+'-existing', 'job_ids':[jobs[p]['job_id'] for p in ('FASTEST','BALANCED','SAFER')]})
        compared=harness.wait_comparison(existing_path)
        require(compared['status']=='COMPLETED' and compared['outcome']==batch['outcome'], 'Existing inputs did not preserve original SDK comparison')
        save('existing_comparison',compared)
        report['checks']['existing_jobs']={'status':'PASS','new_solver_calls':0,'exact_sdk_outcome':True}
        # The worker is stopped while reserving/cancelling, proving cancellation
        # creates no new native child and persists across worker restart.
        harness.wait_idle()
        harness.stop_worker()
        cancelled,cancel_path=harness.comparison(sid,{'request_id':run+'-cancel-batch','expected_revision':revision(before['basis'])})
        cancel_body={'request_id':run+'-cancel'}
        cancelled_receipt=harness.data(cancel_path+'/cancel',cancel_body)
        require(harness.data(cancel_path+'/cancel',cancel_body)==cancelled_receipt,'Cancel retry receipt differs')
        report['checks']['cancel_restart_readiness']=harness.start_worker()
        cancelled_view=harness.wait_comparison(cancel_path)
        require(cancelled_view['status']=='CANCELLED' and all(j['job_id'] is None for j in cancelled_view['jobs']), 'Cancelled unsubmitted batch created children')
        save('cancelled_batch',cancelled_view)
        report['checks']['cancelled_batch']={'status':'PASS','native_children_created':0,'retry_exact':True}
        selected=jobs['BALANCED']['job_id']
        accepted,_=accept(harness,f'/api/sessions/{sid}/jobs/{selected}/accept',
            {'request_id':run+'-accept-S0','expected_revision':revision(before['basis'])})
        view=accepted['execution_view']
        target=(datetime.fromisoformat(view['current_time'])+timedelta(seconds=10)).isoformat()
        advanced=harness.data(f'/api/sessions/{sid}/replay/step',
            {'request_id':run+'-manual-step','expected_revision':revision(view['basis']),'target_time':target})['execution_view']
        warm,_=harness.submit(sid,run+'-warm')
        terminal,_,_=harness.poll_terminal(sid,warm['job_id'],advanced,timeout=600)
        report['new_solver_calls']+=1
        require(terminal['validation']['valid'] is True,'Warm revised-basis witness not certified')
        _,mixed_path=harness.comparison(sid,{'request_id':run+'-mixed','job_ids':[jobs['FASTEST']['job_id'],warm['job_id'],jobs['SAFER']['job_id']]})
        mixed=harness.wait_comparison(mixed_path)
        require(mixed['status']=='COMPLETED' and mixed['outcome']['comparison']['status']=='NON_COMPARABLE', 'Different authenticated bases were ranked/comparable')
        save('mixed_basis_comparison',mixed)
        mixed_text=harness.data(mixed_path+'/narrative')
        require(mixed_text['comparison_status']=='NON_COMPARABLE' and not mixed_text['profiles'], 'Non-comparable jobs produced a trade-off table')
        save('mixed_basis_narrative',mixed_text)
        report['checks']['non_comparable']={'status':'PASS','sdk_reason':mixed['outcome']['reason'],'new_solver_calls':0}
        print('MIXED_BASIS_NON_COMPARABLE_PASS', flush=True)
        sid2=harness.load('S2',run+'-S2-load')
        initial2,_=harness.state(sid2)
        job,_=harness.submit(sid2,run+'-S2-optimize')
        terminal,_,_=harness.poll_terminal(sid2,job['job_id'],initial2,timeout=600)
        report['new_solver_calls']+=1
        require(terminal['validation']['valid'] is True,'S2 initial witness not certified')
        accepted,_=accept(harness,f"/api/sessions/{sid2}/jobs/{job['job_id']}/accept",
            {'request_id':run+'-S2-accept','expected_revision':revision(initial2['basis'])})
        start_body={'request_id':run+'-start','expected_revision':revision(accepted['execution_view']['basis']),'speed':1}
        start=harness.data(f'/api/sessions/{sid2}/replay/start',start_body)
        retry=harness.data(f'/api/sessions/{sid2}/replay/start',start_body)
        require(retry['receipt']==start['receipt'],'Start retry receipt differs')
        speed_body={'request_id':run+'-speed','speed':8}
        speed=harness.data(f'/api/sessions/{sid2}/replay/speed',speed_body)
        require(speed['controller']['speed']==8,'Playback speed did not persist')
        deadline=time.monotonic()+600
        while time.monotonic()<deadline:
            history=harness.data(f'/api/sessions/{sid2}/replay/playback/history')
            if any(row['operation']=='tick_settled' for row in history['history']):
                break
            time.sleep(.5)
        else:
            raise TimeoutError('Native automatic tick never advanced')
        harness.data(f'/api/sessions/{sid2}/replay/playback/pause',{'request_id':run+'-pause'})
        paused=harness.paused(sid2)
        require(paused['execution_view']['basis']['generation']==accepted['execution_view']['basis']['generation'], 'Playback changed plan generation')
        save('playback_paused',paused)
        require(harness.state(sid2)[0]==paused['execution_view'],'Fully paused controller advanced again')
        report['checks']['playback_pause_speed']={'status':'PASS','speed':8,'retry_receipt_exact':True,'generation_unchanged':True,'fully_paused':True}
        # Explicitly persist RUNNING with no worker, then restart. Startup must
        # fence it before any reservation and must never auto-resume.
        harness.stop_worker()
        frozen=paused['execution_view']
        if paused['reason']=='EVENT_BARRIER':
            raise ValueError('Pause test reached event too early to exercise restart')
        harness.data(f'/api/sessions/{sid2}/replay/start',
            {'request_id':run+'-before-restart','expected_revision':revision(frozen['basis']),'speed':1})
        report['checks']['playback_restart_readiness']=harness.start_worker()
        restarted=harness.paused(sid2,reason='PAUSED_WORKER_RESTART')
        require(restarted['execution_view']==frozen,'Worker restart auto-resumed playback')
        save('playback_restart',restarted)
        report['checks']['restart_fence']={'status':'PASS','full_view_unchanged':True,'reason':restarted['reason']}
        print('PLAYBACK_PAUSE_AND_RESTART_PASS', flush=True)
        harness.data(f'/api/sessions/{sid2}/replay/start',
            {'request_id':run+'-resume','expected_revision':revision(frozen['basis']),'speed':8})
        barrier=harness.paused(sid2,reason='EVENT_BARRIER')
        events=harness.data(f'/api/sessions/{sid2}/events')
        require(events['events'] and events['events'][0]['timestamp']==barrier['execution_view']['current_time']
            and events['events'][0]['apply_allowed'] is True,'Automatic replay crossed or applied event barrier')
        save('playback_event_barrier',barrier)
        save('pending_events',events)
        report['checks']['event_barrier']={'status':'PASS','event_id':events['events'][0]['event_id'],
            'exact_barrier':barrier['execution_view']['current_time'],'event_remains_pending':True,'auto_apply':False}
        for label,session_id in [('S0',sid),('S2',sid2)]:
            current,_=harness.state(session_id)
            narrative=harness.data(f'/api/sessions/{session_id}/narrative')
            require(narrative==decision_narrative(current,session_id,report['runtime_build_sha256']), 'Narrative differs from native public metrics')
            save(label+'_narrative',narrative)
            validation=asyncio.run(RuntimeGateway(settings).validate_session(session_id))
            require(validation['valid'] is True,'Independent public SDK session validation failed')
            save(label+'_validation',validation)
        artifact=harness.data(f'/api/sessions/{sid2}/artifacts',{'request_id':run+'-export'},status=201)
        require(artifact['schema_version']=='saferoute-m3-artifact-manifest/2' and len(artifact['files'])==12,'New narrative export inventory differs')
        content=harness.data(artifact['links']['content'])
        raw=content['content_utf8'].encode('utf8')
        require(hashlib.sha256(raw).hexdigest()==content['sha256'],'Bundle hash differs')
        bundle=json.loads(raw)
        narrative_file=next(f for f in bundle['files'] if f['name']=='decision_narrative.json')
        require(json.loads(narrative_file['content_utf8'])==decision_narrative(barrier['execution_view'],sid2),'Export narrative differs from captured physical view')
        save('artifact_manifest',artifact)
        save('artifact_content',content)
        report['checks']['narrative_export']={'status':'PASS','native_sessions':2,'files':12,'exact_public_metrics':True,'bundle_sha256':content['sha256']}
        report['checks']['sdk_validations']={'status':'PASS','sessions':2,'all_valid':True}
        print('M3_COMPLETION_NATIVE_FEATURES_PASS',flush=True)
    except Exception as error:
        failure=error
        report['failure']={'type':type(error).__name__,'message':str(error)}
    finally:
        harness.close()
        report['helpers_stopped']=True
        report['forced_stops']=harness.forced_stops
        report['poll_retries']=harness.poll_retries
    proofs=[]
    try:
        roles=set()
        native_children=0
        for file in sorted(Path(os.environ['SAFEROUTE_OFFLINE_AUDIT_DIR']).glob('*.jsonl')):
            rows=[json.loads(line) for line in file.read_text(encoding='utf8').splitlines()]
            require(any(r['event']=='self_test' and r['outcome']=='PASS' for r in rows)
                and sum(r['self_probe'] and r['outcome']=='BLOCKED' for r in rows)==4,'Process offline proof absent')
            require(not any(r['outcome']=='BLOCKED' and not r['self_probe'] for r in rows),'Unexpected external application access')
            roles.update(r['role'] for r in rows)
            native_children+=int(rows[0]['role']=='sdk-native-worker')
            proofs.append({'path':str(file),'bytes':file.stat().st_size,'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),
                'pid':rows[0]['pid'],'role':rows[0]['role']})
        require({'http-api','compute-worker','sdk-bridge','sdk-native-worker','native-http-verifier'}<=roles,'Actual process tree offline coverage missing')
        report['observed_native_children']=native_children
        report['offline_process_proofs']=proofs
        require(native_children==report['new_solver_calls']==5 and not report['forced_stops'],'Native child count or graceful stop mismatch')
        report['checks']['offline_process_tree']={'status':'PASS','native_children':native_children,'roles':sorted(roles),
            'proofs':proofs,'unexpected_external_attempts':0,'os_airgap_claimed':False}
    except Exception as error:
        if failure is None:
            failure=error
            report['failure']={'type':type(error).__name__,'message':str(error)}
    report['frames']=frames
    report['status']='M3_COMPLETION_NATIVE_PASS' if failure is None else 'M3_COMPLETION_NATIVE_FAIL'
    report['finished_at']=datetime.now(timezone.utc).isoformat()
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
    print(json.dumps({'status':report['status'],'new_solver_calls':report['new_solver_calls'],'helpers_stopped':True}),flush=True)
    if failure:
        raise failure


if __name__=='__main__':
    main()
