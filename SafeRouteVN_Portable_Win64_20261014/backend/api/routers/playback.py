from typing import Annotated

from fastapi import APIRouter, Depends, Request

from backend.api.dependencies import session_owner, session_operator
from backend.api.errors import envelope
from backend.models.http import HttpResponse, ReplayControlRequest
from backend.models.playback import PlaybackSpeedRequest, StartPlaybackRequest, AutoDeliverRequest
from backend.services.auth import Actor
from .jobs import owner_dispatcher
from .sessions import SessionId

router = APIRouter(prefix="/api/sessions", tags=["Automatic replay"], responses={status: {"model": HttpResponse} for status in (401, 403, 404, 409, 503)})


@router.post('/{session_id}/replay/auto-deliver', response_model=HttpResponse)
async def auto_deliver(session_id: SessionId, body: AutoDeliverRequest, request: Request, actor: Annotated[Actor, Depends(session_operator)]):
    return envelope(request.state.request_id, await request.app.state.playback_service.control(session_id, 'auto_deliver', body, actor))


@router.post("/{session_id}/replay/start", response_model=HttpResponse, summary="Start best-effort discrete replay; pause at event barrier or final return")
async def start(session_id: SessionId, body: StartPlaybackRequest, request: Request, actor: Annotated[Actor, Depends(session_operator)]):
    return envelope(request.state.request_id, await request.app.state.playback_service.control(session_id, "start", body, actor))


@router.post("/{session_id}/replay/speed", response_model=HttpResponse, summary="Set durable discrete playback cadence; never resume a paused controller")
async def speed(session_id: SessionId, body: PlaybackSpeedRequest, request: Request, actor: Annotated[Actor, Depends(session_operator)]):
    return envelope(request.state.request_id, await request.app.state.playback_service.control(session_id, "speed", body, actor))


@router.post("/{session_id}/replay/playback/pause", response_model=HttpResponse, summary="Fence new ticks; an already reserved SDK advance may still settle once")
async def pause(session_id: SessionId, body: ReplayControlRequest, request: Request, actor: Annotated[Actor, Depends(session_operator)]):
    return envelope(request.state.request_id, await request.app.state.playback_service.control(session_id, "pause", body, actor))


@router.get("/{session_id}/replay/playback", response_model=HttpResponse, summary="Durable running/paused/in-flight state and current physical SDK view")
async def controller(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, await request.app.state.playback_service.controller(session_id))


@router.get("/{session_id}/replay/playback/history", response_model=HttpResponse, summary="Last 100 durable playback control/stop/tick audit receipts")
async def history(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, request.app.state.playback_service.history(session_id))
