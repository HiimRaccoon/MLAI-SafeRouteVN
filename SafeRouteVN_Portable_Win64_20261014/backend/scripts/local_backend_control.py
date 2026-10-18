"""Private, installation-bound graceful shutdown for local backend restarts."""
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit


def selected_installation(project, override=None):
    project=Path(project).resolve()
    if override is not None:return Path(override).resolve(strict=True)
    pointer=project.parent/'.local/saferoute-active.json'
    if pointer.is_file():
        value=json.loads(pointer.read_bytes())
        local=Path(value['local_root']).resolve(strict=True)
        config=json.loads((local/'installation.json').read_bytes())
        if (value.get('schema_version')!='saferoute-local-selection/1' or Path(value['project_root']).resolve()!=project
                or not local.is_relative_to(pointer.parent.resolve())
                or value['build_sha256']!=config['expected_build_sha256']):
            raise RuntimeError('Active local installation binding differs; choose --local-root explicitly')
        return local
    return (project.parent/'.local/saferoute-dev-20261009').resolve(strict=True)


def installation_digest(local):
    return hashlib.sha256((Path(local) / 'installation.json').read_bytes()).hexdigest()


def save_control(local, value):
    path = Path(local) / 'backend/backend_control.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temporary.replace(path)


def stop_local_services(local, occupied, *, timeout=240, sleep=time.sleep, clock=time.monotonic):
    """Stop the installation's launcher, or its verified legacy backend.

    Only private stop files are signalled; an unrelated listener is never killed.
    """
    local = Path(local).resolve()
    state_path = local/'backend/launcher_status.json'
    state = json.loads(state_path.read_bytes()) if state_path.is_file() else {}
    control_path = local/'backend/backend_control.json'
    control = json.loads(control_path.read_bytes()) if control_path.is_file() else {}
    active = state.get('status') in ('STARTING', 'READY')
    frontend_port = None
    if active:
        origin = urlsplit(state.get('admin_url', ''))
        logs = Path(state.get('logs', '')).resolve()
        if (origin.hostname != '127.0.0.1' or origin.scheme != 'http' or not origin.port
                or logs.parent != local/'receipts' or not logs.name.startswith('desktop_')):
            raise RuntimeError('Launcher ownership record differs; stop its original console')
        frontend_port = origin.port
    backend_port = control.get('port')
    if active and state.get('stop_file'):
        stop = Path(state['stop_file']).resolve()
        if (state.get('installation_sha256') != installation_digest(local)
                or stop != logs/'stop.request'):
            raise RuntimeError('Launcher shutdown binding differs; no stop request sent')
        stop.touch(exist_ok=True)
    elif control.get('status') in ('STARTING', 'READY'):
        # Older launchers observe their backend exiting and close their frontend.
        stop_verified_backend(local, backend_port, occupied, timeout=timeout, sleep=sleep, clock=clock)
    elif active:
        raise RuntimeError('Legacy launcher has no verified shutdown record; close its original console')
    else:
        return
    deadline = clock() + timeout
    while clock() < deadline:
        current = json.loads(state_path.read_bytes()) if state_path.is_file() else {}
        if active and current.get('logs') != state.get('logs'):
            raise RuntimeError('Launcher ownership changed during cleanup')
        launcher_stopped = not active or current.get('status') in ('STOPPED', 'FAILED')
        if launcher_stopped and (not frontend_port or not occupied(frontend_port)):
            if backend_port and occupied(backend_port):
                stop_verified_backend(local, backend_port, occupied, timeout=timeout, sleep=sleep, clock=clock)
            return
        sleep(.25)
    raise RuntimeError('SafeRoute did not release its ports before the deadline; inspect launcher logs')


def stop_verified_backend(local, port, occupied, *, timeout=240, sleep=time.sleep, clock=time.monotonic):
    """Never kill a PID inferred from a port or delete a live OS lock."""
    local = Path(local).resolve()
    try:
        value = json.loads((local / 'backend/backend_control.json').read_bytes())
        stop = Path(value['stop_file']).resolve()
        receipts = (local / 'receipts').resolve()
        if (value['installation_sha256'] != installation_digest(local) or value['port'] != port
                or value['status'] not in ('STARTING', 'READY')
                or stop.name != 'stop.request' or not stop.is_relative_to(receipts)
                or stop.parent.parent != receipts or not stop.parent.name.startswith('launch_')):
            raise ValueError('Backend control binding differs')
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError('Existing backend is not READY and has no matching private shutdown record; '
                           'stop its original launcher before restarting') from error
    stop.touch(exist_ok=True)
    deadline = clock() + timeout
    while clock() < deadline:
        current = json.loads((local / 'backend/backend_control.json').read_bytes())
        if current.get('run_id') != value['run_id']:
            raise RuntimeError('Backend ownership changed during restart')
        if current.get('status') == 'STOPPED' and not occupied(port):
            return
        sleep(0.25)
    raise RuntimeError('Verified backend did not stop within the shutdown deadline; inspect its logs')
