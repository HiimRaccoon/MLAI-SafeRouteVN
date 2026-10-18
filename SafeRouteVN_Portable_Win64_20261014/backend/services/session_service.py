import hashlib
import json
import sqlite3

from backend.api.errors import ApiError
from .display_metadata import display_metadata, ORDER_FIELDS, VEHICLE_FIELDS


class SessionService:
    def __init__(self, settings, repository, catalog, gateway):
        self.settings, self.repository, self.catalog, self.gateway = settings, repository, catalog, gateway
        from .run_service import RunService
        self.runs = RunService(settings, self)

    def installation_identity(self):
        try:
            return self.settings.installation_identity()
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ApiError(503, "INSTALLATION_UNAVAILABLE", "server", "Server installation configuration is unavailable") from error

    def validate_replacement(self, session_id, actor):
        if session_id is not None:
            source = self.session(session_id)
            if source['owner_actor_id'] != actor.actor_id:
                raise ApiError(403, 'FORBIDDEN', 'replace_session_id', 'Only the session owner can replace its driver assignment')

    async def load(self, scenario_id, request_id, actor, *, replace_session_id=None):
        self.validate_replacement(replace_session_id, actor)
        self.catalog.fixture(scenario_id)
        from .demo_scenarios import load_demo_variant
        demo = await load_demo_variant(self, scenario_id, request_id, actor, replace_session_id=replace_session_id)
        if demo is not None:
            return demo
        intent = {"operation": "load", "scenario_id": scenario_id}
        if replace_session_id is not None:
            intent['replace_session_id'] = replace_session_id
        digest = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
        try:
            reservation = self.repository.reserve_load(actor.actor_id, request_id, digest, scenario_id,
                self.installation_identity(), self.catalog.fixture_sha256[scenario_id], self.catalog.catalog_sha256)
            if reservation["cached"] is not None:
                return reservation["cached"]
            try:
                boot = await self.gateway.bootstrap(reservation["session_id"], scenario_id, reservation["command_id"])
                view = await self.gateway.resolve(reservation["session_id"])
                if view["basis"] != boot["basis"]:
                    raise ApiError(503, "BOOTSTRAP_BINDING_CHANGED", "runtime", "Bootstrap and initial execution view differ")
                data = {"schema_version": "saferoute-m3-loaded-session/1", "session": self.public_session(reservation["session"]),
                        "execution_view": view}
                self.repository.complete_load(reservation, data, replace_session_id=replace_session_id)
                self.observe(view, "LOAD", reservation["command_id"])
                return data
            finally:
                self.repository.release_load(reservation)
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "session", "Session metadata is unavailable; retry the same request ID") from error

    async def load_run(self, run_id, request_id, actor, *, replay_reset=False, replace_session_id=None):
        from datetime import datetime
        self.validate_replacement(replace_session_id, actor)
        manifest=self.runs.manifest(run_id,actor.actor_id)
        registered=self.runs.row(run_id,actor.actor_id)
        # Retry of an already reserved load remains valid after weather expires.
        with self.repository._db() as db:
            previous=db.execute('SELECT 1 FROM load_requests WHERE actor_id=? AND request_id=?',(actor.actor_id,request_id)).fetchone()
        if manifest.get('source_kind') != 'IMPORTED_SCENARIO' and not previous and not replay_reset and datetime.now().astimezone() > datetime.fromisoformat(manifest['weather']['expires_at']):
            raise ApiError(409,'WEATHER_STALE','run_id','Weather expired; create a fresh run before starting another session')
        intent = {'operation':'load_run','run_id':run_id}
        if replace_session_id is not None:
            intent['replace_session_id'] = replace_session_id
        digest=hashlib.sha256(json.dumps(intent,sort_keys=True).encode()).hexdigest()
        reservation=self.repository.reserve_load(actor.actor_id,request_id,digest,manifest['scenario_id'],self.installation_identity(),
            manifest['artifacts']['fixture']['sha256'],registered['manifest_sha256'],run_id=run_id)
        if reservation['cached'] is not None: return reservation['cached']
        try:
            boot=await self.gateway.bootstrap_run(reservation['session_id'],reservation['command_id'],run_id,registered['manifest_sha256'])
            view=await self.gateway.resolve(reservation['session_id'])
            if boot['basis'] != view['basis']: raise ApiError(503,'BOOTSTRAP_BINDING_CHANGED','run','Bootstrap basis changed')
            data={'schema_version':'saferoute-m3-loaded-session/1','session':self.public_session(reservation['session']),'execution_view':view}
            self.repository.complete_load(reservation,data,replace_session_id=replace_session_id); self.observe(view,'LOAD_RUN',reservation['command_id'])
            return data
        finally: self.repository.release_load(reservation)

    def fixture(self,row):
        return self.runs.fixture(row) if row.get('run_id') else self.catalog.fixture(row['scenario_id'])

    def session(self, session_id):
        self.catalog.ensure_current()
        try:
            row = self.repository.record(session_id)
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "METADATA_UNAVAILABLE", "session", "Session metadata is unavailable") from error
        if row["status"] != "READY":
            raise ApiError(409, "SESSION_NOT_READY", "session_id", "Load has not completed; retry its original request ID")
        if row.get('run_id'):
            if row['installation_sha256'] != self.installation_identity():
                raise ApiError(409,'INSTALLATION_BINDING_CHANGED','session','Session belongs to another build')
            self.fixture(row)
            return row
        if (row["installation_sha256"] != self.installation_identity() or row["catalog_sha256"] != self.catalog.catalog_sha256
                or row["fixture_sha256"] != self.catalog.fixture_sha256.get(row["scenario_id"])):
            raise ApiError(409, "INSTALLATION_BINDING_CHANGED", "session", "Session belongs to another server installation")
        self.catalog.ensure_current(row["scenario_id"])
        return row

    def public_session(self, row):
        prefix = "/api/sessions/" + row["session_id"]
        return {"session_id": row["session_id"], "scenario_id": row["scenario_id"], "created_at": row["created_at"],
            "build_sha256": self.settings.installation()["expected_build_sha256"], "catalog_sha256": row["catalog_sha256"],
            "fixture_sha256": row["fixture_sha256"], "links": {key: prefix + "/" + key for key in ("state", "orders", "vehicles", "locations")},
            **({'run_id':row['run_id']} if row.get('run_id') else {})}

    def mutation_allowed(self, session_id):
        from .recovery_repository import RecoveryRepository
        try:
            RecoveryRepository(self.settings.metadata_path).assert_mutation_allowed(session_id,
                allow_validated_recovery=getattr(self, "allow_validated_recovery", False))
        except sqlite3.Error as error:
            raise ApiError(503, "RECOVERY_METADATA_UNAVAILABLE", "recovery", "Recovery gate is unavailable") from error

    def observe(self, view, source, reference_id=None):
        from .artifact_repository import ArtifactRepository
        try:
            repository = ArtifactRepository(self.settings.metadata_path)
            repository.initialize()
            repository.observe_execution(view["basis"]["session_id"], view, source=source, reference_id=reference_id)
        except (sqlite3.Error, OSError) as error:
            raise ApiError(503, "EVIDENCE_METADATA_UNAVAILABLE", "evidence", "Public observation could not be durably preserved") from error

    async def state(self, session_id):
        self.session(session_id)
        view = await self.gateway.resolve(session_id)
        self.observe(view, "HTTP_STATE_READ")
        return view

    async def projection(self, session_id, kind, *, view=None):
        row = self.session(session_id)
        if view is None:
            view = await self.gateway.resolve(session_id)
        fixture = self.fixture(row)
        data = {"schema_version": f"saferoute-m3-{kind}-view/1", "basis": view["basis"], "current_time": view["current_time"],
                "execution_mode": view["execution_mode"], "real_world_observation": view["real_world_observation"],
                "units": {"distance": "m", "duration": "s", "mass": "kg", "money": "VND"}}
        if kind == "vehicles":
            data["vehicles"] = view["vehicles"]
            data["vehicle_metadata"] = [{"vehicle_id": item["id"], "vehicle_type": item["type"],
                "cost_per_km_vnd": item["costPerKmVnd"], "range_m": item["rangeKm"] * 1000,
                "working_start": item["workingStart"], "working_end": item["workingEnd"],
                "display": display_metadata(item, VEHICLE_FIELDS)} for item in fixture["initialState"]["vehicles"]]
        else:
            static_orders = {item["id"]: item for item in fixture["initialState"]["orders"]}
            for event in fixture["events"]:
                if event["type"] == "URGENT_ORDER":
                    item = event["orderPayload"]
                    static_orders[item["id"]] = item
            if view.get('delivery_day'):
                for oid,o in view['delivery_day']['catalog'].items():
                    static_orders[oid]={**static_orders.get(oid, {}),'id':oid,'demandKg':o['demand_kg'],'priority':o['priority'],
                        'pickupLocationId':o['pickup_location_id'],'deliveryRegionId':o['delivery_region_id'],
                        'graphNodeId':o['graph_node_id'],'longitude':o['coordinates'][0],'latitude':o['coordinates'][1],
                        'serviceTimeHours':o['service_time_seconds']/3600,'earliest':o['earliest'],
                        'preferredDue':o['preferred_due'],'hardDeadline':o['hard_deadline']}
            if not set(view["order_ids"]) <= set(static_orders):
                raise ApiError(503, "ORDER_METADATA_UNAVAILABLE", "orders", "Runtime order has no verified source metadata")
            orders = [static_orders[key] for key in view["order_ids"]]
            if kind == "locations":
                data["locations"] = [{"location_id": item["id"], "kind": "DEPOT", "graph_node_id": str(item["graphNodeId"]),
                    "coordinates": [item["longitude"], item["latitude"]], "opening_time": item["openingTime"],
                    "closing_time": item["closingTime"]} for item in fixture["initialState"]["locations"]]
                data["locations"].extend({"location_id": "delivery:" + item["id"], "kind": "DELIVERY", "order_id": item["id"],
                    "graph_node_id": str(item["graphNodeId"]), "coordinates": [item["longitude"], item["latitude"]]} for item in orders)
                if view.get('delivery_day'):
                    data['locations']=[{**d,'kind':'DEPOT','graph_node_id':str(d['graph_node_id'])} for d in view['delivery_day']['depots']]+[d for d in data['locations'] if d['kind']=='DELIVERY']
                data['rain'] = None
                if view['basis'].get('overlay_sha256'):
                    from datetime import datetime, timedelta
                    shift = timedelta(seconds=view.get('delivery_day', {}).get('event_shift_seconds', 0))
                    for event in fixture['events']:
                        if event['type'] != 'LOCAL_RAIN_WHAT_IF' or event['eventId'] in view['pending_event_ids']:
                            continue
                        start = datetime.fromisoformat(event['startTime']) + shift
                        end = datetime.fromisoformat(event['endTime']) + shift
                        if start <= datetime.fromisoformat(view['current_time']) < end:
                            data['rain'] = {'polygon': event['polygon'], 'ends_at': end.isoformat()}
                            break
            elif kind == "orders":
                onboard = {key: vehicle["vehicle_id"] for vehicle in view["vehicles"] for key in vehicle["onboard_order_ids"]}
                delivered, planned = set(view["delivered_prefix"]), set(view["planned_served_suffix"])
                unserved = {item["order_id"]: item["reason"] for item in view["unserved"]}
                data["orders"] = [{"order_id": item["id"], "status": "DELIVERED" if item["id"] in delivered else "ONBOARD" if item["id"] in onboard else "WAITING",
                    "owner_vehicle_id": onboard.get(item["id"]), "planned_in_accepted_suffix": item["id"] in planned,
                    "unserved_reason": unserved.get(item["id"]), "demand_kg": item["demandKg"], "priority": item["priority"],
                    "pickup_location_id": item["pickupLocationId"], "delivery_region_id": item["deliveryRegionId"],
                    "graph_node_id": str(item["graphNodeId"]), "coordinates": [item["longitude"], item["latitude"]],
                    "service_time_s": item["serviceTimeHours"] * 3600, "earliest": item["earliest"],
                    "preferred_due": item["preferredDue"], "hard_deadline": item["hardDeadline"],
                    "display": display_metadata(item, ORDER_FIELDS)} for item in orders]
        return data
