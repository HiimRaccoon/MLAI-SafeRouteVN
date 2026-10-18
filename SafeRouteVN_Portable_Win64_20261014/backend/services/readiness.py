from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from backend.api.errors import ApiError
from backend.services.heartbeat_io import read_heartbeat_json


class Readiness:
    def __init__(self, settings, auth, sessions, gateway, catalog=None, jobs=None, plans=None, replays=None):
        self.settings, self.auth, self.sessions, self.gateway = settings, auth, sessions, gateway
        self.catalog = catalog
        self.jobs = jobs
        self.plans = plans
        self.replays = replays

    async def check(self):
        checks = {}
        config = None
        try:
            config = self.settings.installation()
            receipt = json.loads(Path(config["latest_preflight_receipt"]).read_text(encoding="utf-8"))
            if (receipt["status"] != "G0_TECHNICAL_PASS" or receipt["step1_input_completeness"] != "COMPLETE_VERIFIED"
                    or receipt["build_sha256"] != config["expected_build_sha256"]
                    or Path(receipt["snapshot_root"]).resolve() != Path(config["snapshot_root"]).resolve()
                    or Path(config["snapshot_root"]).resolve() != self.settings.project_root.resolve()):
                raise ValueError("Receipt binding")
            checks["g0"] = "PASS"
        except (OSError, ValueError, KeyError, TypeError):
            checks["g0"] = "G0_NOT_VERIFIED"
        try:
            self.auth.records()
            checks["authentication"] = "PASS"
        except ApiError:
            checks["authentication"] = "AUTH_NOT_CONFIGURED"
        try:
            checks["metadata"] = "PASS" if self.sessions.healthy() else "METADATA_UNAVAILABLE"
        except (sqlite3.Error, OSError):
            checks["metadata"] = "METADATA_UNAVAILABLE"
        checks["worker"] = self.worker_status(config)
        if checks["worker"] == "PASS":
            try:
                await self.gateway.capabilities()
                checks["runtime"] = "PASS"
            except ApiError as error:
                checks["runtime"] = error.code
        else:
            # Startup already checks the SDK. Polling must not compete with
            # recovery for the authority-store lock while it is running.
            checks["runtime"] = "WORKER_NOT_READY"
        if self.catalog is not None:
            checks["catalog"] = "PASS" if self.catalog.healthy() else "CATALOG_NOT_VERIFIED"
        if self.jobs is not None:
            try:
                checks["queue"] = "PASS" if self.jobs.healthy() else "QUEUE_UNAVAILABLE"
            except (OSError, sqlite3.Error):
                checks["queue"] = "QUEUE_UNAVAILABLE"
        if self.plans is not None:
            try:
                checks["acceptances"] = "PASS" if self.plans.healthy() else "ACCEPTANCE_METADATA_UNAVAILABLE"
            except (OSError, sqlite3.Error):
                checks["acceptances"] = "ACCEPTANCE_METADATA_UNAVAILABLE"
        if self.replays is not None:
            try:
                checks["replay"] = "PASS" if self.replays.healthy() else "REPLAY_METADATA_UNAVAILABLE"
            except (OSError, sqlite3.Error):
                checks["replay"] = "REPLAY_METADATA_UNAVAILABLE"
        for name in ("outbox", "recovery", "artifacts", "profiles", "playback"):
            repository = getattr(self, name, None)
            if repository is not None:
                try:
                    checks[name] = "PASS" if repository.healthy() else name.upper() + "_UNAVAILABLE"
                except (OSError, sqlite3.Error):
                    checks[name] = name.upper() + "_UNAVAILABLE"
        return {"ready": all(value == "PASS" for value in checks.values()), "checks": checks,
                "execution_mode": "SIMULATED_REPLAY"}

    def worker_status(self, config):
        if not self.settings.heartbeat_path.exists():
            return "WORKER_NOT_CONFIGURED"
        try:
            value = read_heartbeat_json(self.settings.heartbeat_path)
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(value["updated_at"])).total_seconds()
            if (config is None or value["schema_version"] != "saferoute-m3-worker-heartbeat/1"
                    or value["build_sha256"] != config["expected_build_sha256"]
                    or value["installation_sha256"] != self.settings.installation_identity()
                    or value["status"] != "READY" or not 0 <= age <= 30):
                return "WORKER_NOT_READY"
            return "PASS"
        except (OSError, ValueError, KeyError, TypeError):
            return "WORKER_NOT_READY"
