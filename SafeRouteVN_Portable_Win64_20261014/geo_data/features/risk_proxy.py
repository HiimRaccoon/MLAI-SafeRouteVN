"""Configurable dimensionless proxy factors, with explicit missing-data rules."""

import math
from pathlib import Path

from geo_data.bundle import positive
from geo_data.common import read_json

DEFAULT_RISK = Path(__file__).with_name("profiles") / "risk_v1.json"


def load_risk(path):
    config = read_json(path)
    if not config.get("riskModelVersion"):
        raise ValueError("Missing riskModelVersion")
    expected = {"weights": {"roadFactor", "timeFactor", "weatherFactor", "trafficFactor"},
                "roadWeights": {"highway", "width", "surface"}}
    for key, keys in expected.items():
        if set(config[key]) != keys or not math.isclose(sum(config[key].values()), 1, abs_tol=1e-9):
            raise ValueError(f"{key} must contain the required factors and sum to 1")
    factors = [config["missingFactor"], *config["weights"].values(), *config["roadWeights"].values(),
               *config["highwayFactor"].values(), *config["surfaceFactor"].values()]
    if any(not 0 <= positive(v, "factor", zero=True) <= 1 for v in factors):
        raise ValueError("Risk factors must be in [0,1]")
    for key in ("widthReferenceM", "rainReferenceMmPerHour", "windReferenceKph", "visibilityReferenceM"):
        positive(config[key], key)
    positive(config["weatherDelayScale"], "weatherDelayScale", zero=True)
    return config


def clamp(value):
    return min(1.0, max(0.0, value))


def risk_values(edge, tags, weather, travel, config):
    flags = []
    fallback = config["missingFactor"]
    highway = config["highwayFactor"].get(edge["highway"], fallback)
    if edge["highway"] not in config["highwayFactor"]:
        flags.append("risk:highway-fallback")
    width = fallback if edge["widthM"] is None else clamp(1 - positive(edge["widthM"], "widthM") / config["widthReferenceM"])
    if edge["widthM"] is None:
        flags.append("risk:width-fallback")
    surface = config["surfaceFactor"].get(tags.get("surface"), fallback)
    if tags.get("surface") not in config["surfaceFactor"]:
        flags.append("risk:surface-fallback")
    road = sum(config["roadWeights"][key] * value for key, value in (("highway", highway), ("width", width), ("surface", surface)))
    weather_parts = []
    for key, reference in (("precipitationMm", "rainReferenceMmPerHour"), ("windSpeedKph", "windReferenceKph"), ("visibilityM", "visibilityReferenceM")):
        value = weather[key]
        if value is None:
            weather_parts.append(fallback)
            flags.append(f"risk:{key}-fallback")
        else:
            ratio = positive(value, key, zero=True) / config[reference]
            if key == "precipitationMm":
                ratio /= positive(weather["precipitationIntervalHours"], "precipitationIntervalHours")
            weather_parts.append(clamp(1 - ratio if key == "visibilityM" else ratio))
    if weather["fallbackUsed"]:
        flags.append("weather:stale-fallback")
    factors = {"roadFactor": road, "timeFactor": travel["timeFactor"],
               "weatherFactor": max(weather_parts), "trafficFactor": travel["trafficFactor"]}
    proxy = sum(config["weights"][key] * value for key, value in factors.items())
    return {**factors, "edgeProxy": clamp(proxy), "relativeExposure": edge["lengthKm"] * clamp(proxy),
            "weatherTravelMultiplier": 1 + config["weatherDelayScale"] * factors["weatherFactor"],
            "missingFlags": flags, "sourceType": "PROXY"}
