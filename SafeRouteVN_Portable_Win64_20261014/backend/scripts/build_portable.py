"""Build a self-contained Windows x64 ZIP; recipient never runs pip/npm.

Build host needs Python 3.12, installed locked frontend dependencies, a Node
binary and the official embedded Python ZIP. No private installations copied.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zipfile
from xml.etree import ElementTree

from release_tools import relative, verify_release, wheel_pins

PYTHON_SHA256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
VC_REDIST_SHA256 = 'cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b'


def extract_crt(redist, cache):
    """Extract Microsoft's signed redistributable; never run its installer."""
    if sha(redist) != VC_REDIST_SHA256:
        raise ValueError('Expected Microsoft VC++ 14.44.35211 x64 redistributable')
    cache.mkdir(parents=True, exist_ok=False)
    payload = redist.read_bytes()
    start = 0
    while True:
        at = payload.find(b'MSCF', start)
        if at < 0:
            break
        size = struct.unpack_from('<I', payload, at + 8)[0]
        if 36 < size <= len(payload) - at:
            cabinet = cache / f'{at}.cab'
            cabinet.write_bytes(payload[at:at + size])
            subprocess.run(['expand.exe', str(cabinet), '-F:*', str(cache)], check=True, stdout=subprocess.DEVNULL)
        start = at + 4
    manifest = ElementTree.fromstring((cache / '0').read_bytes())
    entries = {e.attrib.get('FilePath'): e.attrib.get('SourcePath') for e in manifest.iter() if e.tag.endswith('Payload')}
    cabinet = cache / entries['packages\\vcRuntimeMinimum_amd64\\cab1.cab']
    dlls = cache / 'dlls'
    dlls.mkdir()
    subprocess.run(['expand.exe', str(cabinet), '-F:*', str(dlls)], check=True, stdout=subprocess.DEVNULL)
    shutil.copy2(cache / entries['license.rtf'], dlls / 'Microsoft-VC-License.rtf')
    return dlls


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def copy_tree(source, target):
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(
        '__pycache__', '.pytest_cache', '.git', '.env', '.env.local', '.vite', '.vite-temp', '*.pyc', '*-wal', '*-shm', '*-journal'))


