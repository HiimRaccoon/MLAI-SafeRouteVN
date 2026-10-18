"""Immutable M3 audit exports and observed public evidence; no authority SQL."""
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from uuid import uuid4

from backend.api.errors import ApiError
from .session_repository import SessionRepository


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_basis(basis, session_id, build=None):
    keys = {"session_id", "root_sha256", "head_sha256", "head_version", "generation", "source_sha256",
            "context_version", "overlay_sha256", "build_sha256"}
    if not isinstance(basis, dict) or set(basis) != keys or basis["session_id"] != session_id:
        raise ValueError("Exact session basis required")
    for key in ("root_sha256", "head_sha256", "source_sha256", "build_sha256"):
        if not isinstance(basis[key], str) or not re.fullmatch(r"[a-f0-9]{64}", basis[key]):
            raise ValueError("Canonical basis hash required")
    for key in ("head_version", "generation"):
        if (not isinstance(basis[key], str) or not re.fullmatch(r"0|[1-9][0-9]{0,18}", basis[key])
                or int(basis[key]) >= 1 << 63):
            raise ValueError("Canonical basis counter required")
    if not isinstance(basis["context_version"], str) or not 1 <= len(basis["context_version"]) <= 200:
        raise ValueError("Context identifier required")
    overlay = basis["overlay_sha256"]
    if overlay is not None and (not isinstance(overlay, str) or not re.fullmatch(r"[a-f0-9]{64}", overlay)):
        raise ValueError("Overlay hash required")
    if build is not None and basis["build_sha256"] != build:
        raise ValueError("Installed build differs")


EXECUTION_KEYS = {"schema_version", "basis", "execution_mode", "real_world_observation", "current_time", "order_ids",
    "delivered_prefix", "planned_served_suffix", "unserved", "metric_scope", "observed_metrics", "vehicles",
    "active_job_id", "pending_event_ids", "planned_suffix_metrics", "projected_whole_metrics", "accepted_trajectory"}
JOB_KEYS = {"schema_version", "job_id", "job_status", "input_basis", "business_status", "internal_status", "diagnostics",
    "validation", "coverage_evaluated", "served_orders", "unserved_orders", "plan_available", "execution_view_required",
    "public_api_v1_dynamic_plan_available"}
DOCUMENT_NAMES = {"session.json", "requests.json", "jobs.json", "acceptances.json", "events.json", "execution_frames.json",
                  "accepted_trajectories.json", "notifications.json", "recovery.json", "validation.json", "provenance.json"}
DOCUMENT_NAMES_V2 = DOCUMENT_NAMES | {"decision_narrative.json"}


def validate_execution(view, session_id, build=None):
    if (not isinstance(view, dict) or set(view) not in (EXECUTION_KEYS,EXECUTION_KEYS|{'delivery_day','delivery_barrier'}) or view["schema_version"] != "task02-m2-execution-view/2"
            or view["execution_mode"] != "SIMULATED_REPLAY" or view["real_world_observation"] is not False
            or view["metric_scope"] != "OBSERVED_PREFIX_ONLY"):
        raise ValueError("Public simulated execution view required")
    if 'delivery_day' in view:
        day=view['delivery_day']
        if not isinstance(day,dict) or any(not isinstance(day.get(k),dict) for k in ('catalog','customers','queue','confirmations')) or type(day.get('complete')) is not bool:
            raise ValueError('Typed delivery-day policy required')
        if set(day['catalog'])!=set(view['order_ids']) or not set(day['queue'])<=set(day['catalog']):
            raise ValueError('Delivery-day order inventory differs')
        if view['delivery_barrier'] is not None:datetime.fromisoformat(view['delivery_barrier'])
    validate_basis(view["basis"], session_id, build)
    if not isinstance(view["current_time"], str) or datetime.fromisoformat(view["current_time"]).utcoffset() is None:
        raise ValueError("Simulation clock with timezone required")
    for key in ("order_ids", "delivered_prefix", "planned_served_suffix", "unserved", "vehicles", "pending_event_ids"):
        if not isinstance(view[key], list):
            raise ValueError("Public execution arrays required")
    for key in ("observed_metrics", "planned_suffix_metrics", "projected_whole_metrics", "accepted_trajectory"):
        if view[key] is not None and not isinstance(view[key], dict):
            raise ValueError("Nullable public execution object required")
    if view["active_job_id"] is not None and not isinstance(view["active_job_id"], str):
        raise ValueError("Active job identifier required")
    if view["accepted_trajectory"] is not None and view["accepted_trajectory"].get("job_id") != view["active_job_id"]:
        raise ValueError("Accepted trajectory/job binding differs")
    canonical_bytes(view)


