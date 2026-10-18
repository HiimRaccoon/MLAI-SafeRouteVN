import json
from pathlib import Path
import sys
from backend.scripts.portable_bootstrap import digest, local_path, prepare, verify_package

root = Path(sys.argv[1]).resolve()
manifest_file = root / 'portable/manifest.json'
manifest = json.loads(manifest_file.read_bytes())
verify_package(root, manifest)
local = local_path(root, digest(manifest_file))
if not (local / 'installation.json').is_file():
    local.mkdir(parents=True, exist_ok=True)
    prepare(root, local, manifest)
print('INSTALL_OK')
