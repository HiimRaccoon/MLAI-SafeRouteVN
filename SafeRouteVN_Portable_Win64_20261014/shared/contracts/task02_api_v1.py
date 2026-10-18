"""Reference validation for the TASK-02 → M3/M4 v1 JSON contract.

This module does not authenticate a client-provided state or implement HTTP.
The caller must supply a server-resolved, receipt-gated state summary.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator


SCHEMA_PATH = Path(__file__).with_name("task02_api_v1.schema.json")
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
REQUEST_VERSION = "task02-m2-m3m4-request/1"
JOB_VERSION = "task02-m2-m3m4-job/1"
DECISION_VERSION = "task02-m2-m3m4-decision/1"
PUBLIC_STATUSES = frozenset({"FEASIBLE", "PARTIAL", "UNSUPPORTED", "INVALID_DATA",
                             "SEARCH_LIMIT", "TIME_LIMIT"})
NO_WITNESS_STATUSES = frozenset({"UNSUPPORTED", "INVALID_DATA", "SEARCH_LIMIT", "TIME_LIMIT"})


def _issue(code: str, path: str, message: str) -> dict[str, str]:
    return {"severity": "ERROR", "code": code, "path": path, "message": message}


def _nonfinite_errors(value: Any, path: str = "") -> list[dict[str, str]]:
    """Reject non-finite Python floats even when a permissive JSON decoder created them."""
    if isinstance(value, float) and not math.isfinite(value):
        return [_issue("NUMBER_NOT_FINITE", path or "payload",
                       "JSON numeric values must be finite")]
    issues: list[dict[str, str]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            issues.extend(_nonfinite_errors(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}.{index}" if path else str(index)
            issues.extend(_nonfinite_errors(child, child_path))
    return issues


def _diagnostic_errors(decision: Any, path: str = "") -> list[dict[str, str]]:
    """Give consumers one stable error when a no-witness result has no reason."""
    if not isinstance(decision, Mapping):
        return []
    status = decision.get("status")
    if not isinstance(status, str) or status not in NO_WITNESS_STATUSES:
        return []
    diagnostics = decision.get("diagnostics")
    actionable = (
        isinstance(diagnostics, list)
        and any(
            isinstance(item, Mapping)
            and item.get("severity") == "ERROR"
            and isinstance(item.get("code"), str) and bool(item["code"])
            and isinstance(item.get("path"), str) and bool(item["path"])
            and isinstance(item.get("message"), str) and bool(item["message"])
            for item in diagnostics
        )
    )
    if actionable:
        return []
    diagnostic_path = f"{path}.diagnostics" if path else "diagnostics"
    return [_issue("DIAGNOSTIC_REQUIRED", diagnostic_path,
                   "a no-witness decision requires at least one actionable ERROR diagnostic")]


def _schema_errors(payload: Any, definition: str) -> list[dict[str, str]]:
    selected = {"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"],
                "$ref": f"#/$defs/{definition}"}
    validator = Draft202012Validator(selected)
    def leaves(error):
        if error.context:
            for child in error.context:
                yield from leaves(child)
        else:
            yield error

    errors = sorted((leaf for error in validator.iter_errors(payload) for leaf in leaves(error)),
                    key=lambda item: (list(map(str, item.absolute_path)), item.message))
    issues = []
    for error in errors:
        path = ".".join(str(part) for part in error.absolute_path)
        if error.validator == "required":
            missing = next((name for name in error.validator_value if name not in error.instance), None)
            if missing:
                path = f"{path}.{missing}" if path else missing
        issues.append(_issue("SCHEMA_INVALID", path or definition, error.message))
    return list({(item["code"], item["path"], item["message"]): item for item in issues}.values())


def canonical_request(payload: Mapping[str, Any]) -> str:
    """Canonical comparison only; durable idempotency storage belongs to M3."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _expected_epoch(state: Mapping[str, Any], event_id: str | None) -> str | None:
    if event_id is None:
        return state.get("decision_epoch")
    return state.get("pending_event_epochs", {}).get(event_id)


