import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


@pytest.fixture
def portable_modules(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import portable_bootstrap
    import build_portable
    return portable_bootstrap, build_portable


def test_relocation_and_new_package_get_separate_state(portable_modules, tmp_path):
    bootstrap, _ = portable_modules
    assert bootstrap.local_path(tmp_path / 'one', 'a') != bootstrap.local_path(tmp_path / 'two', 'a')
    assert bootstrap.local_path(tmp_path / 'one', 'a') != bootstrap.local_path(tmp_path / 'one', 'b')
    assert bootstrap.local_path(tmp_path / 'one', 'a') == bootstrap.local_path(tmp_path / 'one', 'a')


def test_resealed_build_retains_one_matching_installation(portable_modules, tmp_path):
    bootstrap, _ = portable_modules
    project = tmp_path / 'app'
    local = project / 'portable-state' / 'existing'
    local.mkdir(parents=True)
    (local / 'installation.json').write_text(json.dumps({'snapshot_root': str(project.resolve())}))
    assert bootstrap.local_path(project, 'old-manifest') == local
    assert bootstrap.local_path(project, 'new-manifest') == local
    # A relocated folder still creates its own installation.
    (local / 'installation.json').write_text(json.dumps({'snapshot_root': str(tmp_path / 'other-app')}))
    assert bootstrap.local_path(project, 'new-manifest') != local


def test_package_verification_rejects_missing_corrupt_and_escaping_files(portable_modules, tmp_path):
    bootstrap, _ = portable_modules
    file = tmp_path / 'asset'
    file.write_bytes(b'valid')
    manifest = {'schema_version': 'saferoute-portable/1', 'files': {'asset': {'bytes': 5, 'sha256': bootstrap.digest(file)}}}
    bootstrap.verify_package(tmp_path, manifest)
    file.write_bytes(b'wrong')
    with pytest.raises(ValueError, match='missing or changed'):
        bootstrap.verify_package(tmp_path, manifest)
    file.unlink()
    with pytest.raises(ValueError, match='missing or changed'):
        bootstrap.verify_package(tmp_path, manifest)
    manifest['files'] = {'../escape': {}}
    with pytest.raises(ValueError, match='Unsafe'):
        bootstrap.verify_package(tmp_path, manifest)


def test_archive_excludes_session_credentials_and_vite_cache(portable_modules, tmp_path):
    _, build = portable_modules
    stage = tmp_path / 'app'
    (stage / 'portable').mkdir(parents=True)
    (stage / 'production_inventory.json').write_text(json.dumps({'build_sha256': 'a' * 64}))
    (stage / 'portable-state').mkdir()
    (stage / 'portable-state/dev_access.json').write_text('PRIVATE_TOKEN')
    (stage / '.vite').mkdir()
    (stage / '.vite/cache.js').write_text('machine cache')
    manifest = build.seal(stage)
    assert 'portable-state/dev_access.json' not in manifest['files']
    assert '.vite/cache.js' not in manifest['files']
    target = tmp_path / 'app.zip'
    build.archive(stage, target)
    with zipfile.ZipFile(target) as archive:
        assert set(archive.namelist()) == {'app/production_inventory.json', 'app/portable/manifest.json'}
