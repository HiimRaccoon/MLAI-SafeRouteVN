import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from release_tools import relative, sha, verify_release, wheel_pins, extract_package


@pytest.mark.parametrize('path', ['../escape', '/absolute', 'D:/file', 'a\\b', 'a//b', 'a/./b', 'a/../b', ''])
def test_release_paths_cannot_escape_or_alias(path):
    with pytest.raises(ValueError):
        relative(path)


def release(tmp_path):
    file = tmp_path / 'backend/entry.py'
    file.parent.mkdir()
    file.write_text('original')
    manifest = {'schema_version': 'saferoute-m3-release/1', 'files': [{'path': 'backend/entry.py', 'bytes': file.stat().st_size, 'sha256': sha(file)}]}
    (tmp_path / 'release_manifest.json').write_text(json.dumps(manifest))
    return file, manifest


def test_release_detects_changed_same_length_bytes(tmp_path):
    file, _ = release(tmp_path)
    assert verify_release(tmp_path)['schema_version'] == 'saferoute-m3-release/1'
    file.write_text('changed!')
    with pytest.raises(ValueError, match='bytes changed'):
        verify_release(tmp_path)


def test_release_detects_unlisted_private_state(tmp_path):
    release(tmp_path)
    (tmp_path / 'auth_tokens.json').write_text('must not be included')
    with pytest.raises(ValueError, match='file set changed'):
        verify_release(tmp_path)


def test_release_detects_duplicate_manifest_entries(tmp_path):
    _, manifest = release(tmp_path)
    manifest['files'].append(manifest['files'][0])
    (tmp_path / 'release_manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Duplicate'):
        verify_release(tmp_path)


def test_package_rejects_traversal_before_creating_destination(tmp_path):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as zip_file:
        zip_file.writestr('../escape.txt', 'bad')
    destination = tmp_path / 'runtime'
    with pytest.raises(ValueError):
        extract_package(archive, destination, sha(archive), 1)
    assert not destination.exists()


def test_package_never_overwrites_existing_directory(tmp_path):
    archive = tmp_path / 'package.zip'
    with zipfile.ZipFile(archive, 'w') as zip_file:
        zip_file.writestr('entry.py', 'approved')
    destination = tmp_path / 'runtime'
    extract_package(archive, destination, sha(archive), 1)
    with pytest.raises(ValueError, match='already exists'):
        extract_package(archive, destination, sha(archive), 1)
    assert (destination / 'entry.py').read_text() == 'approved'


@pytest.mark.parametrize('version,extra,expected_error', [('2.0', False, 'identity'), ('1.0', True, 'identity'), ('1.0', False, None)])
def test_wheelhouse_requires_exact_unique_lock_members(tmp_path, version, extra, expected_error):
    lock = tmp_path / 'requirements.txt'
    lock.write_text('demo==1.0\n')
    wheel = tmp_path / 'demo.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('demo.dist-info/METADATA', f'Name: demo\nVersion: {version}\n')
    if extra:
        (tmp_path / 'duplicate.whl').write_bytes(wheel.read_bytes())
    if expected_error:
        with pytest.raises(ValueError, match=expected_error):
            wheel_pins(tmp_path, lock)
    else:
        assert wheel_pins(tmp_path, lock)[0]['version'] == '1.0'
