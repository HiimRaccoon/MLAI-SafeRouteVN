from typing import Annotated
import asyncio
from copy import deepcopy
from pydantic import Field

from fastapi import APIRouter, Depends, Path, Request

from backend.api.dependencies import current_actor, dispatcher
from backend.api.errors import envelope
from backend.models.http import HttpResponse, LoadScenarioRequest, HttpModel, Identifier
from backend.services.auth import Actor

ScenarioId = Annotated[str, Path(pattern=r"^S[0-8]$", description="Pinned suite scenario S0–S8")]
router = APIRouter(prefix="/api/scenarios", tags=["Scenarios"], responses={401: {"model": HttpResponse}, 503: {"model": HttpResponse}})


class ImportScenarioRequest(HttpModel):
    request_id: Identifier
    name: str = Field(min_length=1, max_length=100)
    fixture: dict


@router.get('/import/template', response_model=HttpResponse)
def import_template(request: Request, actor: Annotated[Actor, Depends(dispatcher)]):
    return envelope(request.state.request_id, deepcopy(request.app.state.catalog.fixture('S0')))


@router.get('/imports/list', response_model=HttpResponse)
def imports(request: Request, actor: Annotated[Actor, Depends(dispatcher)]):
    service = request.app.state.run_service
    with service.db() as db:
        rows = db.execute("SELECT run_id,body FROM run_builds WHERE actor_id=? AND installation=? AND status='READY' ORDER BY rowid DESC LIMIT 100",
                          (actor.actor_id, service.sessions.installation_identity())).fetchall()
    return envelope(request.state.request_id, {'imports': [service.public(r['run_id'], actor.actor_id) for r in rows
                    if json_kind(r['body']) == 'IMPORTED_SCENARIO']})


def json_kind(body):
    import json
    return json.loads(body).get('source_kind')


@router.post('/import', response_model=HttpResponse, status_code=201)
async def upload(body: ImportScenarioRequest, request: Request, actor: Annotated[Actor, Depends(dispatcher)]):
    from backend.services.scenario_import import import_scenario
    result = await asyncio.to_thread(import_scenario, request.app.state.run_service, body, actor)
    return envelope(request.state.request_id, result)


@router.get("", response_model=HttpResponse, summary="Verified S0–S8 catalog")
def catalog(request: Request, actor: Annotated[Actor, Depends(current_actor)]):
    return envelope(request.state.request_id, request.app.state.catalog.public())


@router.get("/{scenario_id}", response_model=HttpResponse, summary="Scenario counts, epoch and pending source events")
def detail(scenario_id: ScenarioId, request: Request, actor: Annotated[Actor, Depends(current_actor)]):
    return envelope(request.state.request_id, request.app.state.catalog.detail(scenario_id))


@router.post("/{scenario_id}/load", response_model=HttpResponse, status_code=201,
             responses={403: {"model": HttpResponse}, 409: {"model": HttpResponse}},
             summary="Create an owned session through the public M2 SDK; request ID is durable")
async def load(scenario_id: ScenarioId, body: LoadScenarioRequest, request: Request,
               actor: Annotated[Actor, Depends(dispatcher)]):
    data = await request.app.state.session_service.load(scenario_id, body.request_id, actor,
        replace_session_id=body.replace_session_id)
    return envelope(request.state.request_id, data)
