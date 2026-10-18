from typing import Annotated

from fastapi import APIRouter, Depends, Request

from backend.api.dependencies import session_owner
from backend.api.errors import envelope
from backend.models.http import HttpResponse
from backend.services.auth import Actor
from .sessions import SessionId

router = APIRouter(prefix="/api/sessions", tags=["Narrative"])


@router.get("/{session_id}/narrative", response_model=HttpResponse,
            summary="Vietnamese explanation of observed and forecast public metrics")
async def narrative(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    data = await request.app.state.narrative_service.get(session_id, actor)
    return envelope(request.state.request_id, data)
