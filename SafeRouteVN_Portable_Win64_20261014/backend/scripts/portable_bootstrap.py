"""Offline first-run setup for the Windows x64 portable distribution.

Never uses host Python, pip, npm, credentials or an existing developer install.
Moving the folder creates a new installation; old state is preserved.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import subprocess
import sys

from launch_local import instance_lock, choose_port, port_open


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temp.replace(path)


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def verify_package(project, manifest):
    if manifest.get('schema_version') != 'saferoute-portable/1':
        raise ValueError('Unsupported portable package')
    for name, pin in manifest['files'].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:
            raise ValueError('Unsafe portable manifest path')
        path = project / name
        if not path.is_file() or path.stat().st_size != pin['bytes'] or digest(path) != pin['sha256']:
            raise ValueError('Portable file missing or changed: ' + name + '. Extract the ZIP again.')


def local_path(project, manifest_digest):
    # A resealed maintenance build must retain this folder's installation and
    # history. Its runtime authority is upgraded separately with raw validation.
    existing = []
    for config_file in (project / 'portable-state').glob('*/installation.json'):
        try:
            config = json.loads(config_file.read_bytes())
            if Path(config['snapshot_root']).resolve() == project.resolve():
                existing.append(config_file.parent)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if len(existing) == 1:
        return existing[0]
    identity = hashlib.sha256((os.path.normcase(str(project.resolve())) + '\n' + manifest_digest).encode()).hexdigest()[:20]
    return project / 'portable-state' / identity


def prepare(project, local, manifest):
    print('First launch: verifying bundled files and native solver. Please wait...', flush=True)
    verify_package(project, manifest)
    runtime_python = project / 'portable/python-runtime/python.exe'
    probe = '''import json,sys
from pathlib import Path
from optimization.runtime.build import verify
from optimization.runtime.environment import attest
from optimization.runtime.sdk import RuntimeClient
from backend.scripts.preflight_m3 import source_check
root=Path(sys.argv[1]); local=Path(sys.argv[2])
record=json.loads((root/'production_inventory.json').read_bytes())
verify(root,record,record['build_sha256'])
source=source_check(root)
environment=attest(root,smoke=True)
client=RuntimeClient(snapshot_root=root,store_path=local/'receipts/g0.sqlite',expected_build_sha256=record['build_sha256'])
boot=client.command('bootstrap','portable-bootstrap','portable-g0',scenario_id='S0')
assert boot['status']=='OK',boot
validation=client.validate_session('portable-g0')
assert validation.get('valid') is True,validation
print(json.dumps({'status':'G0_TECHNICAL_PASS','step1_input_completeness':'COMPLETE_VERIFIED','snapshot_root':str(root),'source_check':source,'build_sha256':record['build_sha256'],'environment':environment,'validation':validation}))
'''
    (local / 'receipts').mkdir(parents=True, exist_ok=True)
    result = subprocess.run([str(runtime_python), '-X', 'utf8', '-I', '-B', '-c', probe, str(project), str(local)],
                            cwd=project, capture_output=True, timeout=600,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    (local / 'receipts/first-run.log').write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError('Native check failed. See ' + str(local / 'receipts/first-run.log'))
    receipt = json.loads(result.stdout)
    if receipt['build_sha256'] != manifest['build_sha256']:
        raise ValueError('Portable runtime build differs from manifest')
    write(local / 'receipts/preflight.json', receipt)
    token = secrets.token_urlsafe(36)
    write(local / 'backend/dev_access.json', {'credentials': [{'actor_id': 'local-dispatcher', 'role': 'dispatcher', 'token': token}]})
    write(local / 'backend/auth_tokens.json', {'schema_version': 'saferoute-m3-auth/1', 'tokens': [
        {'actor_id': 'local-dispatcher', 'role': 'dispatcher', 'token_sha256': hashlib.sha256(token.encode()).hexdigest()}]})
    (local / 'state').mkdir(exist_ok=True)
    write(local / 'installation.json', {
        'schema_version': 'saferoute-m3-server-installation/1',
        'runtime_root': str(project), 'runtime_python': str(runtime_python),
        'backend_python': str(project / 'portable/python-backend/python.exe'),
        'snapshot_root': str(project), 'expected_build_sha256': manifest['build_sha256'],
        'authority_store_parent': str(local / 'state'),
        'latest_preflight_receipt': str(local / 'receipts/preflight.json'),
        'technical_preflight_status': 'G0_TECHNICAL_PASS', 'deployment_approved': False})


def main():
    project = Path(__file__).resolve().parents[2]
    manifest_file = project / 'portable/manifest.json'
    manifest = json.loads(manifest_file.read_bytes())
    local = local_path(project, digest(manifest_file))
    args = sys.argv[1:]
    if any(a == '--local-root' or a.startswith('--local-root=') for a in args):
        raise ValueError('Portable mode manages its own local installation. Omit --local-root.')
    if '--stop' in args and not (local / 'installation.json').is_file():
        print('No portable installation is running here.', flush=True)
        return
    local.mkdir(parents=True, exist_ok=True)
    with instance_lock(local / 'setup.lock') as owned:
        if not owned:
            print('SafeRoute is preparing this folder in another window. Wait for that window.', flush=True)
            return
        if not (local / 'installation.json').is_file():
            prepare(project, local, manifest)
        config = json.loads((local / 'installation.json').read_bytes())
        if config['snapshot_root'] != str(project) or config['expected_build_sha256'] != manifest['build_sha256']:
            raise ValueError('Portable installation binding changed')
    # Retain this copy's ports across restarts and cleanup; do not stop another app.
    ports_file = local / 'ports.json'
    if ports_file.is_file():
        ports = json.loads(ports_file.read_bytes())
    else:
        api = choose_port(8000)
        ui = choose_port(5174, lambda p: p == api or port_open(p))
        ports = {'backend': api, 'frontend': ui}
        write(ports_file, ports)
    env = {k: v for k, v in os.environ.items() if k.upper() not in ('PYTHONHOME', 'PYTHONPATH', 'NODE_OPTIONS', 'NODE_PATH')}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    command = [sys.executable, '-X', 'utf8', '-B', str(project / 'backend/scripts/launch_local.py'),
               '--local-root', str(local), '--backend-port', str(ports['backend']), '--frontend-port', str(ports['frontend']), *args]
    raise SystemExit(subprocess.call(command, cwd=project, env=env))


if __name__ == '__main__':
    main()
