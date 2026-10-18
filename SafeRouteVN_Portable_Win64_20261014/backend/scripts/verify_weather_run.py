"""Explicit online capture and native lifecycle smoke; records current evidence."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--capture',action='store_true')
    parser.add_argument('--native',action='store_true')
    args=parser.parse_args()
    from optimization.runtime.run_builder import build,write
    output=ROOT/'outputs/weather-native'; output.mkdir(parents=True,exist_ok=True)
    pointer=output/'latest.json'
    if args.capture:
        run_id='run-'+uuid4().hex
        epoch=datetime.now(timezone(timedelta(hours=7))).replace(microsecond=0).isoformat()
        write(pointer,{'run_id':run_id,'status':'BUILDING','epoch':epoch})
        digest=build(ROOT,run_id,'S2',epoch,15,progress=lambda s:print(s,flush=True))
        write(pointer,{'run_id':run_id,'manifest_sha256':digest,'epoch':epoch,'status':'READY'})
        print('RUN_READY',run_id,flush=True)
    if args.native:
        from optimization.runtime.build import inventory
        from optimization.runtime.sdk import RuntimeClient
        data=json.loads(pointer.read_bytes());stamp=uuid4().hex
        client=RuntimeClient(snapshot_root=ROOT,store_path=output/(stamp+'.sqlite'),expected_build_sha256=inventory(ROOT)['build_sha256'])
        sid='weather-'+stamp
        boot=client.bootstrap_run(sid,'boot-'+stamp,data['run_id'],data['manifest_sha256'])
        report={'run':data,'build_sha256':inventory(ROOT)['build_sha256'],'bootstrap':boot}
        write(output/'native.json',report)
        def command(op,key,**fields):
            result=client.command(op,key,sid,**fields)
            assert result['status']=='OK',result
            return result['value']
        def compare(label,basis):
            jobs=[command('submit',label+p,basis=basis,profile=p,budget_seconds=120)['job_id'] for p in ('FASTEST','BALANCED','SAFER')]
            views=client.compute_profiles(sid,jobs)
            report[label]={'jobs':views};write(output/'native.json',report)
            assert all(v['validation']['valid'] is True for v in views),views
            report[label]['comparison']=client.compare_profiles(sid,jobs)
            assert report[label]['comparison']['status']=='COMPARABLE'
            print(label,'COMPARABLE',flush=True)
            return jobs
        jobs=compare('initial',boot['basis'])
        accepted=command('accept','accept-initial',job_id=jobs[1],basis=boot['basis'])
        manifest=json.loads((ROOT/'scenarios/runs'/data['run_id']/'manifest.json').read_bytes())
        fixture=json.loads((ROOT/manifest['artifacts']['fixture']['path']).read_bytes())
        event=fixture['events'][0]
        advanced=command('advance','advance-event',basis=accepted['basis'],target_time=event['timestamp'])
        before=command('resolve','before-event')
        applied=command('apply_event','apply-event',basis=advanced['basis'],event_id=event['eventId'])
        after=command('resolve','after-event')
        assert before['vehicles']==after['vehicles'] and before['delivered_prefix']==after['delivered_prefix']
        assert after['active_job_id'] is None and len(after['order_ids'])==len(before['order_ids'])+1
        report.update(accepted=accepted,advanced=advanced,applied=applied,before=before,after=after)
        write(output/'native.json',report)
        jobs=compare('replan',after['basis'])
        accepted=command('accept','accept-replan',job_id=jobs[1],basis=after['basis'])
        target=(datetime.fromisoformat(event['timestamp'])+timedelta(minutes=1)).isoformat()
        report['post_replan_advance']=command('advance','advance-replan',basis=accepted['basis'],target_time=target)
        report['validation']=client.validate_session(sid)
        assert report['validation'].get('valid') is True,report['validation']
        report['status']='PASS';write(output/'native.json',report)
        print('NATIVE_WEATHER_REPLAN_PASS',flush=True)

if __name__=='__main__':main()
