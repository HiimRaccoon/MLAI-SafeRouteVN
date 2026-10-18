"""Regional weather grid for all retained routing edges, including islands."""

import json
import math
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import LineString

from geo_data.bundle import cached, database, positive, publish, reset_database, temporary, verify
from geo_data.common import exclusive_lock, write_json
from geo_data.osm.process_graph import ROUTING_SCHEMA

PLAN_SCHEMA = "member1-weather-plan/1"


def weather_plan(routing, output, *, grid_km=20, progress=print):
    positive(grid_km, "gridKm")
    routing, output = Path(routing), Path(output)
    source = verify(routing, ROUTING_SCHEMA)
    identity = {"stage": PLAN_SCHEMA, "routingVersion": source["version"], "gridKm": grid_km,
                "assignment": "projected-polyline-midpoint", "crs": "EPSG:32648"}
    project = Transformer.from_crs(4326, 32648, always_xy=True)
    inverse = Transformer.from_crs(32648, 4326, always_xy=True)
    with exclusive_lock(output):
        if saved := cached(output, identity):
            progress(f"Weather grid cache verified: {output}")
            return saved
        path = reset_database(output, "mapping.sqlite")
        regions = {}
        count = 0
        with database(routing / "network.sqlite") as graph, database(path, readonly=False) as db:
            db.execute("CREATE TABLE edge_regions(edgeId TEXT PRIMARY KEY,regionId TEXT NOT NULL)")
            for edge in graph.execute("SELECT edgeId,geometryJson,direction FROM edges ORDER BY edgeId"):
                coords = json.loads(edge["geometryJson"])["coordinates"]
                if edge["direction"] == "backward":
                    coords.reverse()
                x, y = project.transform([c[0] for c in coords], [c[1] for c in coords])
                point = LineString(zip(x, y)).interpolate(0.5, normalized=True)
                col, row = math.floor(point.x / (grid_km * 1000)), math.floor(point.y / (grid_km * 1000))
                region_id = f"c{col}_r{row}"
                if region_id not in regions:
                    lon, lat = inverse.transform((col + 0.5) * grid_km * 1000, (row + 0.5) * grid_km * 1000)
                    regions[region_id] = {"regionId": region_id, "latitude": lat, "longitude": lon, "edgeCount": 0}
                regions[region_id]["edgeCount"] += 1
                db.execute("INSERT INTO edge_regions VALUES (?,?)", (edge["edgeId"], region_id))
                count += 1
                if count % 50000 == 0:
                    progress(f"Weather grid {count:,}/{source['edgeCount']:,} edges | {len(regions)} regions")
            db.execute("CREATE INDEX region_edges ON edge_regions(regionId)")
        write_json(temporary(output, "regions.json"), {"regions": [regions[k] for k in sorted(regions)],
                   "gridKm": grid_km, "assignment": identity["assignment"],
                   "limitation": "One weather sample per grid cell, assigned by edge midpoint; not street observations or provider resolution."})
        return publish(output, identity, PLAN_SCHEMA, ["mapping.sqlite", "regions.json"],
                       routingVersion=source["version"], regionCount=len(regions), edgeCount=count)
