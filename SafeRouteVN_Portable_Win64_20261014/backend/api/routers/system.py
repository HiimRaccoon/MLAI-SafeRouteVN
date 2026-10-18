from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.errors import envelope
from backend.models.http import HttpResponse

router = APIRouter(tags=["System"])


@router.get("/health", response_model=HttpResponse, summary="HTTP process liveness")
def health(request: Request):
    return envelope(request.state.request_id, {"service": "saferoute-backend", "alive": True})


@router.get("/ready", response_model=HttpResponse, responses={503: {"model": HttpResponse}},
            summary="G0, authentication, metadata, runtime and compute worker readiness")
async def ready(request: Request):
    data = await request.app.state.readiness.check()
    diagnostics = [{"severity": "ERROR", "code": value, "path": f"readiness.{name}",
                    "message": "Required service is not ready"}
                   for name, value in data["checks"].items() if value != "PASS"]
    return JSONResponse(envelope(request.state.request_id, data, diagnostics), status_code=200 if data["ready"] else 503)
