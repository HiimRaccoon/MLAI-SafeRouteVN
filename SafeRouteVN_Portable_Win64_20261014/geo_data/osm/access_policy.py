"""Versioned, conservative access decisions. Never infer time-dependent access."""

from pathlib import Path
import re

from geo_data.common import read_json

DEFAULT_POLICY = Path(__file__).with_name("policies") / "motorcycle_v1.json"
ACCESS_KEYS = ("motorcycle", "motor_vehicle", "vehicle", "access")
ONEWAY_KEYS = ("oneway:motorcycle", "oneway:motor_vehicle", "oneway:vehicle", "oneway")


def load_policy(path=DEFAULT_POLICY):
    policy = read_json(path)
    if not isinstance(policy.get("policyVersion"), str) or not policy["policyVersion"]:
        raise ValueError("Policy needs a nonempty policyVersion")
    for key in ("defaultHighways", "explicitMotorAccessHighways", "allowedAccess",
                "passableBarriers", "hardBarriers"):
        if not isinstance(policy.get(key), list) or any(not isinstance(v, str) for v in policy[key]):
            raise ValueError(f"Invalid policy list: {key}")
    for key, value in {"conditionalHandling": "exclude", "unknownAccessHandling": "exclude",
                       "unknownBarrierHandling": "require-explicit-motor-access"}.items():
        if policy.get(key) != value:
            raise ValueError(f"Unsupported policy {key}; expected {value}")
    if set(policy["defaultHighways"]) & set(policy["explicitMotorAccessHighways"]):
        raise ValueError("Highway policy lists must be disjoint")
    if set(policy["hardBarriers"]) & set(policy["passableBarriers"]):
        raise ValueError("Barrier policy lists must be disjoint")
    return policy


def conditional_keys(tags):
    relevant = set(ACCESS_KEYS + ONEWAY_KEYS)
    relevant.update(f"{key}:{direction}" for key in ACCESS_KEYS for direction in ("forward", "backward"))
    return sorted(key for key in tags if key.endswith(":conditional") and key[:-12] in relevant)


def access_value(tags, direction=None):
    for key in ACCESS_KEYS:
        if direction and f"{key}:{direction}" in tags:
            return f"{key}:{direction}", tags[f"{key}:{direction}"]
        if key in tags:
            return key, tags[key]
    return None, None


def way_decision(tags, policy):
    """Return allowed OSM directions and auditable reasons for omitted directions."""
    if conditional_keys(tags):
        return (), ["conditional-access-or-oneway"]
    if tags.get("area") == "yes":
        return (), ["area-highway"]
    highway = tags.get("highway", "")
    if highway not in policy["defaultHighways"] + policy["explicitMotorAccessHighways"]:
        return (), [f"highway-excluded:{highway or 'missing'}"]
    oneway = next((tags[key] for key in ONEWAY_KEYS if key in tags), None)
    if oneway is None:
        oneway = "yes" if tags.get("junction") == "roundabout" or highway == "motorway" else "no"
    directions = {"yes": ("forward",), "1": ("forward",), "true": ("forward",),
                  "-1": ("backward",), "no": ("forward", "backward"),
                  "0": ("forward", "backward"), "false": ("forward", "backward")}.get(oneway)
    if directions is None:
        return (), [f"unknown-oneway:{oneway}"]
    allowed, reasons = [], []
    for direction in directions:
        key, value = access_value(tags, direction)
        if value is not None and value not in policy["allowedAccess"]:
            reasons.append(f"{direction}:{key}={value}")
        elif highway in policy["explicitMotorAccessHighways"] and (
                key is None or key.split(":")[0] == "access"):
            reasons.append(f"{direction}:explicit-motor-access-required:{highway}")
        else:
            allowed.append(direction)
    return tuple(allowed), reasons


def node_block_reason(tags, policy):
    if conditional_keys(tags):
        return "node-conditional-access"
    # Directional node restrictions need approach semantics: exclude conservatively.
    if any(f"{key}:{d}" in tags for key in ACCESS_KEYS for d in ("forward", "backward")):
        return "node-directional-access"
    key, value = access_value(tags)
    if value is not None and value not in policy["allowedAccess"]:
        return f"node-{key}={value}"
    barrier = tags.get("barrier")
    if not barrier or barrier == "no":
        return None
    if barrier in policy["hardBarriers"]:
        return f"hard-barrier:{barrier}"
    if barrier in policy["passableBarriers"]:
        return None
    if key and key != "access" and value in policy["allowedAccess"]:
        return None
    return f"unverified-barrier:{barrier}"


def width_metres(raw):
    """OSM plain numeric widths default to metres; ambiguous strings remain missing."""
    if raw is None:
        return None, "widthM:missing"
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:m)?\s*", raw)
    if match and 0 < float(match[1]) < float("inf"):
        return float(match[1]), None
    return None, "widthM:unparsed"
