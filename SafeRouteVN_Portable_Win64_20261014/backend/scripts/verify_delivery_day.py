"""Native operational smoke against a fresh private authority; no user sessions."""
import argparse,json,sys
from pathlib import Path
from datetime import datetime,timedelta

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from optimization.runtime.sdk import RuntimeClient
from optimization.runtime.build import inventory

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);args=p.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    client=RuntimeClient(snapshot_root=ROOT,store_path=args.out/'authority.sqlite',expected_build_sha256=inventory(ROOT)['build_sha256'])
    sid='delivery-day-smoke';n=0
    def cmd(op,**fields):
        nonlocal n
        n+=1;r=client.command(op,f'cmd-{n}',sid,**fields)
        if r['status']!='OK':raise AssertionError(r)
        return r['value']
    cmd('bootstrap',scenario_id='S0')
    view=cmd('resolve')
    cmd('dispatch',basis=view['basis'],action='enable',data={})
    view=cmd('resolve');assert view['current_time'].endswith('08:00:00+07:00')
    print('DAY_STARTED',flush=True)
    job=cmd('submit',basis=view['basis'],profile='FASTEST',budget_seconds=120)['job_id']
    result=cmd('compute',job_id=job)
    (args.out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    assert result['status']=='COMPLETED',result.get('failure')
    assert result['validation']['valid'] is True
    cmd('accept',basis=cmd('resolve')['basis'],job_id=job)
    print('ACCEPTED',flush=True)
    for _ in range(10):
        view=cmd('resolve')
        if view['delivery_day']['complete']:break
        assert view['delivery_barrier'],view
        cmd('advance',basis=view['basis'],target_time=view['delivery_barrier'])
        view=cmd('resolve')
        ready=[v for v in view['vehicles'] if v.get('awaiting_delivery')]
        assert ready
        for v in ready:
            oid=v['awaiting_delivery'];assert oid not in view['delivered_prefix']
            cmd('dispatch',basis=cmd('resolve')['basis'],action='deliver',data={'vehicle_id':v['vehicle_id'],'order_id':oid})
            assert cmd('resolve')['active_job_id']==job
        print('DELIVERED',','.join(v['awaiting_delivery'] for v in ready),flush=True)
    view=cmd('resolve');assert view['delivery_day']['complete']
    check=client.validate_session(sid)
    assert check['valid'],check
    cmd('dispatch',basis=view['basis'],action='next_day',data={})
    assert client.validate_session(sid)['valid']
    (args.out/'summary.json').write_text(json.dumps({'status':'PASS','validation':check,'delivered':view['delivered_prefix']},indent=2),encoding='utf8')
    print('PASS',flush=True)

if __name__=='__main__':main()
