from typing import Annotated
import sqlite3

from fastapi import APIRouter, Depends, Request

from backend.api.dependencies import session_owner
from backend.api.errors import envelope
from backend.api.errors import ApiError
from backend.models.http import CompareProfilesRequest, HttpResponse, CancelJobRequest
from backend.services.auth import Actor
from backend.services.comparison_narrative import comparison_narrative
from .jobs import JobId, owner_dispatcher
from .sessions import SessionId

router = APIRouter(prefix="/api/sessions", tags=["Profile comparisons"],
    responses={status: {"model": HttpResponse} for status in (401, 403, 404, 409, 503)})


@router.post("/{session_id}/profiles/compare", status_code=202, response_model=HttpResponse,
    summary="Queue three profile forecasts, or compare three existing jobs without recomputing")
async def compare(session_id: SessionId, body: CompareProfilesRequest, request: Request,
                  actor: Annotated[Actor, Depends(owner_dispatcher)]):
    return envelope(request.state.request_id, await request.app.state.profile_service.create(session_id, body, actor))


@router.get("/{session_id}/profiles/comparisons/{comparison_id}", response_model=HttpResponse,
    summary="Read durable lifecycle, typed child outcomes and SDK comparison verdict")
async def poll(session_id: SessionId, comparison_id: JobId, request: Request,
               actor: Annotated[Actor, Depends(session_owner)]):
    # Reading solver results never starts the optional model. Only the explicit
    # explanations endpoint (the Quick explanation tab) requests Qwen.
    return envelope(request.state.request_id, request.app.state.profile_service.poll(session_id, comparison_id))


@router.get('/{session_id}/profiles/comparisons/{comparison_id}/explanations', response_model=HttpResponse)
async def explanations(session_id: SessionId, comparison_id: JobId, request: Request,
                       actor: Annotated[Actor, Depends(session_owner)]):
    group = request.app.state.profile_service.poll(session_id, comparison_id)
    try:
        result = request.app.state.explain_service.prepare(group)
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        raise ApiError(503, 'EXPLANATION_BINDING_CHANGED', 'profiles', 'Verified comparison required') from error
    return envelope(request.state.request_id, result)


@router.get("/{session_id}/profiles/comparisons/{comparison_id}/narrative", response_model=HttpResponse,
    summary="Explain cached certified forecasts only when the SDK declared them comparable")
def narrative(session_id: SessionId, comparison_id: JobId, request: Request,
              actor: Annotated[Actor, Depends(session_owner)]):
    group = request.app.state.profile_service.poll(session_id, comparison_id)
    try:
        result = comparison_narrative(group, build=request.app.state.settings.installation()["expected_build_sha256"])
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        raise ApiError(503, "NARRATIVE_BINDING_CHANGED", "profiles", "Verified cached comparison required") from error
    return envelope(request.state.request_id, result)


@router.post("/{session_id}/profiles/comparisons/{comparison_id}/cancel", response_model=HttpResponse,
    summary="Cancel owned batch jobs; existing-job comparisons leave their inputs unchanged")
async def cancel(session_id: SessionId, comparison_id: JobId, body: CancelJobRequest, request: Request,
                 actor: Annotated[Actor, Depends(owner_dispatcher)]):
    return envelope(request.state.request_id, await request.app.state.profile_service.cancel(session_id, comparison_id, body, actor))
