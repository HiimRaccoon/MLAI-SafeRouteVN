"""Bounded turn-aware connectivity screening for automatic scenario depots."""

from collections import deque

from pyproj import Geod

GEOD = Geod(ellps="WGS84")


def probe_depot(graph, depot, config, *, delivery_area=None):
    """Find distinct outbound delivery nodes, not proof of return/VRP feasibility."""
    target = config["orders"]
    limit = min(config["searchMaxStates"], config.get("depotProbeMaxStates", 5000))
    queue = deque([(depot["nodeId"], None)])
    seen = {(depot["nodeId"], None)}
    positions = {}
    eligible = set()
    visited = 0
    while queue and visited < limit:
        node, incoming = queue.popleft()
        visited += 1
        if node not in positions:
            position = graph.db.execute("SELECT longitude,latitude FROM nodes WHERE nodeId=?", (node,)).fetchone()
            positions[node] = (GEOD.inv(depot["longitude"], depot["latitude"], position[0], position[1])[2] / 1000,
                               delivery_area.covers(position[0], position[1]) if delivery_area else True)
        distance, inside_area = positions[node]
        if (node != depot["nodeId"] and distance >= config["minDistanceKm"] and inside_area
                and (delivery_area is not None or distance <= config["radiusKm"])):
            eligible.add(node)
            if len(eligible) >= target:
                return {"qualified": True, "status": "outbound-screen-passed", "eligibleNodesFound": len(eligible), "visitedStates": visited}
        # The snap screen only considers a local neighborhood. Full route tests follow.
        if delivery_area is None and distance > config["radiusKm"]:
            continue
        for edge in graph.outgoing(node):
            state = (edge["toNodeId"], edge["edgeId"])
            if state not in seen and (incoming, edge["edgeId"]) not in graph.forbidden:
                seen.add(state)
                queue.append(state)
    return {"qualified": False, "status": "probe-budget-exceeded" if queue else "insufficient-local-outbound-nodes",
            "eligibleNodesFound": len(eligible), "visitedStates": visited}


def select_depot(graph, config, *, progress=print, audit=None, delivery_area=None):
    audit = audit if audit is not None else []
    lon, lat = config["anchorLongitude"], config["anchorLatitude"]
    # Exact geodesic ranking; original componentId may be stale after quarantine.
    candidates = []
    for row in graph.db.execute("""SELECT n.* FROM nodes n
        WHERE EXISTS(SELECT 1 FROM edges WHERE fromNodeId=n.nodeId)
          AND EXISTS(SELECT 1 FROM edges WHERE toNodeId=n.nodeId) ORDER BY nodeId"""):
        distance = GEOD.inv(lon, lat, row["longitude"], row["latitude"])[2] / 1000
        if distance <= config["maxDepotSnapKm"] and (delivery_area is None or delivery_area.covers(row["longitude"], row["latitude"])):
            candidates.append((distance, row["nodeId"], dict(row)))
    candidates.sort(key=lambda item: (item[0], item[1]))
    for distance, node_id, node in candidates[:config.get("depotCandidateLimit", 32)]:
        result = probe_depot(graph, node, config, delivery_area=delivery_area)
        audit.append({"nodeId": node_id, "snapDistanceKm": distance, **result})
        progress(f"Depot {node_id} | snap {distance:.3f} km | {result['status']} | delivery nodes {result['eligibleNodesFound']}/{config['orders']}")
        if result["qualified"]:
            return node
    raise ValueError("No auto depot passed the local turn-aware connectivity screen. Inspect diagnostics.json; adjust anchor/depotCandidateLimit/depotProbeMaxStates or supply an explicit depot node.")
