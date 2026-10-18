"""Factual profile trade-off explanations retain source precision and nulls."""
from copy import deepcopy
import json

import pytest

from backend.services.artifact_extensions import extension_metadata
from backend.services.comparison_narrative import METRIC_UNITS, comparison_narrative
from backend.tests.test_artifact_extensions import raw, SESSION, BUILD, INSTALLATION


@pytest.fixture
def group(raw):
    row = extension_metadata(raw, SESSION, BUILD, INSTALLATION)["comparisons"][0]
    return {"schema_version": "saferoute-m3-profile-comparison/1", "session_id": row["session_id"],
        "comparison_id": row["comparison_id"], "mode": row["mode"], "status": row["status"], "input_basis": row["input_basis"],
        "jobs": [{"profile": m["profile"], "job_id": m["job_id"], "view": m["public_view"]} for m in row["jobs"]],
        "outcome": row["outcome"], "execution_mode": "SIMULATED_REPLAY", "real_world_observation": False,
        "metric_scope": "FORECAST_ONLY", "exposure_is_proxy": True}


def test_comparable_copies_actual_five_metrics_units_counts_and_full_basis_without_mutation(group):
    result = group["outcome"]["comparison"]
    result["jobs"][0]["metrics"].update(total_cost_vnd=9007199254740993, total_distance_m=1.2345678901234567, total_soft_lateness_s=0)
    before = deepcopy(group)
    value = comparison_narrative(group)
    assert group == before
    assert value["comparison_status"] == "COMPARABLE" and value["scope"] == "HISTORICAL_FORECAST"
    assert [p["profile"] for p in value["profiles"]] == ["FASTEST", "BALANCED", "SAFER"]
    for profile, source in zip(value["profiles"], result["jobs"]):
        assert profile["metrics"] == source["metrics"] and profile["input_basis"] == source["basis"]
        assert profile["units"] == METRIC_UNITS and profile["units"]["total_exposure"] == "PROXY"
        assert profile["served_count"] == 1 and profile["unserved_count"] == 0
    assert type(value["profiles"][0]["metrics"]["total_cost_vnd"]) is int
    assert not {"best_profile", "recommendation", "percentage", "ranking", "delta"}.intersection(value)
    assert value["auto_accept_plan"] is False and value["language"] == "vi"
    assert "cùng đầu vào" in value["text"] and "cùng miền vật lý" in value["text"]


@pytest.mark.parametrize("different", ["domain", "basis"])
def test_non_comparable_has_sdk_reason_and_no_comparative_table_or_conclusion(group, different):
    group["mode"] = "EXISTING_JOBS"
    value = group["outcome"]["comparison"]
    value["status"] = "NON_COMPARABLE"
    group["outcome"]["reason"] = value["reason"] = "AUTHENTICATED_BASIS_OR_PHYSICAL_DOMAIN_DIFFERS"
    if different == "domain":
        value["jobs"][2]["domain_sha256"] = "e" * 64
    else:
        group["jobs"][2]["view"]["input_basis"]["head_version"] = "999"
        value["jobs"][2]["basis"] = deepcopy(group["jobs"][2]["view"]["input_basis"])
    narrative = comparison_narrative(group)
    assert narrative["comparison_status"] == "NON_COMPARABLE" and narrative["profiles"] == []
    assert narrative["reason"] == value["reason"] and "NON_COMPARABLE" in narrative["text"]
    assert not {"recommendation", "ranking", "percentage"}.intersection(narrative)


def test_no_witness_preserves_null_coverage_and_never_zeroes_unknown_counts(group):
    view = group["jobs"][2]["view"]
    view.update(business_status="SEARCH_LIMIT", internal_status="SEARCH_LIMIT", plan_available=False,
        coverage_evaluated=False, served_orders=[], unserved_orders=[], validation={"status": "NOT_RUN", "valid": None})
    group["outcome"] = {"comparison": None, "reason": "NO_CERTIFIED_WITNESS_FOR_EVERY_PROFILE"}
    value = comparison_narrative(group)
    assert value["comparison_status"] is None and value["profiles"] == []
    child = value["jobs"][2]
    assert child["business_status"] == "SEARCH_LIMIT" and child["served_count"] is child["unserved_count"] is None
    assert not child["witness_certified"] and not child["coverage_evaluated"]


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "CANCEL_REQUESTED"])
def test_unobserved_children_and_pending_outcome_keep_nulls(group, state):
    group["status"], group["outcome"] = state, None
    for child in group["jobs"]:
        child.update(job_id=None, view=None)
    value = comparison_narrative(group)
    assert value["comparison_status"] is value["reason"] is None and value["profiles"] == []
    assert all(j["job_status"] is j["served_count"] is j["unserved_count"] is None for j in value["jobs"])


