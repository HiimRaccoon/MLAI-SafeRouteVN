from typing import Annotated

from fastapi import APIRouter, Depends, Request

from backend.api.dependencies import session_owner
from backend.api.errors import envelope
from backend.models.http import AcceptPlanRequest, HttpResponse
from backend.services.auth import Actor
from .jobs import JobId, owner_dispatcher
from .sessions import SessionId

router = APIRouter(prefix="/api/sessions", tags=["Plans"], responses={status: {"model": HttpResponse} for status in (401, 403, 404, 409, 503)})


@router.post("/{session_id}/jobs/{job_id}/accept", response_model=HttpResponse,
    summary="Activate a certified current-basis job; delivery requires observed replay")
async def accept(session_id: SessionId, job_id: JobId, body: AcceptPlanRequest, request: Request,
                 actor: Annotated[Actor, Depends(owner_dispatcher)]):
    data = await request.app.state.plan_service.accept(session_id, job_id, body, actor)
    return envelope(request.state.request_id, data)


@router.get("/{session_id}/acceptances", response_model=HttpResponse, summary="Last 100 durable acceptance receipts for this session")
async def acceptances(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, request.app.state.plan_service.audit(session_id))
