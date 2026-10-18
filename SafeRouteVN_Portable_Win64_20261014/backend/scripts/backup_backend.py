"""Private offline admin backup. Stop HTTP and worker before invoking."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
from backend.services.runtime_gateway import RuntimeGateway
from backend.services.settings import Settings
from backend.services.worker_lock import WorkerLock


async def backup(settings):
    lock = WorkerLock(settings.worker_lock_path)
    lock.acquire()
    directory = settings.metadata_path.parent / "backups" / (datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_") + uuid4().hex)
    try:
        directory.mkdir(parents=True, exist_ok=False)
        authority = directory / "authority.sqlite"
        receipt = await RuntimeGateway(settings).backup(authority)
        metadata = directory / "metadata.sqlite"
        if not settings.metadata_path.is_file():
            raise ValueError("M3 metadata is missing")
        with sqlite3.connect("file:" + settings.metadata_path.as_posix() + "?mode=ro", uri=True) as source, sqlite3.connect(metadata) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("M3 backup integrity failure")
        files = [{"name": p.name, "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in (authority, metadata)]
        manifest = {"schema_version": "saferoute-m3-private-backup/1", "status": "BACKUP_VERIFIED",
            "scope": "GLOBAL_AUTHORITY_AND_M3_METADATA; ADMIN_PRIVATE", "api_stopped_operator_attestation": True,
            "worker_exclusive_lock_held": True, "build_sha256": settings.installation()["expected_build_sha256"],
            "installation_sha256": settings.installation_identity(), "created_at": datetime.now(timezone.utc).isoformat(),
            "authority_receipt": receipt, "files": files,
            "restore_policy": "KEEP_ORIGINAL_EVIDENCE; FENCED_ADMIN_REVIEW_REQUIRED; NEVER_OVERWRITE_LIVE_AUTHORITY"}
        (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "BACKUP_VERIFIED", "private_directory": str(directory)}, ensure_ascii=True))
    except BaseException:
        # Preserve partial backup bytes for diagnosis. Never overwrite/recreate an authority.
        if directory.exists():
            (directory / "incomplete.json").write_text(json.dumps({"status": "INCOMPLETE", "recorded_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        raise
    finally:
        lock.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-stopped", action="store_true", required=True, help="Operator confirms HTTP API is fully stopped; worker lock alone cannot exclude HTTP writes")
    parser.parse_args()
    try:
        asyncio.run(backup(Settings.from_environment()))
    except Exception as error:
        # Typed diagnostic only; never emit credentials, raw SQLite or traceback.
        print(json.dumps({"status": "FAIL", "code": getattr(error, "code", "BACKUP_UNAVAILABLE")}))
        raise SystemExit(1)
