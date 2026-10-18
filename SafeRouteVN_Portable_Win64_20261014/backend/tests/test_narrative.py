from copy import deepcopy
import asyncio
import hashlib
import json

import pytest

from backend.services.artifact_repository import ArtifactRepository, canonical_bytes
from backend.services.narrative_service import decision_narrative
from backend.tests.test_sessions import setup, client, headers, load
from backend.tests.test_artifacts import artifacts_setup, artifacts_client, exported, session


def initial(client):
    sid = load(client).json()["data"]["session"]["session_id"]
    return sid, client.app.state.gateway.views[sid]


def test_initial_nulls_are_unknown_and_forecast_is_not_observed(client):
    sid, view = initial(client)
    response = client.get(f"/api/sessions/{sid}/narrative", headers=headers())
    assert response.status_code == 200
    result = response.json()["data"]
    assert result == decision_narrative(view, sid)
    assert result["basis"] == view["basis"] and result["delivered_count"] == 0
    assert all(block["metrics"] is None and block["available"] is False for block in result["blocks"])
    assert result["accepted_profile"] is None and result["real_world_observation"] is False


def test_exact_metrics_scopes_units_and_deterministic_text(client):
    sid, view = initial(client)
    view["observed_metrics"] = {"cost_vnd": 12.34, "distance_m": 2.34567890123456,
        "relative_exposure_proxy": 0.006, "service_time_us": 9007199254740991,
        "travel_time_us": 123456789, "waiting_time_us": 0}
    view["planned_suffix_metrics"] = {"total_cost_vnd": 9, "total_distance_m": 0.9,
        "total_exposure": 0.4, "total_travel_time_s": 1.234567}
    view["projected_whole_metrics"] = {"total_cost_vnd": 21.34, "total_distance_m": 3.24567890123456,
        "total_exposure": 0.406, "total_travel_time_s": 124.691356}
    before = deepcopy(view)
    one = decision_narrative(view, sid)
    assert one == decision_narrative(view, sid) and view == before
    assert [x["scope"] for x in one["blocks"]] == ["OBSERVED_PREFIX_ONLY", "PLANNED_SUFFIX_FORECAST", "PROJECTED_WHOLE_FORECAST"]
    assert one["blocks"][0]["metrics"] == view["observed_metrics"]
    assert "9007199254740991 us" in one["blocks"][0]["text"]
    assert "2.34567890123456 m" in one["blocks"][0]["text"]
    assert one["blocks"][1]["units"]["total_exposure"] == "PROXY"
    assert one["delivered_count"] == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, "2"])
def test_invalid_metric_never_becomes_narrative(client, bad):
    sid, view = initial(client)
    view["planned_suffix_metrics"] = {"total_cost_vnd": bad, "total_distance_m": 1,
        "total_exposure": 1, "total_travel_time_s": 1}
    assert client.get(f"/api/sessions/{sid}/narrative", headers=headers()).status_code == 503


def test_owner_checks_precede_sdk_and_build_changes_fail_closed(client):
    sid, view = initial(client)
    count = len(client.app.state.gateway.reads)
    assert client.get(f"/api/sessions/{sid}/narrative", headers=headers("bob")).status_code == 403
    assert len(client.app.state.gateway.reads) == count
    view["basis"]["build_sha256"] = "9" * 64
    assert client.get(f"/api/sessions/{sid}/narrative", headers=headers()).status_code == 503


@pytest.mark.parametrize("field,value", [
    ("delivered_prefix", ["O1", "O1"]), ("delivered_prefix", [2]),
    ("planned_served_suffix", ["unknown"]), ("pending_event_ids", [{}]),
    ("unserved", [{"order_id": "O1", "reason": 2}, {"order_id": "O2", "reason": "NO_ACCEPTED_PLAN"}]),
])
def test_invalid_public_facts_do_not_inflate_or_invent_counts(client, field, value):
    sid, view = initial(client)
    view[field] = value
    assert client.get(f"/api/sessions/{sid}/narrative", headers=headers()).status_code == 503


def test_unknown_trajectory_profile_and_job_are_rejected(client):
    sid, view = initial(client)
    view.update(active_job_id="job-a", accepted_trajectory={"job_id": "job-a", "profile": "UNREVIEWED", "forecast": True})
    assert client.get(f"/api/sessions/{sid}/narrative", headers=headers()).status_code == 503


