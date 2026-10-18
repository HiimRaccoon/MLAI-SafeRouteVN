"""Exercise the real localhost solver without printing credentials."""
import argparse
import json
import time
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--local-root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--resume', action='store_true', help='Continue the job recorded in output')
    args = parser.parse_args()
    credential = json.loads((args.local_root / 'backend/dev_access.json').read_bytes())['credentials'][0]['token']
    opener = build_opener(ProxyHandler({}))
    timings = []

    def call(path, body=None):
        started = time.monotonic()
        payload = None if body is None else json.dumps(body).encode()
        request = Request(f'http://127.0.0.1:{args.port}' + path, data=payload,
                          headers={'Authorization': 'Bearer ' + credential, 'Content-Type': 'application/json'})
        with opener.open(request, timeout=180) as response:
            value = json.load(response)['data']
        timings.append({'path': path, 'seconds': round(time.monotonic() - started, 3)})
        print(json.dumps(timings[-1]), flush=True)
        return value

    def command(**fields):
        return {'request_id': 'smoke-' + uuid4().hex, **fields}

    saved = json.loads(args.output.read_bytes()) if args.resume else None
    loaded = None if saved else call('/api/scenarios/S0/load', command())
    sid = saved['session_id'] if saved else loaded['session']['session_id']
    prefix = '/api/sessions/' + sid
    world = call(prefix + '/world')
    submitted = None if saved else call(prefix + '/optimize', command(profile='BALANCED'))
    jid = saved['job_id'] if saved else submitted['job_id']
    deadline = time.monotonic() + 600
    while True:
        job = call(prefix + '/jobs/' + jid)
        if job['job_status'] in ('COMPLETED', 'FAILED'):
            break
        if time.monotonic() >= deadline:
            raise TimeoutError('Native job did not finish within 600 seconds')
        time.sleep(2)
    forecast = call(prefix + '/jobs/' + jid + '/forecast')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {'session_id': sid, 'job_id': jid, 'world': world, 'job': job, 'forecast': forecast}
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    assert job['plan_available'], job
    assert forecast['trajectory'] and forecast['job_view']['validation']['valid'] is True
    state = call(prefix + '/state')
    assert state['active_job_id'] is None, 'Forecast must be read-only'
    def revision(state):
        return {key: state['basis'][key] for key in ('head_version', 'generation')}
    evidence['accept'] = call(prefix + '/jobs/' + jid + '/accept', command(expected_revision=revision(state)))
    state = call(prefix + '/state')
    evidence['replay'] = call(prefix + '/replay/step', command(expected_revision=revision(state)))
    evidence['after'] = call(prefix + '/world')
    evidence['timings'] = timings
    evidence['status'] = 'PASS'
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print('NATIVE_SMOKE_PASS', flush=True)


if __name__ == '__main__':
    main()
