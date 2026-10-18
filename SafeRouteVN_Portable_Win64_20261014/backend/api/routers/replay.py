from typing import Annotated

from fastapi import APIRouter, Depends, Request

from backend.api.dependencies import session_owner, session_operator
from backend.api.errors import envelope
from backend.models.http import ApplyEventRequest, HttpResponse, ReplayControlRequest, ReplayStepRequest
from backend.services.auth import Actor
from .jobs import JobId, owner_dispatcher
from .sessions import SessionId

router = APIRouter(prefix="/api/sessions", tags=["Events and replay"], responses={status: {"model": HttpResponse} for status in (401, 403, 404, 409, 503)})


@router.get("/{session_id}/events", response_model=HttpResponse, summary="Verified pending event metadata and exact replay barriers")
async def events(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, await request.app.state.replay_service.events(session_id))


@router.post("/{session_id}/events/{event_id}/apply", response_model=HttpResponse, summary="Apply a verified due event once; suspend the previous forecast")
async def apply_event(session_id: SessionId, event_id: JobId, body: ApplyEventRequest, request: Request,
                      actor: Annotated[Actor, Depends(owner_dispatcher)]):
    return envelope(request.state.request_id, await request.app.state.replay_service.mutate(session_id, "apply_event", body, actor, event_id))


@router.post("/{session_id}/replay/step", response_model=HttpResponse, summary="Advance observed replay without crossing a pending event")
async def step(session_id: SessionId, body: ReplayStepRequest, request: Request, actor: Annotated[Actor, Depends(session_operator)]):
    return envelope(request.state.request_id, await request.app.state.replay_service.mutate(session_id, "advance", body, actor))


@router.post("/{session_id}/replay/pause", response_model=HttpResponse, summary="Acknowledge manual paused playback; does not mutate runtime head")
async def pause(session_id: SessionId, body: ReplayControlRequest, request: Request, actor: Annotated[Actor, Depends(owner_dispatcher)]):
    return envelope(request.state.request_id, await request.app.state.replay_service.mutate(session_id, "pause", body, actor))


@router.post("/{session_id}/replay/reset", response_model=HttpResponse, summary="Create a new session from the same verified fixture; preserve old history")
async def reset(session_id: SessionId, body: ReplayControlRequest, request: Request, actor: Annotated[Actor, Depends(owner_dispatcher)]):
    return envelope(request.state.request_id, await request.app.state.replay_service.mutate(session_id, "reset", body, actor))


@router.get("/{session_id}/replay/history", response_model=HttpResponse, summary="Last 100 durable replay/event/control receipts")
async def history(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, request.app.state.replay_service.history(session_id))


@router.get("/{session_id}/replay", response_model=HttpResponse, summary="Manual step controller and current SDK execution view")
async def controller(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, await request.app.state.replay_service.controller(session_id))