def validate_request(payload: Any, resolved_state: Mapping[str, Any] | None = None,
                     existing_request: Mapping[str, Any] | None = None) -> list[dict[str, str]]:
    issues = _nonfinite_errors(payload)
    if issues:
        return issues
    issues = _schema_errors(payload, "request")
    if issues:
        return issues
    if existing_request is not None and payload["request_id"] == existing_request.get("request_id"):
        if canonical_request(payload) != canonical_request(existing_request):
            issues.append(_issue("IDEMPOTENCY_CONFLICT", "request_id",
                                 "the same request_id cannot identify different payloads"))
    if resolved_state is None:
        return issues
    if payload["state_ref"]["state_id"] != resolved_state.get("state_id"):
        issues.append(_issue("SOURCE_MISMATCH", "state_ref.state_id", "server-managed state reference differs"))
    if payload["scenario_id"] != resolved_state.get("scenario_id"):
        issues.append(_issue("SOURCE_MISMATCH", "scenario_id", "scenario differs from resolved state"))
    if payload["expected_state_version"] != resolved_state.get("state_version"):
        issues.append(_issue("VERSION_MISMATCH", "expected_state_version", "state version is stale"))
    if payload["expected_context_version"] != resolved_state.get("context_version"):
        issues.append(_issue("CONTEXT_MISMATCH", "expected_context_version", "context version differs"))
    if payload["decision_epoch"] != _expected_epoch(resolved_state, payload["event_id"]):
        issues.append(_issue("VERSION_MISMATCH", "decision_epoch", "decision epoch differs"))
    if payload["event_id"] is not None and payload["event_id"] not in resolved_state.get("pending_event_ids", []):
        issues.append(_issue("EVENT_NOT_FOUND", "event_id", "event is not pending in resolved state"))
    return issues


def validate_decision(payload: Any, resolved_state: Mapping[str, Any] | None = None) -> list[dict[str, str]]:
    issues = _nonfinite_errors(payload)
    issues.extend(_diagnostic_errors(payload))
    if issues:
        return issues
    issues = _schema_errors(payload, "decision")
    if issues:
        return issues
    status = payload["status"]
    ids = payload["order_ids"]
    served = payload["served_orders"]
    unserved = [item["order_id"] for item in payload["unserved_orders"]]
    if len(unserved) != len(set(unserved)) or set(served) & set(unserved):
        issues.append(_issue("ORDER_PARTITION_INVALID", "unserved_orders", "served/unserved IDs overlap or repeat"))
    if status in {"FEASIBLE", "PARTIAL"}:
        if set(served) | set(unserved) != set(ids):
            issues.append(_issue("ORDER_PARTITION_INVALID", "served_orders", "every order must be served or explicitly unserved"))
        routes = payload["plan"]["vehicle_routes"]
        routed = [order for route in routes for order in route["order_sequence"]]
        if len(routed) != len(set(routed)) or set(routed) != set(served):
            issues.append(_issue("ROUTE_ORDER_MISMATCH", "plan.vehicle_routes", "route assignments must equal served orders exactly"))
        for index, route in enumerate(routes):
            if ([stop["order_id"] for stop in route["stops"]] != route["order_sequence"]
                or len(route["legs"]) != len(route["stops"]) + 1
                or len(route["node_sequence"]) != len(route["stops"]) + 2
                or route["node_sequence"][0] != route["start_node"]
                or route["node_sequence"][-1] != route["end_node"]):
                issues.append(_issue("ROUTE_SEQUENCE_INVALID", f"plan.vehicle_routes.{index}",
                                     "stop/leg/node sequences disagree"))
            elif (any(leg["from_node"] != route["node_sequence"][leg_index]
                      or leg["to_node"] != route["node_sequence"][leg_index + 1]
                      for leg_index, leg in enumerate(route["legs"]))
                  or any(stop["node_id"] != route["node_sequence"][stop_index + 1]
                         for stop_index, stop in enumerate(route["stops"]))):
                issues.append(_issue("ROUTE_CONTINUITY_INVALID", f"plan.vehicle_routes.{index}",
                                     "leg endpoints and stop nodes must follow node_sequence"))
        metrics = payload["plan"]["metrics"]
        if metrics["cost_available"] != (metrics["total_cost_vnd"] is not None):
            issues.append(_issue("COST_AVAILABILITY_MISMATCH", "plan.metrics.total_cost_vnd",
                                 "cost availability must match the nullable value"))
        if any(route["cost_vnd"] is None for route in routes) != (not metrics["cost_available"]):
            issues.append(_issue("COST_AVAILABILITY_MISMATCH", "plan.vehicle_routes",
                                 "route costs must share fleet cost availability"))
    if payload["search"] is not None:
        search = payload["search"]
        if search["truncated"] != bool(search["truncation_reasons"]):
            issues.append(_issue("SEARCH_METADATA_INVALID", "search.truncated", "truncation flag and reasons disagree"))
    if resolved_state is not None:
        for key in ("scenario_id", "state_version", "context_version", "decision_epoch"):
            expected = (_expected_epoch(resolved_state, payload["event_id"])
                        if key == "decision_epoch" else resolved_state.get(key))
            if payload[key] != expected:
                issues.append(_issue("VERSION_MISMATCH" if key != "context_version" else "CONTEXT_MISMATCH",
                                     key, "decision differs from server-resolved state"))
        if ids != resolved_state.get("order_ids"):
            issues.append(_issue("ORDER_SET_MISMATCH", "order_ids", "order IDs differ from resolved state"))
        if payload["event_id"] is not None and payload["event_id"] not in resolved_state.get("pending_event_ids", []):
            issues.append(_issue("EVENT_NOT_FOUND", "event_id", "event not pending in resolved state"))
        source = payload["source"]
        for key in ("suite_id", "source_run", "fixture_sha256", "catalog_sha256", "receipt_sha256",
                    "routing_version", "features_version", "delivery_area_version"):
            if source[key] != resolved_state.get(key):
                issues.append(_issue("SOURCE_MISMATCH", f"source.{key}", "provenance differs from resolved state"))
    return issues


