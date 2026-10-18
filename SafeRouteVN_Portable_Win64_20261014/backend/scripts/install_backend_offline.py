"""Receive a sealed M3 kit into a NEW private installation on an approved team tree."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from release_tools import sha, verify_release, wheel_pins, extract_package


def run(argv, cwd, log, timeout=300):
    env = {key: value for key, value in os.environ.items() if key.upper() not in ('PYTHONPATH', 'PYTHONHOME')}
    env.update(PYTHONDONTWRITEBYTECODE='1', PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
    result = subprocess.run([str(value) for value in argv], cwd=cwd, env=env, capture_output=True,
        timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    log.write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError('Installation check failed; retained log ' + log.name)
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--release-dir', required=True, type=Path)
    parser.add_argument('--expected-manifest-sha256', required=True)
    parser.add_argument('--project-root', required=True, type=Path)
    parser.add_argument('--local-root', required=True, type=Path)
    parser.add_argument('--base-python', required=True, type=Path)
    parser.add_argument('--preflight-receipt', required=True, type=Path)
    args = parser.parse_args()
    kit, project = args.release_dir.resolve(strict=True), args.project_root.resolve(strict=True)
    local = args.local_root.resolve()
    if sha(kit / 'release_manifest.json') != args.expected_manifest_sha256:
        raise ValueError('Release manifest differs from the received digest')
    manifest = verify_release(kit)
    if local.exists() or local.is_relative_to(project) or project.is_relative_to(local) or local.is_relative_to(kit):
        raise ValueError('New private local root outside project and kit required')
    # Install onto the already received backend, never silently overwrite a team edit.
    for row in manifest['files']:
        if row['path'].startswith('backend/'):
            target = project / row['path']
            if not target.is_file() or sha(target) != row['sha256']:
                raise ValueError('Receive matching backend files into the team tree first')
    prior = json.loads(args.preflight_receipt.read_bytes())
    if (prior['status'] != 'G0_TECHNICAL_PASS' or prior['step1_input_completeness'] != 'COMPLETE_VERIFIED'
            or prior['build_sha256'] != manifest['runtime_build_sha256'] or Path(prior['snapshot_root']).resolve() != project):
        raise ValueError('Existing approved source preflight binding differs')
    local.mkdir(parents=True, exist_ok=False)
    receipts = local / 'receipts/install'
    receipts.mkdir(parents=True)
    receipt = {'schema_version': 'saferoute-m3-offline-install/1', 'status': 'INCOMPLETE', 'started_at': datetime.now(timezone.utc).isoformat(),
        'project_root': str(project), 'release_manifest_sha256': sha(kit / 'release_manifest.json'),
        'prior_source_preflight_sha256': sha(args.preflight_receipt), 'prior_source_preflight_reused': True, 'checks': {}}
    try:
        interpreter = json.loads(run([args.base_python, '-I', '-B', '-c', 'import json,sys,platform,struct; print(json.dumps({"version":list(sys.version_info[:2]),"system":platform.system(),"bits":struct.calcsize("P")*8}))'], project, receipts / 'python.log'))
        if interpreter != {'version': [3, 12], 'system': 'Windows', 'bits': 64}:
            raise ValueError('Approved Windows CPython 3.12 x64 required')
        receipt['checks']['interpreter'] = interpreter
        for package in manifest['packages']:
            extract_package(kit / package['path'], local / (package['kind'] + '_STEP7_80694f51'), package['sha256'], package['entries'])
        for kind, name in [('backend', 'backend-venv'), ('runtime', 'venv')]:
            lock = project / 'backend/requirements-backend.lock.txt' if kind == 'backend' else local / 'runtime_STEP7_80694f51/requirements-runtime.lock.txt'
            if wheel_pins(kit / 'wheelhouse' / kind, lock) != manifest['wheelhouses'][kind]:
                raise ValueError('Sealed wheelhouse/lock differs')
            run([args.base_python, '-I', '-B', '-m', 'venv', local / name], project, receipts / (kind + '_venv.log'))
            python = local / name / 'Scripts/python.exe'
            run([python, '-B', '-m', 'pip', 'install', '--no-index', '--no-cache-dir', '--disable-pip-version-check', '--no-deps', '--require-hashes', '--find-links', kit / 'wheelhouse' / kind, '-r', kit / 'wheelhouse' / kind / 'requirements.hashed.txt'], project, receipts / (kind + '_pip.log'))
            run([python, '-B', '-m', 'pip', 'check'], project, receipts / (kind + '_pip_check.log'))
            receipt['checks'][kind + '_offline_dependencies'] = {'status': 'PASS', 'wheels': len(manifest['wheelhouses'][kind]), 'no_index': True, 'require_hashes': True, 'pip_check': 'PASS'}
        config = {'schema_version': 'saferoute-m3-server-installation/1', 'runtime_root': str(local / 'runtime_STEP7_80694f51'),
            'integration_root': str(local / 'integration_STEP7_80694f51'), 'runtime_python': str(local / 'venv/Scripts/python.exe'),
            'snapshot_root': str(project), 'expected_build_sha256': manifest['runtime_build_sha256'],
            'authority_store_parent': str(local / 'state'), 'latest_preflight_receipt': str(args.preflight_receipt.resolve()),
            'technical_preflight_status': 'G0_TECHNICAL_PASS', 'deployment_approved': False, 'client_config_override_allowed': False}
        (local / 'state').mkdir()
        (local / 'installation.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
        private = local / 'backend'
        private.mkdir()
        credentials = [{'actor_id': actor, 'role': role, 'token': secrets.token_urlsafe(36)} for actor, role in [('member3', 'dispatcher'), ('member4', 'dispatcher'), ('observer', 'viewer')]]
        auth = {'schema_version': 'saferoute-m3-auth/1', 'tokens': [{'actor_id': row['actor_id'], 'role': row['role'], 'token_sha256': hashlib.sha256(row['token'].encode()).hexdigest()} for row in credentials]}
        (private / 'auth_tokens.json').write_text(json.dumps(auth, indent=2), encoding='utf-8')
        (private / 'dev_access.json').write_text(json.dumps({'credentials': credentials}, indent=2), encoding='utf-8')
        # Separate opt-in read-only demo identity; never reuse native tokens.
        (private / 'mock_token.txt').write_text(secrets.token_urlsafe(36), encoding='utf-8')
        probe = 'import json,runpy; from pathlib import Path; entry=runpy.run_path("runtime_entry.py"); entry["verify_install"](Path.cwd(),Path("production_inventory.json"),"' + manifest['runtime_build_sha256'] + '"); from optimization.runtime.environment import attest; print(json.dumps(attest(Path.cwd(),smoke=True)))'
        environment = json.loads(run([local / 'venv/Scripts/python.exe', '-I', '-B', '-c', 'import sys; sys.path.insert(0,' + repr(str(local / 'runtime_STEP7_80694f51')) + '); ' + probe], local / 'runtime_STEP7_80694f51', receipts / 'runtime_environment.log'))
        if environment['status'] != 'ENVIRONMENT_READY' or environment['smoke_executed'] is not True or environment['cp_sat_smoke_status'] != 'OPTIMAL':
            raise ValueError('Native environment smoke is not certified')
        receipt['checks']['runtime_native_environment'] = environment
        sys.path.insert(0, str(project / 'backend/scripts'))
        from preflight_m3 import source_check, production_inventory
        receipt['checks']['current_source_pins'] = source_check(project)
        receipt['checks']['current_runtime_inventory'] = production_inventory(local / 'runtime_STEP7_80694f51')
        receipt['status'] = 'M3_OFFLINE_INSTALL_PASS'
    except Exception as error:
        receipt['status'] = 'M3_OFFLINE_INSTALL_FAIL'
        receipt['error_type'] = type(error).__name__
        raise
    finally:
        receipt['finished_at'] = datetime.now(timezone.utc).isoformat()
        (receipts / 'install.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': receipt['status'], 'local_root': str(local), 'receipt': str(receipts / 'install.json'), 'credentials_printed': False}))


if __name__ == '__main__':
    main()
