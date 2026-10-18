"""Hourly model-weather snapshots with raw cache, retry and explicit fallback."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
from pathlib import Path
import time

import requests

from geo_data.bundle import cached, instant, positive, publish, temporary, verify
from geo_data.common import digest, exclusive_lock, now_vn, read_json, write_json
from geo_data.weather.context_builder import PLAN_SCHEMA

ENDPOINT = "https://api.open-meteo.com/v1/forecast"
CONTEXT_SCHEMA = "member1-weather-context/1"
VARIABLES = {"precipitation": ("precipitationMm", "mm"), "wind_speed_10m": ("windSpeedKph", "km/h"),
             "visibility": ("visibilityM", "m"), "weather_code": ("weatherCode", "wmo code")}


def normalize(payload, *, at, fetched_at, region):
    epoch = instant(at)
    if payload.get("error"):
        raise ValueError(f"Weather provider error: {payload.get('reason')}")
    units, hourly = payload["hourly_units"], payload["hourly"]
    if units.get("time") != "unixtime":
        raise ValueError("Weather time unit must be unixtime")
    times = hourly["time"]
    if not times or any(isinstance(t, bool) or not isinstance(t, (int, float)) for t in times):
        raise ValueError("Invalid weather timestamps")
    if any(b-a != 3600 for a, b in zip(times, times[1:])):
        raise ValueError("Weather series must have unique hourly timestamps")
    wanted = epoch.replace(minute=0, second=0, microsecond=0).timestamp()
    if wanted not in times:
        raise ValueError("Weather response does not cover the requested hour")
    index = times.index(wanted)
    missing, values = [], {}
    for key, (field, unit) in VARIABLES.items():
        if str(units.get(key, "")).lower() != unit:
            raise ValueError(f"Unexpected weather unit for {key}: {units.get(key)}")
        array = hourly.get(key)
        if not isinstance(array, list) or len(array) != len(times):
            raise ValueError(f"Invalid hourly array {key}")
        value = array[index]
        if value is None:
            missing.append(field)
        else:
            positive(value, field, zero=True)
            if key == "weather_code" and int(value) != value:
                raise ValueError("Weather code must be an integer")
        values[field] = value
    return {**values, "regionId": region["regionId"], "requestedLatitude": region["latitude"],
            "requestedLongitude": region["longitude"], "providerLatitude": payload.get("latitude"),
            "providerLongitude": payload.get("longitude"),
            "validAt": datetime.fromtimestamp(wanted, timezone.utc).astimezone(epoch.tzinfo).isoformat(),
            "precipitationIntervalHours": 1, "precipitationInterval": "preceding-hour-ending-at-validAt",
            "fetchedAt": fetched_at, "sourceTimestamp": None, "provider": "Open-Meteo Forecast",
            "model": None, "sourceType": "REAL-derived", "missingFlags": missing + ["sourceTimestamp", "model"],
            "fallbackUsed": False, "providerAttribution": "Weather data by Open-Meteo, CC BY 4.0"}


def request_weather(params, user_agent, *, session=requests, attempts=3, sleep=time.sleep, progress=None):
    last = None
    for attempt in range(attempts):
        if progress:
            progress(f"  HTTP attempt {attempt + 1}/{attempts}")
        try:
            response = session.get(ENDPOINT, params=params, headers={"User-Agent": user_agent}, timeout=(15, 45))
            if response.status_code == 429 or response.status_code >= 500:
                last = requests.HTTPError(f"Open-Meteo HTTP {response.status_code}")
                if attempt + 1 < attempts:
                    retry = response.headers.get("Retry-After", "")
                    try:
                        delay = float(retry)
                    except ValueError:
                        try:
                            delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                        except (TypeError, ValueError, OverflowError):
                            delay = 0
                    if not math.isfinite(delay):
                        delay = 0
                    delay = max(2 ** (attempt+1), delay)
                    if progress:
                        progress(f"  HTTP {response.status_code}; retry in {delay:g} seconds")
                    sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = exc
            if attempt + 1 < attempts:
                if progress:
                    progress(f"  Connection interrupted; retry in {2 ** (attempt+1)} seconds")
                sleep(2 ** (attempt+1))
    raise requests.ConnectionError(f"Weather request failed after {attempts} attempts: {last}")


def fetch_context(plan, output, *, at, user_agent=None, offline=False, fallback_context=None,
                  max_stale_hours=6, attempts=3, session=requests, sleep=time.sleep, progress=print):
    plan, output = Path(plan), Path(output)
    source = verify(plan, PLAN_SCHEMA)
    epoch = instant(at)
    positive(max_stale_hours, "maxStaleHours", zero=True)
    if not isinstance(attempts, int) or not 1 <= attempts <= 10:
        raise ValueError("attempts must be between 1 and 10")
    if not offline and not user_agent:
        raise ValueError("Online weather requires --user-agent")
    fallback, fallback_version = {}, None
    if fallback_context:
        previous = verify(fallback_context, CONTEXT_SCHEMA)
        if previous["planVersion"] != source["version"]:
            raise ValueError("Fallback context belongs to another weather grid")
        fallback_version = previous["contextVersion"]
        fallback = {r["regionId"]: r for r in read_json(Path(fallback_context) / "weather_context.json")["regions"]}
    identity = {"stage": CONTEXT_SCHEMA, "planVersion": source["version"], "at": epoch.isoformat(),
                "endpoint": ENDPOINT, "fallbackVersion": fallback_version, "maxStaleHours": max_stale_hours}
    with exclusive_lock(output):
        if saved := cached(output, identity):
            progress(f"Weather snapshot cache verified: {output}")
            return saved
        regions = read_json(plan / "regions.json")["regions"]
        records, raw_names = [], []
        for index, region in enumerate(regions, 1):
            rid = region["regionId"]
            params = {"latitude": region["latitude"], "longitude": region["longitude"],
                      "hourly": ",".join(VARIABLES), "timeformat": "unixtime", "timezone": "Asia/Ho_Chi_Minh",
                      "wind_speed_unit": "kmh", "precipitation_unit": "mm",
                      "start_date": epoch.date().isoformat(), "end_date": epoch.date().isoformat()}
            key = digest({"endpoint": ENDPOINT, "params": params})
            name = f"raw/{rid}.json"
            raw_path = output / name
            progress(f"Weather [{index}/{len(regions)}] {rid}...")
            try:
                if raw_path.exists():
                    raw = read_json(raw_path)
                    if raw["requestKey"] != key or digest(raw["payload"]) != raw["payloadSha256"]:
                        raise ValueError(f"Corrupt raw weather cache: {rid}")
                else:
                    if offline:
                        raise requests.ConnectionError(f"Offline weather cache missing: {rid}")
                    payload = request_weather(params, user_agent, session=session, attempts=attempts, sleep=sleep, progress=progress)
                    raw = {"requestKey": key, "params": params, "fetchedAt": now_vn(), "payload": payload, "payloadSha256": digest(payload)}
                    # Validate before publishing raw cache; malformed responses cannot poison resume.
                    normalize(payload, at=at, fetched_at=raw["fetchedAt"], region=region)
                    write_json(raw_path, raw)
                    sleep(0.25)
                record = normalize(raw["payload"], at=at, fetched_at=raw["fetchedAt"], region=region)
                record["rawFile"] = name
                record["rawPayloadSha256"] = raw["payloadSha256"]
                raw_names.append(name)
            except requests.RequestException as exc:
                old = fallback.get(rid)
                age = (epoch - instant(old["validAt"])).total_seconds() / 3600 if old else None
                if old is None or not 0 <= age <= max_stale_hours:
                    raise ValueError(f"Weather region {rid} unavailable; no valid explicit fallback: {exc}") from exc
                record = {**old, "fallbackUsed": True, "fallbackReason": str(exc), "staleAgeHours": age,
                          "fallbackContextVersion": fallback_version,
                          "missingFlags": sorted(set(old["missingFlags"] + ["stale-weather"]))}
                # rawFile belongs to the old snapshot, never pretend it was fetched here.
                record["fallbackRawFile"] = record.pop("rawFile", None)
            records.append(record)
        context = {"at": epoch.isoformat(), "planVersion": source["version"], "regions": records,
                   "spatialModel": "grid-samples-assigned-by-edge-midpoint", "sourceType": "REAL-derived"}
        write_json(temporary(output, "weather_context.json"), context)
        # Include payload identity: two fresh captures for the same epoch are different snapshots.
        return publish(output, identity, CONTEXT_SCHEMA, ["weather_context.json"], retained=raw_names,
                       version=digest(context),
                       planVersion=source["version"], routingVersion=source["routingVersion"],
                       contextVersion=digest(context), at=epoch.isoformat(), regionCount=len(records),
                       fallbackRegions=sum(r["fallbackUsed"] for r in records),
                       units={"precipitation": "mm", "precipitationInterval": "h", "windSpeed": "km/h", "visibility": "m"})
