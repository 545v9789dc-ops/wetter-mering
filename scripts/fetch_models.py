#!/usr/bin/env python3
"""Mering: robust Open-Meteo explicit single-run collection.
Extended winter and diagnostic parameters. Missing optional variables never
invalidate the core forecast. All timestamps in the hourly arrays are UTC.
"""
import copy
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

LAT, LON = 48.2656, 10.9846
UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
API = "https://single-runs-api.open-meteo.com/v1/forecast"
OUTPUT = Path("data/mering_models.json")
VARIABLES = "temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,pressure_msl"
NEAR_SURFACE = (
    "cloud_cover_low", "relative_humidity_2m", "dew_point_2m",
    "visibility", "sunshine_duration", "weather_code", "cloud_cover",
)
# Each optional field is queried individually. The hourly snowfall-height
# field is experimental: unsupported API/model responses are expected.
WINTER_AND_DIAGNOSTICS = (
    "snowfall", "snow_depth", "freezing_level_height", "snowfall_height",
    "wet_bulb_temperature_2m", "wind_direction_10m",
    "cloud_cover_mid", "cloud_cover_high", "shortwave_radiation",
    "temperature_850hPa", "boundary_layer_height",
)
MODELS = {
    "ICON-D2": {"model": "icon_d2", "cycle_hours": list(range(0, 24, 3)), "days": 3},
    "ICON-EU": {"model": "icon_eu", "cycle_hours": [0, 6, 12, 18],
                "days": 7, "verify_local_coverage": True, "min_future_hours": 6},
    "AROME": {"model": "meteofrance_arome_france",
              "cycle_hours": list(range(0, 24, 3)), "days": 3,
              "verify_local_coverage": True, "min_future_hours": 3},
    "GFS": {"model": "gfs_seamless", "cycle_hours": [0, 6, 12, 18], "days": 16},
    "IFS": {"model": "ecmwf_ifs025", "cycle_hours": [0, 6, 12, 18], "days": 16},
}
FAILURES = (HTTPError, URLError, ValueError, TimeoutError, OSError, TypeError, KeyError)
# Hard request deadline; no unbounded retries. The workflow's 45 min remains
# the final watchdog. Unsupported optional fields are not retried.
REQUEST_TIMEOUT_SECONDS = 12

def numeric(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

def parse_time(stamp):
    dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)

