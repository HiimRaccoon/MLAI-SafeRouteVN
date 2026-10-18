"""Native HTTP/SDK event-boundary replay for S2/S3/S4, using fresh solves.

All mutations use the public M3 HTTP API. Fixture reads are hash-bound to the
HTTP catalog; no M2 internals, mock results or historical solver seeds are used.
"""
import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from verify_step4 import NativeHarness, fingerprint, require
from verify_step5 import accept, require_error, revision


EVENT_TIME = "2026-09-27T21:15:00+07:00"
PHYSICAL_KEYS = ("current_time", "vehicles", "order_ids", "delivered_prefix", "observed_metrics",
    "pending_event_ids", "metric_scope", "execution_mode", "real_world_observation")


def clock(value):
    return datetime.fromisoformat(value)


def fresh_worker_ready(harness, previous_id, *, launched_at, timeout=90):
    """Require this spawned worker's heartbeat, rather than a previous TTL."""
    deadline = time.monotonic() + timeout
    expected_build = harness.settings.installation()["expected_build_sha256"]
    while time.monotonic() < deadline:
        require(harness.worker is not None and harness.worker.poll() is None,
            "Spawned worker exited before publishing its own READY heartbeat")
        try:
            heartbeat, age = harness.heartbeat()
            new_id = heartbeat["worker_id"]
            if (isinstance(new_id, str) and new_id and new_id != previous_id
                    and heartbeat["build_sha256"] == expected_build
                    and clock(heartbeat["updated_at"]) >= launched_at):
                status, response, _ = harness.http("/ready", actor=None)
                require(harness.worker.poll() is None, "Worker exited during the final readiness probe")
                if status == 200 and response["data"]["ready"] is True:
                    return response["data"], {"previous_worker_id": previous_id, "worker_id": new_id,
                        "spawned_process_id": harness.worker.pid, "spawned_process_alive": True,
                        "updated_at": heartbeat["updated_at"], "heartbeat_age_seconds": round(age, 4),
                        "different_worker_id": new_id != previous_id, "installation_sha256": heartbeat["installation_sha256"]}
        except (OSError, ValueError, KeyError, TypeError):
            pass  # STARTING/old heartbeat and bounded sharing errors may be transient.
        time.sleep(0.25)
    raise TimeoutError("New worker did not publish a distinct fresh READY heartbeat within 90 seconds")


