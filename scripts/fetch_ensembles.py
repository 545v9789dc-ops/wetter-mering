#!/usr/bin/env python3
"""Open-Meteo GFS/IFS ensemble collector for Mering."""
import json, math, re, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

MODELS = {"GFS-ENS": "ncep_gefs025", "IFS-ENS": "ecmwf_ifs025_ensemble"}
BASE_FIELDS = ("temperature_2m", "precipitation", "snowfall")
OPTIONAL_FIELDS = ("wind_gusts_10m", "snow_depth")
FIELDS = BASE_FIELDS + OPTIONAL_FIELDS
UNITS = {"wind_gusts_10m": "km/h", "snow_depth": "m"}
TZ = ZoneInfo("Europe/Berlin")
OUT = Path("data/mering_ensembles.json")

def fetch_fields(model, fields):
    query = urlencode(dict(latitude=48.2656, longitude=10.9846, models=model,
                           hourly=",".join(fields), timezone="GMT",
                           forecast_days=15, precipitation_unit="mm",
                           wind_speed_unit="kmh", temperature_unit="celsius"))
    url = "https://ensemble-api.open-meteo.com/v1/ensemble?" + query
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent":"wetter-mering/4.1"}), timeout=25) as r:
                return json.load(r)
        except HTTPError as e:
            if e.code not in (429,500,502,503,504) or attempt==2: raise
        except (URLError, TimeoutError, OSError):
            if attempt==2: raise
        time.sleep(2**attempt)

def fetch(model):
    """Optional field failures must not discard the existing event probabilities."""
    obj = fetch_fields(model, BASE_FIELDS)
    obj["optional_field_errors"] = {}
    for field in OPTIONAL_FIELDS:
        try:
            extra = fetch_fields(model, (field,))
            if extra["hourly"]["time"] != obj["hourly"]["time"]:
                raise ValueError("Hourly time axis differs from base response")
            for key, values in extra["hourly"].items():
                if key == field or re.fullmatch(re.escape(field) + r"_member\d+", key):
                    obj["hourly"][key] = values
                    if key in extra.get("hourly_units", {}):
                        obj.setdefault("hourly_units", {})[key] = extra["hourly_units"][key]
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as e:
            obj["optional_field_errors"][field] = str(e)[:250]
    return obj


