"""M3 entry for the pinned M2 compute child during offline rehearsal only."""
import argparse
import json
from pathlib import Path
import runpy
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installation', required=True, type=Path)
    args, rest = parser.parse_known_args()
    guard = runpy.run_path(str(Path(__file__).with_name('offline_guard.py')))
    if not guard['install_from_environment']('sdk-native-worker', deny_children=True):
        raise ValueError('Offline worker entry requires an active policy')
    config = json.loads(args.installation.read_bytes())
    root = Path(config['runtime_root']).resolve(strict=True)
    entry = runpy.run_path(str(root / 'runtime_entry.py'))
    entry['verify_install'](root, root / 'production_inventory.json', config['expected_build_sha256'])
    sys.path.insert(0, str(root))
    sys.argv = ['optimization.runtime.worker_v2', *rest]
    runpy.run_module('optimization.runtime.worker_v2', run_name='__main__')


if __name__ == '__main__':
    main()
