"""Fresh native M3-reference -> public SDK -> M4 execution-view acceptance.

No HTTP/backend/frontend edits. This is simulated reference transport, not a
production authentication implementation. Evidence names native calls and all
responses; a successful flow is not by itself A01--A67 release certification.
"""
import argparse,json,platform,sys,time,hashlib
from pathlib import Path
from .sdk import RuntimeClient
from .m3_reference import ReferenceTransport
from .build import inventory
from .protocol import canonical,require,sha

def run(snapshot,output,scenario,budget,all_profiles=False,expected_build_sha256=None):
    output=Path(output);output.mkdir(parents=True,exist_ok=False);repo=Path(__file__).resolve().parents[2];build=inventory(repo)
    if expected_build_sha256 is not None:
        from .build import verify
        verify(repo,build,expected_build_sha256)
    (output/'production_inventory.json').write_bytes(canonical(build));client=RuntimeClient(snapshot_root=snapshot,store_path=output/'authority.sqlite',expected_build_sha256=build['build_sha256']);transport=ReferenceTransport(client,output/'m3_reference.sqlite');session='rolling-'+scenario;calls=[]
    def call(op,key,**fields):
        start=time.monotonic();reply=transport.command(op,key,None if op=='capabilities' else session,**fields)
        p=output/f'{len(calls):02d}_{op}.json';p.write_bytes(canonical(reply));calls.append({'operation':op,'command_id':key,'elapsed_seconds':time.monotonic()-start,'response':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size,'status':reply['status']})
        require(reply['status']=='OK','NATIVE_FLOW_FAILED',op,str(reply['diagnostics']));return reply['value']
    boot=call('bootstrap','boot',scenario_id=scenario);basis=boot['basis'];origin=call('get_head','root-clock')['current_time']
    comparisons=[]
    def solve(key,basis):
        submitted=call('submit',key+'-submit',basis=basis,profile='BALANCED',budget_seconds=budget)
        previous=call('get_head',key+'-before');job=call('compute',key+'-compute',job_id=submitted['job_id'])
        require(job['status']=='COMPLETED' and job['validation']['valid'] is True,'NATIVE_WITNESS_MISSING','job',str(job.get('failure')))
        if all_profiles:
            ids=[job['id']]
            for profile in ('FASTEST','SAFER'):
                other=call('submit',key+'-'+profile+'-submit',basis=basis,profile=profile,budget_seconds=budget)
                j=call('compute',key+'-'+profile+'-compute',job_id=other['job_id']);require(j['status']=='COMPLETED' and j['validation']['valid'] is True,'NATIVE_WITNESS_MISSING','job',str(j.get('failure')));ids.append(j['id'])
            comparison=client.compare_profiles(session,ids);comparisons.append(comparison);(output/(key+'_profile_comparison.json')).write_bytes(canonical(comparison))
        require(call('get_head',key+'-after')==previous,'COMPUTE_MUTATED_HEAD','head','compute changed observed execution')
        accepted=call('accept',key+'-accept',job_id=job['id'],basis=basis)
        return job,accepted['basis']
    first,basis=solve('initial',basis)
    if scenario in ('S2','S3','S4'):
        basis=call('advance','event-boundary',basis=basis,target_time='2026-09-27T21:15:00+07:00')['basis']
        observed=call('get_head','before-event');(output/'before_event_view.json').write_bytes(canonical(observed))
        event_id=observed['pending_event_ids'][0];event=call('apply_event','event',basis=basis,event_id=event_id)
        require(call('apply_event','event',basis=basis,event_id=event_id)==event,'IDEMPOTENCY','event','event retry differs');basis=event['basis']
        second,basis=solve('post-event',basis)
    else:
        basis=call('advance','first-epoch',basis=basis,target_time='2026-09-27T21:01:00+07:00')['basis']
        second,basis=solve('second-epoch',basis)
    # Native observed suffix, not a stored forecast converted to delivered.
    from datetime import datetime,timedelta
    # Report an expiry observation separately; a fixed 23:00 sample is not a
    # full-return gate when a valid planned route ends later than that sample.
    if scenario=='S4':
        for n,target in enumerate(('2026-09-27T21:15:00.000001+07:00','2026-09-27T22:14:59.999999+07:00','2026-09-27T22:15:00+07:00','2026-09-27T22:15:00.000001+07:00')):
            basis=call('advance','rain-boundary-'+str(n),basis=basis,target_time=target)['basis']
            sample=call('get_head','rain-observed-'+str(n));(output/('rain_boundary_'+str(n)+'.json')).write_bytes(canonical(sample))
    end_us=max((r['return_us'] for r in second['result']['vehicle_routes']),default=0)
    target=max(datetime.fromisoformat('2026-09-27T23:00:00+07:00'),datetime.fromisoformat(origin)+timedelta(microseconds=end_us+1)).isoformat()
    basis=call('advance','finish',basis=basis,target_time=target)['basis'];view=call('get_head','view')
    (output/'execution_view.json').write_bytes(canonical(view));validation=client.validate_session(session);(output/'whole_lineage_validation.json').write_bytes(canonical(validation))
    call('recover','restart');require(call('get_head','after-recovery')==view,'RECOVERY_CHANGED_HEAD','head','restart differs')
    notifications=transport.poll_outbox(session)
    require(transport.poll_outbox(session)==[],'OUTBOX_RETRY','outbox','M3 poll repeated acknowledged notifications')
    require(client.read_notifications(session)==[],'OUTBOX_RETRY','outbox','ack produced new notifications')
    summary={'schema_version':'task02-m2-native-rolling-flow/2','status':'NATIVE_FLOW_PASS_NOT_RELEASE_GATE','scenario_id':scenario,'platform':platform.platform(),'interpreter':sys.executable,'optimized':not __debug__,
             'build_sha256':build['build_sha256'],'first_business_status':first['result']['status'],'second_business_status':second['result']['status'],
             'installation_trust':'SERVER_APPROVED_EXTERNAL_DIGEST' if expected_build_sha256 else 'DEMO_SELF_INVENTORY_NOT_RELEASE_TRUST',
             'actual_delivered':view['delivered_prefix'],'planned_suffix':view['planned_served_suffix'],'unserved':view['unserved'],'calls':calls,'validation':validation,'profile_comparisons':comparisons,
             'performance_target_seconds':30,'performance_verdict':'NOT_MET' if any(x['operation']=='compute' and x['elapsed_seconds']>30 for x in calls) else 'WITHIN_TARGET_FOR_THIS_FLOW_NOT_SLA',
             'real_world_observation':False,'execution_mode':'SIMULATED_REPLAY','general_m1_validated':False,'e4_run':False,'production_calibration':'UNCONFIGURED'}
    (output/'summary.json').write_bytes(canonical(summary));print(json.dumps({'status':summary['status'],'scenario_id':scenario,'output':str(output),'actual_delivered':summary['actual_delivered']}));return 0

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot-root',required=True,type=Path);p.add_argument('--output-root',required=True,type=Path);p.add_argument('--scenario-id',choices=['S1','S2','S3','S4','S5','S6','S7','S8'],required=True);p.add_argument('--budget-seconds',type=float,default=300);p.add_argument('--all-profiles',action='store_true');p.add_argument('--expected-build-sha256');a=p.parse_args()
    return run(a.snapshot_root,a.output_root,a.scenario_id,a.budget_seconds,a.all_profiles,a.expected_build_sha256)
if __name__=='__main__':raise SystemExit(main())
