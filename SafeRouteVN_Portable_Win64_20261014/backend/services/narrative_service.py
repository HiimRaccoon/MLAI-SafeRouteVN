"""Deterministic explanations of public execution facts, never solver recommendations."""
from copy import deepcopy
import json
import math
import re

from backend.api.errors import ApiError
from .artifact_repository import validate_execution
from .comparison_narrative import comparison_narrative


OBSERVED_UNITS = {"cost_vnd": "VND", "distance_m": "m", "relative_exposure_proxy": "PROXY",
                  "service_time_us": "us", "travel_time_us": "us", "waiting_time_us": "us"}
FORECAST_UNITS = {"total_cost_vnd": "VND", "total_distance_m": "m", "total_exposure": "PROXY",
                  "total_travel_time_s": "s"}


def _metrics(value, units):
    if value is None:
        return None
    if set(value) != set(units):
        raise ValueError("Public metric fields differ")
    for key, number in value.items():
        if units[key] == "us" and type(number) is str:
            if re.fullmatch(r"0|[1-9][0-9]{0,18}", number) is None or int(number) >= 1 << 63:
                raise ValueError("Canonical nonnegative int64 microseconds required")
            continue
        if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
            raise ValueError("Finite nonnegative public metric required")
        if units[key] == "us" and (type(number) is not int or number >= 1 << 63):
            raise ValueError("Exact integer microseconds required")
    return deepcopy(value)


def _ids(values):
    if any(type(key) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", key) is None for key in values):
        raise ValueError("Public identifiers required")
    if len(set(values)) != len(values):
        raise ValueError("Unique public identifiers required")
    return set(values)


def _facts(view):
    orders = _ids(view["order_ids"])
    delivered = _ids(view["delivered_prefix"])
    planned = _ids(view["planned_served_suffix"])
    _ids(view["pending_event_ids"])
    for row in view["unserved"]:
        if (type(row) is not dict or not {"order_id", "reason"} <= set(row)
                or not set(row) <= {"order_id", "reason", "owner_vehicle_id"}
                or type(row["reason"]) is not str or not 1 <= len(row["reason"]) <= 200):
            raise ValueError("Typed unserved facts required")
        if "owner_vehicle_id" in row and row["owner_vehicle_id"] is not None:
            _ids([row["owner_vehicle_id"]])
    unserved = _ids([row["order_id"] for row in view["unserved"]])
    if delivered & planned or delivered & unserved or planned & unserved or delivered | planned | unserved != orders:
        raise ValueError("Exact public order partition required")
    trajectory = view["accepted_trajectory"]
    if view["active_job_id"] is None:
        if trajectory is not None or planned:
            raise ValueError("No active plan has no accepted trajectory or planned served suffix")
    elif (trajectory is None or trajectory.get("forecast") is not True
            or trajectory.get("profile") not in ("FASTEST", "BALANCED", "SAFER")
            or trajectory.get("job_id") != view["active_job_id"]):
        raise ValueError("Certified public trajectory profile/job binding required")


def decision_narrative(view, session_id, build=None, *, comparisons=()):
    validate_execution(view, session_id, build)
    _facts(view)
    observed = _metrics(view["observed_metrics"], OBSERVED_UNITS)
    suffix = _metrics(view["planned_suffix_metrics"], FORECAST_UNITS)
    whole = _metrics(view["projected_whole_metrics"], FORECAST_UNITS)
    blocks = []
    for scope, title, value, units in (
            ("OBSERVED_PREFIX_ONLY", "Đã thực hiện trong replay mô phỏng", observed, OBSERVED_UNITS),
            ("PLANNED_SUFFIX_FORECAST", "Dự báo phần kế hoạch còn lại", suffix, FORECAST_UNITS),
            ("PROJECTED_WHOLE_FORECAST", "Dự báo tổng hành trình", whole, FORECAST_UNITS)):
        text = ("Chưa có số liệu cho phạm vi này." if value is None else
                "; ".join(f"{key}: {number if type(number) is str else json.dumps(number, allow_nan=False)} {units[key]}" for key, number in value.items()))
        blocks.append({"scope": scope, "title": title, "available": value is not None,
                       "metrics": value, "units": dict(units), "text": text})
    trajectory = view["accepted_trajectory"]
    return {"schema_version": "saferoute-m3-decision-narrative/1", "language": "vi",
            "source": "VALIDATED_PUBLIC_EXECUTION_VIEW", "basis": deepcopy(view["basis"]),
            "current_time": view["current_time"], "execution_mode": "SIMULATED_REPLAY",
            "real_world_observation": False, "active_job_id": view["active_job_id"],
            "accepted_profile": None if trajectory is None else trajectory["profile"],
            "delivered_count": len(view["delivered_prefix"]),
            "planned_served_count": len(view["planned_served_suffix"]),
            "unserved": deepcopy(view["unserved"]), "pending_event_ids": list(view["pending_event_ids"]),
            "blocks": blocks,
            "profile_comparisons": [comparison_narrative(group, build=build) for group in comparisons],
            "statements": [f"Replay đã ghi nhận giao {len(view['delivered_prefix'])} đơn trong mô phỏng.",
                f"Kế hoạch còn lại dự kiến phục vụ {len(view['planned_served_suffix'])} đơn; đây là dự báo.",
                "Exposure là chỉ số proxy; không phải xác suất tai nạn hoặc chứng nhận an toàn.",
                "Kết quả không chứng minh tối ưu toàn cục; narrative không tự chọn hoặc chấp nhận profile."],
            "limitations": ["SIMULATED_REPLAY_NOT_GPS", "EXPOSURE_IS_PROXY", "NOT_OPTIMALITY"]}


class NarrativeService:
    def __init__(self, settings, sessions):
        self.settings, self.sessions = settings, sessions

    async def get(self, session_id, actor):
        row = self.sessions.session(session_id)
        if row["owner_actor_id"] != actor.actor_id:
            raise ApiError(403, "FORBIDDEN", "session_id", "Session access denied")
        view = await self.sessions.state(session_id)
        try:
            return decision_narrative(view, session_id, self.settings.installation()["expected_build_sha256"])
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            raise ApiError(503, "NARRATIVE_BINDING_CHANGED", "narrative", "Validated public metric evidence required") from error
