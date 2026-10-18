from contextlib import asynccontextmanager
import sqlite3

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.models.http import REQUEST_MODELS
from backend.services.auth import AuthStore
from backend.services.readiness import Readiness
from backend.services.runtime_gateway import RuntimeGateway
from backend.services.session_repository import SessionRepository
from backend.services.settings import Settings
from backend.services.scenario_catalog import ScenarioCatalog
from backend.services.session_service import SessionService
from backend.services.run_service import RunService
from backend.services.job_repository import JobRepository
from backend.services.job_service import JobService
from backend.services.plan_repository import PlanRepository
from backend.services.plan_service import PlanService
from backend.services.replay_repository import ReplayRepository
from backend.services.replay_service import ReplayService
from backend.services.outbox_repository import OutboxRepository
from backend.services.outbox_service import OutboxService
from backend.services.recovery_repository import RecoveryRepository
from backend.services.artifact_repository import ArtifactRepository
from backend.services.artifact_service import ArtifactService
from backend.services.narrative_service import NarrativeService
from backend.services.llm_explain import ExplainService
from backend.services.profile_repository import ProfileRepository
from backend.services.profile_service import ProfileService
from backend.services.playback_repository import PlaybackRepository
from backend.services.playback_service import PlaybackService
from .errors import ApiError
from .errors import register_handlers
from .middleware import HttpBoundaryMiddleware
from .routers import identity, runs, capabilities, system, scenarios, jobs, plans, replay, notifications, artifacts, narrative, profiles, playback, sessions as session_routes


def create_app(settings: Settings | None = None, *, gateway=None, catalog=None):
    settings = settings or Settings.from_environment()
    if "*" in settings.cors_origins:
        raise ValueError("Explicit CORS origins required")
    auth, sessions = AuthStore(settings.auth_path), SessionRepository(settings.metadata_path)
    gateway = gateway or RuntimeGateway(settings)
    catalog = catalog or ScenarioCatalog(settings)
    job_repository = JobRepository(settings.metadata_path)
    plan_repository = PlanRepository(settings.metadata_path)
    replay_repository = ReplayRepository(settings.metadata_path)
    outbox_repository = OutboxRepository(settings.metadata_path)
    recovery_repository = RecoveryRepository(settings.metadata_path)
    artifact_repository = ArtifactRepository(settings.metadata_path)
    profile_repository = ProfileRepository(settings.metadata_path)
    playback_repository = PlaybackRepository(settings.metadata_path)

    @asynccontextmanager
    async def lifespan(app):
        try:
            sessions.initialize()
            app.state.run_service.initialize()
            job_repository.initialize()
            plan_repository.initialize()
            replay_repository.initialize()
            outbox_repository.initialize()
            recovery_repository.initialize()
            artifact_repository.initialize()
            profile_repository.initialize()
            playback_repository.initialize()
        except (OSError, sqlite3.Error):
            # Liveness stays available; readiness/authenticated session operations fail closed.
            pass
        try:
            catalog.initialize()
        except ApiError:
            pass
        try:
            yield
        finally:
            await app.state.explain_service.close()
            if isinstance(gateway,RuntimeGateway):
                import asyncio
                await asyncio.to_thread(gateway.close)

    app = FastAPI(title="SafeRoute VN â€” Member 3 Backend", version="0.8.0", lifespan=lifespan,
                  description="Durable profile comparison, controlled autoplay and factual Vietnamese narrative. SIMULATED_REPLAY is not real GPS.")
    app.state.settings, app.state.auth, app.state.sessions, app.state.gateway = settings, auth, sessions, gateway
    app.state.catalog = catalog
    app.state.session_service = SessionService(settings, sessions, catalog, gateway)
    app.state.run_service = app.state.session_service.runs
    app.state.session_service.runs = app.state.run_service
    app.state.job_repository = job_repository
    app.state.job_service = JobService(settings, job_repository, app.state.session_service, gateway)
    app.state.plan_repository = plan_repository
    app.state.plan_service = PlanService(settings, plan_repository, app.state.session_service, app.state.job_service, gateway)
    app.state.replay_repository = replay_repository
    app.state.replay_service = ReplayService(settings, replay_repository, app.state.session_service, gateway)
    app.state.outbox_repository = outbox_repository
    app.state.outbox_service = OutboxService(settings, outbox_repository, gateway, app.state.session_service)
    app.state.recovery_repository = recovery_repository
    app.state.artifact_repository = artifact_repository
    app.state.artifact_service = ArtifactService(settings, artifact_repository, app.state.session_service, gateway)
    app.state.narrative_service = NarrativeService(settings, app.state.session_service)
    app.state.explain_service = ExplainService(settings)
    app.state.profile_repository = profile_repository
    app.state.profile_service = ProfileService(settings, profile_repository, app.state.session_service, app.state.job_service, gateway)
    app.state.playback_repository = playback_repository
    app.state.playback_service = PlaybackService(settings, playback_repository, app.state.session_service, app.state.replay_service, gateway)
    app.state.replay_service.playback = app.state.playback_service
    app.state.readiness = Readiness(settings, auth, sessions, gateway, catalog, job_repository, plan_repository, replay_repository)
    app.state.readiness.outbox = outbox_repository
    app.state.readiness.recovery = recovery_repository
    app.state.readiness.artifacts = artifact_repository
    app.state.readiness.profiles = profile_repository
    app.state.readiness.playback = playback_repository
    register_handlers(app)
    app.include_router(system.router)
    app.include_router(identity.router)
    app.include_router(capabilities.router)
    app.include_router(scenarios.router)
    app.include_router(runs.router)
    app.include_router(session_routes.router)
    app.include_router(jobs.router)
    app.include_router(plans.router)
    app.include_router(replay.router)
    app.include_router(notifications.router)
    app.include_router(artifacts.router)
    app.include_router(narrative.router)
    app.include_router(profiles.router)
    app.include_router(playback.router)
    from .routers import delivery_day
    app.include_router(delivery_day.router)
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
                       allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type"],
                       expose_headers=["X-Request-ID", "X-Process-Time"])
    app.add_middleware(HttpBoundaryMiddleware, max_body_bytes=settings.max_body_bytes, cors_origins=settings.cors_origins)
    original_openapi = app.openapi

    def openapi():
        schema = original_openapi()
        models = schema.setdefault("components", {}).setdefault("schemas", {})
        for model in REQUEST_MODELS:
            if model.__name__ in models:
                continue
            item = model.model_json_schema(ref_template="#/components/schemas/{model}")
            models.update(item.pop("$defs", {}))
            item["description"] = "Reserved request model for subsequent M3 steps; endpoint not implemented yet."
            models[model.__name__] = item
        return schema

    app.openapi = openapi
    return app


app = create_app()