def resume_verified_scenarios(path, harness, catalog, config, budget, requested):
    """Reuse completed scenarios only after artifacts and live authority agree."""
    source = path.resolve(strict=True)
    source_raw = source.read_bytes()
    previous = json.loads(source_raw)
    local_receipts = (harness.settings.installation_path.parent / "receipts").resolve(strict=True)
    require(source.is_relative_to(local_receipts), "Resume source must stay inside the configured private receipts directory")
    roots, ancestry, visited = [], [], set()
    ancestor_path, expected_ancestor_sha = source, None
    while True:
        ancestor_path = ancestor_path.resolve(strict=True)
        require(ancestor_path.is_relative_to(local_receipts), "Resume ancestor receipt escaped the configured private receipts directory")
        require(ancestor_path not in visited and len(ancestry) < 10, "Resume ancestry contains a cycle or exceeds ten receipts")
        visited.add(ancestor_path)
        ancestor_raw = ancestor_path.read_bytes()
        ancestor_sha = hashlib.sha256(ancestor_raw).hexdigest()
        if expected_ancestor_sha is not None:
            require(ancestor_sha == expected_ancestor_sha, "Resume ancestor receipt bytes differ from the child's pinned SHA-256")
        ancestor = json.loads(ancestor_raw)
        require(isinstance(ancestor, dict) and ancestor["schema_version"] == "saferoute-m3-step6-native-replay/1"
            and Path(ancestor["project_root"]).resolve() == harness.project.resolve()
            and ancestor["runtime_build_sha256"] == config["expected_build_sha256"]
            and ancestor["compute_budget_seconds"] == budget, "Resume ancestor project/build/budget/schema binding differs")
        root = ancestor_path.parent.resolve()
        roots.append(root)
        ancestry.append({"source_receipt": str(ancestor_path), "sha256": ancestor_sha,
            "frame_root": str(root), "source_overall_status": ancestor["status"]})
        link = ancestor.get("resume")
        if link is None:
            break
        require(isinstance(link, dict) and isinstance(link["source_receipt"], str)
            and isinstance(link["sha256"], str), "Resume ancestry requires a receipt path and pinned SHA-256")
        ancestor_path, expected_ancestor_sha = Path(link["source_receipt"]), link["sha256"]
    restored = []
    seen = set()
    for old in previous["scenarios"]:
        scenario_id = old["scenario_id"]
        if old.get("status") != "M3_STEP6_NATIVE_SCENARIO_PASS" or scenario_id not in requested:
            continue
        require(scenario_id not in seen, "Resume report duplicated a completed scenario")
        seen.add(scenario_id)
        entry = next(item for item in catalog["scenarios"] if item["scenario_id"] == scenario_id)
        fixture_path = harness.project / "scenarios/fixtures" / catalog["suite_id"] / (scenario_id + ".json")
        require(old["fixture_sha256"] == entry["fixture_sha256"]
            and hashlib.sha256(fixture_path.read_bytes()).hexdigest() == entry["fixture_sha256"],
            "Resume fixture differs from the current verified HTTP catalog")
        directory = Path(old["directory"]).resolve(strict=True)
        require(any(directory.is_relative_to(root) for root in roots) and isinstance(old["frames"], list) and old["frames"],
            "Resume frame directory must remain inside a verified ancestor receipt directory")
        for frame in old["frames"]:
            artifact = Path(frame["path"]).resolve(strict=True)
            require(any(artifact.is_relative_to(root) for root in roots) and artifact.is_relative_to(directory),
                "Resume frame path escaped its verified ancestor receipt directory")
            raw = artifact.read_bytes()
            require(len(raw) == frame["bytes"] and hashlib.sha256(raw).hexdigest() == frame["sha256"],
                "Resume frame bytes/hash changed: " + artifact.name)
        session_id = old["session_id"]
        require(harness.state(session_id)[0] == old["final_state"], "Resume live current execution state differs from the recorded completed scenario")
        status, response, _ = harness.http(f"/api/sessions/{session_id}/replay/history")
        require(status == 200 and response["data"]["history"] == old["replay_history"], "Resume durable replay history differs")
        retry = old["restart_retry"]
        require(retry["path"] == f"/api/sessions/{session_id}/events/{old['event_id']}/apply",
            "Resume retry path does not belong to its recorded session/event")
        status, response, _ = harness.http(retry["path"], retry["body"], timeout=180)
        require(status == 200 and response["data"]["receipt"] == retry["receipt"]
            and response["data"]["execution_view"] == old["final_state"], "Resume historical receipt/current execution view differs")
        old["checks"]["resume_validation"] = {"status": "PASS", "source_receipt": str(source),
            "frame_count_verified": len(old["frames"]), "all_frame_bytes_and_hashes_verified": True,
            "source_fixture_unchanged": True, "current_state_history_and_original_receipt_verified": True,
            "verified_ancestor_receipt_count": len(ancestry)}
        restored.append(old)
        print(scenario_id + " VERIFIED_COMPLETED_SCENARIO_RESUMED", flush=True)
    require(restored, "Resume report has no individually completed requested scenarios")
    return restored, {"source_receipt": str(source), "sha256": hashlib.sha256(source_raw).hexdigest(),
        "source_overall_status": previous["status"], "resumed_scenarios": [item["scenario_id"] for item in restored],
        "verified_ancestor_receipts": ancestry, "configured_receipts_root": str(local_receipts)}