def seal(stage):
    manifest_file = stage / 'portable/manifest.json'
    files = {}
    for path in sorted(stage.rglob('*')):
        rel = path.relative_to(stage)
        if not path.is_file() or path == manifest_file or rel.parts[0] == 'portable-state':
            continue
        if '__pycache__' in rel.parts or '.vite' in rel.parts or '.vite-temp' in rel.parts or path.suffix == '.pyc':
            continue
        files[rel.as_posix()] = {'bytes': path.stat().st_size, 'sha256': sha(path)}
    record = json.loads((stage / 'production_inventory.json').read_bytes())
    manifest = {'schema_version': 'saferoute-portable/1', 'platform': 'Windows x64',
                'build_sha256': record['build_sha256'], 'python_archive_sha256': PYTHON_SHA256,
                'vc_redist_sha256': VC_REDIST_SHA256,
                'files': files}
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def archive(stage, output):
    # Never package generated credentials, sessions or first-run receipts.
    manifest = json.loads((stage / 'portable/manifest.json').read_bytes())
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name in [*manifest['files'], 'portable/manifest.json']:
            z.write(stage / name, stage.name + '/' + name)
    output.with_suffix('.zip.sha256').write_text(sha(output) + '  ' + output.name + '\n', encoding='ascii')
    print(f'ZIP_READY: {output} ({output.stat().st_size / 1024**2:.1f} MiB)', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', required=True, type=Path)
    parser.add_argument('--python-embed', type=Path)
    parser.add_argument('--node', type=Path)
    parser.add_argument('--vc-redist', type=Path)
    parser.add_argument('--zip', type=Path)
    parser.add_argument('--seal-only', action='store_true')
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    stage = args.stage.resolve()
    if stage.is_relative_to(project) or project.is_relative_to(stage):
        raise ValueError('Build into a separate sibling directory')
    if not args.seal_only:
        if not args.python_embed or sha(args.python_embed) != PYTHON_SHA256:
            raise ValueError('Expected official Python 3.12.10 embed-amd64 ZIP')
        if not args.vc_redist or sha(args.vc_redist) != VC_REDIST_SHA256:
            raise ValueError('Expected pinned Microsoft Visual C++ redistributable')
        node = args.node or Path(shutil.which('node') or '')
        if not node.is_file() or not (project / 'frontend/node_modules/vite/bin/vite.js').is_file():
            raise ValueError('Build host requires Node and npm ci in frontend')
        stage.mkdir(parents=True, exist_ok=False)
        for folder in ('backend', 'optimization', 'configs', 'shared', 'geo_data'):
            copy_tree(project / folder, stage / folder)
        (stage / 'scenarios/cached_context/hcmc').mkdir(parents=True)
        for folder in ('fixtures', 'manifests'):
            copy_tree(project / 'scenarios' / folder, stage / 'scenarios' / folder)
        copy_tree(project / 'scenarios/cached_context/hcmc/member1-tdbt-v1',
                  stage / 'scenarios/cached_context/hcmc/member1-tdbt-v1')
        for file in ('runtime_entry.py', 'requirements-runtime.lock.txt', 'CHAY_SAFEROUTE.bat', 'DON_PORT_SAFEROUTE.bat'):
            shutil.copy2(project / file, stage / file)
        for folder in ('src', 'public', 'scripts', 'node_modules'):
            if (project / 'frontend' / folder).exists():
                copy_tree(project / 'frontend' / folder, stage / 'frontend' / folder)
        for file in ('package.json', 'package-lock.json', 'index.html', 'vite.config.ts', 'tsconfig.json', 'tsconfig.app.json', 'tsconfig.node.json'):
            shutil.copy2(project / 'frontend' / file, stage / 'frontend' / file)
        (stage / 'portable/node').mkdir(parents=True)
        shutil.copy2(node, stage / 'portable/node/node.exe')
        shutil.copy2(node.parent / 'LICENSE', stage / 'portable/node/LICENSE')
        cache = stage.parent / (stage.name + '-build-only')
        cache.mkdir(exist_ok=False)
        with zipfile.ZipFile(project / 'releases/M3_backend_0.8.0_20261006_113056.zip') as z:
            for item in z.infolist():
                relative(item.filename.rstrip('/'))
            z.extractall(cache)
        release = verify_release(cache)
        crt = extract_crt(args.vc_redist, cache / 'crt')
        for kind, lock in [('backend', project / 'backend/requirements-backend.lock.txt'), ('runtime', project / 'requirements-runtime.lock.txt')]:
            print('Bundling Python + ' + kind + ' dependencies...', flush=True)
            wheels = cache / 'wheelhouse' / kind
            if wheel_pins(wheels, lock) != release['wheelhouses'][kind]:
                raise ValueError('Wheel identity differs: ' + kind)
            target = stage / ('portable/python-' + kind)
            with zipfile.ZipFile(args.python_embed) as z:
                z.extractall(target)
            for dll in crt.glob('*.dll_amd64'):
                shutil.copy2(dll, target / dll.name.removesuffix('_amd64'))
            shutil.copy2(crt / 'Microsoft-VC-License.rtf', target / 'Microsoft-VC-License.rtf')
            # Relative paths survive extraction to another drive/user/folder.
            (target / 'python312._pth').write_text('python312.zip\n.\nLib/site-packages\n../../\n../../backend/scripts\nimport site\n', encoding='ascii')
            subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-index', '--no-deps', '--no-compile', '--require-hashes',
                            '--find-links', str(wheels), '-r', str(wheels / 'requirements.hashed.txt'),
                            '--target', str(target / 'Lib/site-packages')], check=True, stdout=subprocess.DEVNULL)
        sys.path.insert(0, str(project))
        from optimization.runtime.build import inventory
        (stage / 'production_inventory.json').write_text(json.dumps(inventory(stage), indent=2), encoding='utf-8')
        (stage / 'HUONG_DAN.txt').write_text((project / 'docs/PORTABLE_README.txt').read_text(encoding='utf-8'), encoding='utf-8-sig')
    print('Sealing package inventory...', flush=True)
    seal(stage)
    if args.zip:
        archive(stage, args.zip.resolve())
    else:
        print('STAGE_READY: ' + str(stage), flush=True)


if __name__ == '__main__':
    main()
