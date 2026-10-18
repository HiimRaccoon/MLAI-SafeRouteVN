"""Build a NEW offline backend kit; never include private state/auth or M1 DBs."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import zipfile
from release_tools import sha, wheel_pins, verify_release


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wheelhouse', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if output.exists() or output.with_suffix('.zip').exists() or output.is_relative_to(project):
        raise ValueError('New output outside project required')
    output.mkdir(parents=True, exist_ok=False)
    for file in sorted((project / 'backend').rglob('*')):
        public_mock_asset = file.parent == project / 'backend/mock/fixtures' and file.name in {
            'manifest.json', 'S2_execution_view.json', 'S3_execution_view.json', 'S4_execution_view.json'}
        if file.is_file() and '__pycache__' not in file.parts and (file.suffix in ('.py', '.txt', '.md') or public_mock_asset):
            target = output / file.relative_to(project)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
    for file in sorted((project / 'docs').glob('M3_*HANDOFF*.md')):
        target = output / 'docs' / file.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)
    for name in ('M3_OPERATIONS_RUNBOOK.md', 'M3_M4_E2E_CHECKLIST.md', 'M3_STEP8_CRITERIA_MATRIX.md', 'M3_OPENAPI_20261006.json'):
        file = project / 'docs' / name
        if not file.is_file():
            raise ValueError('Release handoff not yet present: ' + name)
        shutil.copyfile(file, output / 'docs' / name)
    packages = []
    for kind, name, digest, count in [
        ('runtime', 'SafeRouteVN_TASK02_Step7_Final_Release_Runtime_Windows_20261004.zip', 'd5345e75db2b41914cf4d0ed96a83e50e9c251637065ae1264eb1f33f41cbe4e', 149),
        ('integration', 'SafeRouteVN_TASK02_Step7_Final_Release_Integration_Windows_20261004.zip', 'd59917dded655398a04f3856d1ae73c658cc3dec3002a3467fb24e8d4a45b5e7', 182)]:
        file = project / 'docs' / name
        if sha(file) != digest:
            raise ValueError('Pinned package changed')
        target = output / 'packages' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)
        packages.append({'kind': kind, 'path': target.relative_to(output).as_posix(), 'sha256': digest, 'entries': count})
    wheelhouses = {}
    for kind, lock in [('backend', project / 'backend/requirements-backend.lock.txt'), ('runtime', project / 'requirements-runtime.lock.txt')]:
        rows = wheel_pins(args.wheelhouse / kind, lock)
        destination = output / 'wheelhouse' / kind
        destination.mkdir(parents=True)
        for row in rows:
            shutil.copyfile(args.wheelhouse / kind / row['file'], destination / row['file'])
        (destination / 'requirements.hashed.txt').write_text(''.join(f"{row['name']}=={row['version']} --hash=sha256:{row['sha256']}\n" for row in rows), encoding='utf-8')
        wheelhouses[kind] = rows
    manifest = {'schema_version': 'saferoute-m3-release/1', 'status': 'BACKEND_RELEASE_CANDIDATE',
        'created_at': datetime.now(timezone.utc).isoformat(), 'app_version': '0.8.0', 'operations_revision': 'M3-COMPLETE/1',
        'runtime_build_sha256': '80694f511dc735d0b6a1a0a830edd7f6267df395e87dad0ccf180914b49d5a41',
        'platform': 'Windows CPython 3.12 x64', 'packages': packages, 'wheelhouses': wheelhouses,
        'prerequisites': ['Approved CPython 3.12 x64', 'Verified team tree with frozen M1 snapshots/M2 upload map', 'Existing COMPLETE_VERIFIED G0 receipt for that tree'],
        'excluded': ['Bearer tokens', 'Installation paths/configuration', 'Authority and M3 metadata/receipts', 'M1 database copies', 'Python executable/venvs', 'Frontend source'],
        'limitations': ['FRONTEND_E2E_PENDING_M4', 'E4_NOT_RUN', 'GENERAL_M1_NOT_VALIDATED', 'PRODUCTION_CALIBRATION_UNCONFIGURED', 'PERFORMANCE_NOT_MET', 'SIMULATED_REPLAY_NOT_GPS', 'EXPOSURE_IS_PROXY', 'NOT_OPTIMALITY'],
        'files': []}
    for file in sorted(output.rglob('*')):
        if file.is_file():
            manifest['files'].append({'path': file.relative_to(output).as_posix(), 'bytes': file.stat().st_size, 'sha256': sha(file)})
    (output / 'release_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    verify_release(output)
    archive = output.with_suffix('.zip')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as zip_file:
        for file in sorted(output.rglob('*')):
            if file.is_file():
                zip_file.write(file, file.relative_to(output).as_posix())
    print(json.dumps({'status': 'BACKEND_RELEASE_CANDIDATE', 'archive': str(archive), 'sha256': sha(archive), 'bytes': archive.stat().st_size, 'files': len(manifest['files'])}))


if __name__ == '__main__':
    main()
