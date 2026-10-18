"""Owner-scoped, immutable public audit bundles with explicit history gaps."""
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from backend.api.errors import ApiError
from .artifact_repository import canonical_bytes, validate_basis, validate_execution, validate_job
from .outbox_repository import OutboxRepository
from .narrative_service import decision_narrative
from .artifact_extensions import extension_metadata


RECEIPT_FIELDS = ("schema_version", "session_id", "job_id", "profile", "acceptance_id", "mutation_id", "operation", "status",
    "source", "input_basis", "basis", "recorded_at", "target_time", "event_id", "event_sha256", "event_type",
    "new_session_id", "mode", "paused")
VALIDATION_FIELDS = {"validator_version", "valid", "checked_physical_mutations", "historical_execution_builds_preserved",
                     "current_checker_build_sha256", "scope"}
LIMITATIONS = ["GENERAL_M1_NOT_VALIDATED", "E4_NOT_RUN", "PRODUCTION_CALIBRATION_UNCONFIGURED", "PERFORMANCE_NOT_MET",
              "SIMULATED_REPLAY_NOT_GPS", "EXPOSURE_IS_PROXY", "NOT_OPTIMALITY"]


class ArtifactService:
    def __init__(self, settings, repository, sessions, gateway):
        self.settings, self.repository, self.sessions, self.gateway = settings, repository, sessions, gateway

    def _session(self, session_id, actor):
        row = self.sessions.session(session_id)
        if row["owner_actor_id"] != actor.actor_id:
            raise ApiError(403, "FORBIDDEN", "session_id", "Session access denied")
        return row

    @staticmethod
    def _receipt(value):
        if not isinstance(value, dict):
            raise ValueError("Persisted public receipt must be an object")
        return {key: value[key] for key in RECEIPT_FIELDS if key in value}

    def _metadata(self, raw, session_id, build):
        """Allowlist M3 records; ephemeral claims and private configuration never leave the server."""
        def decode(value):
            return json.loads(value) if value is not None else None

        def basis(value):
            parsed = decode(value)
            if parsed is not None:
                validate_basis(parsed, session_id, build)
            return parsed

        session = raw["sessions"][0]
        public_session = {key: session[key] for key in ("session_id", "owner_actor_id", "scenario_id", "installation_sha256",
                         "fixture_sha256", "catalog_sha256", "created_at", "status")}
        requests, initial_frames = [], []
        for table, operation in (("load_requests", "load"), ("job_requests", None), ("accept_requests", "accept"), ("replay_requests", None)):
            for row in raw[table]:
                op = operation or row["operation"]
                result = decode(row["response_json"])
                request = {key: row[key] for key in ("actor_id", "session_id", "request_id", "request_sha256", "command_id")}
                request.update(operation=op, metadata_status="COMPLETED" if result is not None else
                    "REJECTED" if row.get("error_code") else "PENDING_UNCERTAIN", input_basis=basis(row.get("basis_json")),
                    created_at=row.get("created_at", session["created_at"]),
                    installation_sha256=row.get("installation_sha256", session["installation_sha256"]))
                for key in ("job_id", "profile", "budget_seconds", "error_code", "error_status", "acceptance_id", "mutation_id"):
                    if key in row:
                        request[key] = row[key]
                if "payload_json" in row:
                    payload = decode(row["payload_json"])
                    allowed = {"advance": ("target_time", "current_time"), "apply_event": ("event_id", "event_type", "timestamp"),
                               "pause": (), "reset": ("scenario_id", "load_request_id")}[op]
                    request["server_payload"] = {key: payload[key] for key in allowed}
                if op == "load":
                    request["server_payload"] = {"scenario_id": session["scenario_id"]}
                    if result is not None:
                        view = result["execution_view"]
                        validate_execution(view, session_id, build)
                        request["receipt"] = {"schema_version": result["schema_version"], "session_id": session_id,
                                              "initial_basis": view["basis"]}
                        initial_frames.append({"source": "PERSISTED_BOOTSTRAP_RESPONSE", "reference_id": row["command_id"],
                            "captured_at": None, "capture_time_known": False, "basis": view["basis"],
                            "simulation_time": view["current_time"], "public_view": view})
                elif result is not None:
                    request["receipt"] = self._receipt(result)
                requests.append(request)
        queues = []
        for row in raw["compute_queue"]:
            queue = {key: row[key] for key in ("job_id", "session_id", "installation_sha256", "profile", "budget_seconds",
                "compute_command_id", "stale_cancel_command_id", "queue_state", "error_code", "created_at")}
            queue["input_basis"] = basis(row["basis_json"])
            queues.append(queue)
        audits = {}
        for table in ("accept_audit", "replay_audit"):
            audits[table] = [{**{key: row[key] for key in ("actor_id", "session_id", "request_id", "command_id", "recorded_at")},
                             "receipt": self._receipt(decode(row["receipt_json"]))} for row in raw[table]]
        observations = []
        for row in raw.get("evidence_observations", []):
            payload = decode(row["payload_json"])
            if hashlib.sha256(canonical_bytes(payload)).hexdigest() != row["payload_sha256"]:
                raise ValueError("Persisted evidence changed")
            if row["installation_sha256"] != session["installation_sha256"]:
                raise ValueError("Persisted evidence installation differs")
            if row["kind"] == "EXECUTION_VIEW":
                validate_execution(payload, session_id, build)
            elif row["kind"] == "JOB_VIEW":
                validate_job(payload, session_id, row["job_id"], build)
            else:
                raise ValueError("Unknown evidence kind")
            observations.append({**{key: row[key] for key in ("evidence_id", "kind", "job_id", "source", "reference_id", "captured_at", "payload_sha256")},
                                 "public_view": payload})
        notifications = []
        for row in raw.get("notifications", []):
            OutboxRepository.validate(session_id, {key: row[key] for key in ("session_id", "event_id", "kind", "body_sha256")})
            if row["installation_sha256"] != session["installation_sha256"] or type(row["cursor"]) is not int or not 0 < row["cursor"] < (1 << 63):
                raise ValueError("Notification installation/cursor differs")
            public = {key: row[key] for key in ("cursor", "installation_sha256", "session_id", "event_id", "kind", "body_sha256",
                                              "command_id", "created_at", "acked_at")}
            public["cursor"] = str(row["cursor"])
            public["ack_receipt"] = decode(row["ack_receipt_json"])
            if public["ack_receipt"] is not None and public["ack_receipt"] != {"status": "ACKNOWLEDGED", "event_id": row["event_id"]}:
                raise ValueError("Stored acknowledgement binding differs")
            notifications.append(public)
        recoveries = []
        for row in raw.get("recovery_audit", []):
            public = {key: row[key] for key in ("audit_id", "session_id", "worker_id", "command_id", "operation", "recorded_at", "diagnostic_code")}
            response = decode(row["response_json"])
            def projection(value):
                return None if value is None else {key: value[key] for key in ("status", "fenced_jobs", "basis", *VALIDATION_FIELDS) if key in value}
            public["response"] = ({"recovery": projection(response.get("recovery")), "validation": projection(response.get("validation"))}
                                  if response is not None and ("recovery" in response or "validation" in response) else projection(response))
            public["actor_type"] = "SERVER_WORKER"
            recoveries.append(public)
        recovery_status = []
        for row in raw.get("session_recovery", []):
            public = {key: row[key] for key in ("session_id", "status", "diagnostic_code", "checked_at", "worker_id", "installation_sha256")}
            value = decode(row["validation_json"])
            public["validation"] = None if value is None else {key: value[key] for key in VALIDATION_FIELDS if key in value}
            public["basis"] = basis(row["basis_json"])
            recovery_status.append(public)
        extensions = extension_metadata(raw, session_id, build, session["installation_sha256"])
        return {"session": public_session, "requests": requests, "queues": queues, "initial_frames": initial_frames,
                "extensions": extensions,
                **audits, "observations": observations, "notifications": notifications,
                "recovery_audit": recoveries, "recovery_status": recovery_status,
                "missing_optional_tables": [name for name in ("notifications", "session_recovery", "recovery_audit") if name not in raw]}

    def _validation(self, value, build):
        if (not isinstance(value, dict) or set(value) != VALIDATION_FIELDS or value["valid"] is not True
                or value["validator_version"] != "task02-m2-bound-session-validator/2"
                or value["current_checker_build_sha256"] != build or value["historical_execution_builds_preserved"] is not True
                or type(value["checked_physical_mutations"]) is not int or value["checked_physical_mutations"] < 0
                or not isinstance(value["scope"], str) or "SIMULATED_REPLAY" not in value["scope"]):
            raise ValueError("Verified public session validation receipt required")
        return value

    async def create(self, session_id, request_id, actor):
        session = self._session(session_id, actor)
        if actor.role != "dispatcher":
            raise ApiError(403, "FORBIDDEN", "role", "Dispatcher role required")
        installation = self.sessions.installation_identity()
        digest = hashlib.sha256(canonical_bytes({"operation": "artifact_export", "session_id": session_id})).hexdigest()
        row = None
        try:
            row = self.repository.reserve(actor.actor_id, session_id, request_id, digest, installation)
            if row["bundle"] is not None:
                return self.repository.get(session_id, row["artifact_id"])["manifest"]
            build = self.settings.installation()["expected_build_sha256"]
            captured_start = datetime.now(timezone.utc).isoformat()
            metadata = self._metadata(self.repository.metadata_snapshot(session_id), session_id, build)
            current = await self.gateway.resolve(session_id)
            validate_execution(current, session_id, build)
            validation = self._validation(await self.gateway.validate_session(session_id), build)
            jobs = []
            for queue in metadata["queues"]:
                view = await self.gateway.job_view(session_id, queue["job_id"])
                validate_job(view, session_id, queue["job_id"], build)
                if view["input_basis"] != queue["input_basis"]:
                    raise ValueError("Typed job differs from immutable submit basis")
                jobs.append({"source": "PUBLIC_SDK_JOB_VIEW_AT_EXPORT", "captured_at": datetime.now(timezone.utc).isoformat(), "public_view": view})
            final = await self.gateway.resolve(session_id)
            validate_execution(final, session_id, build)
            after_metadata = self._metadata(self.repository.metadata_snapshot(session_id), session_id, build)
            if final != current or canonical_bytes(after_metadata) != canonical_bytes(metadata):
                raise ApiError(409, "ARTIFACT_STATE_CHANGED", "artifact", "Session changed during capture; retry the same request ID")
            self._session(session_id, actor)
            if self.sessions.installation_identity() != installation:
                raise ApiError(409, "INSTALLATION_BINDING_CHANGED", "artifact", "Installation changed during capture")
            observed = self.repository.observe_execution(session_id, current, source="ARTIFACT_EXPORT", reference_id=row["artifact_id"])
            current_frame = {"source": observed["source"], "reference_id": row["artifact_id"], "evidence_id": observed["evidence_id"],
                "captured_at": observed["captured_at"], "capture_time_known": True, "basis": current["basis"],
                "simulation_time": current["current_time"], "public_view": current}
            frames = metadata["initial_frames"] + [{"source": item["source"], "reference_id": item["reference_id"],
                "evidence_id": item["evidence_id"], "captured_at": item["captured_at"], "capture_time_known": True,
                "basis": item["public_view"]["basis"], "simulation_time": item["public_view"]["current_time"],
                "public_view": item["public_view"]} for item in metadata["observations"]
                if item["kind"] == "EXECUTION_VIEW" and item["evidence_id"] != observed["evidence_id"]] + [current_frame]
            jobs += [{"source": item["source"], "reference_id": item["reference_id"], "captured_at": item["captured_at"],
                      "evidence_id": item["evidence_id"], "public_view": item["public_view"]}
                     for item in metadata["observations"] if item["kind"] == "JOB_VIEW"]
            trajectories = [{"source": frame["source"], "captured_at": frame["captured_at"], "basis": frame["basis"],
                             "accepted_trajectory": frame["public_view"]["accepted_trajectory"]}
                            for frame in frames if frame["public_view"]["accepted_trajectory"] is not None]
            accepts = metadata["accept_audit"]
            known_trajectories = {item["accepted_trajectory"]["job_id"] for item in trajectories}
            missing_trajectories = sorted({item["receipt"]["job_id"] for item in accepts} - known_trajectories)
            receipts = [item["receipt"] for item in accepts + metadata["replay_audit"]]
            missing_frames = [item.get("acceptance_id", item.get("mutation_id")) for item in receipts
                              if not any(frame["basis"] == item["basis"] for frame in frames)]
            captured_end = datetime.now(timezone.utc).isoformat()
            narrative_groups = [{"schema_version": "saferoute-m3-profile-comparison/1", "session_id": session_id,
                "comparison_id": item["comparison_id"], "mode": item["mode"], "status": item["status"],
                "input_basis": item["input_basis"], "outcome": item["outcome"],
                "jobs": [{"profile": member["profile"], "job_id": member["job_id"], "view": member["public_view"]} for member in item["jobs"]],
                "execution_mode": "SIMULATED_REPLAY", "real_world_observation": False,
                "metric_scope": "FORECAST_ONLY", "exposure_is_proxy": True}
                for item in metadata["extensions"]["comparisons"]]
            documents = {
                "decision_narrative.json": decision_narrative(current, session_id, build, comparisons=narrative_groups),
                "session.json": metadata["session"],
                "requests.json": {"records": metadata["requests"], "full_session_history": True,
                    "profile_comparison_requests": [{key: item[key] for key in ("comparison_id", "actor_id", "request_id", "request_sha256", "mode", "input_basis", "submission_receipt")}
                        for item in metadata["extensions"]["comparisons"]],
                    "profile_cancellations": metadata["extensions"]["cancellations"],
                    "playback_controls": metadata["extensions"]["playback_controls"],
                    "body_policy": "Stored request digests and normalized server inputs; original HTTP bodies are not retained",
                    "pending_policy": "PENDING_UNCERTAIN may have committed in SDK; no unsuccessful physical outcome is inferred"},
                "jobs.json": {"queue_records": metadata["queues"], "public_job_views": jobs, "forecast_only": True,
                    "profile_comparisons": metadata["extensions"]["comparisons"]},
                "acceptances.json": {"records": accepts, "full_session_history": True},
                "events.json": {"source_events": self.sessions.catalog.detail(session["scenario_id"])["events"],
                    "replay_audit": metadata["replay_audit"], "full_session_history": True,
                    "playback_audit": metadata["extensions"]["playback_audit"],
                    "playback_controller": metadata["extensions"]["playback_controller"]},
                "execution_frames.json": {"frames": frames, "execution_mode": "SIMULATED_REPLAY", "real_world_observation": False,
                    "basis_policy": "Each frame has its own observed basis; public frame bytes hash differs from private SDK head hash"},
                "accepted_trajectories.json": {"records": trajectories, "forecast": True, "missing_historical_job_ids": missing_trajectories},
                "notifications.json": {"records": metadata["notifications"], "full_persisted_session_history": True,
                    "authority_history_policy": "Only notifications persisted by M3 are available; pre-existing SDK acknowledgements cannot be reconstructed"},
                "recovery.json": {"status": metadata["recovery_status"], "audit": metadata["recovery_audit"]},
                "validation.json": {"source": "M2_PUBLIC_SDK_VALIDATE_SESSION", "captured_at": captured_end, "receipt": validation,
                    "basis_before_and_after": current["basis"]},
                "provenance.json": {"source": "M3_COMMITTED_METADATA_AND_PUBLIC_SDK", "runtime_build_sha256": build,
                    "installation_sha256": installation, "fixture_sha256": session["fixture_sha256"], "catalog_sha256": session["catalog_sha256"],
                    "capture_started_at": captured_start, "capture_finished_at": captured_end, "current_basis": current["basis"],
                    "simulation_time": current["current_time"], "limitations": LIMITATIONS,
                    "test_metadata": "No solver/native/E2E test verdict is inferred from an audit export",
                    "snapshot_policy": "M3 metadata and public SDK state were stable across capture checks; separate stores have no shared transaction",
                    "history_gaps": {"missing_exact_receipt_frame_ids": missing_frames, "missing_historical_accepted_job_ids": missing_trajectories,
                        "missing_optional_metadata_tables": metadata["missing_optional_tables"] + metadata["extensions"]["missing_optional_tables"],
                        "original_http_bodies_not_retained": True, "historical_mutation_commit_wall_time_not_retained": True,
                        "historical_simulation_times_may_be_unknown": True,
                        "pre_capture_job_lifecycle_transitions_not_reconstructed": True,
                        "no_private_authority_or_raw_job_records": True}}}
            files, manifest_files = [], []
            for name, document in documents.items():
                raw = canonical_bytes(document)
                files.append({"name": name, "content_utf8": raw.decode("utf-8")})
                manifest_files.append({"name": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "media_type": "application/json"})
            prefix = f"/api/sessions/{session_id}/artifacts/{row['artifact_id']}"
            manifest = {"schema_version": "saferoute-m3-artifact-manifest/2", "artifact_id": row["artifact_id"],
                "session_id": session_id, "request_id": request_id, "status": "READY", "created_at": row["created_at"],
                "captured_at": captured_end, "runtime_build_sha256": build, "captured_basis": current["basis"],
                "execution_mode": "SIMULATED_REPLAY", "real_world_observation": False, "limitations": LIMITATIONS,
                "files": manifest_files, "links": {"manifest": prefix, "content": prefix + "/content"}}
            complete = self.repository.complete(row, {"schema_version": "saferoute-m3-artifact-bundle/2", "manifest": manifest, "files": files})
            return complete["manifest"]
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "artifact", "Audit storage is unavailable; retry the same request ID") from error
        except (ValueError, KeyError, TypeError, IndexError, OverflowError) as error:
            raise ApiError(503, "ARTIFACT_BINDING_CHANGED", "artifact", "Public evidence or persisted audit binding could not be verified") from error
        finally:
            if row is not None and row["bundle"] is None:
                try:
                    self.repository.release(row)
                except (sqlite3.Error, OSError) as error:
                    raise ApiError(503, "METADATA_UNAVAILABLE", "artifact", "Audit claim could not be released; retry the same request ID") from error

    def _read(self, session_id, actor, operation):
        self._session(session_id, actor)
        try:
            return operation()
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "artifact", "Audit storage is unavailable") from error

    def list(self, session_id, actor):
        return self._read(session_id, actor, lambda: {"schema_version": "saferoute-m3-artifact-list/1", "session_id": session_id,
            "artifacts": [row["manifest"] for row in self.repository.list(session_id)], "full_session_list": True})

    def manifest(self, session_id, artifact_id, actor):
        return self._read(session_id, actor, lambda: self.repository.get(session_id, artifact_id)["manifest"])

    def content(self, session_id, artifact_id, actor):
        def read():
            row = self.repository.get(session_id, artifact_id)
            return {"schema_version": "saferoute-m3-artifact-content/1", "artifact_id": artifact_id, "session_id": session_id,
                    "encoding": "UTF-8", "sha256": row["bundle_sha256"], "bytes": row["bytes"], "content_utf8": row["content_utf8"]}
        return self._read(session_id, actor, read)
