from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from backend.api.dependencies import session_owner
from backend.api.errors import envelope
from backend.models.http import HttpResponse
from backend.services.auth import Actor

SessionId = Annotated[str, Path(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")]
router = APIRouter(prefix="/api/sessions", tags=["Sessions"], responses={
    401: {"model": HttpResponse}, 403: {"model": HttpResponse}, 404: {"model": HttpResponse},
    409: {"model": HttpResponse}, 503: {"model": HttpResponse}})


@router.get('/{session_id}/world', response_model=HttpResponse, summary='Coherent public projections from one verified SDK read')
async def world(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    services = request.app.state
    view = await services.session_service.state(session_id)
    data = {'state': view}
    for kind in ('orders', 'vehicles', 'locations'):
        data[kind] = await services.session_service.projection(session_id, kind, view=view)
    data['events'] = await services.replay_service.events(session_id, view=view)
    data['history'] = services.replay_service.history(session_id)
    data['playback'] = await services.playback_service.controller(session_id, view=view)
    return envelope(request.state.request_id, data)


@router.get("/{session_id}", response_model=HttpResponse, summary="Owned session metadata and read links")
def session(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    service = request.app.state.session_service
    return envelope(request.state.request_id, service.public_session(service.session(session_id)))


@router.get("/{session_id}/state", response_model=HttpResponse, summary="Fresh validated execution-view/2 from the SDK")
async def state(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, await request.app.state.session_service.state(session_id))


@router.get("/{session_id}/orders", response_model=HttpResponse, summary="Verified order metadata with physical status from the current view")
async def orders(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, await request.app.state.session_service.projection(session_id, "orders"))


@router.get("/{session_id}/vehicles", response_model=HttpResponse, summary="Current SDK vehicles and static vehicle metadata")
async def vehicles(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, await request.app.state.session_service.projection(session_id, "vehicles"))


@router.get("/{session_id}/locations", response_model=HttpResponse, summary="WGS84 [longitude,latitude] depot and current-order delivery locations")
async def locations(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, await request.app.state.session_service.projection(session_id, "locations"))
