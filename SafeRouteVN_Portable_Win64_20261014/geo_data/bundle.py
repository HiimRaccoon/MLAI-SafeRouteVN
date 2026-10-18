"""Checksummed, immutable stage outputs and small validation helpers."""

from contextlib import contextmanager
from datetime import datetime
import math
import os
from pathlib import Path
import sqlite3

from geo_data.common import digest, now_vn, read_json, write_json
from geo_data.osm.extract_download import file_hash
from geo_data.units import timestamp_vn


def positive(value, name, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or (value < 0 if zero else value <= 0):
        raise ValueError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")
    return value


def instant(value):
    return datetime.fromisoformat(timestamp_vn(value))


@contextmanager
def database(path, *, readonly=True):
    path = Path(path)
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) if readonly else sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        yield db
        if not readonly:
            db.commit()
    finally:
        db.close()


def verify(directory, schema=None):
    directory = Path(directory)
    manifest = read_json(directory / "manifest.json")
    if manifest.get("complete") is not True or not manifest.get("files"):
        raise ValueError(f"Incomplete bundle: {directory}")
    if schema and manifest.get("schemaVersion") != schema:
        raise ValueError(f"Expected {schema}: {directory}")
    for name, meta in manifest["files"].items():
        path = directory / name
        if not path.resolve().is_relative_to(directory.resolve()) or not path.is_file() or file_hash(path) != meta["sha256"]:
            raise ValueError(f"Bundle checksum failed: {path}")
    return manifest


def cached(output, identity):
    output = Path(output)
    if (output / "manifest.json").exists():
        manifest = verify(output)
        if manifest.get("identity") != identity:
            raise ValueError("Output belongs to different inputs/config; choose a new output directory")
        return manifest
    state = output / "build-state.json"
    if state.exists() and read_json(state) != identity:
        raise ValueError("Partial output belongs to different inputs/config; choose a new directory")
    write_json(state, identity)
    return None


def temporary(output, name):
    path = Path(output) / ("partial-" + name)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def reset_database(output, name):
    """Restart this stage's private temporary DB, including crash journals."""
    path = temporary(output, name)
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return path


def publish(output, identity, schema, names, *, retained=(), **metadata):
    output = Path(output)
    files = {name: {"sha256": file_hash(temporary(output, name)), "bytes": temporary(output, name).stat().st_size} for name in names}
    files.update({name: {"sha256": file_hash(output / name), "bytes": (output / name).stat().st_size} for name in retained})
    for name in names:
        os.replace(temporary(output, name), output / name)
    manifest = {"schemaVersion": schema, "identity": identity, "version": digest(identity),
                "complete": True, "createdAt": now_vn(), "files": files, **metadata}
    write_json(output / "manifest.json", manifest)
    return manifest
