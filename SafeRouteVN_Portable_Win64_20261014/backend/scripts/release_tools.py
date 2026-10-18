"""Standard-library-only sealed release and wheel verification."""
import hashlib
from email.parser import BytesParser
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def relative(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or '..' in path.parts or '\\' in name
            or ':' in name or str(path) != name):
        raise ValueError('Unsafe release path')
    return path


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def wheel_pins(folder, lock):
    expected = {}
    for line in Path(lock).read_text(encoding='utf-8').splitlines():
        if line.strip() and not line.startswith('#'):
            name, version = line.strip().split('==')
            if normalized(name) in expected:
                raise ValueError('Duplicate dependency pin')
            expected[normalized(name)] = version
    rows = []
    actual = {}
    for file in sorted(Path(folder).glob('*.whl')):
        with zipfile.ZipFile(file) as wheel:
            entries = wheel.infolist()
            names = [row.filename for row in entries]
            if len(names) != len(set(names)) or wheel.testzip() is not None:
                raise ValueError('Invalid wheel archive')
            for row in entries:
                relative(row.filename.rstrip('/'))
                if stat.S_ISLNK(row.external_attr >> 16):
                    raise ValueError('Wheel symlink')
            metadata_names = [name for name in names if name.endswith('.dist-info/METADATA')]
            if len(metadata_names) != 1:
                raise ValueError('Ambiguous wheel metadata')
            metadata = BytesParser().parsebytes(wheel.read(metadata_names[0]))
            name, version = normalized(metadata['Name']), metadata['Version']
        if name in actual or expected.get(name) != version:
            raise ValueError('Wheel identity differs from lock')
        actual[name] = version
        rows.append({'name': name, 'version': version, 'file': file.name,
            'bytes': file.stat().st_size, 'sha256': sha(file)})
    if actual != expected:
        raise ValueError('Wheelhouse is incomplete')
    return rows


def verify_release(root):
    root = Path(root).resolve(strict=True)
    manifest = json.loads((root / 'release_manifest.json').read_bytes())
    if manifest.get('schema_version') != 'saferoute-m3-release/1':
        raise ValueError('Release schema')
    names = set()
    for row in manifest['files']:
        name = str(relative(row['path']))
        if name in names:
            raise ValueError('Duplicate manifest file')
        names.add(name)
        target = root / name
        if target.is_symlink() or not target.resolve(strict=True).is_relative_to(root):
            raise ValueError('Release escaped root')
        if target.stat().st_size != row['bytes'] or sha(target) != row['sha256']:
            raise ValueError('Release bytes changed')
    actual = {file.relative_to(root).as_posix() for file in root.rglob('*') if file.is_file()}
    if actual != names | {'release_manifest.json'}:
        raise ValueError('Release file set changed')
    return manifest


def extract_package(archive_path, destination, expected_sha, expected_count):
    if sha(archive_path) != expected_sha:
        raise ValueError('Runtime package digest changed')
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Package destination already exists')
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != expected_count or len(names) != len(set(names)) or archive.testzip() is not None:
            raise ValueError('Package entry count/CRC differs')
        for entry in entries:
            relative(entry.filename)
            if entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('Unsafe runtime package entry')
        destination.mkdir(parents=True, exist_ok=False)
        for entry in entries:
            target = destination / entry.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(entry))
    return destination
