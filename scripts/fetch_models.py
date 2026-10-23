#!/usr/bin/env python3
"""Fetch explicit Open-Meteo single-run forecasts for Mering.

AROME means Météo-France AROME France, not ARPEGE or a blended model.
Run selection is API-verified, but never presented as original-GRIB verification.
"""
import copy
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
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
    "sunshine_duration",
    "weather_code",
    "cloud_cover",
)
MODELS = {
    "ICON-D2": {"model": "icon_d2", "cycle_hours": list(range(0, 24, 3)), "days": 3},
    "ICON-EU": {
        "model": "icon_eu",  # DWD ICON Europe, ~7 km, up to +120 h
        "cycle_hours": [0, 6, 12, 18],
        # Seven calendar days is the API query envelope, NOT seven days
        # of ICON-EU forecast. The actual model horizon is up to 120 h.
        "days": 7,
        "verify_local_coverage": True,
        "min_future_hours": 6,
    },
    "AROME": {
        "model": "meteofrance_arome_france",  # Météo-France, 2.5 km
        "cycle_hours": list(range(0, 24, 3)),
        "days": 3,
        "verify_local_coverage": True,
        "min_future_hours": 3,
    },
    "GFS": {"model": "gfs_seamless", "cycle_hours": [0, 6, 12, 18], "days": 16},
    "IFS": {"model": "ecmwf_ifs025", "cycle_hours": [0, 6, 12, 18], "days": 16},
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
    with urlopen(Request(url, headers={"User-Agent": "wetter-mering/3.0"}), timeout=20) as res:
        return json.load(res)


def api_url(model, run, days, variables):
    query = urlencode({
        "latitude": LAT, "longitude": LON, "models": model,
        "run": run.strftime("%Y-%m-%dT%H:%M"),
        "hourly": variables, "timezone": "GMT", "forecast_days": days,
        "precipitation_unit": "mm", "wind_speed_unit": "kmh",
    })
    return API + "?" + query


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_coverage(obj, hourly, now, min_future_hours):
    """Reject local-model responses without nearby grid metadata or future data."""
    grid_lat, grid_lon = obj.get("latitude"), obj.get("longitude")
    if not numeric(grid_lat) or not numeric(grid_lon):
        raise ValueError("Regional model: grid coordinates missing; coverage not verified")
    # Coordinate distances are a guard against extrapolation to distant grids,
    # not an assertion of the exact French model domain boundary.
    if grid_distance(grid_lat, grid_lon) > 35:
        raise ValueError("Regional model: returned grid point is too far from Mering")
    valid_future = 0
    for stamp, value in zip(hourly["time"], hourly.get("temperature_2m", [])):
        try:
            dt = parse_time(stamp)
        except (TypeError, ValueError):
            continue
        if dt >= now and numeric(value):
            valid_future += 1
    if valid_future < min_future_hours:
        raise ValueError(f"Regional model: only {valid_future} valid future temperature hours")
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
            validate_hourly(extra_hourly)
            if grid_distance(extra.get("latitude"), extra.get("longitude")) > 35:
                raise ValueError("Optional field grid too distant")
            if (extra.get("latitude"), extra.get("longitude")) != (record["grid_latitude"], record["grid_longitude"]):
                raise ValueError("Optional field grid differs from base forecast")
            if key == "sunshine_duration" and extra.get("hourly_units", {}).get(key) not in ("s", "seconds"):
                raise ValueError("Unexpected sunshine unit; expected seconds")
            clean_values(extra_hourly)
            values = extra_hourly[key]
            mapping = dict(zip(stamps, values))
            aligned = [mapping.get(stamp) for stamp in base_times]
            count = sum(numeric(v) for v in aligned)
            if count:
                hourly[key] = aligned
                record["hourly_units"][key] = extra.get("hourly_units", {}).get(key)
            availability[key] = {"valid_hours": count, "status": ("ok" if count == len(base_times) else "partial") if count else "unavailable", "source_url": url}
        except (HTTPError, URLError, ValueError, TimeoutError, OSError, TypeError, KeyError) as exc:
            availability[key] = {"valid_hours": 0, "status": "unavailable", "reason": str(exc)[:140]}
    record["near_surface_variable_coverage"] = availability
    for key, state in availability.items():
        print(f"{model} {key}: {state}", file=sys.stderr)
    if "sunshine_duration" in hourly:
        hourly["sunshine_duration_minutes"] = [v / 60 if numeric(v) else None for v in hourly["sunshine_duration"]]
        record["hourly_units"]["sunshine_duration_minutes"] = "min"
    record["fog_parameter_note"] = "Sunshine minutes = API seconds / 60 for preceding hour; model estimate, not minute-resolved prediction. Low cloud and weather codes are not fog probabilities."


def request_run(model, dt, days, *, verify_local_coverage=False, min_future_hours=0, now=None):
    run = dt.strftime("%Y-%m-%dT%H:%M")
    url = api_url(model, dt, days, VARIABLES)
    obj = fetch_json(url)
    if obj.get("error") or not isinstance(obj.get("hourly"), dict):
        raise ValueError(obj.get("reason", "No hourly forecast"))
    hourly = obj["hourly"]
    validate_hourly(hourly)
    invalid_counts = clean_values(hourly)
    times = hourly.get("time", [])
    if not times or not any(numeric(v) for v in hourly.get("temperature_2m", [])):
        raise ValueError("No usable temperature forecast values")
    if times[-1] < run:
        raise ValueError("Forecast ends before selected run")
    valid_future = validate_coverage(obj, hourly, now or datetime.now(timezone.utc), max(1, min_future_hours))
    if verify_local_coverage:
        valid_future = validate_coverage(
            obj, hourly, now or datetime.now(timezone.utc), min_future_hours
        )
    record = {
        "status": "ok",
        "data_retrieved_at_utc": (now or datetime.now(timezone.utc)).isoformat(),
        "grid_distance_km": round(grid_distance(obj.get("latitude"), obj.get("longitude")), 3),
        "model_run_utc": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_verification": "Single Runs API returned usable data for explicitly requested run; original GRIB not independently checked",
        "api_model": model,
        "source_url": url,
        "grid_latitude": obj.get("latitude"),
        "grid_longitude": obj.get("longitude"),
        "grid_elevation_m": obj.get("elevation"),
        "hourly_units": obj.get("hourly_units", {}).copy(),
        "hourly": hourly,
        "invalid_values_replaced_by_null": invalid_counts,
    }
    if verify_local_coverage:
        record["local_coverage_verification"] = (
            f"Returned grid location near Mering and {valid_future} valid future hourly temperatures"
        )
    # Only local high-resolution models need the extra fog/cloud parameters.
    if model in ("icon_d2", "icon_eu", "meteofrance_arome_france"):
        add_optional_variables(record, model, dt, days)
    record["valid_hours_by_variable"] = {
        key: sum(numeric(v) for v in values)
        for key, values in record["hourly"].items()
        if key != "time" and isinstance(values, list)
    }
    enrich(record, now or datetime.now(timezone.utc))
    return record


UTC = timezone.utc
BERLIN = ZoneInfo('Europe/Berlin')
FAILURES = (HTTPError, URLError, ValueError, TimeoutError, OSError, TypeError, KeyError)


def parse_time(stamp):
    dt = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC))