def valid(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def distribution(values):
    """Linear empirical quantiles; null statistics for an empty sample."""
    values = sorted(values)
    def quantile(q):
        if not values:
            return None
        index = (len(values) - 1) * q
        lo, hi = math.floor(index), math.ceil(index)
        return round(values[lo] + (values[hi] - values[lo]) * (index - lo), 3)
    return {"available_members": len(values),
            "mean": round(sum(values) / len(values), 3) if values else None,
            "min": values[0] if values else None, "max": values[-1] if values else None,
            "p10": quantile(.1), "p25": quantile(.25), "p50": quantile(.5),
            "p75": quantile(.75), "p90": quantile(.9)}


def summarize(obj):
    h = obj["hourly"]
    stamps = []
    for raw in h["time"]:
        stamp = datetime.fromisoformat(raw)
        stamps.append((stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None
                       else stamp.astimezone(timezone.utc)))
    if not stamps or any(b - a != timedelta(hours=1) for a, b in zip(stamps, stamps[1:])):
        raise ValueError("Expected a nonempty, unique, consecutive hourly UTC time axis")
    times = [t.astimezone(TZ).date().isoformat() for t in stamps]
    members, control, invalid_arrays, unit_errors = {}, {}, [], []
    units = obj.get("hourly_units", {})
    for key, values in h.items():
        for field in FIELDS:
            match = re.fullmatch(re.escape(field) + r"_member(\d+)", key)
            if key != field and match is None:
                continue
            member = int(match.group(1)) if match else 0
            target = control if member == 0 else members.setdefault(member, {})
            if not isinstance(values, list) or len(values) != len(times):
                invalid_arrays.append(key)
                target[field] = None
            elif field in UNITS and units.get(key) != UNITS[field]:
                unit_errors.append({"key": key, "received": units.get(key), "expected": UNITS[field]})
                target[field] = None
            else:
                target[field] = values
    if not members:
        raise ValueError("No ensemble member arrays found")

    def value(member, field, i):
        array = member.get(field)
        v = array[i] if array is not None else None
        # Negative depths/gusts are invalid, not zero.
        return v if valid(v) and (field not in OPTIONAL_FIELDS or v >= 0) else None

    def quality(field, indices):
        counts = [sum(value(m, field, i) is not None for i in indices)
                  for m in members.values() if m.get(field) is not None]
        n = len(indices)
        return {"complete_members": sum(c == n for c in counts),
                "partial_members": sum(0 < c < n for c in counts),
                "empty_members": sum(c == 0 for c in counts),
                "missing_field_members": len(members) - len(counts),
                "minimum_valid_hours": min(counts) if counts else None,
                "maximum_valid_hours": max(counts) if counts else None,
                "valid_value_fraction": round(sum(counts) / (n * len(members)), 3)}

    daily = []
    for day in sorted(set(times)):
        indices = [i for i, d in enumerate(times) if d == day]
        midnight = datetime.fromisoformat(day).replace(tzinfo=TZ)
        next_midnight = midnight + timedelta(days=1)
        calendar_hours = int((next_midnight.astimezone(timezone.utc) -
                              midnight.astimezone(timezone.utc)).total_seconds() / 3600)
        row = {"date": day, "probabilities": {}, "available_members": {},
               "data_quality": {"expected_hours": len(indices),
                                "calendar_day_hours": calendar_hours,
                                "full_calendar_day": len(indices) == calendar_hours,
                                "fields": {f: quality(f, indices) for f in FIELDS}}}
        for label, field, predicate in (
            ("frost_any_hour", "temperature_2m", lambda v: min(v) < 0),
            ("precipitation_ge_1mm", "precipitation", lambda v: sum(v) >= 1),
            ("snowfall_positive", "snowfall", lambda v: sum(v) > 0)):
            outcomes = []
            for m in members.values():
                segment = [value(m, field, i) for i in indices]
                if all(v is not None for v in segment):
                    outcomes.append(predicate(segment))
            row["available_members"][label] = len(outcomes)
            row["probabilities"][label] = round(sum(outcomes) / len(outcomes), 3) if outcomes else None
        # Phase 1: daily temperature extremes and precipitation distributions.
        daily_extremes = {}
        for field, operations in (('temperature_2m', ('min', 'max')),
                                  ('precipitation', ('sum',))):
            complete = []
            for member in members.values():
                segment = [value(member, field, i) for i in indices]
                if all(v is not None for v in segment):
                    complete.append(segment)
            for operation in operations:
                values_for_day = [min(v) if operation == 'min' else
                                  max(v) if operation == 'max' else sum(v)
                                  for v in complete]
                daily_extremes[field + '_' + operation] = {
                    'unit': '°C' if field == 'temperature_2m' else 'mm',
                    **distribution(values_for_day),
                    'missing_members': len(members) - len(values_for_day)}
        row['daily_distributions'] = daily_extremes
        precip = []
        for member in members.values():
            segment = [value(member, 'precipitation', i) for i in indices]
            if all(v is not None for v in segment):
                precip.append(sum(segment))
        row['precipitation_threshold_probabilities'] = {
            str(threshold): round(sum(v >= threshold for v in precip) / len(precip), 3)
            if precip else None for threshold in (0.1, 1, 5, 10, 20)}
        row['precipitation_threshold_available_members'] = len(precip)
        for field in OPTIONAL_FIELDS:
            maxima = []
            for m in members.values():
                segment = [value(m, field, i) for i in indices]
                if all(v is not None for v in segment):
                    maxima.append(max(segment))
            row[field + "_max"] = {"unit": UNITS[field], **distribution(maxima)}
        daily.append(row)
    hourly_depth = []
    for i, stamp in enumerate(stamps):
        values = [v for m in members.values() if (v := value(m, "snow_depth", i)) is not None]
        hourly_depth.append({"time": stamp.isoformat(), "unit": "m", **distribution(values),
                             "missing_members": len(members) - len(values),
                             "member_coverage": round(len(values) / len(members), 3)})
    availability = {}
    for field in OPTIONAL_FIELDS:
        q = quality(field, list(range(len(times))))
        total = sum(value(m, field, i) is not None for m in members.values() for i in range(len(times)))
        availability[field] = {"unit": UNITS[field], "valid_values": total,
                               "status": "unavailable" if total == 0 else
                                         "complete" if q["valid_value_fraction"] == 1 and total == len(members)*len(times) else "partial",
                               **q}
    return {"member_count": len(members), "member_ids": sorted(members), "daily": daily,
            "hourly": {"snow_depth": hourly_depth},
            "data_quality": {"fields": availability, "invalid_arrays": invalid_arrays,
                             "unit_errors": unit_errors,
                             "optional_field_errors": obj.get("optional_field_errors", {}),
                             "control_fields_present": sorted(control)},
            "quality_note": "Perturbed members only; unsuffixed/control member 0 excluded, preserving existing event fractions. "
                            "Daily statistics require all returned hours per member; full_calendar_day flags truncated edge days. "
                            "Maxima are computed per member before ensemble statistics. Snow depth is instantaneous, never summed. "
                            "Hourly snow depth uses all valid members at that timestamp. Units are km/h for gusts and m for depth. "
                            "Hourly values can be interpolated by Open-Meteo; temporal coverage does not imply native hourly model output."}

def main():
    result={"retrieved_at_utc":datetime.now(timezone.utc).isoformat(),
            "source":"Open-Meteo Ensemble API","models":{},"errors":{},
            "note":"Member fractions are model-based estimates, not calibrated probabilities."}
    for label,model in MODELS.items():
        try: result["models"][label]=summarize(fetch(model))
        except (HTTPError,URLError,TimeoutError,OSError,ValueError,KeyError,TypeError) as e:
            result["errors"][label]=str(e)[:250]
    if not result["models"]: raise SystemExit("No valid ensembles; existing output preserved")
    OUT.parent.mkdir(parents=True,exist_ok=True)
    tmp=OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    tmp.replace(OUT)

if __name__=="__main__": main()
