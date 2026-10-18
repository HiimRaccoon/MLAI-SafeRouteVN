"""Create an isolated LOCAL DEVELOPMENT installation; preserve sealed releases.

Two phases: --prepare installs verified offline wheels; --finalize snapshots
current runtime source with a new build digest and performs real native G0 checks.
Never rewrites a receipt or upgrades an existing authority.
"""
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
import zipfile

from release_tools import relative, sha, verify_release, wheel_pins


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--local-root', type=Path, required=True)
    p.add_argument('--prepare', action='store_true')
    p.add_argument('--finalize', action='store_true')
    args = p.parse_args()
    project = Path(__file__).resolve().parents[2]
    local = args.local_root.resolve()
    if local.is_relative_to(project) or project.is_relative_to(local):
        raise ValueError('Private sibling directory required')
    if args.prepare == args.finalize:
        p.error('Select one phase')

    def run(argv, name, cwd=project, timeout=300):
        env = {k: v for k, v in os.environ.items() if k.upper() not in ('PYTHONPATH', 'PYTHONHOME')}
        env.update(PYTHONDONTWRITEBYTECODE='1', PIP_NO_INDEX='1')
        result = subprocess.run([str(a) for a in argv], cwd=cwd, env=env, capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        (local / 'receipts' / (name + '.log')).write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError('Check failed; see receipts/' + name + '.log')
        print(name + ': PASS', flush=True)
        return result.stdout

    if args.prepare:
        local.mkdir(parents=True, exist_ok=False)
        (local / 'receipts').mkdir()
        write(local / 'local_source.json', {'source': str(project), 'scope': 'LOCAL_DEVELOPMENT'})
        kit = local / 'kit'
        with zipfile.ZipFile(project / 'releases/M3_backend_0.8.0_20261006_113056.zip') as archive:
            for entry in archive.infolist():
                name = relative(entry.filename)
                if entry.is_dir():
                    continue
                target = kit / str(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(entry))
        manifest = verify_release(kit)
        for kind, folder, lock in [('backend','backend-venv',project/'backend/requirements-backend.lock.txt'), ('runtime','venv',project/'requirements-runtime.lock.txt')]:
            if wheel_pins(kit/'wheelhouse'/kind,lock) != manifest['wheelhouses'][kind]:
                raise ValueError('Wheel identity mismatch')
            run([sys.executable,'-m','venv',local/folder],kind+'_venv')
            python=local/folder/'Scripts/python.exe'
            run([python,'-m','pip','install','--no-index','--no-deps','--require-hashes','--find-links',kit/'wheelhouse'/kind,'-r',kit/'wheelhouse'/kind/'requirements.hashed.txt'],kind+'_install')
            run([python,'-m','pip','check'],kind+'_dependencies')
        return

    if json.loads((local/'local_source.json').read_bytes())['source'] != str(project) or (local/'installation.json').exists():
        raise ValueError('Matching fresh prepared directory required; existing installations are immutable')
    sys.path.insert(0,str(project))
    from optimization.runtime.build import inventory
    from preflight_m3 import source_check
    source=source_check(project)
    record=inventory(project)
    runtime=local/'runtime'
    runtime.mkdir(exist_ok=False)
    for name in record['files']:
        target=runtime/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(project/name,target)
    write(runtime/'production_inventory.json',record)
    runtime_python=local/'venv/Scripts/python.exe'
    probe = '''import json,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
from optimization.runtime.environment import attest
from optimization.runtime.sdk import RuntimeClient
from optimization.runtime.build import verify
record=json.loads(Path('production_inventory.json').read_bytes())
verify(Path.cwd(),record,record['build_sha256'])
environment=attest(Path.cwd(),smoke=True)
client=RuntimeClient(snapshot_root=sys.argv[1],store_path=sys.argv[2],expected_build_sha256=record['build_sha256'])
boot=client.command('bootstrap','local-g0-bootstrap','local-g0',scenario_id='S0')
assert boot['status']=='OK',boot
resolved=client.command('resolve','local-g0-resolve','local-g0')
assert resolved['status']=='OK',resolved
assert len(resolved['value']['order_ids'])==3
validated=client.validate_session('local-g0')
print(json.dumps({'environment':environment,'bootstrap':boot,'resolve':resolved,'validation':validated}))
'''
    result=json.loads(run([runtime_python,'-I','-B','-c',probe,project,local/'receipts/g0_authority.sqlite'],'native_g0',runtime,600))
    write(local/'receipts/native_g0.json',result)
    validation=result['validation']
    if validation.get('valid') is not True:
        raise ValueError('Native session validation did not pass')
    receipt={'status':'G0_TECHNICAL_PASS','step1_input_completeness':'COMPLETE_VERIFIED','build_sha256':record['build_sha256'],
             'snapshot_root':str(project),'scope':'LOCAL_DEVELOPMENT_NOT_PRODUCTION_APPROVAL','source_check':source,
             'native_g0_receipt_sha256':sha(local/'receipts/native_g0.json'),'created_at':datetime.now(timezone.utc).isoformat()}
    write(local/'receipts/preflight.json',receipt)
    (local/'state').mkdir()
    token=secrets.token_urlsafe(36)
    write(local/'backend/dev_access.json',{'credentials':[{'actor_id':'local-dispatcher','role':'dispatcher','token':token}]})
    write(local/'backend/auth_tokens.json',{'schema_version':'saferoute-m3-auth/1','tokens':[{'actor_id':'local-dispatcher','role':'dispatcher','token_sha256':hashlib.sha256(token.encode()).hexdigest()}]})
    write(local/'installation.json',{'schema_version':'saferoute-m3-server-installation/1','runtime_root':str(runtime),'runtime_python':str(runtime_python),
          'snapshot_root':str(project),'expected_build_sha256':record['build_sha256'],'authority_store_parent':str(local/'state'),
          'latest_preflight_receipt':str(local/'receipts/preflight.json'),'technical_preflight_status':'G0_TECHNICAL_PASS','deployment_approved':False})
    print('LOCAL_INSTALL_PASS; credentials saved privately, not printed')


if __name__ == '__main__':
    main()
