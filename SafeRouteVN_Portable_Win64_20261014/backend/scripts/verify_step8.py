"""Fresh offline HTTP/worker/SDK replay and eleven-criterion evidence.

Uses a NEW installation; invokes only public HTTP and public SDK validation.
The Python socket policy is explicitly not an OS/network security sandbox.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from verify_step4 import NativeHarness, require, fingerprint
from verify_step5 import accept, revision
from verify_step6 import ReplayVerifier


class OfflineHarness(NativeHarness):
    def __init__(self, *args):
        super().__init__(*args)
        self.controls = {}
        self.forced_stops = []
        self.poll_retries = []

    def http(self, path, body=None, *, actor='member3', timeout=90):
        started = time.monotonic()
        deadline = started + timeout
        while True:
            status, value, elapsed = super().http(path, body, actor=actor, timeout=max(0.1, deadline - time.monotonic()))
            busy = any(row.get('code') == 'RUNTIME_BUSY' for row in value.get('diagnostics', []))
            if body is not None or '/jobs/' not in path or status != 503 or not busy:
                return status, value, time.monotonic() - started
            self.poll_retries.append({'path': path, 'status': status, 'code': 'RUNTIME_BUSY', 'read_seconds': round(elapsed, 4)})
            if time.monotonic() + 1 >= deadline:
                return status, value, time.monotonic() - started
            time.sleep(1)

    def spawn(self, script, log, *args):
        stop = self.output.parent / (script + '_' + uuid4().hex + '.stop')
        process = super().spawn(script, log, '--stop-file', str(stop), *args)
        self.controls[process.pid] = stop
        return process

    def stop(self, process):
        if process is not None and process.poll() is None:
            self.controls[process.pid].touch(exist_ok=True)
            try:
                process.wait(timeout=360)
            except subprocess.TimeoutExpired:
                self.forced_stops.append(process.pid)
                NativeHarness.stop(process)

    def start_worker(self):
        from backend.services.heartbeat_io import read_heartbeat_json
        try:
            previous = read_heartbeat_json(self.settings.heartbeat_path).get('worker_id')
        except (OSError, ValueError):
            previous = None
        started = datetime.now(timezone.utc)
        start = time.monotonic()
        self.worker = self.spawn('start_worker.py', self.worker_log)
        deadline = start + 1800
        while time.monotonic() < deadline:
            require(self.worker.poll() is None, 'Offline worker stopped before READY')
            try:
                row = read_heartbeat_json(self.settings.heartbeat_path)
                if row['status'] == 'DEGRADED':
                    raise RuntimeError('Offline startup failed: ' + str(row.get('last_error_code')))
                if (row['status'] == 'READY' and row['worker_id'] != previous
                        and datetime.fromisoformat(row['updated_at']) >= started):
                    status, data, _ = self.http('/ready', actor=None, timeout=130)
                    if status == 200 and data['data']['ready'] is True:
                        return {'status': 'PASS', 'worker_id': row['worker_id'], 'previous_worker_id': previous,
                            'different_worker_id': row['worker_id'] != previous,
                            'seconds': round(time.monotonic() - start, 4), 'aggregate_test_bound_seconds': 1800}
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.5)
        raise TimeoutError('Offline startup aggregate test bound exceeded')


def recorded_views(scenario):
    values = []
    for frame in scenario['frames']:
        path = Path(frame['path'])
        raw = path.read_bytes()
        require(len(raw) == frame['bytes'] and hashlib.sha256(raw).hexdigest() == frame['sha256'], 'Native frame hash differs')
        value = json.loads(raw)
        if isinstance(value, dict) and 'execution_view' in value:
            value = value['execution_view']
        if (isinstance(value, dict) and value.get('schema_version') == 'task02-m2-execution-view/2'
                and value['basis']['session_id'] == scenario['session_id']):
            values.append((frame['name'], value))
    return values


def criterion_evidence(scenarios, project, sdk_validation):
    partitions = loads = frames = certified = 0
    for scenario in scenarios:
        fixture = json.loads((project / f"scenarios/fixtures/thu-duc-binh-thanh-v1/{scenario['scenario_id']}.json").read_bytes())
        demands = {order['id']: order['demandKg'] for order in fixture['initialState']['orders']}
        for event in fixture['events']:
            if 'orderPayload' in event:
                demands[event['orderPayload']['id']] = event['orderPayload']['demandKg']
        previous = set()
        for _, view in recorded_views(scenario):
            frames += 1
            delivered = view['delivered_prefix']
            require(len(delivered) == len(set(delivered)) and previous <= set(delivered), 'Observed delivered prefix changed or duplicated')
            previous = set(delivered)
            require(view['execution_mode'] == 'SIMULATED_REPLAY' and view['real_world_observation'] is False, 'Replay mislabelled real observation')
            if view['active_job_id'] is not None:
                served, unserved = view['planned_served_suffix'], [order['order_id'] for order in view['unserved']]
                combined = delivered + served + unserved
                require(len(combined) == len(set(combined)) and set(combined) == set(view['order_ids']), 'Certified prefix/suffix/unserved partition differs')
                partitions += 1
            for vehicle in view['vehicles']:
                load = vehicle['current_load_kg']
                require(math.isfinite(load) and -1e-9 <= load <= vehicle['capacity_kg'] + 1e-9, 'Observed capacity exceeded')
                ids = vehicle['onboard_order_ids']
                require(len(ids) == len(set(ids)), 'Cargo duplicated within owner')
                require(abs(sum(demands[order] for order in ids) - load) <= 1e-7, 'Observed load differs from source cargo demand')
                loads += 1
        for solve in scenario['solves']:
            validation = solve['job_view']['validation']
            require(validation['valid'] is True and validation['status'] == 'VALIDATED', 'Uncertified native witness')
            certified += 1
        require(sdk_validation[scenario['scenario_id']]['valid'] is True, 'Independent public session validation failed')
    require(partitions > 0 and loads > 0 and certified == 6, 'Fresh native criteria were not exercised')
    s3 = next(row for row in scenarios if row['scenario_id'] == 'S3')
    require(s3['checks']['unavailable_custody']['custody_exercised'] is True
        and s3['checks']['no_custody_transfer_in_replan']['status'] == 'PASS', 'S3 onboard custody not exercised')
    return {'recorded_execution_frames': frames, 'certified_partitions': partitions, 'vehicle_load_checks': loads,
        'fresh_raw_validated_witnesses': certified, 's3_custody_owner': s3['custody_owner_id'],
        's3_onboard_order_ids': s3['custody_order_ids'], 'validator_scope': 'PUBLIC_SDK_RAW_ALL_COLUMN_AND_CAUSAL_JOURNAL_VALIDATION',
        'narrative': 'NOT_CHECKED_BY_STEP8_USE_COMPLETION_VERIFIER', 'cold_solver_determinism_claimed': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--budget-seconds', type=float, default=120)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project))
    from backend.services.settings import Settings
    from backend.services.runtime_gateway import RuntimeGateway
    from backend.services.offline_guard import install_from_environment
    args.output.parent.mkdir(parents=True, exist_ok=True)
    require(os.getenv('SAFEROUTE_OFFLINE_MODE') == 'loopback-only', 'An active offline policy is required')
    install_from_environment('native-http-verifier')
    settings = Settings.from_environment()
    run_id = 'm3-step8-' + uuid4().hex
    result = {'schema_version': 'saferoute-m3-step8-native-offline/1', 'status': 'RUNNING', 'started_at': datetime.now(timezone.utc).isoformat(),
        'project_root': str(project), 'runtime_build_sha256': settings.installation()['expected_build_sha256'],
        'compute_budget_seconds': args.budget_seconds, 'checks': {}, 'scenarios': [], 'new_solver_calls': 0,
        'offline_scope': 'TRUSTED_PYTHON_SOCKET_POLICY_API_WORKER_SDK_AND_NATIVE_COMPUTE_CHILD; NOT_OS_SANDBOX',
        'frontend_e2e': 'PENDING_M4_SOURCE', 'execution_mode': 'SIMULATED_REPLAY', 'real_world_observation': False}
    harness = OfflineHarness(project, settings, args.output)
    harness.env['SAFEROUTE_COMPUTE_BUDGET_SECONDS'] = str(args.budget_seconds)
    failure = None
    try:
        harness.start_server()
        result['checks']['initial_readiness'] = harness.start_worker()
        code, catalog, _ = harness.http('/api/scenarios')
        require(code == 200, 'Offline catalog unavailable')
        verifier = ReplayVerifier(harness, args.output, run_id, catalog['data'], args.budget_seconds)
        sdk_validation = {}
        for scenario_id in ('S2', 'S3', 'S4'):
            row = verifier.run(scenario_id)
            result['scenarios'].append(row)
            result['new_solver_calls'] += len(row['solves'])
            sdk_validation[scenario_id] = asyncio.run(RuntimeGateway(settings).validate_session(row['session_id']))
            require(sdk_validation[scenario_id]['valid'] is True, 'Offline public SDK independent validation rejected committed replay')
            print(scenario_id + ' OFFLINE_HTTP_WORKER_REPLAY_PASS', flush=True)
        result['public_sdk_validation'] = sdk_validation
        result['checks']['criterion_observations'] = {'status': 'PASS', **criterion_evidence(result['scenarios'], project, sdk_validation)}
        # Two actual same-basis BALANCED jobs: cold domain then certified warm reuse.
        sid = harness.load('S0', run_id + '-repro-load')
        before, _ = harness.state(sid)
        jobs = []
        for suffix in ('cold', 'warm'):
            request = run_id + '-repro-' + suffix
            submission, _ = harness.submit(sid, request)
            job, _, _ = harness.poll_terminal(sid, submission['job_id'], before, timeout=args.budget_seconds + 90)
            require(job['plan_available'] is True and job['validation']['valid'] is True, 'Reproducibility test lacks a certified witness')
            retry, _ = harness.submit(sid, request)
            require(retry['job_id'] == submission['job_id'] and harness.state(sid)[0] == before, 'Submission retry changed job/head')
            jobs.append(job)
            result['new_solver_calls'] += 1
        keys = ('input_basis', 'business_status', 'validation', 'coverage_evaluated', 'served_orders', 'unserved_orders', 'plan_available')
        semantics = [{key: job[key] for key in keys} for job in jobs]
        require(fingerprint(semantics[0]) == fingerprint(semantics[1]), 'Same-basis cold/warm certified public semantics differ')
        result['checks']['reproducibility'] = {'status': 'PASS', 'jobs': jobs, 'compared_fields': list(keys), 'semantic_sha256': fingerprint(semantics[0]),
            'scope': 'SAME_BASIS_BALANCED_COLD_THEN_WARM_PUBLIC_CERTIFIED_COVERAGE; NO_GENERAL_COLD_OPTIMUM_OR_TIMING_DETERMINISM_CLAIM'}
        s3 = next(row for row in result['scenarios'] if row['scenario_id'] == 'S3')
        status, manifest, _ = harness.http(f"/api/sessions/{s3['session_id']}/artifacts", {'request_id': run_id + '-offline-artifact'}, timeout=300)
        require(status == 201 and len(manifest['data']['files']) == 12, 'Offline audit artifact did not complete')
        artifact_path = manifest['data']['links']['content']
        status, content, _ = harness.http(artifact_path)
        saved = content['data']
        raw = saved['content_utf8'].encode('utf-8')
        require(status == 200 and len(raw) == saved['bytes'] and hashlib.sha256(raw).hexdigest() == saved['sha256'], 'Offline immutable bundle bytes differ')
        bundle = json.loads(raw)
        provenance = json.loads(next(file['content_utf8'] for file in bundle['files'] if file['name'] == 'provenance.json'))
        require('EXPOSURE_IS_PROXY' in provenance['limitations'], 'Export dropped exposure proxy limitation')
        result['checks']['proxy_export'] = {'status': 'PASS', 'artifact_id': saved['artifact_id'], 'sha256': saved['sha256'], 'bytes': saved['bytes'], 'files': 12,
            'scope': 'RELATIVE_EXPOSURE_PROXY; NOT_CRASH_PROBABILITY', 'content_route': artifact_path}
        require(harness.http(f"/api/sessions/{s3['session_id']}/state", actor='member4')[0] == 403, 'Other owner accessed offline session')
        require(harness.http('/api/scenarios', actor=None)[0] == 401, 'Offline mode bypassed authentication')
        result['checks']['authentication_owner'] = {'status': 'PASS', 'other_owner_status': 403, 'unauthenticated_status': 401}
        heads = {row['session_id']: row['final_state'] for row in result['scenarios']}
        heads[sid] = before
        harness.wait_idle(timeout=180)
        harness.stop_worker()
        harness.stop_server()
        require(not harness.forced_stops, 'Graceful offline helper stop exceeded its bound')
        harness.start_server()
        result['checks']['restart_readiness'] = harness.start_worker()
        for row in result['scenarios']:
            current, _ = harness.state(row['session_id'])
            require(current == heads[row['session_id']], 'Offline restart changed full head')
            status, history, _ = harness.http(f"/api/sessions/{row['session_id']}/replay/history")
            require(status == 200 and history['data']['history'] == row['replay_history'], 'Offline restart changed replay receipts')
        require(harness.state(sid)[0] == before, 'Offline restart changed forecast-only physical head')
        status, retried, _ = harness.http(artifact_path)
        require(status == 200 and retried['data'] == saved, 'Offline restart changed artifact bytes')
        result['checks']['restart_persistence'] = {'status': 'PASS', 'full_heads': len(heads), 'exact_artifact_preserved': True, 'exact_receipts_preserved': True}
        status, openapi, _ = harness.http('/openapi.json', actor=None)
        require(status == 200 and not any('/backup' in path or '/recover' in path for path in openapi['paths']), 'Private admin leaked into HTTP')
        (args.output.parent / 'openapi.json').write_text(json.dumps(openapi, ensure_ascii=False, indent=2), encoding='utf-8')
        result['checks']['openapi'] = {'status': 'PASS', 'app_version': openapi['info']['version'], 'sha256': fingerprint(openapi), 'no_admin_routes': True}
    except Exception as error:
        failure = error
        result['failure'] = {'type': type(error).__name__, 'message': str(error)}
    finally:
        harness.close()
        result['server_running_after_test'] = False
        result['worker_running_after_test'] = False
        result['forced_stops'] = harness.forced_stops
    try:
        roles, proofs = set(), []
        for file in sorted(Path(os.environ['SAFEROUTE_OFFLINE_AUDIT_DIR']).glob('*.jsonl')):
            rows = [json.loads(line) for line in file.read_text(encoding='utf-8').splitlines()]
            require(any(row['event'] == 'self_test' and row['outcome'] == 'PASS' for row in rows)
                and sum(row['outcome'] == 'BLOCKED' and row['self_probe'] for row in rows) == 4, 'A process did not prove external access denial')
            require(not any(row['outcome'] == 'BLOCKED' and not row['self_probe'] for row in rows), 'Application attempted a blocked external service')
            roles.update(row['role'] for row in rows)
            proofs.append({'path': str(file), 'bytes': file.stat().st_size, 'sha256': hashlib.sha256(file.read_bytes()).hexdigest(), 'pid': rows[0]['pid'], 'role': rows[0]['role']})
        require({'http-api', 'compute-worker', 'sdk-bridge', 'sdk-native-worker', 'native-http-verifier'} <= roles, 'Offline policy did not cover the actual complete process tree')
        result['checks']['offline_policy'] = {'status': 'PASS', 'roles': sorted(roles), 'proofs': proofs, 'non_probe_external_attempts': 0,
            'os_network_disconnection_claimed': False, 'security_sandbox_claimed': False}
    except Exception as error:
        if failure is None:
            failure = error
            result['failure'] = {'type': type(error).__name__, 'message': str(error)}
    result['status'] = 'M3_STEP8_NATIVE_OFFLINE_PASS' if failure is None else 'M3_STEP8_NATIVE_OFFLINE_FAIL'
    result['poll_retries'] = harness.poll_retries
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps({'status': result['status'], 'scenarios': len(result['scenarios']), 'new_solver_calls': result['new_solver_calls'], 'helpers_stopped': True}), flush=True)
    if failure:
        raise failure


if __name__ == '__main__':
    main()
