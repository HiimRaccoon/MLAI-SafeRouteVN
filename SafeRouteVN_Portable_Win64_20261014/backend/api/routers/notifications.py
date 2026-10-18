from typing import Annotated
from fastapi import APIRouter, Depends, Query, Request
from backend.api.dependencies import session_owner
from backend.api.errors import ApiError, envelope
from backend.models.http import HttpResponse
from backend.services.auth import Actor
from .sessions import SessionId

router = APIRouter(prefix="/api/sessions", tags=["Notifications"])


@router.get("/{session_id}/notifications", response_model=HttpResponse, summary="Durable SDK summaries with an M3 cursor; reading never acknowledges")
def notifications(session_id: SessionId, request: Request, actor: Annotated[Actor, Depends(session_owner)],
                  after: Annotated[str, Query(pattern=r"^(0|[1-9][0-9]*)$", max_length=19)] = "0",
                  limit: Annotated[int, Query(ge=1, le=500)] = 100):
    if int(after) >= 1 << 63:
        raise ApiError(422, "INVALID_CURSOR", "after", "Cursor exceeds signed int64")
    return envelope(request.state.request_id, request.app.state.outbox_service.list(session_id, int(after), limit))
