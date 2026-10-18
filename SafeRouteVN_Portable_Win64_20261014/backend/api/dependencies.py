from typing import Annotated
import asyncio
import time
import sqlite3

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.services.auth import Actor
from backend.services.driver_assignment import resolve_driver_assignment
from .errors import ApiError

bearer = HTTPBearer(auto_error=False, scheme_name="ServerBearer")


def current_actor(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "authentication", "Bearer token required")
    actor = request.app.state.auth.authenticate(credentials.credentials)
    actor = resolve_driver_assignment(actor, request.app.state.settings.metadata_path)
    request.state.actor_id = actor.actor_id
    return actor


def dispatcher(actor: Annotated[Actor, Depends(current_actor)]):
    if actor.role != "dispatcher":
        raise ApiError(403, "FORBIDDEN", "role", "Dispatcher role required")
    return actor


async def session_owner(session_id: str, request: Request, actor: Annotated[Actor, Depends(current_actor)]):
    try:
        owner = request.app.state.sessions.owner(session_id)
    except (sqlite3.Error, OSError) as error:
        raise ApiError(503, "METADATA_UNAVAILABLE", "session", "Session metadata is unavailable") from error
    if owner is None:
        raise ApiError(404, "SESSION_NOT_FOUND", "session_id", "Session not found")
    if not actor.can_access_session(session_id, owner):
        raise ApiError(403, "FORBIDDEN", "session_id", "Session access denied")
    if request.app.state.settings.deferred_session_recovery:
        deadline = time.monotonic() + 120
        recovery = request.app.state.recovery_repository
        while not recovery.request_verification(session_id):
            if time.monotonic() >= deadline:
                raise ApiError(503, "SESSION_RECOVERING", "recovery", "Saved session verification is still running; retry shortly")
            await asyncio.sleep(0.25)
    return actor


def session_operator(actor: Annotated[Actor, Depends(session_owner)]):
    if actor.role not in ('dispatcher', 'driver'):
        raise ApiError(403, 'FORBIDDEN', 'role', 'Dispatcher or assigned driver required')
    return actor
