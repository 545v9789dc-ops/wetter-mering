#!/usr/bin/env python3
"""Collect model-specific hourly forecasts for Mering from Open-Meteo."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOCATION = {"name": "Mering", "postcode": "86415",
            "latitude": 48.2656, "longitude": 10.9846,
            "reference_elevation_m": 510}
MODELS = {
    "ICON-D2": {"api_model": "icon_d2", "forecast_days": 3},
    "GFS": {"api_model": "gfs_seamless", "forecast_days": 16},
    "IFS": {"api_model": "ecmwf_ifs025", "forecast_days": 15},
}
VARIABLES = "temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,pressure_msl"

def fetch(config):
    params = {
        "latitude": LOCATION["latitude"],
        "longitude": LOCATION["longitude"],
        "models": config["api_model"],
        "hourly": VARIABLES,
        "timezone": "GMT",
        "forecast_days": config["forecast_days"],
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urlencode(params)
    request = Request(url, headers={"User-Agent": "wetter-mering/1.0"})
    with urlopen(request, timeout=60) as response:
        payload = json.load(response)
    if payload.get("error") or not isinstance(payload.get("hourly"), dict):
        raise ValueError(payload.get("reason", "Missing hourly data"))
    if not payload["hourly"].get("time"):
        raise ValueError("No forecast timestamps")
    return {
        "status": "ok",
        "source_url": url,
        "api_model": config["api_model"],
        "grid_latitude": payload.get("latitude"),
        "grid_longitude": payload.get("longitude"),
        "grid_elevation_m": payload.get("elevation"),
        "hourly_units": payload.get("hourly_units"),
        "hourly": payload["hourly"],
    }

def main():
    output = {
        "location": LOCATION,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_type": "Open-Meteo interpolated model-specific forecasts, not original GRIB grid values",
        "model_run_utc": None,
        "model_run_note": "Actual initialization times are not established by this API response.",
        "models": {},
    }
    for label, config in MODELS.items():
        try:
            output["models"][label] = fetch(config)
            print(f"{label}: OK")
        except Exception as exc:
            output["models"][label] = {"status": "error", "reason": str(exc)}
            print(f"{label}: ERROR: {exc}", file=sys.stderr)

    if not any(item["status"] == "ok" for item in output["models"].values()):
        raise SystemExit("No model available; existing published file remains unchanged")

    target = Path("data/mering_models.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Saved {target}")

if __name__ == "__main__":
    main()
