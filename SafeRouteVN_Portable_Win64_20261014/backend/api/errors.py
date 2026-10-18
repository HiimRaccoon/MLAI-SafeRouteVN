from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
import logging


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, path: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.path, self.message = status_code, code, path, message


def envelope(request_id: str, data=None, diagnostics=()):
    return {"schema_version": "saferoute-m3-http-response/1", "request_id": request_id,
            "status": "ERROR" if diagnostics else "OK", "data": data, "diagnostics": list(diagnostics)}


def error_response(scope, error: ApiError):
    request_id = scope.get("state", {}).get("request_id", "unavailable")
    response = JSONResponse(envelope(request_id, diagnostics=[{
        "severity": "ERROR", "code": error.code, "path": error.path, "message": error.message}]), status_code=error.status_code)
    if error.status_code == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


def register_handlers(app):
    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError):
        return error_response(request.scope, error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        issues = [{"severity": "ERROR", "code": "VALIDATION_ERROR",
                   "path": ".".join(str(item) for item in e["loc"]),
                   "message": "Field is not permitted" if e["type"] == "extra_forbidden" else e["msg"]}
                  for e in error.errors()]
        return JSONResponse(envelope(request.state.request_id, diagnostics=issues), status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        message = "Route not found" if error.status_code == 404 else "HTTP request rejected"
        return error_response(request.scope, ApiError(error.status_code, f"HTTP_{error.status_code}", "request", message))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        logging.getLogger("saferoute.http").error("HTTP failure request_id=%s exception_type=%s", request.state.request_id, type(error).__name__)
        return error_response(request.scope, ApiError(500, "INTERNAL_ERROR", "server", "Internal server error"))