@pytest.mark.parametrize("value", ["9007199254740992", "9223372036854775807", "0"])
def test_canonical_exact_microsecond_strings_preserved(client, value):
    sid, view = initial(client)
    view["observed_metrics"] = {"cost_vnd": 0, "distance_m": 0, "relative_exposure_proxy": 0,
        "travel_time_us": value, "service_time_us": 0, "waiting_time_us": 0}
    result = decision_narrative(view, sid)
    assert result["blocks"][0]["metrics"]["travel_time_us"] == value
    assert f"travel_time_us: {value} us" in result["blocks"][0]["text"]


@pytest.mark.parametrize("value", ["01", "-1", "1.0", "9223372036854775808", True, 1.5])
def test_noncanonical_or_unsafe_microseconds_rejected(client, value):
    sid, view = initial(client)
    view["observed_metrics"] = {"cost_vnd": 0, "distance_m": 0, "relative_exposure_proxy": 0,
        "travel_time_us": value, "service_time_us": 0, "waiting_time_us": 0}
    with pytest.raises(ValueError):
        decision_narrative(view, sid)


def test_new_export_captures_narrative_and_old_v1_remains_readable(artifacts_client, artifacts_setup):
    sid = session(artifacts_client)
    manifest, wrapper, bundle, files = exported(artifacts_client, sid)
    assert bundle["schema_version"] == "saferoute-m3-artifact-bundle/2"
    assert len(files) == 12 and files["decision_narrative.json"]["basis"] == manifest["captured_basis"]
    assert files["decision_narrative.json"] == decision_narrative(artifacts_setup[1].views[sid], sid)
    old = deepcopy(bundle)
    old["schema_version"] = "saferoute-m3-artifact-bundle/1"
    old["manifest"]["schema_version"] = "saferoute-m3-artifact-manifest/1"
    old["files"] = [f for f in old["files"] if f["name"] != "decision_narrative.json"]
    old["manifest"]["files"] = [f for f in old["manifest"]["files"] if f["name"] != "decision_narrative.json"]
    raw = canonical_bytes(old)
    record = {"artifact_id": manifest["artifact_id"], "session_id": sid, "bundle": raw,
        "bundle_sha256": hashlib.sha256(raw).hexdigest(), "manifest_json": json.dumps(old["manifest"])}
    assert ArtifactRepository.verified(record)["content_utf8"].encode() == raw
    old["manifest"]["schema_version"] = "saferoute-m3-artifact-manifest/2"
    raw = canonical_bytes(old)
    record.update(bundle=raw, bundle_sha256=hashlib.sha256(raw).hexdigest(), manifest_json=json.dumps(old["manifest"]))
    with pytest.raises(Exception) as caught:
        ArtifactRepository.verified(record)
    assert caught.value.code == "ARTIFACT_CORRUPT"


def test_comparison_narrative_is_owned_cached_and_included_in_new_export(artifacts_client, artifacts_setup):
    sid = session(artifacts_client)
    root = f"/api/sessions/{sid}/profiles"
    receipt = artifacts_client.post(root + "/compare", headers=headers(), json={"request_id": "narrative-batch"}).json()["data"]
    path = root + "/comparisons/" + receipt["comparison_id"]
    assert artifacts_client.post(path + "/cancel", headers=headers(), json={"request_id": "narrative-cancel"}).status_code == 200
    asyncio.run(artifacts_client.app.state.profile_service.reconcile_one())
    reads = len(artifacts_setup[1].reads)
    response = artifacts_client.get(path + "/narrative", headers=headers())
    assert response.status_code == 200 and response.json()["data"]["profiles"] == []
    assert len(artifacts_setup[1].reads) == reads
    assert artifacts_client.get(path + "/narrative", headers=headers("bob")).status_code == 403
    _, _, _, files = exported(artifacts_client, sid)
    assert files["decision_narrative.json"]["profile_comparisons"] == [response.json()["data"]]
    assert len(files["jobs.json"]["profile_comparisons"]) == 1
    assert len(files["requests.json"]["profile_cancellations"]) == 1
