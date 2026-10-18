from typing import Annotated

from fastapi import APIRouter, Depends, Request

from backend.api.dependencies import current_actor, session_owner
from backend.api.errors import ApiError, envelope
from backend.services.auth import Actor
from .sessions import SessionId

router = APIRouter(tags=['Identity'])


@router.get('/api/auth/me')
def identity(request: Request, actor: Annotated[Actor, Depends(current_actor)]):
    session = None
    if actor.role == 'driver':
        service = request.app.state.session_service
        session = service.public_session(service.session(actor.session_id))
    return envelope(request.state.request_id, {
        'actor_id': actor.actor_id, 'role': actor.role, 'session': session,
        'scope': 'assigned_session' if actor.role == 'driver' else 'owned_sessions',
    })


@router.get('/api/sessions/{session_id}/run')
def session_run(session_id: SessionId, request: Request,
                actor: Annotated[Actor, Depends(session_owner)]):
    services = request.app.state
    session = services.session_service.session(session_id)
    if not session.get('run_id'):
        raise ApiError(404, 'RUN_NOT_FOUND', 'run', 'Session has no weather/import run')
    # Read only: the session grant authorizes this specific run, never another owner run.
    return envelope(request.state.request_id,
                    services.run_service.public(session['run_id'], session['owner_actor_id']))