def grid_distance(lat, lon):
    if not numeric(lat) or not numeric(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("Missing or invalid grid coordinates")
    a, b = math.radians(LAT), math.radians(lat)
    h = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(math.radians(lon-LON)/2)**2
    return 6371 * 2 * math.asin(min(1, math.sqrt(h)))

def candidate_runs(now, cycle_hours, limit=12):
    hour = now.replace(minute=0, second=0, microsecond=0)
    candidates = []
    for offset in range(84):
        dt = hour - timedelta(hours=offset)
        if dt.hour in cycle_hours:
            candidates.append(dt)
            if len(candidates) >= limit:
                break
    return candidates

def fetch_json(url):
    with urlopen(Request(url, headers={"User-Agent": "wetter-mering/4.0"}), timeout=REQUEST_TIMEOUT_SECONDS) as res:
        return json.load(res)

def api_url(model, run, days, variables):
    query = urlencode({
        "latitude": LAT, "longitude": LON, "models": model,
        "run": run.strftime("%Y-%m-%dT%H:%M"),
        "hourly": variables, "timezone": "GMT", "forecast_days": days,
        "precipitation_unit": "mm", "wind_speed_unit": "kmh",
    })
    return API + "?" + query

def validate_hourly(hourly):
    times = hourly.get("time", [])
    if not times or not isinstance(times, list):
        raise ValueError("Missing time axis")
    dates = [parse_time(t) for t in times]
    if any(t.minute or t.second or t.microsecond for t in dates):
        raise ValueError("Non-hourly timestamps")
    if any(b-a != timedelta(hours=1) for a, b in zip(dates, dates[1:])):
        raise ValueError("Time axis duplicated, unordered or not hourly")
    for key, values in hourly.items():
        if not isinstance(values, list) or len(values) != len(times):
            raise ValueError(f"Invalid array length: {key}")

BOUNDS = {
    "temperature_2m": (-100, 65), "precipitation": (0, 1000),
    "wind_speed_10m": (0, 500), "wind_gusts_10m": (0, 500),
    "pressure_msl": (800, 1100), "cloud_cover": (0, 100),
    "cloud_cover_low": (0, 100), "cloud_cover_mid": (0, 100),
    "cloud_cover_high": (0, 100), "relative_humidity_2m": (0, 100),
    "dew_point_2m": (-110, 65), "visibility": (0, 1000000),
    "sunshine_duration": (0, 3600), "sunshine_duration_minutes": (0, 60),
    "snowfall": (0, 300), "snow_depth": (0, 30),
    "freezing_level_height": (-1000, 25000), "snowfall_height": (-1000, 25000),
    "wet_bulb_temperature_2m": (-100, 65), "wind_direction_10m": (0, 360),
    "shortwave_radiation": (0, 1500), "temperature_850hPa": (-110, 65),
    "boundary_layer_height": (0, 15000),
}
WEATHER_CODES = {0,1,2,3,45,48,51,53,55,56,57,61,63,65,66,67,
                 71,73,75,77,80,81,82,85,86,95,96,99}

def clean_values(hourly):
    invalid_counts = {}
    for key, values in hourly.items():
        if key == "time":
            continue
        low, high = BOUNDS.get(key, (-math.inf, math.inf))
        original = values
        hourly[key] = [
            v if numeric(v) and low <= v <= high and
            (key != "weather_code" or v in WEATHER_CODES) else None
            for v in values
        ]
        invalid_counts[key] = sum(v is not None and w is None
                                  for v, w in zip(original, hourly[key]))
    return invalid_counts

def validate_coverage(obj, hourly, now, min_future_hours):
    if grid_distance(obj.get("latitude"), obj.get("longitude")) > 35:
        raise ValueError("Model grid too far from Mering")
    valid = sum(
        parse_time(t) >= now and numeric(v)
        for t, v in zip(hourly["time"], hourly.get("temperature_2m", []))
    )
    if valid < min_future_hours:
        raise ValueError(f"Only {valid} valid future temperature hours")
    return valid

def add_optional_variables(record, model, run, days, keys=None):
    """Isolate every optional failure; preserve existing base and old optional data."""
    keys = tuple(keys if keys is not None else NEAR_SURFACE + WINTER_AND_DIAGNOSTICS)
    hourly = record["hourly"]
    base_times = hourly["time"]
    coverage = {}
    started = time.monotonic()
    optional_budget_seconds = 48
    for key in keys:
        if time.monotonic() - started >= optional_budget_seconds:
            coverage[key] = {"valid_hours": 0, "status": "not_checked",
                             "reason": "Optional request time budget exhausted"}
            continue
        url = api_url(model, run, days, key)
        try:
            extra = fetch_json(url)
            h = extra.get("hourly")
            if extra.get("error") or not isinstance(h, dict):
                raise ValueError(extra.get("reason", "Unsupported optional field"))
            if key not in h:
                raise ValueError("Optional field missing")
            validate_hourly(h)
            if grid_distance(extra.get("latitude"), extra.get("longitude")) > 35:
                raise ValueError("Optional grid too far from Mering")
            if (extra.get("latitude"), extra.get("longitude")) != (
                record["grid_latitude"], record["grid_longitude"]
            ):
                raise ValueError("Optional grid differs from base")
            unit = extra.get("hourly_units", {}).get(key)
            if key == "sunshine_duration" and unit not in ("s", "seconds"):
                raise ValueError("Unexpected sunshine unit")
            # Unit checks prevent confusing metres of snow depth with cm snowfall.
            if key == "snow_depth" and unit != "m":
                raise ValueError(f"Unexpected snow depth unit: {unit}")
            if key == "snowfall" and unit != "cm":
                raise ValueError(f"Unexpected snowfall unit: {unit}")
            clean_values(h)
            aligned_map = dict(zip(h["time"], h[key]))
            aligned = [aligned_map.get(t) for t in base_times]
            count = sum(numeric(v) for v in aligned)
            if count:
                hourly[key] = aligned
                record["hourly_units"][key] = unit
            coverage[key] = {
                "valid_hours": count,
                "status": ("ok" if count == len(base_times) else "partial")
                          if count else "unavailable",
            }
        except FAILURES as exc:
            coverage[key] = {"valid_hours": 0, "status": "unavailable",
                             "reason": str(exc)[:140]}
        print(f"{model} {key}: {coverage[key]['status']}", file=sys.stderr)
    record["optional_variable_coverage"] = coverage
    # Keep legacy field used by earlier reporting code.
    record["near_surface_variable_coverage"] = {
        k: v for k, v in coverage.items() if k in NEAR_SURFACE
    }
    if "sunshine_duration" in hourly:
        hourly["sunshine_duration_minutes"] = [
            v / 60 if numeric(v) else None for v in hourly["sunshine_duration"]
        ]
        record["hourly_units"]["sunshine_duration_minutes"] = "min"
    record["fog_parameter_note"] = (
        "Sunshine is modelled seconds per hour; cloud and weather codes "
        "are not fog probabilities. Snowfall (cm) is not snow depth (m)."
    )

def daily_totals(hourly, units):
    """Sum interval-ending hourly data into Berlin local days, including DST."""
    stamps = [parse_time(t) for t in hourly["time"]]
    dates = sorted({(t-timedelta(hours=1)).astimezone(BERLIN).date()
                    for t in stamps})
    result = []
    for day in dates:
        start = datetime.combine(day, datetime.min.time(), BERLIN).astimezone(UTC)
        end = datetime.combine(day+timedelta(days=1),
                               datetime.min.time(), BERLIN).astimezone(UTC)
        hours = int((end-start).total_seconds()/3600)
        expected = [start+timedelta(hours=i) for i in range(1, hours+1)]
        row = {"date": day.isoformat(), "expected_hours": hours}
        for key, unit, factor, name in [
            ("precipitation", "mm", 1, "precipitation_mm"),
            ("sunshine_duration", "s", 1/60, "sunshine_minutes"),
            ("snowfall", "cm", 1, "snowfall_cm"),
        ]:
            lookup = dict(zip(stamps, hourly.get(key, [])))
            unit_ok = units.get(key) in (("s", "seconds") if unit == "s" else (unit,))
            valid = [lookup.get(t) for t in expected if numeric(lookup.get(t))] if unit_ok else []
            row[name] = {
                "sum": round(sum(valid)*factor, 6) if len(valid) == hours else None,
                "available_sum": round(sum(valid)*factor, 6) if valid else None,
                "valid_hours": len(valid), "complete": len(valid) == hours,
                "unit_verified": unit_ok,
            }
        result.append(row)
    return result

def enrich(record, now):
    h = record["hourly"]
    validate_hourly(h)
    clean_values(h)
    record["valid_hours_by_variable"] = {
        k: sum(numeric(v) for v in vs) for k, vs in h.items() if k != "time"
    }
    valid = [parse_time(t) for t, v in zip(h["time"], h.get("temperature_2m", []))
             if numeric(v)]
    if not valid:
        raise ValueError("No valid temperature")
    record["coverage"] = {
        "first_valid_utc": min(valid).isoformat(),
        "last_valid_utc": max(valid).isoformat(),
        "valid_future_hours": sum(t >= now for t in valid),
        "missing_temperature_hours": len(h["time"])-len(valid),
    }
    record["run_age_hours"] = round(
        (now-parse_time(record["model_run_utc"])).total_seconds()/3600, 2
    )
    retrieved = record.get("data_retrieved_at_utc")
    record["data_age_hours"] = round(
        (now-parse_time(retrieved)).total_seconds()/3600, 2
    ) if retrieved else None
    record["daily_timezone"] = "Europe/Berlin"
    record["daily"] = daily_totals(h, record.get("hourly_units", {}))

def request_run(model, dt, days, *, verify_local_coverage=False,
                min_future_hours=0, now=None, with_optional=True):
    now = now or datetime.now(UTC)
    run = dt.strftime("%Y-%m-%dT%H:%M")
    url = api_url(model, dt, days, VARIABLES)
    obj = fetch_json(url)
    if obj.get("error") or not isinstance(obj.get("hourly"), dict):
        raise ValueError(obj.get("reason", "No hourly forecast"))
    hourly = obj["hourly"]
    validate_hourly(hourly)
    invalid_counts = clean_values(hourly)
    times = hourly["time"]
    if not any(numeric(v) for v in hourly.get("temperature_2m", [])):
        raise ValueError("No usable temperature")
    if times[-1] < run:
        raise ValueError("Forecast ends before run")
    valid_future = validate_coverage(obj, hourly, now, max(1, min_future_hours))
    record = {
        "status": "ok", "data_retrieved_at_utc": now.isoformat(),
        "grid_distance_km": round(grid_distance(obj.get("latitude"),
                                                obj.get("longitude")), 3),
        "model_run_utc": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_verification": "Single Runs API returned usable data for explicitly "
                            "requested run; original GRIB not independently checked",
        "api_model": model, "source_url": url,
        "grid_latitude": obj.get("latitude"), "grid_longitude": obj.get("longitude"),
        "grid_elevation_m": obj.get("elevation"),
        "hourly_units": obj.get("hourly_units", {}).copy(),
        "hourly": hourly, "invalid_values_replaced_by_null": invalid_counts,
    }
    if verify_local_coverage:
        record["local_coverage_verification"] = (
            f"Returned grid near Mering and {valid_future} future temperature hours"
        )
    if with_optional:
        add_optional_variables(record, model, dt, days)
    enrich(record, now)
    return record

def previous_record(previous, label, config, now):
    old = previous.get("models", {}).get(label)
    if not isinstance(old, dict) or old.get("api_model") != config["model"]:
        return None
    old = copy.deepcopy(old)
    old.pop("extended_forecast", None)
    try:
        distance = grid_distance(old.get("grid_latitude"), old.get("grid_longitude"))
        if distance > 35:
            return None
        old["grid_distance_km"] = round(distance, 3)
        old.setdefault("data_retrieved_at_utc", previous.get("retrieved_at_utc"))
        enrich(old, now)
        if (old["coverage"]["valid_future_hours"] <
                max(1, config.get("min_future_hours", 0)) or
                not 0 <= old["run_age_hours"] <= 72):
            return None
    except FAILURES:
        return None
    old["status"] = "stale"
    old["age_warning"] = "Previous dataset reused; not a fresh retrieval."
    return old

def select_model(label, config, now, previous):
    records, errors = [], []
    for dt in candidate_runs(now, config["cycle_hours"]):
        try:
            # Important: do not fetch 18 optional fields for every IFS
            # candidate run. Enrich only the selected latest run below.
            rec = request_run(config["model"], dt, config["days"],
                              verify_local_coverage=config.get("verify_local_coverage", False),
                              min_future_hours=config.get("min_future_hours", 1),
                              now=now, with_optional=False)
            records.append(rec)
            if label != "IFS":
                break
        except FAILURES as exc:
            errors.append(f"{dt.isoformat()}: {str(exc)[:180]}")
    cached = previous_record(previous, label, config, now)
    if not records:
        rec = cached or {"status": "unverified", "model_run_utc": None,
                         "reason": "No usable current or previous forecast"}
        rec["recent_errors"] = errors
        return rec
    latest = records[0]
    latest["selection_note"] = "Newest usable candidate; newer failures logged."
    latest["recent_errors"] = errors
    if label == "IFS":
        options = records + ([cached] if cached else [])
        best = max(options, key=lambda r: (
            parse_time(r["coverage"]["last_valid_utc"]),
            r["coverage"]["valid_future_hours"],
            parse_time(r["model_run_utc"]),
        ))
        latest["checked_runs"] = [
            {"model_run_utc": r["model_run_utc"], **r["coverage"]}
            for r in records
        ]
        if (parse_time(best["coverage"]["last_valid_utc"]) >
                parse_time(latest["coverage"]["last_valid_utc"])):
            latest["extended_forecast"] = copy.deepcopy(best)
            latest["extended_forecast"]["selection_note"] = (
                "Separate longer-reaching run, not the newest run."
            )
    # Optional errors never invalidate the verified core forecast.
    try:
        if not latest.get("source_url"):
            return latest  # In-memory test records have no API source.
        add_optional_variables(latest, config["model"],
                               parse_time(latest["model_run_utc"]), config["days"])
        enrich(latest, now)
    except FAILURES as exc:
        latest["optional_enrichment_error"] = str(exc)[:180]
        print(f"{label}: optional enrichment failed: {exc}", file=sys.stderr)
    return latest

def main():
    now = datetime.now(UTC)
    try:
        previous = json.loads(OUTPUT.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    result = {
        "schema_version": 5,
        "location": {"name": "Mering", "postcode": "86415",
                     "latitude": LAT, "longitude": LON, "reference_elevation_m": 510},
        "retrieved_at_utc": now.isoformat(),
        "source_type": "Open-Meteo explicit single runs; GRIB not independently verified",
        "models": {},
    }
    for label, config in MODELS.items():
        result["models"][label] = select_model(label, config, now, previous)
        rec = result["models"][label]
        print(f"{label}: {rec['status']} run={rec.get('model_run_utc')} "
              f"age={rec.get('run_age_hours')} h")
    result["collection_status"] = (
        "ok" if all(r["status"] == "ok" for r in result["models"].values())
        else "degraded"
    )
    # Do not overwrite a good dataset if no model is freshly verified.
    if not any(r["status"] == "ok" for r in result["models"].values()):
        raise SystemExit("No fresh models; previous JSON preserved")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix(".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                               allow_nan=False)+"\n", encoding="utf-8")
    temp.replace(OUTPUT)

if __name__ == "__main__":
    main()