class ReplayVerifier:
    def __init__(self, harness, output, run_id, catalog, budget):
        self.harness, self.output, self.run_id, self.catalog, self.budget = harness, output, run_id, catalog, budget
        self.current = None
        self.frame_sequence = 0

    def frame(self, name, value):
        self.frame_sequence += 1
        path = self.current["directory"] / f"{self.frame_sequence:02d}_{name}.json"
        raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        path.write_bytes(raw)
        self.current["frames"].append({"name": name, "path": str(path), "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()})
        return value

    def history(self, session_id):
        status, response, _ = self.harness.http(f"/api/sessions/{session_id}/replay/history")
        require(status == 200 and isinstance(response["data"]["history"], list), "Replay history HTTP projection failed")
        return response["data"]["history"]

    def events(self, session_id):
        status, response, _ = self.harness.http(f"/api/sessions/{session_id}/events")
        data = response["data"]
        require(status == 200 and data["schema_version"] == "saferoute-m3-pending-events/1"
            and isinstance(data["events"], list), "Pending events HTTP projection failed")
        return data

    def mutation(self, path, body, expected_status, name, *, remember=True):
        status, response, seconds = self.harness.http(path, body, timeout=180)
        require(status == 200 and response["status"] == "OK", "Replay mutation " + name + " returned HTTP " + str(status)
            + " diagnostics " + json.dumps([item.get("code") for item in response.get("diagnostics", [])]))
        data = response["data"]
        receipt = data["receipt"]
        require(data["schema_version"] == "saferoute-m3-replay-view/1"
            and receipt["schema_version"] == "saferoute-m3-replay-receipt/1" and receipt["status"] == expected_status
            and receipt["session_id"] == self.current["session_id"] and isinstance(receipt["mutation_id"], str),
            "Replay mutation receipt schema/status/binding invalid: " + name)
        require(data["execution_view"]["schema_version"] == "task02-m2-execution-view/2"
            and data["execution_view"]["execution_mode"] == "SIMULATED_REPLAY"
            and data["execution_view"]["real_world_observation"] is False,
            "Replay mutation did not return typed simulated execution view")
        if remember:
            self.current["receipts"].append(receipt)
        self.current["timings"].append({"operation": name, "seconds": round(seconds, 4)})
        self.frame(name, data)
        return data

    def step(self, session_id, before, target, name, *, expected_status="ADVANCED"):
        path = f"/api/sessions/{session_id}/replay/step"
        body = {"request_id": self.run_id + "-" + self.current["scenario_id"] + "-" + name,
            "expected_revision": revision(before["basis"]), "target_time": target}
        data = self.mutation(path, body, expected_status, name)
        require(data["receipt"]["input_basis"] == before["basis"] and data["receipt"]["target_time"] == target,
            "Replay receipt changed persisted input basis/target")
        require(data["execution_view"]["current_time"] == target
            and set(before["delivered_prefix"]) <= set(data["execution_view"]["delivered_prefix"]),
            "Replay did not reach exact target or rewrote delivered prefix")
        require(data["receipt"]["basis"] == data["execution_view"]["basis"]
            and data["execution_view"]["basis"]["generation"] == before["basis"]["generation"],
            "Replay receipt basis differs from current head or changed activation generation")
        if expected_status == "ADVANCED":
            require(int(data["execution_view"]["basis"]["head_version"]) == int(before["basis"]["head_version"]) + 1,
                "Advance did not commit exactly one physical head version")
        require(self.harness.state(session_id)[0] == data["execution_view"], "Replay response is not current SDK state")
        return data, path, body

    def solve_accept(self, session_id, before, name):
        submission, seconds = self.harness.submit(session_id, self.run_id + "-" + self.current["scenario_id"] + "-" + name)
        require(submission["input_basis"] == before["basis"], "Solve submit changed full server basis")
        self.current["timings"].append({"operation": name + "_submit", "seconds": round(seconds, 4)})
        self.frame(name + "_submission", submission)
        started = time.monotonic()
        job, observations, running = self.harness.poll_terminal(session_id, submission["job_id"], before, timeout=self.budget + 90)
        self.frame(name + "_job", job)
        self.current["timings"].append({"operation": name + "_poll_to_terminal", "seconds": round(time.monotonic() - started, 4)})
        self.current["solves"].append({"job_id": submission["job_id"], "job_view": job,
            "observations": observations, "while_running": running})
        require(job["job_status"] == "COMPLETED" and job["plan_available"] is True
            and job["validation"]["valid"] is True, "Native " + self.current["scenario_id"] + " " + name + " lacks a certified witness")
        require(self.harness.state(session_id)[0] == before, "Forecast compute rewrote physical head")
        body = {"request_id": self.run_id + "-" + self.current["scenario_id"] + "-accept-" + name,
            "expected_revision": revision(before["basis"])}
        data, accept_seconds = accept(self.harness, f"/api/sessions/{session_id}/jobs/{submission['job_id']}/accept", body)
        after = data["execution_view"]
        require(data["receipt"]["input_basis"] == before["basis"] and after["active_job_id"] == submission["job_id"]
            and int(after["basis"]["generation"]) == int(before["basis"]["generation"]) + 1,
            "Native accept did not activate exactly one generation")
        for key in PHYSICAL_KEYS:
            require(before[key] == after[key], "Accept changed physical execution field " + key)
        require(after["accepted_trajectory"]["forecast"] is True, "Accepted trajectory is not a forecast")
        self.current["timings"].append({"operation": name + "_accept", "seconds": round(accept_seconds, 4)})
        self.frame(name + "_accepted", data)
        print(self.current["scenario_id"] + " " + name.upper() + " NATIVE_WITNESS_ACCEPTED", flush=True)
        return after

    def run(self, scenario_id):
        directory = self.output.parent / "scenarios" / scenario_id
        directory.mkdir(parents=True, exist_ok=True)
        scenario = {"scenario_id": scenario_id, "directory": directory, "frames": [], "receipts": [], "timings": [], "solves": [], "checks": {}}
        self.current, self.frame_sequence = scenario, 0
        entry = next(item for item in self.catalog["scenarios"] if item["scenario_id"] == scenario_id)
        source_path = self.harness.project / "scenarios/fixtures" / self.catalog["suite_id"] / (scenario_id + ".json")
        source_bytes = source_path.read_bytes()
        require(hashlib.sha256(source_bytes).hexdigest() == entry["fixture_sha256"], "Native verifier fixture does not match HTTP-verified catalog")
        fixture = json.loads(source_bytes)
        source_event = next(item for item in fixture["events"] if item["eventId"] == scenario_id + "-E1")
        require(source_event["timestamp"] == EVENT_TIME, "Pinned source event boundary differs from approved S2/S3/S4 suite")
        scenario["fixture_sha256"] = entry["fixture_sha256"]
        scenario["event_id"] = source_event["eventId"]
        scenario["source_event_type"] = source_event["type"]
        scenario["source_type"] = source_event.get("sourceType")
        scenario["source_initial_time"] = fixture["initialState"]["currentTime"]
        session_id = scenario["session_id"] = self.harness.load(scenario_id, self.run_id + "-load-" + scenario_id)
        initial = self.frame("initial", self.harness.state(session_id)[0])
        events = self.frame("initial_events", self.events(session_id))
        selected = next(item for item in events["events"] if item["event_id"] == scenario["event_id"])
        require(selected["timestamp"] == EVENT_TIME and selected["event_type"] == source_event["type"]
            and selected["apply_allowed"] is False and events["basis"] == initial["basis"], "Verified pending event metadata differs")
        accepted = self.solve_accept(session_id, initial, "initial")
        prefix = f"/api/sessions/{session_id}"
        apply_path = prefix + "/events/" + scenario["event_id"] + "/apply"
        scenario["checks"]["early_event"] = require_error(self.harness, apply_path,
            {"request_id": self.run_id + "-" + scenario_id + "-early-event", "expected_revision": revision(accepted["basis"])},
            409, "EVENT_NOT_DUE")
        scenario["checks"]["barrier"] = require_error(self.harness, prefix + "/replay/step",
            {"request_id": self.run_id + "-" + scenario_id + "-cross-barrier", "expected_revision": revision(accepted["basis"]),
                "target_time": (clock(EVENT_TIME) + timedelta(microseconds=1)).isoformat()}, 409, "EVENT_TRANSITION_REQUIRED")
        require(self.harness.state(session_id)[0] == accepted, "Rejected early event/barrier crossing mutated state")
        pause_body = {"request_id": self.run_id + "-" + scenario_id + "-pause"}
        paused = self.mutation(prefix + "/replay/pause", pause_body, "PAUSED", "pause")
        require(paused["execution_view"] == accepted and paused["receipt"]["input_basis"] == accepted["basis"]
            and paused["receipt"]["basis"] == accepted["basis"], "Manual pause acknowledgement changed SDK head")
        paused_retry = self.mutation(prefix + "/replay/pause", pause_body, "PAUSED", "pause_retry", remember=False)
        require(paused_retry["receipt"] == paused["receipt"] and paused_retry["execution_view"] == accepted, "Pause retry changed receipt/head")
        boundary, boundary_path, boundary_body = self.step(session_id, accepted, EVENT_TIME, "event_boundary")
        before_event = boundary["execution_view"]
        boundary_retry = self.mutation(boundary_path, boundary_body, "ADVANCED", "boundary_retry", remember=False)
        require(boundary_retry["receipt"] == boundary["receipt"] and boundary_retry["execution_view"] == before_event, "Boundary replay retry changed receipt/head")
        no_op, _, _ = self.step(session_id, before_event, EVENT_TIME, "same_time_noop", expected_status="NOOP")
        require(no_op["execution_view"] == before_event and no_op["receipt"]["basis"] == before_event["basis"], "Same-time replay NOOP rewrote head")
        due = self.frame("due_events", self.events(session_id))
        require(next(item for item in due["events"] if item["event_id"] == scenario["event_id"])["apply_allowed"] is True,
            "Server did not mark exact event boundary apply_allowed")
        apply_body = {"request_id": self.run_id + "-" + scenario_id + "-apply-event", "expected_revision": revision(before_event["basis"])}
        applied = self.mutation(apply_path, apply_body, "APPLIED", "event_applied")
        after_event = applied["execution_view"]
        require(applied["receipt"]["input_basis"] == before_event["basis"] and applied["receipt"]["event_id"] == scenario["event_id"]
            and isinstance(applied["receipt"]["event_sha256"], str) and len(applied["receipt"]["event_sha256"]) == 64,
            "Applied event receipt does not bind verified event/basis")
        require(after_event["active_job_id"] is None and after_event["accepted_trajectory"] is None
            and after_event["current_time"] == before_event["current_time"]
            and after_event["delivered_prefix"] == before_event["delivered_prefix"]
            and after_event["observed_metrics"] == before_event["observed_metrics"]
            and scenario["event_id"] not in after_event["pending_event_ids"], "Event rewrote prefix/time/observed metrics or failed to suspend old plan")
        require(applied["receipt"]["basis"] == after_event["basis"]
            and int(after_event["basis"]["head_version"]) == int(before_event["basis"]["head_version"]) + 1
            and after_event["basis"]["generation"] == before_event["basis"]["generation"],
            "Event commit did not change exactly one head version with activation generation preserved")
        repeat = self.mutation(apply_path, apply_body, "APPLIED", "event_retry", remember=False)
        require(repeat["receipt"] == applied["receipt"] and repeat["execution_view"] == after_event, "Apply retry duplicated event/state")
        require(self.events(session_id)["events"] == [], "Applied event remains in server pending list")
        scenario["checks"]["advance_requires_reaccept"] = require_error(self.harness, prefix + "/replay/step",
            {"request_id": self.run_id + "-" + scenario_id + "-without-reaccept", "expected_revision": revision(after_event["basis"]),
                "target_time": (clock(EVENT_TIME) + timedelta(microseconds=1)).isoformat()}, 409, "ACCEPTED_PLAN_REQUIRED")
        require(self.harness.state(session_id)[0] == after_event, "Replay without reaccept changed suspended head")
        if scenario_id == "S2":
            urgent_id = source_event["orderPayload"]["id"]
            require(urgent_id == "O009" and urgent_id not in before_event["order_ids"]
                and after_event["order_ids"].count(urgent_id) == 1 and len(after_event["order_ids"]) == len(before_event["order_ids"]) + 1,
                "S2 urgent order was absent/duplicated or rewrote original order universe")
            require(after_event["vehicles"] == before_event["vehicles"], "S2 event changed physical vehicle/custody frame")
            scenario["checks"]["urgent_order"] = {"status": "PASS", "added_once": urgent_id,
                "orders_before": len(before_event["order_ids"]), "orders_after": len(after_event["order_ids"]), "actual_prefix_unchanged": True}
        elif scenario_id == "S3":
            owner_id = source_event["vehicleId"]
            original_owner = next(item for item in before_event["vehicles"] if item["vehicle_id"] == owner_id)
            unavailable = next(item for item in after_event["vehicles"] if item["vehicle_id"] == owner_id)
            require(unavailable["availability"] == "UNAVAILABLE" and unavailable["activity"] == "IMMOBILIZED"
                and unavailable["active_commitment"] is None
                and unavailable["suspended_commitment"] == original_owner["active_commitment"]
                and unavailable["suspension_reason"] == "VEHICLE_UNAVAILABLE_NO_RECOVERY",
                "S3 did not mark source vehicle unavailable/immobilized")
            physical_fields = ("vehicle_id", "position", "position_timestamp", "current_load_kg", "capacity_kg",
                "remaining_range_m", "onboard_order_ids", "executed_metrics")
            for key in physical_fields:
                require(unavailable[key] == original_owner[key], "S3 availability event changed custody/physical vehicle field " + key)
            require([item for item in before_event["vehicles"] if item["vehicle_id"] != owner_id]
                == [item for item in after_event["vehicles"] if item["vehicle_id"] != owner_id], "S3 event changed another physical vehicle")
            scenario["custody_owner_id"] = owner_id
            scenario["custody_order_ids"] = list(original_owner["onboard_order_ids"])
            scenario["checks"]["unavailable_custody"] = {"status": "PASS", "vehicle_id": owner_id,
                "held_order_ids": scenario["custody_order_ids"], "custody_exercised": bool(scenario["custody_order_ids"]),
                "physical_custody_fields_unchanged": True, "physical_fields_verified": list(physical_fields),
                "lifecycle_changed_fields": ["availability", "activity", "active_commitment"],
                "availability": unavailable["availability"], "activity_before": original_owner["activity"], "activity_after": unavailable["activity"],
                "previous_active_commitment_preserved_in_suspension": True, "suspension_reason": unavailable["suspension_reason"]}
        else:
            require(source_event["type"] == "LOCAL_RAIN_WHAT_IF" and "WHAT-IF" in source_event["sourceType"], "S4 source is not labelled WHAT-IF")
            require(after_event["basis"]["overlay_sha256"] is not None
                and after_event["basis"]["overlay_sha256"] != before_event["basis"]["overlay_sha256"]
                and after_event["basis"]["context_version"] == before_event["basis"]["context_version"]
                and after_event["vehicles"] == before_event["vehicles"], "S4 overlay/context/physical vehicle invariants failed")
            scenario["checks"]["rain_what_if"] = {"status": "PASS", "source_type": source_event["sourceType"],
                "overlay_sha256": after_event["basis"]["overlay_sha256"], "base_context_version": after_event["basis"]["context_version"],
                "start_time": source_event["startTime"], "end_time": source_event["endTime"]}
        reaccepted = self.solve_accept(session_id, after_event, "post_event")
        if scenario_id == "S3":
            held = set(scenario["custody_order_ids"])
            owner_id = scenario["custody_owner_id"]
            for vehicle in reaccepted["vehicles"]:
                if vehicle["vehicle_id"] != owner_id:
                    require(not held.intersection(vehicle["onboard_order_ids"]), "S3 replan transferred unavailable owner cargo")
            for route in reaccepted["accepted_trajectory"]["vehicle_routes"]:
                if route["vehicle_id"] != owner_id:
                    require(not held.intersection(route["order_sequence"]), "S3 replan assigned unavailable owner cargo to another vehicle")
            scenario["checks"]["no_custody_transfer_in_replan"] = {"status": "PASS", "held_order_ids": sorted(held)}
        current = reaccepted
        if scenario_id == "S4":
            rain_samples = []
            for name, target in (("rain_start_plus_us", clock(source_event["startTime"]) + timedelta(microseconds=1)),
                ("rain_end_minus_us", clock(source_event["endTime"]) - timedelta(microseconds=1)),
                ("rain_end_exact", clock(source_event["endTime"])),
                ("rain_end_plus_us", clock(source_event["endTime"]) + timedelta(microseconds=1))):
                sample, _, _ = self.step(session_id, current, target.isoformat(), name)
                current = sample["execution_view"]
                rain_samples.append({"name": name, "current_time": current["current_time"], "basis": current["basis"],
                    "observed_metrics": current["observed_metrics"], "delivered_prefix": current["delivered_prefix"]})
            scenario["checks"]["rain_boundaries"] = {"status": "PASS", "samples": rain_samples,
                "no_positive_wet_exposure_or_overlay_clear_assumption": True}
        end_us = max((int(route["return_us"]) for route in reaccepted["accepted_trajectory"]["vehicle_routes"]), default=0)
        final_target = max(clock("2026-09-27T23:00:00+07:00"), clock(scenario["source_initial_time"]) + timedelta(microseconds=end_us + 1))
        finished, _, _ = self.step(session_id, current, final_target.isoformat(), "finish")
        final = finished["execution_view"]
        require(final["observed_metrics"] is not None and set(before_event["delivered_prefix"]) <= set(final["delivered_prefix"])
            and final["real_world_observation"] is False, "Final replay lost actual prefix or mislabelled real-world observation")
        if scenario_id == "S3":
            held = set(scenario["custody_order_ids"])
            owner_id = scenario["custody_owner_id"]
            owner = next(item for item in final["vehicles"] if item["vehicle_id"] == owner_id)
            require(owner["availability"] == "UNAVAILABLE" and held <= set(owner["onboard_order_ids"]), "Final S3 replay lost unavailable owner cargo")
            require(not held.intersection(final["delivered_prefix"]), "Unavailable S3 owner cargo was implicitly delivered")
        late_retry = self.mutation(apply_path, apply_body, "APPLIED", "event_retry_after_finish", remember=False)
        require(late_retry["receipt"] == applied["receipt"] and late_retry["execution_view"] == final,
            "Later retry must keep historical event receipt and return separately current frame")
        history = self.frame("history", self.history(session_id))
        require(len(history) == len(scenario["receipts"]) and {item["mutation_id"] for item in history}
            == {item["mutation_id"] for item in scenario["receipts"]}, "Replay retries duplicated audit entries or lost receipts")
        reset_body = {"request_id": self.run_id + "-" + scenario_id + "-reset"}
        reset_status, reset_response, _ = self.harness.http(prefix + "/replay/reset", reset_body)
        reset = reset_response["data"]
        require(reset_status == 200 and reset["schema_version"] == "saferoute-m3-replay-reset/1"
            and reset["source_session_id"] == session_id and reset["session"]["scenario_id"] == scenario_id
            and reset["session"]["session_id"] != session_id, "Reset did not create an independent session from the same verified fixture")
        reset_receipt = reset["receipt"]
        require(reset_receipt["schema_version"] == "saferoute-m3-replay-receipt/1"
            and reset_receipt["status"] == "RESET" and reset_receipt["operation"] == "reset",
            "Reset did not publish its durable parent/new-session lineage receipt")
        scenario["receipts"].append(reset_receipt)
        new_initial = reset["execution_view"]
        require(new_initial["current_time"] == initial["current_time"] and new_initial["active_job_id"] is None
            and new_initial["observed_metrics"] is None and new_initial["delivered_prefix"] == []
            and new_initial["order_ids"] == initial["order_ids"] and new_initial["pending_event_ids"] == initial["pending_event_ids"],
            "Reset is not a fresh initial replay state")
        reset_retry_status, reset_retry, _ = self.harness.http(prefix + "/replay/reset", reset_body)
        require(reset_retry_status == 200 and reset_retry["data"] == reset and self.harness.state(session_id)[0] == final,
            "Reset retry created another session or rewrote original completed session")
        self.frame("reset", reset)
        history = self.frame("history_after_reset", self.history(session_id))
        require(len(history) == len(scenario["receipts"]) and {item["mutation_id"] for item in history}
            == {item["mutation_id"] for item in scenario["receipts"]}, "Reset retry duplicated audit history or lost parent lineage receipt")
        scenario["checks"]["manual_pause_reset"] = {"status": "PASS", "pause_does_not_mutate_head": True,
            "reset_session_id": reset["session"]["session_id"], "reset_retry_same_session": True, "original_state_preserved": True}
        scenario["checks"]["receipt_audit"] = {"status": "PASS", "successful_history_count": len(history),
            "same_id_same_receipt": True, "historical_receipt_current_frame_separated": True}
        scenario["final_state"] = final
        scenario["replay_history"] = history
        scenario["restart_retry"] = {"path": apply_path, "body": apply_body, "receipt": applied["receipt"]}
        scenario["status"] = "M3_STEP6_NATIVE_SCENARIO_PASS"
        scenario["directory"] = str(directory)
        print(scenario_id + " NATIVE_REPLAY_EVENT_REPLAN_PASS", flush=True)
        return scenario


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--budget-seconds", type=float, default=120)
    parser.add_argument("--scenarios", nargs="+", choices=("S2", "S3", "S4"), default=["S2", "S3", "S4"])
    parser.add_argument("--resume", type=Path, help="Verify and carry individually completed scenarios from an earlier receipt; run missing scenarios fresh")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project))
    from backend.services.settings import Settings
    settings = replace(Settings.from_environment(), compute_budget_seconds=args.budget_seconds)
    config = settings.installation()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    run_id = "m3-step6-" + uuid4().hex
    receipt = {"schema_version": "saferoute-m3-step6-native-replay/1", "status": "RUNNING", "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(), "project_root": str(project),
        "runtime_build_sha256": config["expected_build_sha256"], "compute_budget_seconds": args.budget_seconds,
        "requested_scenarios": args.scenarios, "execution_mode": "SIMULATED_REPLAY", "real_world_observation": False,
        "checks": {}, "scenarios": [], "notes": ["Fresh native solves, accepted replay and source-bound events; no mock or historical seed.",
            "S4 rain is SYNTHETIC WHAT-IF; positive WET exposure and overlay removal are not assumed.",
            "Observed elapsed times are acceptance observations, not an SLA."]}
    harness = NativeHarness(project, settings, args.output)
    harness.env["SAFEROUTE_COMPUTE_BUDGET_SECONDS"] = str(args.budget_seconds)
    failed = None
    verifier = None
    try:
        harness.start_server()
        from backend.services.heartbeat_io import read_heartbeat_json
        try:
            initial_previous_id = read_heartbeat_json(settings.heartbeat_path).get("worker_id")
        except (OSError, ValueError, TypeError):
            initial_previous_id = None
        launched_at = datetime.now(timezone.utc)
        harness.start_worker()
        initial_ready, initial_worker = fresh_worker_ready(harness, initial_previous_id, launched_at=launched_at)
        receipt["checks"]["initial_readiness"] = {"status": "PASS", "data": initial_ready,
            "http_status": 200, "worker_evidence": initial_worker}
        status, response, _ = harness.http("/api/scenarios")
        require(status == 200, "Verified HTTP catalog unavailable")
        verifier = ReplayVerifier(harness, args.output, run_id, response["data"], args.budget_seconds)
        if args.resume is not None:
            completed, resume_evidence = resume_verified_scenarios(args.resume, harness, response["data"], config,
                args.budget_seconds, args.scenarios)
            receipt["scenarios"].extend(completed)
            receipt["resume"] = resume_evidence
            receipt["notes"].append("Verified native scenario evidence resumed for " + ", ".join(resume_evidence["resumed_scenarios"])
                + "; all ancestor receipts/frame bytes and live state/history/receipts are checked again.")
        completed_ids = {item["scenario_id"] for item in receipt["scenarios"]}
        receipt["fresh_scenarios"] = [scenario_id for scenario_id in dict.fromkeys(args.scenarios) if scenario_id not in completed_ids]
        for scenario_id in dict.fromkeys(args.scenarios):
            if scenario_id not in completed_ids:
                receipt["scenarios"].append(verifier.run(scenario_id))
        stopped_heartbeat = harness.wait_idle(timeout=args.budget_seconds + 90)
        stopped_worker_id = stopped_heartbeat["worker_id"]
        harness.stop_worker()
        harness.stop_server()
        harness.start_server()
        restarted_at = datetime.now(timezone.utc)
        harness.start_worker()
        restart_ready, restart_worker = fresh_worker_ready(harness, stopped_worker_id, launched_at=restarted_at)
        for scenario in receipt["scenarios"]:
            state = harness.state(scenario["session_id"])[0]
            retry = scenario["restart_retry"]
            status, response, _ = harness.http(retry["path"], retry["body"], timeout=180)
            require(status == 200 and response["data"]["receipt"] == retry["receipt"]
                and response["data"]["execution_view"] == scenario["final_state"] and state == scenario["final_state"],
                "Restart lost durable mutation receipt or current replay state")
            require(verifier.history(scenario["session_id"]) == scenario["replay_history"], "Restart lost/duplicated replay history")
            scenario["checks"]["restart"] = {"status": "PASS", "exact_current_frame_preserved": True, "receipt_and_history_preserved": True}
        receipt["checks"]["restart_readiness"] = {"status": "PASS", "http_status": 200,
            "data": restart_ready, "worker_evidence": restart_worker}
        status, openapi, _ = harness.http("/openapi.json", actor=None)
        require(status == 200 and all(path in openapi["paths"] for path in
            ("/api/sessions/{session_id}/events", "/api/sessions/{session_id}/events/{event_id}/apply",
                "/api/sessions/{session_id}/replay/step", "/api/sessions/{session_id}/replay/pause",
                "/api/sessions/{session_id}/replay/reset", "/api/sessions/{session_id}/replay/history")), "Native OpenAPI lacks replay/event routes")
        (args.output.parent / "openapi.json").write_text(json.dumps(openapi, ensure_ascii=False, indent=2), encoding="utf-8")
        receipt["checks"]["openapi"] = {"status": "PASS", "path": str(args.output.parent / "openapi.json")}
    except Exception as error:
        failed = error
        receipt["failure"] = {"type": type(error).__name__, "message": str(error)}
        if verifier is not None and verifier.current is not None and verifier.current not in receipt["scenarios"]:
            partial = {**verifier.current, "status": "M3_STEP6_NATIVE_SCENARIO_FAIL"}
            partial["directory"] = str(partial["directory"])
            receipt["scenarios"].append(partial)
    finally:
        harness.close()
        receipt["server_running_after_test"] = False
        receipt["worker_running_after_test"] = False
    try:
        check = subprocess.run([sys.executable, "-B", str(project / "docs/M2_STEP7_VERIFY_20261004.py"),
            "--project-root", str(project)], cwd=project, capture_output=True, text=True, encoding="utf-8", timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        verified = json.loads(check.stdout)
        require(check.returncode == 0 and verified["status"] == "TEAM_HANDOFF_BYTES_VERIFIED", "Frozen M2 handoff changed")
        receipt["checks"]["frozen_handoff"] = {"status": "PASS", "result": verified}
    except Exception as error:
        failed = failed or error
        receipt["checks"]["frozen_handoff"] = {"status": "FAIL", "type": type(error).__name__, "message": str(error)}
    receipt["status"] = "M3_STEP6_NATIVE_REPLAY_PASS" if failed is None else "M3_STEP6_NATIVE_REPLAY_FAIL"
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"status": receipt["status"], "receipt": str(args.output),
        "scenarios_passed": sum(item.get("status") == "M3_STEP6_NATIVE_SCENARIO_PASS" for item in receipt["scenarios"]),
        "server_stopped": True, "worker_stopped": True}), flush=True)
    return 0 if failed is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
