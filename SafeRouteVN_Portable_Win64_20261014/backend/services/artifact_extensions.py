"""Allowlisted persisted M3 comparison/playback history for artifact /2.

No SDK invocation, private authority read, physical inference or verdict is
performed here. Stored public projections are checked before being exported.
"""
from datetime import datetime, timezone
import json
import math
import re
from backend.models.playback import PLAYBACK_SPEEDS

from .artifact_repository import canonical_bytes, validate_basis, validate_job
from .profile_repository import PROFILES
from .runtime_gateway import RuntimeGateway

TABLES = ("profile_comparisons", "profile_members", "profile_cancel_requests",
          "playback_controls", "playback_audit", "playback_controllers")
CONTROLLER_KEYS = {"mode", "paused", "fully_paused", "in_flight", "reason", "speed",
                   "controller_revision", "next_tick_at", "end_time"}
COMPARISON_METRICS = {"total_distance_m", "total_travel_time_s", "total_exposure",
                      "total_cost_vnd", "total_soft_lateness_s"}


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", value):
        raise ValueError("Canonical public identifier required")
    return value


def _actor(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value):
        raise ValueError("Public actor identifier required")
    return value


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Canonical digest required")
    return value


def _code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,199}", value):
        raise ValueError("Public reason code required")
    return value


def _stamp(value):
    if not isinstance(value, str) or datetime.fromisoformat(value).utcoffset() is None:
        raise ValueError("Timestamp with timezone required")
    return value


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Nonnegative finite metadata timestamp required")
    return value


def _decode(value):
    return json.loads(value) if value is not None else None


def _bound(row, sid, installation, *, install=True):
    if row["session_id"] != sid:
        raise ValueError("Stored history session differs")
    if install and row["installation_sha256"] != installation:
        raise ValueError("Stored history installation differs")


def _controller(value):
    if not isinstance(value, dict) or not CONTROLLER_KEYS <= set(value):
        raise ValueError("Public controller required")
    out = {key: value[key] for key in CONTROLLER_KEYS}
    for key in ("paused", "fully_paused", "in_flight"):
        if type(out[key]) is not bool:
            raise ValueError("Typed controller flag required")
    if (out["mode"] not in ("STEP", "AUTOMATIC") or out["paused"] != (out["mode"] == "STEP")
            or out["fully_paused"] != (out["paused"] and not out["in_flight"])
            or type(out["speed"]) is not int or out["speed"] not in PLAYBACK_SPEEDS):
        raise ValueError("Controller lifecycle/cadence differs")
    revision = out["controller_revision"]
    if not isinstance(revision, str) or not re.fullmatch(r"0|[1-9][0-9]{0,18}", revision) or int(revision) >= 1 << 63:
        raise ValueError("Canonical int64 controller revision required")
    _code(out["reason"])
    for key in ("next_tick_at", "end_time"):
        if out[key] is not None:
            _stamp(out[key])
    if out["paused"] and out["next_tick_at"] is not None:
        raise ValueError("Paused controller cannot schedule a tick")
    return out


def _playback_receipt(value, sid):
    base = {"schema_version", "receipt_id", "session_id", "operation", "source", "recorded_at"}
    if (not isinstance(value, dict) or not base <= set(value)
            or value["schema_version"] != "saferoute-m3-playback-receipt/1"
            or value["session_id"] != sid or value["source"] != "M3_PLAYBACK_CONTROL"):
        raise ValueError("Bound public playback receipt required")
    out = {key: value[key] for key in base}
    _id(out["receipt_id"])
    _stamp(out["recorded_at"])
    op = out["operation"]
    if op in ("start", "speed", "pause"):
        out.update(actor_id=_actor(value["actor_id"]), request_id=_id(value["request_id"]),
                   controller=_controller(value["controller"]))
    elif op == "stop":
        if type(value["in_flight"]) is not bool:
            raise ValueError("Typed in-flight flag required")
        out.update(reason=_code(value["reason"]), in_flight=value["in_flight"])
    elif op == "tick_settled":
        if value["status"] not in ("ADVANCED", "NOOP", "REJECTED"):
            raise ValueError("Public tick lifecycle required")
        out.update(mutation_id=_id(value["mutation_id"]), status=value["status"],
            reason=None if value["reason"] is None else _code(value["reason"]),
            target_time=_stamp(value["target_time"]), controller=_controller(value["controller"]))
    else:
        raise ValueError("Known playback receipt operation required")
    return out