def validate_job(payload: Any, resolved_state: Mapping[str, Any] | None = None) -> list[dict[str, str]]:
    issues = _nonfinite_errors(payload)
    if isinstance(payload, Mapping):
        issues.extend(_diagnostic_errors(payload.get("decision"), "decision"))
    if issues:
        return issues
    issues = _schema_errors(payload, "job")
    if issues:
        return issues
    if payload["job_status"] == "COMPLETED":
        issues.extend(validate_decision(payload["decision"], resolved_state))
        if payload["decision"]["run_id"] != payload["run_id"]:
            issues.append(_issue("RUN_ID_MISMATCH", "decision.run_id", "job/decision run IDs differ"))
    return issues


def _trusted_value(record: Mapping[str, Any], key: str) -> Any:
    """Read a trusted persistence field without treating wire claims as evidence."""
    return record.get(key)


def validate_bound_job(payload: Any, accepted_request: Any,
                       resolved_state: Mapping[str, Any] | None,
                       trusted_job: Mapping[str, Any]) -> list[dict[str, str]]:
    """Validate a job together with the request/job record accepted by M3.

    ``validate_job`` remains useful for portable shape/semantic checks, but it
    cannot authenticate request binding or provenance claims by itself.  This
    entry point adds those checks against server-controlled records.
    """
    required_state_fields = {
        "state_id", "scenario_id", "state_version", "context_version", "decision_epoch",
        "order_ids", "pending_event_ids", "pending_event_epochs", "suite_id", "source_run",
        "fixture_sha256", "catalog_sha256", "receipt_sha256", "routing_version",
        "features_version", "delivery_area_version",
    }
    if (not isinstance(resolved_state, Mapping)
        or not required_state_fields.issubset(resolved_state)):
        return [_issue("TRUSTED_STATE_REQUIRED", "resolved_state",
                       "bound validation requires a complete server-resolved state")]

    request_issues = validate_request(accepted_request, resolved_state)
    if request_issues:
        return request_issues
    issues = validate_job(payload, resolved_state)
    if issues:
        return issues
    if not isinstance(trusted_job, Mapping):
        return [_issue("TRUSTED_RECORD_INVALID", "trusted_job",
                       "trusted job evidence must be a server-controlled object")]

    if payload["request_id"] != accepted_request["request_id"]:
        issues.append(_issue("REQUEST_MISMATCH", "request_id",
                             "job request_id differs from the accepted request"))
    for key in ("request_id", "run_id"):
        if payload[key] != _trusted_value(trusted_job, key):
            issues.append(_issue("TRUSTED_RECORD_MISMATCH", key,
                                 f"job {key} differs from the trusted persistence record"))

    if payload["job_status"] != "COMPLETED":
        return issues

    decision = payload["decision"]
    request_pairs = {
        "scenario_id": "scenario_id",
        "event_id": "event_id",
        "decision_epoch": "decision_epoch",
        "state_version": "expected_state_version",
        "context_version": "expected_context_version",
    }
    for decision_key, request_key in request_pairs.items():
        if decision[decision_key] != accepted_request[request_key]:
            issues.append(_issue("REQUEST_MISMATCH", f"decision.{decision_key}",
                                 f"decision {decision_key} differs from the accepted request"))

    expected_gate = _trusted_value(trusted_job, "validation_gate")
    if decision["validation"]["gate"] != expected_gate:
        issues.append(_issue("TRUSTED_EVIDENCE_MISMATCH", "decision.validation.gate",
                             "validation gate differs from trusted server evidence"))
    expected_derived = _trusted_value(trusted_job, "derived_from")
    if decision["derived_from"] != expected_derived:
        issues.append(_issue("TRUSTED_EVIDENCE_MISMATCH", "decision.derived_from",
                             "derived_from differs from trusted server evidence"))
    return issues
