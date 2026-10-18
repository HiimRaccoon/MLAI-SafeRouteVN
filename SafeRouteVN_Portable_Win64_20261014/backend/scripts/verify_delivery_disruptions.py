"""Native multi-depot, unexpected-order, recovery and shared-profile checks."""
import sys,json,argparse
from pathlib import Path
from datetime import datetime,timedelta
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from optimization.runtime.sdk import RuntimeClient
from optimization.runtime.build import inventory

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    client=RuntimeClient(snapshot_root=ROOT,store_path=a.out/'authority.sqlite',expected_build_sha256=inventory(ROOT)['build_sha256'])
    sid='disruptions';n=0
    def command(op,**fields):
        nonlocal n;n+=1
        r=client.command(op,'c'+str(n),sid,**fields)
        assert r['status']=='OK',r
        return r['value']
    def action(kind,**data):return command('dispatch',basis=command('resolve')['basis'],action=kind,data=data)
    def optimize(tag):
        view=command('resolve');ids=[]
        cached=a.out/(tag+'.json')
        if cached.exists():ids=[r['job_id'] for r in json.loads(cached.read_bytes())]
        else:
            for profile in ('FASTEST','BALANCED','SAFER'):
                ids.append(command('submit',basis=view['basis'],profile=profile,budget_seconds=150)['job_id'])
        results=client.compute_profiles(sid,ids)
        (a.out/(tag+'.json')).write_text(json.dumps(results,indent=2),encoding='utf8')
        assert all(r['job_status']=='COMPLETED' and r['validation']['valid'] for r in results),results
        comparison=client.compare_profiles(sid,ids);assert comparison['status']=='COMPARABLE'
        command('accept',job_id=ids[0],basis=command('resolve')['basis'])
        print(tag,'ACCEPTED',flush=True)
        return command('resolve')
    if (a.out/'before.json').exists():n=1000
    else:
        command('bootstrap',scenario_id='S3');action('enable')
        action('add_depot',depot_id='DEPOT-2',location_order_id='O002')
        action('assign_depot',order_id='O002',depot_id='DEPOT-2')
        for i in range(4,9):action('defer',order_id=f'O{i:03}')
    view=optimize('before')
    command('advance',basis=view['basis'],target_time=(datetime.fromisoformat(view['current_time'])+timedelta(microseconds=1)).isoformat())
    # Pause at a real observed point, inject two orders unknown to the initial plan.
    action('add_order',order_id='NEW-NORMAL',template_id='O003',depot_id='DEPOT-2',name='Normal test',phone='0900000001',urgent=False)
    assert command('resolve')['active_job_id'], 'late regular order must preserve the accepted route'
    action('add_order',order_id='NEW-URGENT',template_id='O003',depot_id='DEPOT',name='Urgent test',phone='0900000002',urgent=True)
    view=optimize('urgent')
    assert 'NEW-NORMAL' in view['delivery_day']['queue']
    routes=view['accepted_trajectory']['vehicle_routes']
    assigned=next(r for r in routes if 'NEW-URGENT' in r['order_sequence'])
    assert assigned['order_sequence'][0]=='NEW-URGENT'
    assert next(a for a in assigned['actions'] if a['kind'] in ('PICKUP','SERVICE'))['kind']=='PICKUP'
    action('unavailable',vehicle_id='V1',recovery_minutes=30)
    view=command('resolve');assert view['delivery_day']['queue']['O001']['depot_id']=='DEPOT'
    assert next(v for v in view['vehicles'] if v['vehicle_id']=='V1')['onboard_order_ids']
    view=optimize('unavailable')
    assert all(r['vehicle_id']!='V1' for r in view['accepted_trajectory']['vehicle_routes'])
    validation=client.validate_session(sid);assert validation['valid']
    (a.out/'summary.json').write_text(json.dumps({'status':'PASS','validation':validation,'urgent_vehicle':assigned['vehicle_id'],'queue':view['delivery_day']['queue']},indent=2),encoding='utf8')
    print('PASS',flush=True)
if __name__=='__main__':main()