def grid_distance(lat, lon):
    if not numeric(lat) or not numeric(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('Missing or invalid grid coordinates')
    a, b = math.radians(LAT), math.radians(lat)
    h = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(math.radians(lon-LON)/2)**2
    return 6371 * 2 * math.asin(min(1, math.sqrt(h)))


def validate_hourly(hourly):
    times = hourly.get('time', [])
    if not times or not isinstance(times, list):
        raise ValueError('Missing time axis')
    dates = [parse_time(t) for t in times]
    if any(t.minute or t.second or t.microsecond for t in dates):
        raise ValueError('Non-hourly timestamps')
    if any(b-a != timedelta(hours=1) for a, b in zip(dates, dates[1:])):
        raise ValueError('Time axis is duplicated, unordered or not hourly')
    for key, values in hourly.items():
        if not isinstance(values, list) or len(values) != len(times):
            raise ValueError(f'Invalid array length: {key}')


def clean_values(hourly):
    bounds = {'temperature_2m': (-100, 65), 'precipitation': (0, 1000),
              'wind_speed_10m': (0, 500), 'wind_gusts_10m': (0, 500),
              'pressure_msl': (800, 1100), 'cloud_cover': (0, 100),
              'cloud_cover_low': (0, 100), 'relative_humidity_2m': (0, 100),
              'dew_point_2m': (-110, 65), 'visibility': (0, 1000000),
              'sunshine_duration': (0, 3600), 'sunshine_duration_minutes': (0, 60)}
    codes = {0,1,2,3,45,48,51,53,55,56,57,61,63,65,66,67,71,73,75,77,80,81,82,85,86,95,96,99}
    invalid_counts = {}
    for key, values in hourly.items():
        if key == 'time':
            continue
        low, high = bounds.get(key, (-math.inf, math.inf))
        original = values
        hourly[key] = [v if numeric(v) and low <= v <= high and (key != 'weather_code' or v in codes) else None for v in values]
        invalid_counts[key] = sum(v is not None and cleaned is None for v, cleaned in zip(original, hourly[key]))
    return invalid_counts


def daily_totals(hourly, units):
    """API sums describe (timestamp - 1 hour, timestamp], not cumulative runs.

    Bucket by interval start in Berlin. Iterate expected UTC interval endpoints
    between local midnights, preserving both autumn 02:00 hours.
    """
    stamps = [parse_time(t) for t in hourly['time']]
    dates = sorted({(t-timedelta(hours=1)).astimezone(BERLIN).date() for t in stamps})
    result = []
    for day in dates:
        start = datetime.combine(day, datetime.min.time(), BERLIN).astimezone(UTC)
        end = datetime.combine(day+timedelta(days=1), datetime.min.time(), BERLIN).astimezone(UTC)
        hours = int((end-start).total_seconds()/3600)
        expected = [start+timedelta(hours=i) for i in range(1, hours+1)]
        row = {'date': day.isoformat(), 'expected_hours': hours}
        for key, unit, factor in [('precipitation','mm',1), ('sunshine_duration','s',1/60)]:
            lookup = dict(zip(stamps, hourly.get(key, [])))
            unit_ok = units.get(key) in ((unit, 'seconds') if unit == 's' else (unit,))
            valid = [lookup.get(t) for t in expected if numeric(lookup.get(t))] if unit_ok else []
            complete = len(valid) == hours
            name = 'precipitation_mm' if key == 'precipitation' else 'sunshine_minutes'
            row[name] = {'sum': round(sum(valid)*factor, 6) if complete else None,
                         'available_sum': round(sum(valid)*factor, 6) if valid else None,
                         'valid_hours': len(valid), 'complete': complete,
                         'unit_verified': unit_ok}
        result.append(row)
    return result


def enrich(record, now):
    hourly = record['hourly']
    validate_hourly(hourly)
    clean_values(hourly)
    record['valid_hours_by_variable'] = {k: sum(numeric(v) for v in vs) for k, vs in hourly.items() if k != 'time'}
    valid = [parse_time(t) for t,v in zip(hourly['time'], hourly.get('temperature_2m', [])) if numeric(v)]
    if not valid:
        raise ValueError('No valid temperature')
    record['coverage'] = {'first_valid_utc': min(valid).isoformat(), 'last_valid_utc': max(valid).isoformat(),
                          'valid_future_hours': sum(t >= now for t in valid),
                          'missing_temperature_hours': len(hourly['time'])-len(valid)}
    record['run_age_hours'] = round((now-parse_time(record['model_run_utc'])).total_seconds()/3600, 2)
    retrieved = record.get('data_retrieved_at_utc')
    record['data_age_hours'] = round((now-parse_time(retrieved)).total_seconds()/3600, 2) if retrieved else None
    record['daily_timezone'] = 'Europe/Berlin'
    record['daily'] = daily_totals(hourly, record.get('hourly_units', {}))


def previous_record(previous, label, config, now):
    old = previous.get('models', {}).get(label)
    if not isinstance(old, dict) or old.get('api_model') != config['model']:
        return None
    old = copy.deepcopy(old)
    old.pop('extended_forecast', None)
    try:
        distance = grid_distance(old.get('grid_latitude'), old.get('grid_longitude'))
        if distance > 35:
            return None
        old['grid_distance_km'] = round(distance, 3)
        # Never reset the original retrieval time after repeated fallbacks.
        old.setdefault('data_retrieved_at_utc', previous.get('retrieved_at_utc'))
        enrich(old, now)
        if old['coverage']['valid_future_hours'] < max(1, config.get('min_future_hours', 0)) or old['run_age_hours'] > 72 or old['run_age_hours'] < 0:
            return None
    except FAILURES:
        return None
    old['status'] = 'stale'
    old['age_warning'] = 'Previous dataset reused; check run_age_hours and data_age_hours. Not a fresh retrieval.'
    return old


def select_model(label, config, now, previous):
    records, errors = [], []
    for dt in candidate_runs(now, config['cycle_hours']):
        try:
            record = request_run(config['model'], dt, config['days'],
                                 min_future_hours=config.get('min_future_hours', 1), now=now)
            records.append(record)
            if label != 'IFS':
                break
        except FAILURES as exc:
            errors.append(f'{dt.isoformat()}: {str(exc)[:180]}')
    cached = previous_record(previous, label, config, now)
    if not records:
        record = cached or {'status':'unverified', 'model_run_utc':None, 'reason':'No usable current or previous forecast'}
        record['recent_errors'] = errors
        return record
    # Candidates are descending in initialization time; never call an older run latest.
    latest = records[0]
    latest['selection_note'] = 'Newest usable candidate among checked runs; newer unavailable candidates are logged.'
    latest['recent_errors'] = errors
    if label == 'IFS':
        options = records + ([cached] if cached else [])
        best = max(options, key=lambda r: (parse_time(r['coverage']['last_valid_utc']), r['coverage']['valid_future_hours'], parse_time(r['model_run_utc'])))
        latest['checked_runs'] = [{'model_run_utc':r['model_run_utc'], **r['coverage']} for r in records]
        if parse_time(best['coverage']['last_valid_utc']) > parse_time(latest['coverage']['last_valid_utc']):
            latest['extended_forecast'] = copy.deepcopy(best)
            latest['extended_forecast']['selection_note'] = 'Separate longer-reaching run; not the newest run. Do not splice without preserving provenance.'
    return latest


def main():
    now = datetime.now(UTC)
    try:
        previous = json.loads(OUTPUT.read_text(encoding='utf-8'))
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    result = {'schema_version': 4,
              'location': {'name':'Mering', 'postcode':'86415', 'latitude':LAT, 'longitude':LON, 'reference_elevation_m':510},
              'retrieved_at_utc':now.isoformat(),
              'source_type':'Open-Meteo explicit single runs; original GRIB not independently verified',
              'models': {}}
    for label, config in MODELS.items():
        result['models'][label] = select_model(label, config, now, previous)
        rec = result['models'][label]
        print(f"{label}: {rec['status']} run={rec.get('model_run_utc')} age={rec.get('run_age_hours')} h")
    result['collection_status'] = 'ok' if all(r['status']=='ok' for r in result['models'].values()) else 'degraded'
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix('.tmp')
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    temp.replace(OUTPUT)
    if not any(r['status']=='ok' for r in result['models'].values()):
        raise SystemExit('No freshly verified models; saved explicit stale/unavailable status')


if __name__ == '__main__':
    main()
