"""A separately launched MOCK_DEMO app. Never a production fallback."""
import secrets
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware

from .fixtures import FixtureStore, SOURCE_BUILD


MODE = "MOCK_DEMO"
DEFAULT_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000")
UNITS = {"distance": "m", "duration": "s", "action_duration": "us", "mass": "kg",
         "money": "VND", "relative_exposure": "PROXY"}


def envelope(data=None, *, status="OK", code=None, message=None):
    return {"schema_version": "saferoute-m3-mock-http-response/1", "mode": MODE,
            "authority_access": False, "runtime_jobs": False, "status": status, "data": data,
            "diagnostics": [] if code is None else [{"severity": "ERROR", "code": code,
                "path": "mock", "message": message}]}


def failure(status, code, message):
    headers = {"X-SafeRoute-Mode": MODE, "Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(envelope(status="ERROR", code=code, message=message), status_code=status, headers=headers)


def _origins(origins):
    result = tuple(origins)
    if not result:
        raise ValueError("Explicit frontend origins required")
    for origin in result:
        if not isinstance(origin, str):
            raise ValueError("Explicit HTTP frontend origins required")
        parsed = urlsplit(origin)
        if (not isinstance(origin, str) or origin == "*" or parsed.scheme not in ("http", "https")
                or not parsed.hostname or parsed.path or parsed.query or parsed.fragment
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("Explicit HTTP frontend origins required")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Valid frontend origin port required")
    return result


def create_mock_app(*, enabled=False, bearer_token=None, fixture_root=None, cors_origins=DEFAULT_ORIGINS):
    if enabled is not True:
        raise ValueError("Explicit read-only demo opt-in required")
    if (not isinstance(bearer_token, str) or not 32 <= len(bearer_token) <= 256
            or not bearer_token.isascii() or any(not (c.isalnum() or c in "_-") for c in bearer_token)):
        raise ValueError("Separate mock bearer token of 32 to 256 ASCII characters required")
    origins = _origins(cors_origins)
    fixtures = FixtureStore(fixture_root)

    async def authorized(request: Request):
        values = request.headers.getlist("authorization")
        if len(values) != 1 or not values[0].startswith("Bearer "):
            raise HTTPException(401, detail="MOCK_UNAUTHORIZED")
        candidate = values[0][7:]
        if not candidate.isascii() or not secrets.compare_digest(candidate, bearer_token):
            raise HTTPException(401, detail="MOCK_UNAUTHORIZED")

    app = FastAPI(title="SafeRoute VN — MOCK_DEMO read-only examples", version="1.0.0",
                  description="Isolated pinned examples for M4 adapter development. No runtime, jobs, mutation or authority access.",
                  docs_url=None, redoc_url=None,
                  dependencies=[Depends(authorized)])
    app.state.fixtures = fixtures
    app.add_middleware(CORSMiddleware, allow_origins=list(origins), allow_credentials=False,
                       allow_methods=["GET", "HEAD"], allow_headers=["Authorization"],
                       expose_headers=["X-SafeRoute-Mode", "X-Fixture-SHA256"])

    @app.middleware("http")
    async def read_only_boundary(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            response = failure(405, "MOCK_READ_ONLY", "This demo never optimizes, accepts, applies events or changes state")
            response.headers["Allow"] = "GET, HEAD, OPTIONS"
        elif request.query_params:
            response = failure(422, "MOCK_REQUEST_FIELDS", "Mock reads do not accept query fields, paths, state or server configuration")
        else:
            has_body = False
            if request.method != "OPTIONS":
                # Any first nonempty chunk is rejected, without buffering an
                # unbounded body or decoding browser-supplied fixture content.
                async for chunk in request.stream():
                    if chunk:
                        has_body = True
                        break
            if has_body:
                response = failure(422, "MOCK_REQUEST_FIELDS", "Mock reads do not accept a request body")
            else:
                response = await call_next(request)
        response.headers["X-SafeRoute-Mode"] = MODE
        response.headers["Cache-Control"] = "no-store"
        origin = request.headers.get("origin")
        if origin in origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Expose-Headers"] = "X-SafeRoute-Mode, X-Fixture-SHA256"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        if error.status_code == 401:
            return failure(401, "MOCK_UNAUTHORIZED", "Use the separate server-configured mock bearer token")
        if error.detail == "MOCK_SCENARIO_NOT_FOUND":
            return failure(404, "MOCK_SCENARIO_NOT_FOUND", "Only the pinned S2/S3/S4 demo scenarios are available")
        if error.detail == "MOCK_FIXTURE_INVALID":
            return failure(503, "MOCK_FIXTURE_INVALID", "Pinned mock example verification failed")
        return failure(error.status_code, "MOCK_ROUTE_NOT_FOUND", "Unknown read-only demo route")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return failure(422, "MOCK_REQUEST_FIELDS", "Invalid demo request")

    def fixture(scenario_id):
        try:
            return fixtures.view(scenario_id)
        except KeyError:
            raise HTTPException(404, detail="MOCK_SCENARIO_NOT_FOUND") from None
        except (OSError, ValueError, TypeError):
            raise HTTPException(503, detail="MOCK_FIXTURE_INVALID") from None

    @app.exception_handler(Exception)
    async def internal_error(request, error):
        return failure(503, "MOCK_UNAVAILABLE", "The read-only demo is unavailable")

    @app.api_route("/health", methods=["GET", "HEAD"])
    async def health():
        return envelope({"alive": True, "purpose": "M4_ADAPTER_DEVELOPMENT_ONLY", "production_fallback": False})

    @app.api_route("/api/mock/capabilities", methods=["GET", "HEAD"])
    async def capabilities():
        return envelope({"read_only": True, "source_package_build_sha256": SOURCE_BUILD,
                         "example_execution_build_may_be_historical": True, "execution_mode": "SIMULATED_REPLAY",
                         "real_world_observation": False, "production_fallback": False,
                         "can_optimize": False, "can_accept": False, "can_apply_event": False, "can_replay": False,
                         "metric_units": UNITS, "fixture_validation_scope": "PINNED_BYTES_AND_PUBLIC_EXECUTION_SHAPE_ONLY",
                         "native_solver_or_raw_validator_test": False})

    @app.api_route("/api/mock/scenarios", methods=["GET", "HEAD"])
    async def catalog():
        # Verify every example again before advertising it; mutated assets fail closed.
        for row in fixtures.catalog():
            fixture(row["scenario_id"])
        return envelope({"scenarios": fixtures.catalog()})

    @app.api_route("/api/mock/scenarios/{scenario_id}/execution-view", methods=["GET", "HEAD"])
    async def execution_view(scenario_id: str):
        view = fixture(scenario_id)
        record = fixtures.record(scenario_id)
        return envelope({"scenario_id": scenario_id, "fixture_sha256": record["sha256"],
                         "source_package_build_sha256": SOURCE_BUILD,
                         "fixture_build_sha256": record["fixture_build_sha256"],
                         "metric_units": UNITS, "execution_view": view})

    @app.api_route("/api/mock/scenarios/{scenario_id}/source", methods=["GET", "HEAD"])
    async def source(scenario_id: str):
        fixture(scenario_id)
        record = fixtures.record(scenario_id)
        # Exact frozen source bytes let M4 use the canonical JS consumer directly.
        return Response(fixtures.raw(scenario_id), media_type="application/json", headers={
            "X-Fixture-SHA256": record["sha256"], "X-SafeRoute-Mode": MODE})

    return app
