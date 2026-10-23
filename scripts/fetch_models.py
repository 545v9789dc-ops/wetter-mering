#!/usr/bin/env python3
"""Fetch explicit Open-Meteo single-run forecasts for Mering.

AROME means Météo-France AROME France, not ARPEGE or a blended model.
Run selection is API-verified, but never presented as original-GRIB verification.
"""
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LAT, LON = 48.2656, 10.9846
VARIABLES = "temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,pressure_msl"
# Optional parameters are fetched separately: a model that lacks e.g. visibility
# should not lose the otherwise usable temperature/precipitation forecast.
NEAR_SURFACE = (
    "cloud_cover_low",
    "relative_humidity_2m",
    "dew_point_2m",
    "visibility",
)
MODELS = {
    "ICON-D2": {"model": "icon_d2", "cycle_hours": list(range(0, 24, 3)), "days": 3},
    "AROME": {
        "model": "meteofrance_arome_france",  # Météo-France, 2.5 km
        "cycle_hours": list(range(0, 24, 3)),
        "days": 3,
        "verify_local_coverage": True,
        "min_future_hours": 3,
    },
    "GFS": {"model": "gfs_seamless", "cycle_hours": [0, 6, 12, 18], "days": 16},
    "IFS": {"model": "ecmwf_ifs025", "cycle_hours": [0, 6, 12, 18], "days": 15},
}
API = "https://single-runs-api.open-meteo.com/v1/forecast"
OUTPUT = Path("data/mering_models.json")


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
    with urlopen(Request(url, headers={"User-Agent": "wetter-mering/3.0"}), timeout=60) as res:
        return json.load(res)


def api_url(model, run, days, variables):
    query = urlencode({
        "latitude": LAT, "longitude": LON, "models": model,
        "run": run.strftime("%Y-%m-%dT%H:%M"),
        "hourly": variables, "timezone": "GMT", "forecast_days": days,
    })
    return API + "?" + query


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_coverage(obj, hourly, now, min_future_hours):
    """Reject AROME responses without grid metadata near Mering or future data."""
    grid_lat, grid_lon = obj.get("latitude"), obj.get("longitude")
    if not numeric(grid_lat) or not numeric(grid_lon):
        raise ValueError("AROME: grid coordinates missing; coverage not verified")
    # Coordinate distances are a guard against extrapolation to distant grids,
    # not an assertion of the exact French model domain boundary.
    if abs(grid_lat - LAT) > 0.35 or abs(grid_lon - LON) > 0.35:
        raise ValueError("AROME: returned grid point is too far from Mering")
    valid_future = 0
    for stamp, value in zip(hourly["time"], hourly.get("temperature_2m", [])):
        try:
            dt = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if dt >= now and numeric(value):
            valid_future += 1
    if valid_future < min_future_hours:
        raise ValueError(f"AROME: only {valid_future} valid future temperature hours")
    return valid_future


def add_optional_variables(record, model, run, days):
    hourly = record["hourly"]
    base_times = hourly.get("time", [])
    availability = {}
    for key in NEAR_SURFACE:
        url = api_url(model, run, days, key)
        try:
            extra = fetch_json(url)
            extra_hourly = extra.get("hourly", {})
            if extra.get("error") or not isinstance(extra_hourly, dict):
                raise ValueError(extra.get("reason", "Unsupported optional field"))
            values = extra_hourly.get(key, [])
            stamps = extra_hourly.get("time", [])
            if not stamps or len(values) != len(stamps):
                raise ValueError("Missing or inconsistent optional hourly values")
            mapping = dict(zip(stamps, values))
            aligned = [mapping.get(stamp) for stamp in base_times]
            count = sum(numeric(v) for v in aligned)
            if count:
                hourly[key] = aligned
                record["hourly_units"][key] = extra.get("hourly_units", {}).get(key)
            availability[key] = {"valid_hours": count, "status": "ok" if count else "unavailable"}
        except (HTTPError, URLError, ValueError, TimeoutError, OSError) as exc:
            availability[key] = {"valid_hours": 0, "status": "unavailable", "reason": str(exc)[:140]}
    record["near_surface_variable_coverage"] = availability


def request_run(model, dt, days, *, verify_local_coverage=False, min_future_hours=0, now=None):
    run = dt.strftime("%Y-%m-%dT%H:%M")
    url = api_url(model, dt, days, VARIABLES)
    obj = fetch_json(url)
    if obj.get("error") or not isinstance(obj.get("hourly"), dict):
        raise ValueError(obj.get("reason", "No hourly forecast"))
    hourly = obj["hourly"]
    times = hourly.get("time", [])
    if not times or not any(numeric(v) for v in hourly.get("temperature_2m", [])):
        raise ValueError("No usable temperature forecast values")
    if times[-1] < run:
        raise ValueError("Forecast ends before selected run")
    valid_future = None
    if verify_local_coverage:
        valid_future = validate_coverage(
            obj, hourly, now or datetime.now(timezone.utc), min_future_hours
        )
    record = {
        "status": "ok",
        "model_run_utc": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_verification": "Single Runs API returned usable data for explicitly requested run; original GRIB not independently checked",
        "api_model": model,
        "source_url": url,
        "grid_latitude": obj.get("latitude"),
        "grid_longitude": obj.get("longitude"),
        "grid_elevation_m": obj.get("elevation"),
        "hourly_units": obj.get("hourly_units", {}).copy(),
        "hourly": hourly,
    }
    if verify_local_coverage:
        record["local_coverage_verification"] = (
            f"Returned grid location near Mering and {valid_future} valid future hourly temperatures"
        )
    # Only local high-resolution models need the extra fog/cloud parameters.
    if model in ("icon_d2", "meteofrance_arome_france"):
        add_optional_variables(record, model, dt, days)
    record["valid_hours_by_variable"] = {
        key: sum(numeric(v) for v in values)
        for key, values in record["hourly"].items()
        if key != "time" and isinstance(values, list)
    }
    return record


def main():
    now = datetime.now(timezone.utc)
    result = {
        "location": {"name": "Mering", "postcode": "86415", "latitude": LAT,
                     "longitude": LON, "reference_elevation_m": 510},
        "retrieved_at_utc": now.isoformat(),
        "source_type": "Open-Meteo archived explicit single-run point forecasts, not original GRIB",
        "models": {},
    }
    for label, config in MODELS.items():
        errors = []
        for dt in candidate_runs(now, config["cycle_hours"]):
            try:
                record = request_run(
                    config["model"], dt, config["days"],
                    verify_local_coverage=config.get("verify_local_coverage", False),
                    min_future_hours=config.get("min_future_hours", 0),
                    now=now,
                )
                result["models"][label] = record
                print(f"{label}: {record['model_run_utc']} "
                      f"({record['valid_hours_by_variable']['temperature_2m']} temperature hours)")
                break
            except (HTTPError, URLError, ValueError, TimeoutError, OSError) as exc:
                errors.append(f"{dt:%Y-%m-%dT%H:%MZ}: {str(exc)[:160]}")
        else:
            result["models"][label] = {
                "status": "unverified",
                "model_run_utc": None,
                "reason": "No candidate single run returned usable local data",
                "recent_errors": errors[:4],
            }
            print(f"{label}: no verified run", file=sys.stderr)

    if not any(v["status"] == "ok" for v in result["models"].values()):
        raise SystemExit("No verified model runs; preserving previous JSON")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {OUTPUT}")


if __name__ == "__main__":
    main()
