#!/usr/bin/env python3
"""Mering model forecasts from explicitly selected, verified UTC model runs."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LAT, LON = 48.2656, 10.9846
VARIABLES = "temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,pressure_msl"
MODELS = {
    "ICON-D2": {"model": "icon_d2", "cycle_hours": list(range(0, 24, 3)), "days": 3},
    "GFS": {"model": "gfs_seamless", "cycle_hours": [0, 6, 12, 18], "days": 16},
    "IFS": {"model": "ecmwf_ifs025", "cycle_hours": [0, 6, 12, 18], "days": 15},
}
API = "https://single-runs-api.open-meteo.com/v1/forecast"
OUTPUT = Path("data/mering_models.json")

def candidate_runs(now, cycle_hours, limit=12):
    hour = now.replace(minute=0, second=0, microsecond=0)
    candidates = []
    for offset in range(0, 84):
        dt = hour - timedelta(hours=offset)
        if dt.hour in cycle_hours:
            candidates.append(dt)
            if len(candidates) == limit:
                break
    return candidates

def request_run(model, dt, days):
    run = dt.strftime("%Y-%m-%dT%H:%M")
    query = urlencode({
        "latitude": LAT, "longitude": LON, "models": model,
        "run": run, "hourly": VARIABLES, "timezone": "GMT",
        "forecast_days": days,
    })
    url = API + "?" + query
    with urlopen(Request(url, headers={"User-Agent": "wetter-mering/2.0"}), timeout=60) as res:
        obj = json.load(res)
    if obj.get("error") or not isinstance(obj.get("hourly"), dict):
        raise ValueError(obj.get("reason", "No hourly forecast"))
    hourly = obj["hourly"]
    times = hourly.get("time", [])
    if not times or not any(isinstance(v, (int, float)) for v in hourly.get("temperature_2m", [])):
        raise ValueError("No usable forecast values")
    # Avoid false verification if API returns a different model's or run's data.
    if times[-1] < run:
        raise ValueError("Returned forecast ends before selected run")
    return {
        "status": "ok",
        "model_run_utc": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_verification": "Single Runs API returned usable forecast for explicitly requested run",
        "api_model": model,
        "source_url": url,
        "grid_latitude": obj.get("latitude"),
        "grid_longitude": obj.get("longitude"),
        "grid_elevation_m": obj.get("elevation"),
        "hourly_units": obj.get("hourly_units"),
        "hourly": hourly,
    }

def main():
    now = datetime.now(timezone.utc)
    result = {
        "location": {"name": "Mering", "postcode": "86415",
                     "latitude": LAT, "longitude": LON, "reference_elevation_m": 510},
        "retrieved_at_utc": now.isoformat(),
        "source_type": "Open-Meteo archived single-run point forecasts, not original GRIB",
        "models": {},
    }
    for label, config in MODELS.items():
        errors = []
        for dt in candidate_runs(now, config["cycle_hours"]):
            try:
                record = request_run(config["model"], dt, config["days"])
                result["models"][label] = record
                print(f"{label}: verified {record['model_run_utc']}")
                break
            except (HTTPError, URLError, ValueError, TimeoutError) as exc:
                errors.append(f"{dt:%Y-%m-%dT%H:%MZ}: {str(exc)[:160]}")
        else:
            result["models"][label] = {
                "status": "unverified",
                "model_run_utc": None,
                "reason": "No candidate single run returned usable data",
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
