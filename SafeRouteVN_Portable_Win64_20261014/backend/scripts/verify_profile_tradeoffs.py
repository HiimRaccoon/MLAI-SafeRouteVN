"""Native profile regression on isolated stores; no server or credentials needed."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True, help='New evidence directory')
    parser.add_argument('--budget', type=int, default=35)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from optimization.runtime.build import inventory
    from optimization.runtime.sdk import RuntimeClient
    from optimization.runtime.store import Store

    build = inventory(ROOT)['build_sha256']
    path = args.output / 'authority.sqlite'
    client = RuntimeClient(snapshot_root=ROOT, store_path=path, expected_build_sha256=build)
    store = Store(path, build)
    report = dict(build_sha256=build, budget_seconds=args.budget, cases=[])
    for scenario, day, expected in [('S0', False, 3), ('S3', False, 8), ('S4', False, 8), ('S4', True, 8)]:
        sid = scenario + ('-day' if day else '')
        def command(operation, key, **fields):
            response = client.command(operation, key, sid, **fields)
            if response['status'] != 'OK':
                raise AssertionError(response)
            return response['value']
        basis = command('bootstrap', 'boot', scenario_id=scenario)['basis']
        if day:
            basis = command('dispatch', 'day', basis=basis, action='enable', data={})['basis']
        jobs = [command('submit', profile, basis=basis, profile=profile,
                        budget_seconds=args.budget)['job_id'] for profile in ('FASTEST', 'BALANCED', 'SAFER')]
        start = monotonic()
        views = client.compute_profiles(sid, jobs)
        elapsed = monotonic() - start
        if not all(v['job_status'] == 'COMPLETED' and v['validation']['valid'] is True
                   and len(v['served_orders']) == expected for v in views):
            (args.output / (sid + '-failure.json')).write_text(json.dumps(views, indent=2), encoding='utf8')
            raise AssertionError('Certification/coverage regression: ' + sid)
        comparison = client.compare_profiles(sid, jobs)
        assert comparison['status'] == 'COMPARABLE'
        results = [store.job(sid, jid)['result'] for jid in jobs]
        metrics = [r['metrics'] for r in results]
        assert len({r['domain_sha256'] for r in results}) == 1
        assert all(m['total_soft_lateness_s'] == 0 for m in metrics)
        assert metrics[0]['total_travel_time_s'] <= metrics[1]['total_travel_time_s'] + 1e-6
        assert metrics[1]['total_travel_time_s'] <= metrics[2]['total_travel_time_s'] + 1e-6
        assert metrics[0]['total_exposure'] + 1e-6 >= metrics[1]['total_exposure']
        assert metrics[1]['total_exposure'] + 1e-6 >= metrics[2]['total_exposure']
        # Equality is legal. Record physical route identities, never demand
        # three different routes when the finite domain has a common winner.
        case = dict(scenario=sid, elapsed_seconds=elapsed, served=expected, profiles=[],
                    queries=dict(Counter(q.get('objective') for q in results[0]['domain']['queries'])))
        for r in results:
            case['profiles'].append(dict(profile=r['profile'], metrics=r['metrics'],
                routes=[c['route_column_id'] for c in r['vehicle_routes']]))
        report['cases'].append(case)
        (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf8')
        print(sid, 'PASS', round(elapsed, 2), 'seconds', flush=True)
    assert client.verify_store()
    report['status'] = 'PASS'
    (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf8')


if __name__ == '__main__':
    main()
