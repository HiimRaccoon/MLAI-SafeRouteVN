"""Resolve driver grants through committed, owner-authorized session replacements."""
import json
import sqlite3
from dataclasses import replace
from contextlib import closing

from backend.api.errors import ApiError


def resolve_driver_assignment(actor, metadata_path):
    if actor.role != 'driver':
        return actor
    try:
        with closing(sqlite3.connect(f'{metadata_path.as_uri()}?mode=ro', uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            root = db.execute('SELECT * FROM sessions WHERE session_id=?', (actor.session_id,)).fetchone()
            if root is None:
                raise ApiError(404, 'SESSION_NOT_FOUND', 'assignment', 'Assigned session no longer exists; ask the dispatcher to reassign this account')
            current, visited = actor.session_id, set()
            while current not in visited:
                visited.add(current)
                replacement = db.execute('SELECT * FROM driver_session_successors WHERE source_session_id=?', (current,)).fetchone()
                if replacement is not None:
                    target = db.execute('SELECT * FROM sessions WHERE session_id=?', (replacement['target_session_id'],)).fetchone()
                    if (not target or target['status'] != 'READY'
                            or any(row['owner_actor_id'] != root['owner_actor_id']
                                   or row['installation_sha256'] != root['installation_sha256']
                                   for row in (replacement, target))):
                        raise ValueError('Invalid scenario assignment')
                    current = target['session_id']
                    continue
                row = db.execute("""SELECT response_json FROM replay_requests
                    WHERE session_id=? AND actor_id=? AND installation_sha256=?
                    AND operation='reset' AND response_json IS NOT NULL AND error_code IS NULL
                    ORDER BY rowid DESC LIMIT 1""",
                    (current, root['owner_actor_id'], root['installation_sha256'])).fetchone()
                if row is None:
                    return replace(actor, session_id=current)
                receipt = json.loads(row['response_json'])
                destination = receipt.get('new_session_id')
                target = db.execute('SELECT * FROM sessions WHERE session_id=?', (destination,)).fetchone()
                if (receipt.get('operation') != 'reset' or receipt.get('status') != 'RESET'
                        or receipt.get('session_id') != current or not target
                        or target['owner_actor_id'] != root['owner_actor_id']
                        or target['installation_sha256'] != root['installation_sha256']
                        or target['status'] != 'READY'):
                    raise ValueError('Invalid reset assignment')
                current = destination
            raise ValueError('Cyclic session lineage')
    except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError) as error:
        raise ApiError(503, 'ASSIGNMENT_UNAVAILABLE', 'assignment', 'Cannot verify the driver assignment; retry shortly') from error
