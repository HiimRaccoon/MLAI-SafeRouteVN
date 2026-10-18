"""Authenticated operational commands use the runtime's durable CAS journal."""
import asyncio
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Request
from backend.models.http import AcceptPlanRequest, HttpResponse
from backend.api.errors import envelope
from backend.services.auth import Actor
from backend.api.dependencies import session_operator
from .sessions import SessionId
from pydantic import Field, JsonValue

class DeliveryDayRequest(AcceptPlanRequest):
    basis: dict[str, str | None]
    action: Literal['enable','add_order','unavailable','deliver','defer','next_day','wait_recovery','add_depot','assign_depot','customer']
    data: dict[str, JsonValue] = Field(default_factory=dict)

router=APIRouter(prefix='/api/sessions',tags=['Delivery day'])

@router.post('/{session_id}/delivery-day',response_model=HttpResponse)
async def dispatch(session_id:SessionId, body:DeliveryDayRequest, request:Request,
                   actor:Annotated[Actor,Depends(session_operator)]):
    from backend.api.errors import ApiError
    if actor.role == 'driver' and body.action not in ('deliver', 'defer'):
        raise ApiError(403, 'FORBIDDEN', 'action', 'Driver can only deliver or defer in the assigned session')
    services=request.app.state
    services.session_service.mutation_allowed(session_id)
    services.playback_service.stop(session_id,'DISPATCH_COMMAND')
    view=await services.gateway.resolve(session_id)
    # The complete basis is supplied by the server. The request's revision
    # remains fixed so retries cannot silently target a later physical world.
    basis=body.basis
    from backend.api.errors import ApiError
    if basis.get('session_id')!=session_id or any(basis.get(k)!=v for k,v in body.expected_revision.model_dump().items()):
        raise ApiError(409,'STALE_HEAD','basis','Request basis and revision differ')
    result=await asyncio.to_thread(services.gateway._call,'dispatch',{
        'session_id':session_id,'command_id':'day:'+body.request_id,
        'basis':basis,'action':body.action,'data':body.data})
    return envelope(request.state.request_id,result)