@pytest.mark.parametrize("state,reason", [("CANCELLED", "BATCH_CANCELLED"), ("FAILED", "STALE_HEAD")])
def test_cancelled_failed_lifecycle_is_explained_without_sdk_verdict(group, state, reason):
    group["status"], group["outcome"] = state, {"comparison": None, "reason": reason}
    value = comparison_narrative(group)
    assert value["group_status"] == state and value["reason"] == reason and value["comparison_status"] is None
    assert value["profiles"] == [] and value["source"] == "PUBLIC_M3_COMPARISON_LIFECYCLE"


@pytest.mark.parametrize("change", [lambda g: g.update(real_world_observation=True), lambda g: g.update(metric_scope="OBSERVED_PREFIX_ONLY"),
    lambda g: g.update(exposure_is_proxy=False), lambda g: g["jobs"].pop(),
    lambda g: g["jobs"][1].update(profile="FASTEST"), lambda g: g["jobs"][1].update(job_id=g["jobs"][0]["job_id"]),
    lambda g: g["jobs"][0]["view"].update(job_status="RUNNING"),
    lambda g: g["jobs"][0]["view"]["input_basis"].update(session_id="foreign"),
    lambda g: g["outcome"]["comparison"]["jobs"][0]["basis"].update(generation="1"),
    lambda g: g["outcome"]["comparison"]["jobs"][0].update(profile="SAFER"),
    lambda g: g["outcome"]["comparison"]["jobs"][0]["metrics"].update(runtime_root="private-path"),
    lambda g: g["outcome"]["comparison"]["jobs"][0]["metrics"].update(total_exposure=True),
    lambda g: g["outcome"]["comparison"]["jobs"][0]["metrics"].update(total_distance_m=-1),
    lambda g: g["outcome"]["comparison"]["jobs"][0]["metrics"].update(total_cost_vnd=float("nan")),
    lambda g: g["outcome"].update(reason="other"), lambda g: g.update(status="RUNNING")])
def test_invalid_public_sdk_group_bindings_metrics_and_lifecycle_are_rejected(group, change):
    change(group)
    with pytest.raises((ValueError, KeyError, TypeError)):
        comparison_narrative(group)


@pytest.mark.parametrize("verdict", ["COMPARABLE", "NON_COMPARABLE"])
def test_contradictory_sdk_verdict_is_rejected_without_replacement(group, verdict):
    value = group["outcome"]["comparison"]
    if verdict == "COMPARABLE":
        value["jobs"][1]["domain_sha256"] = "e" * 64
    else:
        value["status"] = "NON_COMPARABLE"
        group["outcome"]["reason"] = value["reason"] = "AUTHENTICATED_BASIS_OR_PHYSICAL_DOMAIN_DIFFERS"
    with pytest.raises(ValueError):
        comparison_narrative(group)


def test_existing_comparison_creation_basis_can_be_newer_than_all_historical_jobs(group):
    group["mode"] = "EXISTING_JOBS"
    group["input_basis"]["head_version"] = "999"
    value = comparison_narrative(group)
    assert value["captured_group_basis"]["head_version"] == "999"
    assert all(p["input_basis"]["head_version"] == "17" for p in value["profiles"])


def test_private_extra_top_fields_do_not_escape(group):
    group["runtime_root"] = "PRIVATE-PATH-CANARY"
    group["claim_token"] = "CLAIM-CANARY"
    serialized = json.dumps(comparison_narrative(group), ensure_ascii=False)
    assert "CANARY" not in serialized and "runtime_root" not in serialized and "claim_token" not in serialized


def test_duplicate_or_overlapping_coverage_cannot_create_misleading_counts(group):
    group["jobs"][0]["view"]["served_orders"] = ["order-1", "order-1"]
    with pytest.raises(ValueError):
        comparison_narrative(group)


def test_server_pinned_build_is_checked_independently_of_group_header(group):
    assert comparison_narrative(group, build=BUILD)["comparison_status"] == "COMPARABLE"
    with pytest.raises(ValueError):
        comparison_narrative(group, build="9" * 64)


def test_false_no_witness_explanation_with_all_certified_children_is_rejected(group):
    group["outcome"] = {"comparison": None, "reason": "NO_CERTIFIED_WITNESS_FOR_EVERY_PROFILE"}
    with pytest.raises(ValueError):
        comparison_narrative(group)
