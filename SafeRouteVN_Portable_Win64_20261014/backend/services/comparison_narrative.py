"""Factual historical profile explanations from cached public projections only."""
from copy import deepcopy
import math

from .artifact_extensions import _id, _code
from .artifact_repository import validate_basis, validate_job, canonical_bytes
from .profile_repository import PROFILES
from .runtime_gateway import RuntimeGateway

METRIC_UNITS = {"total_cost_vnd": "VND", "total_distance_m": "m", "total_exposure": "PROXY",
                "total_travel_time_s": "s", "total_soft_lateness_s": "s"}


def comparison_narrative(group, build=None):
    """Copy verified scalar metrics; never compute a rank, score or percentage.

    The public SDK verdict is retained unchanged. This validator rejects a
    contradictory stored verdict instead of deriving a replacement verdict.
    A later physical head does not refresh or activate these historical jobs.
    """
    if (not isinstance(group, dict) or group["schema_version"] != "saferoute-m3-profile-comparison/1"
            or group["mode"] not in ("NEW_BATCH", "EXISTING_JOBS")
            or group["status"] not in ("QUEUED", "RUNNING", "CANCEL_REQUESTED", "COMPLETED", "CANCELLED", "FAILED")
            or group["execution_mode"] != "SIMULATED_REPLAY" or group["real_world_observation"] is not False
            or group["metric_scope"] != "FORECAST_ONLY" or group["exposure_is_proxy"] is not True):
        raise ValueError("Public simulated comparison group required")
    sid, cid, basis = _id(group["session_id"]), _id(group["comparison_id"]), group["input_basis"]
    validate_basis(basis, sid, build)
    build = basis["build_sha256"]
    members = group["jobs"]
    if (not isinstance(members, list) or len(members) != 3
            or any(not isinstance(m, dict) or set(m) != {"profile", "job_id", "view"} for m in members)
            or {m["profile"] for m in members} != set(PROFILES)):
        raise ValueError("Three exact public profile members required")
    members = sorted(members, key=lambda m: PROFILES.index(m["profile"]))
    jobs, identifiers = [], []
    for member in members:
        jid, view = member["job_id"], member["view"]
        if jid is not None:
            _id(jid)
            identifiers.append(jid)
        elif view is not None or group["mode"] == "EXISTING_JOBS":
            raise ValueError("Public child requires its persisted identifier")
        certified, covered = False, False
        if view is not None:
            validate_job(view, sid, jid, build)
            if group["mode"] == "NEW_BATCH" and view["input_basis"] != basis:
                raise ValueError("Created batch and child full basis differ")
            certified = view["job_status"] == "COMPLETED" and view["validation"].get("valid") is True
            covered = certified and view["coverage_evaluated"]
            served = [_id(oid) for oid in view["served_orders"]]
            unserved = [_id(item["order_id"]) for item in view["unserved_orders"]]
            if len(set(served)) != len(served) or len(set(unserved)) != len(unserved) or set(served) & set(unserved):
                raise ValueError("Typed coverage identifiers are duplicated or overlap")
        jobs.append({"profile": member["profile"], "job_id": jid,
            "job_status": None if view is None else view["job_status"],
            "business_status": None if view is None else view["business_status"],
            "witness_certified": certified, "coverage_evaluated": bool(covered),
            "served_count": len(view["served_orders"]) if covered else None,
            "unserved_count": len(view["unserved_orders"]) if covered else None})
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Distinct persisted children required")
    outcome, result, reason = group["outcome"], None, None
    if outcome is not None:
        if not isinstance(outcome, dict) or set(outcome) != {"comparison", "reason"}:
            raise ValueError("Exact public group outcome required")
        result, reason = outcome["comparison"], outcome["reason"]
        if result is None:
            _code(reason)
            if group["status"] == "COMPLETED" and (reason != "NO_CERTIFIED_WITNESS_FOR_EVERY_PROFILE"
                    or all(j["witness_certified"] for j in jobs)):
                raise ValueError("No-witness outcome must match its typed children")
        elif group["status"] != "COMPLETED":
            raise ValueError("SDK comparison belongs to a completed group")
    elif group["status"] in ("COMPLETED", "CANCELLED", "FAILED"):
        raise ValueError("Terminal group requires its persisted outcome")
    profiles = []
    verdict = None
    if result is not None:
        RuntimeGateway.check_comparison(result, sid, [m["job_id"] for m in members], build)
        verdict = result["status"]
        if reason != result["reason"]:
            raise ValueError("SDK reason differs from stored outcome")
        for member, compared in zip(members, result["jobs"]):
            view = member["view"]
            if (view is None or view["job_status"] != "COMPLETED" or view["validation"].get("valid") is not True
                    or not view["coverage_evaluated"] or compared["profile"] != member["profile"]
                    or compared["basis"] != view["input_basis"]):
                raise ValueError("Comparison must bind every certified typed profile")
            metrics = compared["metrics"]
            if (set(metrics) != set(METRIC_UNITS) or any(type(n) not in (int, float) or not math.isfinite(n) or n < 0 for n in metrics.values())):
                raise ValueError("Exact finite nonnegative public comparison metrics required")
        same = (all(r["basis"] == result["jobs"][0]["basis"] for r in result["jobs"])
                and len({r["domain_sha256"] for r in result["jobs"]}) == 1)
        if same != (verdict == "COMPARABLE"):
            raise ValueError("Stored SDK verdict contradicts authenticated basis/domain fields")
        if verdict == "COMPARABLE":
            for child, compared in zip(jobs, result["jobs"]):
                profiles.append({"profile": compared["profile"], "job_id": compared["job_id"],
                    "scope": "HISTORICAL_FORECAST", "input_basis": deepcopy(compared["basis"]),
                    "domain_sha256": compared["domain_sha256"], "metrics": deepcopy(compared["metrics"]),
                    "units": dict(METRIC_UNITS), "served_count": child["served_count"], "unserved_count": child["unserved_count"]})
            text = "Ba phương án có cùng đầu vào trạng thái đã xác thực và cùng miền vật lý theo SDK. Bảng giữ nguyên số liệu dự báo của từng profile để người điều phối xem chi phí, quãng đường, thời gian, độ trễ và exposure proxy."
        else:
            text = "SDK báo NON_COMPARABLE vì đầu vào trạng thái đã xác thực hoặc miền vật lý khác nhau. Chưa có cơ sở cho một bảng so sánh trade-off giữa ba phương án này."
    elif group["status"] == "COMPLETED":
        text = "Chưa có witness được chứng nhận cho mọi profile. Hãy xem trạng thái và chẩn đoán của từng job; chưa có bảng so sánh trade-off."
    elif group["status"] in ("CANCELLED", "FAILED"):
        text = "Yêu cầu so sánh đã kết thúc với lý do được lưu trong kết quả. Chưa có bảng so sánh trade-off."
    else:
        text = "Yêu cầu so sánh đang chờ kết quả được chứng nhận từ các job. Số liệu trade-off sẽ có khi SDK xác nhận COMPARABLE."
    value = {"schema_version": "saferoute-m3-comparison-narrative/1", "language": "vi", "session_id": sid,
        "comparison_id": cid, "group_status": group["status"], "comparison_status": verdict,
        "reason": reason, "source": "M2_PUBLIC_SDK_COMPARISON" if result is not None else "PUBLIC_M3_COMPARISON_LIFECYCLE",
        "scope": "HISTORICAL_FORECAST", "captured_group_basis": deepcopy(basis), "jobs": jobs,
        "profiles": profiles, "text": text, "execution_mode": "SIMULATED_REPLAY", "real_world_observation": False,
        "auto_accept_plan": False, "statements": [
            "Số liệu thuộc các dự báo lịch sử; chọn một phương án cần kiểm tra lại trạng thái hiện tại khi accept.",
            "Exposure là chỉ số proxy; không biểu diễn xác suất tai nạn.",
            "Kết quả giữ nguyên verdict của SDK và không chứng minh tối ưu toàn cục."],
        "limitations": ["HISTORICAL_FORECAST_REQUIRES_CURRENT_ACCEPT_CAS", "EXPOSURE_IS_PROXY", "NOT_OPTIMALITY", "SIMULATED_REPLAY_NOT_GPS"]}
    canonical_bytes(value)
    return value
