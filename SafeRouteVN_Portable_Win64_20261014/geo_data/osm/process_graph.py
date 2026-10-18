"""Turn-aware routing preparation: via-node rules plus conservative exclusions."""

from collections import Counter
from pathlib import Path

from geo_data.bundle import cached, database, publish, reset_database, temporary, verify
from geo_data.common import exclusive_lock, read_json, write_json

ROUTING_SCHEMA = "member1-routing/1"
MODES = ("motorcycle", "motor_vehicle", "vehicle")
VALUES = {"no_left_turn", "no_right_turn", "no_straight_on", "no_u_turn",
          "only_left_turn", "only_right_turn", "only_straight_on", "only_u_turn"}


def classify(record):
    tags = record["tags"]
    keys = ["restriction:" + mode for mode in MODES] + ["restriction"]
    key = next((k for k in keys if k in tags), None)
    conditional = any(k + ":conditional" in tags for k in keys)
    exceptions = set(tags.get("except", "").replace(",", ";").split(";"))
    exceptions = {e.strip() for e in exceptions}
    if exceptions.intersection(MODES):
        # Conflicting explicit mode restriction is not treated as an exemption.
        if any(k in tags or k + ":conditional" in tags for k in keys[:-1]):
            return "quarantine", "conflicting-mode-exception", None
        return "exempt", "except-motorcycle", None
    if not key and not conditional and any(k.startswith("restriction:") for k in tags):
        return "exempt", "other-mode", None
    value = tags.get(key, "")
    roles = sorted((m["type"], m["role"]) for m in record["members"])
    if conditional or "@" in value:
        return "quarantine", "conditional", value
    if roles != [("n", "via"), ("w", "from"), ("w", "to")] or value not in VALUES:
        return "quarantine", "unsupported-members-or-value", value
    ways = [m["ref"] for m in record["members"] if m["type"] == "w"]
    if ways[0] == ways[1] and value not in ("no_u_turn", "only_u_turn"):
        return "quarantine", "ambiguous-same-way-turn", value
    return "via-node", "static-via-node", value


def prepare_routing(graph, output, *, progress=print):
    graph, output = Path(graph), Path(output)
    source = verify(graph, "member1-road-graph-preview/1")
    identity = {"stage": ROUTING_SCHEMA, "graphVersion": source["graphVersion"],
                "sourceFiles": source["files"], "unsupportedPolicy": "exclude-from-ways-or-all-anchors/1"}
    with exclusive_lock(output):
        if saved := cached(output, identity):
            progress(f"Routing cache verified: {output}")
            return saved
        records = read_json(graph / "restrictions.json")["records"]
        audit, static = [], []
        target = reset_database(output, "network.sqlite")
        progress("Routing [1/3] copying graph locally; compiling restriction policy...")
        with database(graph / "graph.sqlite") as src, database(target, readonly=False) as db:
            src.backup(db)
            db.executescript("""
              CREATE TABLE excluded_edges(edgeId TEXT NOT NULL,relationId INTEGER NOT NULL,reason TEXT NOT NULL,
                                          PRIMARY KEY(edgeId,relationId));
              CREATE TABLE forbidden_turns(inEdgeId TEXT NOT NULL,outEdgeId TEXT NOT NULL,relationId INTEGER NOT NULL,
                                           PRIMARY KEY(inEdgeId,outEdgeId,relationId));
              CREATE INDEX turns_in ON forbidden_turns(inEdgeId);
            """)
            for record in records:
                kind, reason, value = classify(record)
                row = {"relationId": record["relationId"], "action": kind, "reason": reason,
                       "excludedEdges": 0, "forbiddenPairs": 0}
                if kind == "quarantine":
                    from_ways = [m["ref"] for m in record["members"] if m["type"] == "w" and m["role"] == "from"]
                    all_ways = [m["ref"] for m in record["members"] if m["type"] == "w"]
                    nodes = [m["ref"] for m in record["members"] if m["type"] == "n"]
                    anchors = from_ways or all_ways
                    if not anchors and not nodes:
                        raise ValueError(f"Restriction {record['relationId']} has no usable anchors; manual correction required")
                    for way in anchors:
                        db.execute("INSERT OR IGNORE INTO excluded_edges SELECT edgeId,?,? FROM edges WHERE osmWayId=?",
                                   (record["relationId"], reason, way))
                    # For malformed relations without from ways also exclude node approaches.
                    if not from_ways:
                        for node in nodes:
                            db.execute("INSERT OR IGNORE INTO excluded_edges SELECT edgeId,?,? FROM edges WHERE fromNodeId=? OR toNodeId=?",
                                       (record["relationId"], reason, node, node))
                    row["excludedEdges"] = db.execute("SELECT COUNT(*) FROM excluded_edges WHERE relationId=?", (record["relationId"],)).fetchone()[0]
                elif kind == "via-node":
                    static.append((record, row, value))
                audit.append(row)
            db.execute("DELETE FROM edges WHERE edgeId IN (SELECT edgeId FROM excluded_edges)")
            progress("Routing [2/3] compiling forbidden incoming/outgoing edge pairs...")
            for record, row, value in static:
                members = {m["role"]: m["ref"] for m in record["members"]}
                incoming = list(db.execute("SELECT * FROM edges WHERE osmWayId=? AND toNodeId=?", (members["from"], members["via"])))
                outgoing = list(db.execute("SELECT * FROM edges WHERE fromNodeId=?", (members["via"],)))
                for before in incoming:
                    for after in outgoing:
                        matches = after["osmWayId"] == members["to"]
                        if value.endswith("u_turn") and members["from"] == members["to"]:
                            matches = matches and before["startIndex"] == after["startIndex"] and before["endIndex"] == after["endIndex"] and before["direction"] != after["direction"]
                        forbidden = not matches if value.startswith("only_") else matches
                        if forbidden:
                            db.execute("INSERT OR IGNORE INTO forbidden_turns VALUES (?,?,?)", (before["edgeId"], after["edgeId"], record["relationId"]))
                            row["forbiddenPairs"] += 1
                row["incomingEdges"] = len(incoming)
            db.execute("DELETE FROM nodes WHERE nodeId NOT IN (SELECT fromNodeId FROM edges UNION SELECT toNodeId FROM edges)")
            edge_count = db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            if edge_count == 0:
                raise ValueError("Restriction policy removed all graph edges")
            excluded_count = db.execute("SELECT COUNT(DISTINCT edgeId) FROM excluded_edges").fetchone()[0]
            pairs = db.execute("SELECT COUNT(*) FROM forbidden_turns").fetchone()[0]
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('routingReady','true-with-turn-aware-reader')")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Routing SQLite integrity failed")
        report = {"records": audit, "counts": dict(Counter(row["action"] for row in audit)),
                  "excludedEdges": excluded_count, "forbiddenPairs": pairs,
                  "policy": "Unsupported restrictions exclude whole from ways in both directions; otherwise all available anchors.",
                  "limitations": ["Conservative exclusions can remove valid routes.", "Access defaults remain engineering assumptions.",
                                  "Original componentId values are provenance only after edge removal; check directed reachability again."]}
        write_json(temporary(output, "restriction_report.json"), report)
        progress(f"Routing [3/3] {edge_count:,} edges; {excluded_count:,} exclusions; {pairs:,} forbidden pairs")
        return publish(output, identity, ROUTING_SCHEMA, ["network.sqlite", "restriction_report.json"],
                       graphVersion=source["graphVersion"], scope=source["scope"], routingReady=True,
                       routingMode="conservative-turn-aware", requiresTurnAwareReader=True,
                       integrated=False, edgeCount=edge_count)
