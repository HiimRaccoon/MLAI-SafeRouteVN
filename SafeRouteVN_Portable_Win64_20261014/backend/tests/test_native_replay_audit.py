"""Native artifact comparison accepts order changes, never altered history."""
from copy import deepcopy
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def verify_replay_audit(monkeypatch):
    # Import only definitions. The harness starts processes exclusively in main.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_step7").verify_replay_audit


@pytest.fixture
def receipts():
    sid = "native-public-session"
    basis = {"session_id": sid, "root_sha256": "1" * 64, "head_sha256": "2" * 64,
             "head_version": "1", "generation": "0", "source_sha256": "3" * 64,
             "context_version": "public-context", "overlay_sha256": None, "build_sha256": "4" * 64}
    common = {"schema_version": "saferoute-m3-replay-receipt/1", "session_id": sid,
              "input_basis": basis, "basis": deepcopy(basis), "source": "M2_PUBLIC_SDK"}
    advance = {**deepcopy(common), "mutation_id": "advance-1", "operation": "advance", "status": "ADVANCED",
               "recorded_at": "2026-10-05T14:00:00+00:00", "target_time": "2026-09-27T21:01:00+07:00"}
    advance["basis"].update(head_version="2", head_sha256="5" * 64)
    event = {**deepcopy(common), "mutation_id": "event-1", "operation": "apply_event", "status": "APPLIED",
             "recorded_at": "2026-10-05T14:01:00+00:00", "event_id": "urgent-1", "event_sha256": "6" * 64,
             "event_type": "URGENT_ORDER"}
    event["input_basis"] = deepcopy(advance["basis"])
    event["basis"].update(head_version="3", head_sha256="7" * 64)
    pause = {**deepcopy(common), "mutation_id": "pause-1", "operation": "pause", "status": "PAUSED",
             "recorded_at": "2026-10-05T14:02:00+00:00", "source": "M3_MANUAL_CONTROL", "mode": "STEP", "paused": True}
    pause["input_basis"] = deepcopy(event["basis"])
    pause["basis"] = deepcopy(event["basis"])
    reset = {**deepcopy(common), "mutation_id": "reset-1", "operation": "reset", "status": "RESET",
             "recorded_at": "2026-10-05T14:03:00+00:00", "source": "M3_NEW_SESSION", "new_session_id": "new-public-session"}
    reset["input_basis"] = deepcopy(event["basis"])
    reset["basis"]["session_id"] = "new-public-session"
    exported = [advance, event, pause, reset]
    history = list(reversed(deepcopy(exported)))
    for receipt in history:
        receipt["links"] = {"state": f"/api/sessions/{sid}/state", "history": f"/api/sessions/{sid}/replay/history"}
    history[0]["new_session"] = {"session_id": "new-public-session", "scenario_id": "S2", "status": "READY"}
    return sid, exported, history


def test_complete_normalized_history_accepts_opposite_order_and_documented_nested_omissions(verify_replay_audit, receipts):
    sid, exported, history = receipts
    before = deepcopy((exported, history))
    assert [item["mutation_id"] for item in exported] == list(reversed([item["mutation_id"] for item in history]))
    verify_replay_audit(exported, history, sid)
    assert (exported, history) == before


@pytest.mark.parametrize("side", ["exported", "history"])
def test_duplicate_identity_is_rejected_even_when_counts_match(verify_replay_audit, receipts, side):
    sid, exported, history = receipts
    records = exported if side == "exported" else history
    records[1] = deepcopy(records[0])
    with pytest.raises(ValueError, match="complete committed native history"):
        verify_replay_audit(exported, history, sid)


@pytest.mark.parametrize("side", ["exported", "history"])
def test_missing_historical_receipt_is_rejected(verify_replay_audit, receipts, side):
    sid, exported, history = receipts
    (exported if side == "exported" else history).pop()
    with pytest.raises(ValueError, match="complete committed native history"):
        verify_replay_audit(exported, history, sid)


def test_equal_counts_with_replaced_identity_are_rejected(verify_replay_audit, receipts):
    sid, exported, history = receipts
    exported[1]["mutation_id"] = "uncommitted-event"
    with pytest.raises(ValueError, match="complete committed native history"):
        verify_replay_audit(exported, history, sid)


@pytest.mark.parametrize("change", ["event_hash", "old_input_basis", "paused_integer", "counter_number", "unrecorded_null_field"])
def test_retained_fields_must_exist_and_match_exact_json_value_types(verify_replay_audit, receipts, change):
    sid, exported, history = receipts
    if change == "event_hash":
        exported[1]["event_sha256"] = "f" * 64
    elif change == "old_input_basis":
        exported[1]["input_basis"]["head_sha256"] = "f" * 64
    elif change == "paused_integer":
        exported[2]["paused"] = 1  # Python True == 1 must not pass an audit check.
    elif change == "counter_number":
        exported[0]["basis"]["head_version"] = 2
    else:
        exported[0]["uncommitted_field"] = None
    with pytest.raises(ValueError, match="complete committed native history"):
        verify_replay_audit(exported, history, sid)


@pytest.mark.parametrize("field", ["event_sha256", "input_basis", "basis"])
def test_original_normalized_lineage_field_cannot_be_omitted(verify_replay_audit, receipts, field):
    sid, exported, history = receipts
    exported[1].pop(field)
    with pytest.raises(ValueError, match="complete committed native history"):
        verify_replay_audit(exported, history, sid)


@pytest.mark.parametrize("side", ["exported", "history"])
@pytest.mark.parametrize("field", ["schema_version", "session_id", "mutation_id", "operation", "status", "recorded_at"])
def test_core_receipt_fields_are_required_on_both_sides(verify_replay_audit, receipts, side, field):
    sid, exported, history = receipts
    (exported if side == "exported" else history)[0].pop(field)
    with pytest.raises(ValueError, match="complete committed native history"):
        verify_replay_audit(exported, history, sid)


@pytest.mark.parametrize("side", ["exported", "history", "both"])
def test_matching_receipt_ids_cannot_hide_another_session(verify_replay_audit, receipts, side):
    sid, exported, history = receipts
    if side in ("exported", "both"):
        exported[0]["session_id"] = "other-session"
    if side in ("history", "both"):
        next(item for item in history if item["mutation_id"] == exported[0]["mutation_id"])["session_id"] = "other-session"
    with pytest.raises(ValueError, match="complete committed native history"):
        verify_replay_audit(exported, history, sid)


def test_matching_unreviewed_schema_is_rejected(verify_replay_audit, receipts):
    sid, exported, history = receipts
    exported[0]["schema_version"] = history[-1]["schema_version"] = "unreviewed-replay/1"
    with pytest.raises(ValueError, match="complete committed native history"):
        verify_replay_audit(exported, history, sid)
