"""Timestamped native-command receipts for the Step-4 Windows acceptance.

This wraps the existing Step-3 byte-preserving capture helper without changing
its frozen implementation or interpreting command stdout as a certificate.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from optimization.integration.member1_profile_evidence import capture_command

EVIDENCE_VERSION = "task02-m1-motion-native-command-evidence/1"


def capture(kind: str, command: list[str], cwd: Path, output_dir: Path,
            record_output: Path) -> dict:
    started = datetime.now(timezone.utc).isoformat()
    record = capture_command(kind, command, cwd, output_dir)
    ended = datetime.now(timezone.utc).isoformat()
    record["started_at_utc"] = started
    record["ended_at_utc"] = ended
    envelope = {"schema_version": EVIDENCE_VERSION, "records": [record]}
    record_output.parent.mkdir(parents=True, exist_ok=True)
    record_output.write_text(json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                                        indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--record-output", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    record = capture(args.kind, command, args.cwd, args.output_dir, args.record_output)
    print(json.dumps(record, ensure_ascii=False))
    return 0 if record["exit_code"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