def validate_job(view, session_id, job_id, build=None):
    if (not isinstance(view, dict) or set(view) != JOB_KEYS or view["schema_version"] != "task02-m2-runtime-job-view/1"
            or view["job_id"] != job_id or view["job_status"] not in ("QUEUED", "RUNNING", "COMPLETED", "FAILED")):
        raise ValueError("Typed public job view required")
    validate_basis(view["input_basis"], session_id, build)
    if not isinstance(view["validation"], dict) or not isinstance(view["diagnostics"], list):
        raise ValueError("Typed job validation and diagnostics required")
    if any(type(view[key]) is not bool for key in ("coverage_evaluated", "plan_available", "execution_view_required", "public_api_v1_dynamic_plan_available")):
        raise ValueError("Typed job flags required")
    if not isinstance(view["served_orders"], list) or not isinstance(view["unserved_orders"], list):
        raise ValueError("Typed coverage arrays required")
    if view["plan_available"] and (view["job_status"] != "COMPLETED" or view["validation"].get("valid") is not True):
        raise ValueError("Plan availability requires a certified completed job")
    canonical_bytes(view)


class ArtifactRepository(SessionRepository):
    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS artifacts(
                artifact_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, session_id TEXT NOT NULL,
                request_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, installation_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL, claim_token TEXT, claim_expires REAL NOT NULL DEFAULT 0,
                bundle BLOB, bundle_sha256 TEXT, manifest_json TEXT,
                UNIQUE(actor_id,session_id,request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS evidence_observations(
                evidence_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, installation_sha256 TEXT NOT NULL,
                kind TEXT NOT NULL, job_id TEXT, source TEXT NOT NULL, reference_id TEXT,
                captured_at TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL)""")

    def healthy(self):
        with self._db() as db:
            db.execute("SELECT artifact_id FROM artifacts LIMIT 1").fetchall()
            db.execute("SELECT evidence_id FROM evidence_observations LIMIT 1").fetchall()
        return True

    def reserve(self, actor_id, session_id, request_id, request_sha256, installation_sha256):
        token, now = uuid4().hex, time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM artifacts WHERE actor_id=? AND session_id=? AND request_id=?",
                             (actor_id, session_id, request_id)).fetchone()
            if row is not None:
                if row["request_sha256"] != request_sha256:
                    raise ApiError(409, "IDEMPOTENCY_CONFLICT", "request_id", "Artifact request ID was used with different content")
                if row["installation_sha256"] != installation_sha256:
                    raise ApiError(409, "INSTALLATION_BINDING_CHANGED", "artifact", "Artifact belongs to another installation")
                if row["bundle"] is not None:
                    return dict(row)
                if row["claim_token"] and row["claim_expires"] > now:
                    raise ApiError(409, "REQUEST_IN_PROGRESS", "request_id", "Artifact capture is in progress; retry the same request ID")
                artifact_id = row["artifact_id"]
            else:
                artifact_id = "artifact-" + uuid4().hex
                db.execute("""INSERT INTO artifacts(artifact_id,actor_id,session_id,request_id,request_sha256,installation_sha256,created_at)
                    VALUES(?,?,?,?,?,?,?)""", (artifact_id, actor_id, session_id, request_id, request_sha256,
                    installation_sha256, datetime.now(timezone.utc).isoformat()))
            db.execute("UPDATE artifacts SET claim_token=?,claim_expires=? WHERE artifact_id=?", (token, now + 600, artifact_id))
            return dict(db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone())

    def complete(self, row, bundle):
        raw = canonical_bytes(bundle)
        with self._db() as db:
            changed = db.execute("""UPDATE artifacts SET bundle=?,bundle_sha256=?,manifest_json=?,claim_token=NULL,claim_expires=0
                WHERE artifact_id=? AND claim_token=? AND claim_expires>? AND bundle IS NULL""",
                (raw, hashlib.sha256(raw).hexdigest(), canonical_bytes(bundle["manifest"]).decode(), row["artifact_id"],
                 row["claim_token"], time.time())).rowcount
            if changed != 1:
                raise ApiError(503, "ARTIFACT_LEASE_LOST", "request_id", "Artifact capture lease expired; retry the same request ID")
        return self.get(row["session_id"], row["artifact_id"])

    def release(self, row):
        with self._db() as db:
            db.execute("UPDATE artifacts SET claim_token=NULL,claim_expires=0 WHERE artifact_id=? AND claim_token=?",
                       (row["artifact_id"], row["claim_token"]))

    @staticmethod
    def verified(row):
        value = dict(row)
        if value["bundle"] is None:
            raise ApiError(409, "ARTIFACT_NOT_READY", "artifact_id", "Artifact capture has not completed")
        try:
            if not isinstance(value["bundle"], bytes):
                raise ValueError("Stored UTF-8 bytes required")
            raw = value["bundle"]
            if hashlib.sha256(raw).hexdigest() != value["bundle_sha256"]:
                raise ValueError("Bundle hash differs")
            bundle = json.loads(raw)
            if canonical_bytes(bundle) != raw:
                raise ValueError("Canonical JSON bytes required")
            manifest = json.loads(value["manifest_json"])
            version = {"saferoute-m3-artifact-bundle/1": "1", "saferoute-m3-artifact-bundle/2": "2"}.get(bundle["schema_version"])
            if (version is None or manifest["schema_version"] != "saferoute-m3-artifact-manifest/" + version or bundle["manifest"] != manifest
                    or manifest["artifact_id"] != value["artifact_id"] or manifest["session_id"] != value["session_id"]):
                raise ValueError("Manifest binding differs")
            files = {file["name"]: file for file in bundle["files"]}
            names = DOCUMENT_NAMES if version == "1" else DOCUMENT_NAMES_V2
            if (len(files) != len(bundle["files"]) or len(manifest["files"]) != len(names)
                    or set(files) != names or set(files) != {file["name"] for file in manifest["files"]}):
                raise ValueError("File inventory differs")
            for file in manifest["files"]:
                content = files[file["name"]]["content_utf8"]
                if type(content) is not str:
                    raise ValueError("UTF-8 public text required")
                data = content.encode("utf-8")
                if len(data) != file["bytes"] or hashlib.sha256(data).hexdigest() != file["sha256"]:
                    raise ValueError("File hash differs")
            value.update(manifest=manifest, content_utf8=raw.decode("utf-8"), bytes=len(raw))
            return value
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            raise ApiError(503, "ARTIFACT_CORRUPT", "artifact", "Stored artifact bytes or manifest could not be verified") from error

    def get(self, session_id, artifact_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM artifacts WHERE session_id=? AND artifact_id=?", (session_id, artifact_id)).fetchone()
        if row is None:
            raise ApiError(404, "ARTIFACT_NOT_FOUND", "artifact_id", "Artifact not found in this session")
        return self.verified(row)

    def list(self, session_id):
        with self._db() as db:
            rows = db.execute("SELECT * FROM artifacts WHERE session_id=? AND bundle IS NOT NULL ORDER BY created_at,artifact_id",
                              (session_id,)).fetchall()
        return [self.verified(row) for row in rows]

    def _observe(self, session_id, kind, view, source, reference_id=None, job_id=None):
        if not isinstance(source, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", source):
            raise ValueError("Server evidence source required")
        if reference_id is not None and (not isinstance(reference_id, str) or len(reference_id) > 200):
            raise ValueError("Opaque server reference required")
        raw = canonical_bytes(view)
        digest = hashlib.sha256(raw).hexdigest()
        installation = self.record(session_id)["installation_sha256"]
        key = hashlib.sha256(canonical_bytes([session_id, installation, kind, source, reference_id, digest])).hexdigest()
        evidence_id = "evidence-" + key
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO evidence_observations VALUES(?,?,?,?,?,?,?,?,?,?)",
                (evidence_id, session_id, installation, kind, job_id, source, reference_id,
                 datetime.now(timezone.utc).isoformat(), raw.decode(), digest))
            row = dict(db.execute("SELECT * FROM evidence_observations WHERE evidence_id=?", (evidence_id,)).fetchone())
            if row["payload_json"].encode() != raw or row["installation_sha256"] != installation:
                raise ApiError(503, "EVIDENCE_CONFLICT", "evidence", "Immutable evidence identifier changed content")
        return row

    def observe_execution(self, session_id, view, *, source, reference_id=None):
        try:
            validate_execution(view, session_id)
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            raise ApiError(503, "EVIDENCE_BINDING_CHANGED", "evidence", "Public execution evidence could not be verified") from error
        return self._observe(session_id, "EXECUTION_VIEW", view, source, reference_id)

    def observe_job(self, session_id, job_id, view, *, source, reference_id=None):
        try:
            validate_job(view, session_id, job_id)
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            raise ApiError(503, "EVIDENCE_BINDING_CHANGED", "evidence", "Public job evidence could not be verified") from error
        with self._db() as db:
            queued = db.execute("SELECT basis_json FROM compute_queue WHERE session_id=? AND job_id=?", (session_id, job_id)).fetchone()
        if queued is None or json.loads(queued["basis_json"]) != view["input_basis"]:
            raise ApiError(503, "EVIDENCE_BINDING_CHANGED", "evidence", "Evidence job differs from the persisted submit basis")
        return self._observe(session_id, "JOB_VIEW", view, source, reference_id, job_id)

    def metadata_snapshot(self, session_id):
        tables = ("sessions", "load_requests", "job_requests", "compute_queue", "accept_requests", "accept_audit",
                  "replay_requests", "replay_audit", "evidence_observations", "notifications", "session_recovery", "recovery_audit",
                  "profile_comparisons", "profile_cancel_requests", "playback_controllers", "playback_controls", "playback_audit")
        with self._db() as db:
            db.execute("BEGIN")
            existing = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            value = {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE session_id=? ORDER BY rowid", (session_id,))]
                     for table in tables if table in existing}
            if "profile_comparisons" in existing and "profile_members" in existing:
                value["profile_members"] = [dict(row) for row in db.execute(
                    "SELECT m.* FROM profile_members m JOIN profile_comparisons c ON c.comparison_id=m.comparison_id WHERE c.session_id=? ORDER BY m.rowid", (session_id,))]
        return value
