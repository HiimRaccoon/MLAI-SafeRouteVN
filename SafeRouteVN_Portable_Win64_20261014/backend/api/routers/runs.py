from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Request, Path
from pydantic import Field
from backend.api.dependencies import dispatcher
from backend.api.errors import envelope
from backend.models.http import HttpModel, Identifier, LoadRunRequest, HttpResponse
from backend.services.auth import Actor

router=APIRouter(prefix='/api/runs',tags=['Weather runs'])
RunId=Annotated[str,Path(pattern=r'^run-[a-f0-9]{32}$')]

class CreateRunRequest(HttpModel):
    request_id: Identifier
    scenario_id: Literal['S0','S1','S2','S3','S4']='S2'
    event_after_minutes: int=Field(default=15,ge=1,le=120)

@router.post('',status_code=202,response_model=HttpResponse)
async def create(body:CreateRunRequest,request:Request,actor:Annotated[Actor,Depends(dispatcher)]):
    return envelope(request.state.request_id,await request.app.state.run_service.create(body,actor))

@router.get('/{run_id}',response_model=HttpResponse)
def status(run_id:RunId,request:Request,actor:Annotated[Actor,Depends(dispatcher)]):
    return envelope(request.state.request_id,request.app.state.run_service.public(run_id,actor.actor_id))

@router.post('/{run_id}/load',status_code=201,response_model=HttpResponse)
async def load(run_id:RunId,body:LoadRunRequest,request:Request,actor:Annotated[Actor,Depends(dispatcher)]):
    return envelope(request.state.request_id,await request.app.state.session_service.load_run(
        run_id,body.request_id,actor,replace_session_id=body.replace_session_id))
