from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from backend.api.dependencies import session_owner
from backend.api.errors import envelope
from backend.models.http import HttpResponse, LoadScenarioRequest
from backend.services.auth import Actor
from .jobs import owner_dispatcher
from .sessions import SessionId

ArtifactId = Annotated[str, Path(pattern=r"^artifact-[a-f0-9]{32}$")]
router = APIRouter(prefix="/api/sessions", tags=["Audit artifacts"],
                   responses={status: {"model": HttpResponse} for status in (401, 403, 404, 409, 503)})


@router.post("/{session_id}/artifacts", status_code=201, response_model=HttpResponse,
             summary="Capture an immutable owner-scoped public audit bundle; request ID retries reuse exact bytes")
async def create(session_id: SessionId, body: LoadScenarioRequest, request: Request,
                 actor: Annotated[Actor, Depends(owner_dispatcher)]):
    return envelope(request.state.request_id, await request.app.state.artifact_service.create(session_id, body.request_id, actor))


@router.get("/{session_id}/artifacts", response_model=HttpResponse, summary="All completed artifacts owned by this session")
async def list_artifacts(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, request.app.state.artifact_service.list(session_id, actor))


@router.get("/{session_id}/artifacts/{artifact_id}", response_model=HttpResponse, summary="Immutable artifact manifest and exact document hashes")
async def manifest(session_id: SessionId, artifact_id: ArtifactId, request: Request,
                   actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, request.app.state.artifact_service.manifest(session_id, artifact_id, actor))


@router.get("/{session_id}/artifacts/{artifact_id}/content", response_model=HttpResponse,
            summary="Exact saved UTF-8 bundle text and SHA-256; contains public evidence, no private authority store")
async def content(session_id: SessionId, artifact_id: ArtifactId, request: Request,
                  actor: Annotated[Actor, Depends(session_owner)]):
    return envelope(request.state.request_id, request.app.state.artifact_service.content(session_id, artifact_id, actor))
