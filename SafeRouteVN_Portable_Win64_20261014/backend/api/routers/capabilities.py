from typing import Annotated

from fastapi import APIRouter, Depends, Request

from backend.api.dependencies import current_actor
from backend.api.errors import envelope
from backend.models.http import HttpResponse
from backend.services.auth import Actor

router = APIRouter(prefix="/api/runtime", tags=["Runtime"])


@router.get("/capabilities", response_model=HttpResponse,
            responses={401: {"model": HttpResponse}, 503: {"model": HttpResponse}},
            summary="Capabilities from the verified public M2 SDK")
async def capabilities(request: Request, actor: Annotated[Actor, Depends(current_actor)]):
    data = await request.app.state.gateway.capabilities()
    return envelope(request.state.request_id, {**data, 'm3_world_snapshot': True})
