from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
import asyncio
from types import SimpleNamespace
from uuid import uuid4

from backend.api.errors import ApiError


class ReplayService:
    def __init__(self, settings, repository, sessions, gateway):
        self.settings, self.repository, self.sessions, self.gateway = settings, repository, sessions, gateway
        self.playback = None

    @staticmethod
    def digest(operation, body, resource_id=None):
        return hashlib.sha256(json.dumps({"operation": operation, "body": body, "resource_id": resource_id},
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def event_map(self, session):
        fixture = self.sessions.fixture(session)
        return {event["eventId"]: event for event in fixture["events"]}

    def pending(self, session, view):
        events = self.event_map(session)
        if view.get('delivery_day'):
            for event in view['delivery_day']['pending_events']:
                if event['event_id'] in events:
                    events[event['event_id']]={**events[event['event_id']],'timestamp':event['timestamp']}
        if not set(view["pending_event_ids"]) <= set(events):
            raise ApiError(503, "EVENT_METADATA_UNAVAILABLE", "events", "Runtime event has no verified source metadata")
        return sorted((events[key] for key in view["pending_event_ids"]), key=lambda item: (datetime.fromisoformat(item["timestamp"]), item["eventId"]))

    async def events(self, session_id, *, view=None):
        session = self.sessions.session(session_id)
        if view is None:
            view = await self.gateway.resolve(session_id)
        return {"schema_version": "saferoute-m3-pending-events/1", "basis": view["basis"], "current_time": view["current_time"],
            "execution_mode": "SIMULATED_REPLAY", "real_world_observation": False,
            "events": [{"event_id": event["eventId"], "event_type": event["type"], "timestamp": event["timestamp"],
                "apply_allowed": view["current_time"] == event["timestamp"] and view["observed_metrics"] is not None}
                for event in self.pending(session, view)]}

    @staticmethod
    def check_revision(view, body):
        if any(view["basis"][key] != getattr(body.expected_revision, key) for key in ("head_version", "generation")):
            raise ApiError(409, "STALE_HEAD", "expected_revision", "Expected revision differs from the current server head")

    def target(self, session, view, requested, *, step_seconds=None):
        current = datetime.fromisoformat(view["current_time"])
        boundaries = [(datetime.fromisoformat(event["timestamp"]), event["timestamp"]) for event in self.pending(session, view)]
        if view.get('delivery_barrier'):
            barrier=view['delivery_barrier']
            if datetime.fromisoformat(barrier)<=current:
                raise ApiError(409,'DELIVERY_CONFIRMATION_REQUIRED','orders','Driver must press Deliver before replay continues')
            boundaries.append((datetime.fromisoformat(barrier),barrier));boundaries.sort()
        if requested is None:
            if boundaries and boundaries[0][0] <= current:
                raise ApiError(409, "EVENT_TRANSITION_REQUIRED", "events", "Apply the due event before stepping forward")
            target = current + timedelta(seconds=step_seconds or self.settings.replay_step_seconds)
            if boundaries:
                target = min(target, boundaries[0][0])
        else:
            target = datetime.fromisoformat(requested)
        if target < current:
            raise ApiError(409, "TIME_REWIND", "target_time", "Observed time cannot go backwards; reset creates a new session")
        if any(target > stamp for stamp, _ in boundaries):
            raise ApiError(409, "EVENT_TRANSITION_REQUIRED", "target_time", "Step cannot cross an unapplied event")
        for stamp, wire in boundaries:
            if target == stamp:
                return wire
        if target == current:
            return view["current_time"]
        return target.isoformat(timespec="microseconds" if target.microsecond else "seconds")

    async def mutate(self, session_id, operation, body, actor, event_id=None):
        session = self.sessions.session(session_id)
        self.sessions.mutation_allowed(session_id)
        installation = self.sessions.installation_identity()
        digest = self.digest(operation, body.model_dump(), event_id)
        try:
            previous = self.repository.find_request(actor.actor_id, session_id, operation, body.request_id, digest, installation)
            basis, payload = None, None
            if previous is None:
                if self.playback is not None:
                    self.playback.stop(session_id, "MANUAL_" + operation.upper())
                view = await self.gateway.resolve(session_id)
                basis = view["basis"]
                if operation in ("advance", "apply_event"):
                    self.check_revision(view, body)
                if operation == "advance":
                    if view["active_job_id"] is None:
                        raise ApiError(409, "ACCEPTED_PLAN_REQUIRED", "active_job_id", "Accept a certified plan before observed replay")
                    payload = {"target_time": self.target(session, view, body.target_time), "current_time": view["current_time"]}
                elif operation == "apply_event":
                    events = self.event_map(session)
                    if event_id not in events:
                        raise ApiError(404, "EVENT_NOT_FOUND", "event_id", "Event is not in this verified scenario")
                    self.pending(session, view)
                    if event_id not in view["pending_event_ids"]:
                        raise ApiError(409, "EVENT_ALREADY_APPLIED", "event_id", "Event is no longer pending")
                    event = next(e for e in self.pending(session,view) if e['eventId']==event_id)
                    if view["current_time"] != event["timestamp"]:
                        raise ApiError(409, "EVENT_NOT_DUE", "event_id", "Advance the accepted replay to the exact event barrier")
                    if view["observed_metrics"] is None:
                        raise ApiError(409, "ACCEPTED_REPLAY_REQUIRED", "events", "Event requires an observed replay frame before applying")
                    payload = {"event_id": event_id, "event_type": event["type"], "timestamp": event["timestamp"]}
                elif operation == "reset":
                    payload = {"scenario_id": session["scenario_id"], "load_request_id": "m3-reset-load-" + uuid4().hex,
                        **({'run_id':session['run_id']} if session.get('run_id') else {})}
                elif operation == "pause":
                    payload = {}
                else:
                    raise ValueError("Unknown server replay operation")
            row = self.repository.reserve_request(actor.actor_id, session_id, operation, body.request_id, digest, installation, basis=basis, payload=payload)
            receipt = await self.execute_request(row)
            if operation == "reset":
                view = await self.gateway.resolve(receipt["new_session_id"])
                self.sessions.observe(view, "RESET_CURRENT_OBSERVATION", receipt["mutation_id"])
                return {"schema_version": "saferoute-m3-replay-reset/1", "source_session_id": session_id,
                    "session": receipt["new_session"], "receipt": receipt, "execution_view": view}
            view = await self.gateway.resolve(session_id)
            self.sessions.observe(view, "REPLAY_CURRENT_OBSERVATION", receipt["mutation_id"])
            return {"schema_version": "saferoute-m3-replay-view/1", "receipt": receipt, "execution_view": view}
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "replay", "Replay metadata is unavailable; retry the same request ID") from error

    def validate_receipt(self, row, result):
        try:
            self._validate_receipt(row, result)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ApiError(503, "REPLAY_BINDING_CHANGED", "runtime", "Replay receipt has an invalid shape or binding") from error

    def _validate_receipt(self, row, result):
        if not isinstance(result, dict) or not isinstance(result.get("basis"), dict):
            raise ValueError("Receipt/basis object required")
        old, new, op = row["basis"], result["basis"], row["operation"]
        keys = {"session_id", "root_sha256", "head_sha256", "head_version", "generation", "source_sha256", "context_version", "overlay_sha256", "build_sha256"}
        if set(new) != keys:
            raise ValueError("Exact basis fields required")
        for key in ("root_sha256", "head_sha256", "source_sha256", "build_sha256"):
            if not isinstance(new[key], str) or re.fullmatch(r"[a-f0-9]{64}", new[key]) is None:
                raise ValueError("Canonical basis digest required")
        for key in ("generation", "head_version"):
            if (not isinstance(new[key], str) or re.fullmatch(r"0|[1-9][0-9]{0,18}", new[key]) is None
                    or int(new[key]) >= 1 << 63):
                raise ValueError("Canonical nonnegative int64 counter required")
        if (not isinstance(new["session_id"], str) or not isinstance(new["context_version"], str)
                or not new["context_version"] or len(new["context_version"]) > 200
                or (new["overlay_sha256"] is not None and (not isinstance(new["overlay_sha256"], str)
                    or re.fullmatch(r"[a-f0-9]{64}", new["overlay_sha256"]) is None))):
            raise ValueError("Basis identifiers/overlay required")
        status = result["status"]
        if op == 'deliver':
            if status != 'DISPATCHED':
                raise ValueError('Delivery dispatch receipt required')
        elif op == "advance":
            expected_status = "NOOP" if row["payload"]["target_time"] == row["payload"]["current_time"] else "ADVANCED"
            if status != expected_status:
                raise ApiError(503, "REPLAY_BINDING_CHANGED", "runtime", "Replay lifecycle differs from the persisted target")
        elif status != "APPLIED" or result["event_id"] != row["payload"]["event_id"] or not re.fullmatch(r"[a-f0-9]{64}", result["event_sha256"]):
            raise ApiError(503, "REPLAY_BINDING_CHANGED", "runtime", "Applied event receipt differs from the persisted event")
        if status == "NOOP":
            valid = new == old
        else:
            mutable = {"head_version", "head_sha256"}
            if op == "apply_event" and row["payload"]["event_type"] == "LOCAL_RAIN_WHAT_IF":
                mutable.add("overlay_sha256")
            valid = (set(new) == set(old) and new["head_version"] == str(int(old["head_version"]) + 1)
                and all(new[key] == old[key] for key in old if key not in mutable)
                and ("head_sha256" not in old or new["head_sha256"] != old["head_sha256"]))
            if "overlay_sha256" in mutable:
                valid = valid and isinstance(new["overlay_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", new["overlay_sha256"]) is not None
        if not valid:
            raise ApiError(503, "REPLAY_BINDING_CHANGED", "runtime", "Replay receipt differs from the persisted input basis")

    async def execute_request(self, row):
        if row["response"] is not None:
            return row["response"]
        try:
            operation, payload = row["operation"], row["payload"]
            extra = {}
            if operation == 'deliver':
                result = await asyncio.to_thread(self.gateway._call, 'dispatch', {
                    'session_id':row['session_id'], 'command_id':row['command_id'], 'basis':row['basis'],
                    'action':'deliver', 'data':payload['delivery']})
                self.validate_receipt(row, result)
                extra.update(payload['delivery'])
            elif operation == "advance":
                result = await self.gateway.advance(row["session_id"], row["command_id"], row["basis"], payload["target_time"])
                self.validate_receipt(row, result)
                extra["target_time"] = payload["target_time"]
            elif operation == "apply_event":
                result = await self.gateway.apply_event(row["session_id"], row["command_id"], row["basis"], payload["event_id"])
                self.validate_receipt(row, result)
                extra.update(event_id=payload["event_id"], event_sha256=result["event_sha256"], event_type=payload["event_type"])
            elif operation == "pause":
                result = {"status": "PAUSED", "basis": row["basis"]}
                extra.update(mode="STEP", paused=True)
            elif operation == "reset":
                loaded = (await self.sessions.load_run(payload['run_id'],payload['load_request_id'],SimpleNamespace(actor_id=row['actor_id']),replay_reset=True)
                    if payload.get('run_id') else await self.sessions.load(payload["scenario_id"], payload["load_request_id"], SimpleNamespace(actor_id=row["actor_id"])))
                result = {"status": "RESET", "basis": loaded["execution_view"]["basis"]}
                extra.update(new_session_id=loaded["session"]["session_id"], new_session=loaded["session"])
            else:
                raise ValueError("Unknown persisted replay operation")
            destination = extra.get("new_session_id", row["session_id"])
            receipt = {"schema_version": "saferoute-m3-replay-receipt/1", "mutation_id": row["mutation_id"],
                "session_id": row["session_id"], "operation": operation, "status": result["status"],
                "source": "M2_PUBLIC_SDK" if operation in ("advance", "apply_event", "deliver") else "M3_MANUAL_CONTROL" if operation == "pause" else "M3_NEW_SESSION",
                "input_basis": row["basis"], "basis": result["basis"], "recorded_at": datetime.now(timezone.utc).isoformat(),
                "links": {"state": f"/api/sessions/{destination}/state", "history": f'/api/sessions/{row["session_id"]}/replay/history'}, **extra}
            self.repository.complete_replay(row, receipt)
            return receipt
        except ApiError as error:
            if error.status_code in (400, 404, 409, 422):
                self.repository.reject_request(row, error)
            raise
        finally:
            self.repository.release_request(row)

    async def reconcile(self, pending):
        self.sessions.session(pending["session_id"])
        self.sessions.mutation_allowed(pending["session_id"])
        row = self.repository.reserve_request(*self.repository.request_keys(pending), pending["request_sha256"], self.sessions.installation_identity())
        return await self.execute_request(row)

    def history(self, session_id):
        self.sessions.session(session_id)
        try:
            return {"schema_version": "saferoute-m3-replay-history/1", "session_id": session_id, "history": self.repository.history(session_id)}
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "replay", "Replay history is unavailable") from error

    async def controller(self, session_id):
        self.sessions.session(session_id)
        view = await self.gateway.resolve(session_id)
        self.sessions.observe(view, "HTTP_REPLAY_READ")
        return {"schema_version": "saferoute-m3-replay-controller/1", "mode": "STEP", "paused": True,
            "step_seconds": self.settings.replay_step_seconds, "execution_view": view}
