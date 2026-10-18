"""Versioned demo fixtures use isolated, authenticated native run sources."""
import asyncio
import hashlib
import json
import sqlite3
from types import SimpleNamespace

from backend.api.errors import ApiError
from .scenario_import import import_scenario


async def load_demo_variant(service, scenario_id, request_id, actor, *, replace_session_id=None):
    if scenario_id != 'S4':
        return None
    path = service.settings.project_root / 'scenarios/demo_variants/S4.json'
    if not path.is_file():
        return None
    # An existing request must retain its exact original source, even after a
    # demo release changes. Incomplete reservations follow the same rule.
    try:
        with service.repository._db() as db:
            previous = db.execute('''SELECT s.run_id,s.scenario_id FROM load_requests l
                JOIN sessions s ON s.session_id=l.session_id
                WHERE l.actor_id=? AND l.request_id=?''', (actor.actor_id, request_id)).fetchone()
    except (sqlite3.Error, OSError) as error:
        raise ApiError(503, 'METADATA_UNAVAILABLE', 'session', 'Session metadata is unavailable; retry the same request ID') from error
    if previous is not None:
        if previous['scenario_id'] != scenario_id:
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', 'request_id', 'Request ID was already used with another scenario')
        return (await service.load_run(previous['run_id'], request_id, actor, replace_session_id=replace_session_id)
                if previous['run_id'] else None)
    try:
        raw = path.read_bytes()
        fixture = json.loads(raw)
        if fixture.get('scenarioId') != scenario_id:
            raise ValueError('Demo fixture scenario differs')
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ApiError(503, 'DEMO_SCENARIO_INVALID', 'scenario', 'Bundled demo scenario is unavailable') from error
    body = SimpleNamespace(request_id='demo-s4-'+hashlib.sha256(raw).hexdigest(),
        name='S4 · Mưa hành lang cầu Sài Gòn', fixture=fixture)
    # The normal import pipeline checks graph nodes, source pins, event data
    # and ownership. Frozen benchmark fixtures and old sessions stay intact.
    prepared = await asyncio.to_thread(import_scenario, service.runs, body, actor)
    return await service.load_run(prepared['run_id'], request_id, actor, replace_session_id=replace_session_id)