def extension_metadata(raw, session_id, build, installation):
    """Project all persisted rows, including history beyond HTTP's 100 limit."""
    _id(session_id)
    _hash(build)
    _hash(installation)
    missing = [table for table in TABLES if table not in raw]
    comparisons, cancellations, controls, audits = [], [], [], []
    groups = {}
    for row in raw.get("profile_comparisons", []):
        _bound(row, session_id, installation)
        cid = _id(row["comparison_id"])
        if cid in groups:
            raise ValueError("Duplicate comparison identity")
        groups[cid] = row
    members = {cid: [] for cid in groups}
    for member in raw.get("profile_members", []):
        if member["comparison_id"] not in groups:
            raise ValueError("Member is outside captured session comparisons")
        members[member["comparison_id"]].append(member)
    for cid, row in groups.items():
        basis = _decode(row["basis_json"])
        validate_basis(basis, session_id, build)
        mode, state = row["mode"], row["state"]
        if mode not in ("NEW_BATCH", "EXISTING_JOBS") or state not in ("QUEUED", "RUNNING", "CANCEL_REQUESTED", "COMPLETED", "CANCELLED", "FAILED"):
            raise ValueError("Known comparison mode/lifecycle required")
        receipt = _decode(row["receipt_json"])
        keys = {"schema_version", "session_id", "comparison_id", "mode", "input_basis", "profiles", "links"}
        prefix = f"/api/sessions/{session_id}/profiles/comparisons/{cid}"
        if (not isinstance(receipt, dict) or set(receipt) != keys or receipt["schema_version"] != "saferoute-m3-profile-submission/1"
                or receipt["session_id"] != session_id or receipt["comparison_id"] != cid or receipt["mode"] != mode
                or receipt["input_basis"] != basis or receipt["profiles"] != list(PROFILES)
                or receipt["links"] != {"poll": prefix, "cancel": prefix + "/cancel"}):
            raise ValueError("Immutable profile submission receipt differs")
        rows = members[cid]
        if len(rows) != 3 or {m["profile"] for m in rows} != set(PROFILES):
            raise ValueError("Exactly three persisted profile members required")
        rows.sort(key=lambda m: PROFILES.index(m["profile"]))
        jobs = []
        for member in rows:
            jid, view = member["job_id"], _decode(member["view_json"])
            if jid is not None:
                _id(jid)
            elif view is not None or mode == "EXISTING_JOBS":
                raise ValueError("Typed child requires a persisted job")
            if view is not None:
                validate_job(view, session_id, jid, build)
                if mode == "NEW_BATCH" and view["input_basis"] != basis:
                    raise ValueError("Created batch child basis differs")
            jobs.append({"profile": member["profile"], "job_id": jid, "public_view": view})
        identifiers = [j["job_id"] for j in jobs if j["job_id"] is not None]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Comparison children must be distinct")
        outcome = _decode(row["outcome_json"])
        if outcome is not None:
            if not isinstance(outcome, dict) or set(outcome) != {"comparison", "reason"}:
                raise ValueError("Exact comparison outcome required")
            result = outcome["comparison"]
            if result is not None:
                RuntimeGateway.check_comparison(result, session_id, [j["job_id"] for j in jobs], build)
                if state != "COMPLETED" or outcome["reason"] != result["reason"]:
                    raise ValueError("Completed SDK comparison reason differs")
                for job, compared in zip(jobs, result["jobs"]):
                    view = job["public_view"]
                    if (view is None or view["job_status"] != "COMPLETED" or view["validation"].get("valid") is not True
                            or compared["profile"] != job["profile"] or compared["basis"] != view["input_basis"]):
                        raise ValueError("SDK comparison does not bind every certified public child")
                    metrics = compared["metrics"]
                    if set(metrics) != COMPARISON_METRICS:
                        raise ValueError("Exact frozen SDK public metric fields required")
                    for number in metrics.values():
                        _number(number)
            else:
                _code(outcome["reason"])
                if state not in ("COMPLETED", "CANCELLED", "FAILED"):
                    raise ValueError("Outcome belongs to a terminal comparison")
        elif state in ("COMPLETED", "CANCELLED", "FAILED"):
            raise ValueError("Terminal comparison requires its persisted outcome")
        comparisons.append({"comparison_id": cid, "session_id": session_id, "actor_id": _actor(row["actor_id"]),
            "request_id": _id(row["request_id"]), "request_sha256": _hash(row["request_sha256"]),
            "installation_sha256": installation, "mode": mode, "input_basis": basis, "status": state,
            "outcome": outcome, "submission_receipt": receipt, "created_at": _number(row["created_at"]),
            "updated_at": _number(row["updated_at"]), "jobs": jobs})
    for row in raw.get("profile_cancel_requests", []):
        _bound(row, session_id, installation)
        cid = row["comparison_id"]
        if cid not in groups:
            raise ValueError("Cancellation comparison is outside this session")
        receipt = _decode(row["response_json"])
        if (not isinstance(receipt, dict) or set(receipt) != {"schema_version", "session_id", "comparison_id", "status", "affects_existing_jobs"}
                or receipt["schema_version"] != "saferoute-m3-profile-cancellation/1" or receipt["session_id"] != session_id
                or receipt["comparison_id"] != cid or receipt["status"] not in ("CANCEL_REQUESTED", "COMPLETED_IMMUTABLE")
                or receipt["affects_existing_jobs"] is not False):
            raise ValueError("Public profile cancellation receipt differs")
        cancellations.append({"actor_id": _actor(row["actor_id"]), "session_id": session_id,
            "request_id": _id(row["request_id"]), "comparison_id": cid, "installation_sha256": installation, "receipt": receipt})
    states = raw.get("playback_controllers", [])
    if len(states) > 1:
        raise ValueError("One captured playback controller required")
    controller = None
    if states:
        row = states[0]
        _bound(row, session_id, installation)
        basis = _decode(row["basis_json"])
        if basis is not None:
            validate_basis(basis, session_id, build)
        if row["status"] not in ("RUNNING", "PAUSED") or type(row["epoch"]) is not int or not 0 <= row["epoch"] < 1 << 63:
            raise ValueError("Known durable controller lifecycle required")
        pending, running = row["pending_request"] is not None, row["status"] == "RUNNING"
        if pending:
            _id(row["pending_request"])
            _actor(row["pending_actor"])
        if running and (basis is None or row["end_time"] is None):
            raise ValueError("Running playback requires its accepted basis/endpoint")
        _number(row["next_due"])
        public = _controller({"mode": "AUTOMATIC" if running else "STEP", "paused": not running,
            "fully_paused": not running and not pending, "in_flight": pending, "reason": row["reason"],
            "speed": row["speed"], "controller_revision": str(row["epoch"]),
            "next_tick_at": datetime.fromtimestamp(row["next_due"], timezone.utc).isoformat() if running else None,
            "end_time": row["end_time"]})
        controller = {"session_id": session_id, "actor_id": _actor(row["actor_id"]),
            "installation_sha256": installation, "basis": basis, "updated_at": _stamp(row["updated_at"]), **public}
    for row in raw.get("playback_controls", []):
        _bound(row, session_id, installation)
        receipt = _playback_receipt(_decode(row["receipt_json"]), session_id)
        if (receipt["operation"] != row["operation"] or row["operation"] not in ("start", "speed", "pause")
                or receipt["actor_id"] != row["actor_id"] or receipt["request_id"] != row["request_id"]):
            raise ValueError("Playback control row and receipt differ")
        controls.append({"actor_id": _actor(row["actor_id"]), "session_id": session_id, "operation": row["operation"],
            "request_id": _id(row["request_id"]), "request_sha256": _hash(row["request_sha256"]),
            "installation_sha256": installation, "receipt": receipt})
    for row in raw.get("playback_audit", []):
        _bound(row, session_id, installation, install=False)
        if controller is None:
            raise ValueError("Playback audit installation needs its persisted controller")
        receipt = _playback_receipt(_decode(row["receipt_json"]), session_id)
        if receipt["receipt_id"] != row["receipt_id"] or receipt["recorded_at"] != row["recorded_at"]:
            raise ValueError("Playback audit identity/timestamp differs")
        audits.append({"receipt_id": _id(row["receipt_id"]), "session_id": session_id,
            "recorded_at": _stamp(row["recorded_at"]), "receipt": receipt})
    result = {"comparisons": comparisons, "cancellations": cancellations, "playback_controls": controls,
              "playback_audit": audits, "playback_controller": controller, "missing_optional_tables": missing}
    canonical_bytes(result)
    return result
