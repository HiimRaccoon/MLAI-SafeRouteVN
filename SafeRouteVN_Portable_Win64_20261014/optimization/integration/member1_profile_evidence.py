"""Native-process evidence capture for the Windows Step-3 acceptance gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
from time import monotonic
from typing import Any, Sequence

EVIDENCE_VERSION = "task02-m1-profile-command-evidence/2"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_command(kind: str, argv: Sequence[str], cwd: str | Path,
                    output_dir: str | Path, *, environment_blocked_on_failure: bool = False,
                    env: dict[str, str] | None = None) -> dict[str, Any]:
    if not kind or not argv or any(not isinstance(item, str) or not item for item in argv):
        raise ValueError("kind and argv must be nonempty strings")
    working = Path(cwd).resolve()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    started = monotonic()
    completed = subprocess.run(list(argv), cwd=working, env=env or os.environ.copy(),
                               capture_output=True, check=False)
    duration = monotonic() - started
    stdout_path = destination / f"{kind}.stdout.log"
    stderr_path = destination / f"{kind}.stderr.log"
    log_path = destination / f"{kind}.log"
    stdout_path.write_bytes(completed.stdout)
    stderr_path.write_bytes(completed.stderr)
    transcript = (f"command_json: {json.dumps(list(argv), ensure_ascii=False)}\n"
                  f"cwd: {working}\nplatform: {platform.platform()}\n"
                  f"exit_code: {completed.returncode}\n"
                  f"status: {'PASS' if completed.returncode == 0 else ('ENVIRONMENT_BLOCKED' if environment_blocked_on_failure else 'FAIL')}\n"
                  f"duration_seconds: {duration:.9f}\n--- stdout ---\n").encode("utf-8")
    transcript += completed.stdout + b"\n--- stderr ---\n" + completed.stderr
    log_path.write_bytes(transcript)
    return {
        "kind": kind,
        "status": ("PASS" if completed.returncode == 0
                   else "BLOCKED" if environment_blocked_on_failure else "FAIL"),
        "exit_code": completed.returncode, "argv": list(argv),
        "interpreter": str(Path(argv[0]).resolve()), "cwd": str(working),
        "platform": platform.platform(), "duration_seconds": duration,
        "stdout_path": str(stdout_path), "stdout_bytes": stdout_path.stat().st_size,
        "stdout_sha256": _sha(stdout_path),
        "stderr_path": str(stderr_path), "stderr_bytes": stderr_path.stat().st_size,
        "stderr_sha256": _sha(stderr_path),
        "log_path": str(log_path), "log_bytes": log_path.stat().st_size,
        "log_sha256": _sha(log_path),
    }


def merge_evidence(record_files: Sequence[str | Path], output: str | Path) -> Path:
    records: list[dict[str, Any]] = []
    for source in record_files:
        value = json.loads(Path(source).read_bytes())
        items = value.get("records") if isinstance(value, dict) else None
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError(f"evidence record file is invalid: {source}")
        records.append(items[0])
    if len({item.get("kind") for item in records}) != len(records):
        raise ValueError("evidence kinds must be unique")
    destination = Path(output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({"schema_version": EVIDENCE_VERSION,
                                       "records": records}, ensure_ascii=False,
                                      sort_keys=True, indent=2, allow_nan=False) + "\n",
                           encoding="utf-8")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--record-output", required=True, type=Path)
    parser.add_argument("--environment-blocked-on-failure", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    record = capture_command(args.kind, command, args.cwd, args.output_dir,
                             environment_blocked_on_failure=args.environment_blocked_on_failure)
    value = {"schema_version": EVIDENCE_VERSION, "records": [record]}
    args.record_output.parent.mkdir(parents=True, exist_ok=True)
    args.record_output.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                             indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False))
    return 0 if record["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["EVIDENCE_VERSION", "capture_command", "merge_evidence"]
