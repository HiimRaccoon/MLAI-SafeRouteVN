from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from backend.api.dependencies import session_owner
from backend.api.errors import ApiError, envelope
from backend.models.http import CancelJobRequest, HttpResponse, OptimizeRequest
from backend.services.auth import Actor
from .sessions import SessionId

JobId = Annotated[str, Path(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")]
router = APIRouter(prefix="/api/sessions", tags=["Jobs"], responses={status: {"model": HttpResponse} for status in (401, 403, 404, 409, 503)})


def owner_dispatcher(actor: Annotated[Actor, Depends(session_owner)]):
    if actor.role != "dispatcher":
        raise ApiError(403, "FORBIDDEN", "role", "Dispatcher role required")
    return actor


@router.post("/{session_id}/optimize", status_code=202, response_model=HttpResponse,
             summary="Persist input binding, submit a forecast job and enqueue compute")
async def optimize(session_id: SessionId, body: OptimizeRequest, request: Request, actor: Annotated[Actor, Depends(owner_dispatcher)]):
    data = await request.app.state.job_service.optimize(session_id, body, actor)
    return envelope(request.state.request_id, data)


@router.get("/{session_id}/jobs/{job_id}", response_model=HttpResponse, summary="Typed runtime job lifecycle, business outcome and witness status")
async def poll(session_id: SessionId, job_id: JobId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, await request.app.state.job_service.poll(session_id, job_id))


@router.post("/{session_id}/jobs/{job_id}/cancel", response_model=HttpResponse, summary="Idempotent cancellation; completed jobs stay immutable")
async def cancel(session_id: SessionId, job_id: JobId, body: CancelJobRequest, request: Request,
                 actor: Annotated[Actor, Depends(owner_dispatcher)]):
    return envelope(request.state.request_id, await request.app.state.job_service.cancel(session_id, job_id, body, actor))


@router.get('/{session_id}/jobs/{job_id}/forecast', response_model=HttpResponse, summary='Certified public forecast; does not accept or dispatch')
async def forecast(session_id: SessionId, job_id: JobId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, await request.app.state.job_service.forecast(session_id, job_id))
