"""Coordinate localhost API and one worker in a configured private installation.

Foreground operator command. Child processes are hidden and stopped only by
their private control file/owned process IDs. Never installs dependencies.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen, ProxyHandler, build_opener
from uuid import uuid4
try:
    from .local_backend_control import installation_digest, save_control
except ImportError:
    from local_backend_control import installation_digest, save_control


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--local-root', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--startup-timeout', type=float, default=1800)
    parser.add_argument('--shutdown-timeout', type=float, default=225)
    parser.add_argument('--stop-file', type=Path, help='Optional external private stop trigger for an operator/test')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 0 < args.startup_timeout <= 3600 or not 0 < args.shutdown_timeout <= 3600:
        raise ValueError('Invalid localhost port or operational timeout')
    project = Path(__file__).resolve().parents[2]
    local = args.local_root.resolve(strict=True)
    config = json.loads((local / 'installation.json').read_bytes())
    if Path(config['snapshot_root']).resolve() != project.resolve():
        raise ValueError('Installation source root differs from this backend')
    python = Path(config.get('backend_python', local / 'backend-venv/Scripts/python.exe'))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_') + uuid4().hex
    output = local / 'receipts' / ('launch_' + stamp)
    output.mkdir(parents=True, exist_ok=False)
    stop = output / 'stop.request'
    if args.stop_file is not None and (not args.stop_file.is_absolute() or args.stop_file.exists()):
        raise ValueError('New absolute external stop file required')
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
        'SAFEROUTE_INSTALLATION_CONFIG': str(local / 'installation.json'),
        'SAFEROUTE_AUTH_FILE': str(local / 'backend/auth_tokens.json'),
        'SAFEROUTE_METADATA_DB': str(local / 'backend/metadata.sqlite'),
        'SAFEROUTE_WORKER_HEARTBEAT': str(local / 'backend/worker_heartbeat.json')}
    if args.offline:
        env.update(SAFEROUTE_OFFLINE_MODE='loopback-only', SAFEROUTE_OFFLINE_AUDIT_DIR=str(output / 'network-audit'))
    logs, children = [], []
    receipt = {'schema_version': 'saferoute-m3-launch/1', 'status': 'STARTING', 'offline': args.offline,
        'started_at': datetime.now(timezone.utc).isoformat(), 'startup_wait_bound_seconds': args.startup_timeout,
        'shutdown_wait_bound_seconds': args.shutdown_timeout, 'forced_stop': False}
    opener = build_opener(ProxyHandler({}))
    control = {'run_id': stamp, 'installation_sha256': installation_digest(local),
               'port': args.port, 'stop_file': str(stop), 'status': 'STARTING'}
    save_control(local, control)

    def external_stop():
        return args.stop_file is not None and args.stop_file.exists()

    try:
        for script, extra in [('start_backend.py', ['--port', str(args.port)]), ('start_worker.py', [])]:
            log = (output / (script + '.log')).open('w', encoding='utf-8')
            logs.append(log)
            children.append(subprocess.Popen([str(python), '-X', 'utf8', '-B', str(project / 'backend/scripts' / script), '--stop-file', str(stop), *extra], cwd=project, env=env, stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
        deadline = time.monotonic() + args.startup_timeout
        while time.monotonic() < deadline:
            if external_stop():
                receipt['status'] = 'STOP_REQUESTED_DURING_STARTUP'
                return
            if any(child.poll() is not None for child in children):
                raise RuntimeError('Owned API/worker exited during startup')
            try:
                with opener.open(f'http://127.0.0.1:{args.port}/ready', timeout=15) as response:
                    if response.status == 200 and json.loads(response.read())['data']['ready'] is True:
                        break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            raise TimeoutError('Operational startup wait exceeded; no reset/repair performed')
        receipt['status'] = 'READY'
        control['status'] = 'READY'
        save_control(local, control)
        receipt['ready_at'] = datetime.now(timezone.utc).isoformat()
        (output / 'launch.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        print(json.dumps({'status': 'READY', 'url': f'http://127.0.0.1:{args.port}', 'receipt': str(output / 'launch.json'), 'credentials_printed': False}), flush=True)
        while not external_stop():
            if any(child.poll() is not None for child in children):
                raise RuntimeError('Owned API/worker stopped unexpectedly')
            time.sleep(0.25)
        receipt['status'] = 'STOPPED'
    except KeyboardInterrupt:
        receipt['status'] = 'STOPPED'
    except Exception as error:
        receipt.update(status='LAUNCH_FAILED', error_type=type(error).__name__)
        raise
    finally:
        stop.touch(exist_ok=True)
        deadline = time.monotonic() + args.shutdown_timeout
        for child in reversed(children):
            try:
                child.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                receipt['forced_stop'] = True
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                else:
                    child.kill()
                child.wait(timeout=15)
        receipt['all_owned_children_stopped'] = all(child.poll() is not None for child in children)
        control['status'] = 'STOPPED' if receipt['all_owned_children_stopped'] else 'STOP_FAILED'
        save_control(local, control)
        receipt['child_exit_codes'] = [child.returncode for child in children]
        receipt['finished_at'] = datetime.now(timezone.utc).isoformat()
        for log in logs:
            log.close()
        (output / 'launch.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        print(json.dumps({'status': receipt['status'], 'all_owned_children_stopped': receipt['all_owned_children_stopped'], 'forced_stop': receipt['forced_stop']}), flush=True)


if __name__ == '__main__':
    main()
