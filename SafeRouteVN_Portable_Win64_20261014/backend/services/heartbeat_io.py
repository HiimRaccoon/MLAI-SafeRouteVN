"""Bound transient Windows sharing violations without masking permanent I/O failures."""
import json
import os
from pathlib import Path
import time


def read_heartbeat_json(path):
    for attempt in range(5):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02)


def replace_heartbeat_file(source, destination):
    for attempt in range(5):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02)
