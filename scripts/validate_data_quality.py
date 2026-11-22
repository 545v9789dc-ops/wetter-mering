#!/usr/bin/env python3
"""Post-fetch quality gate. Never alters forecast JSON or interprets fetch time as model age."""
import json
import math
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

UTC = timezone.utc
ROOT = Path('data')

# Must agree with scripts/fetch_models.py; do not use a retrieval timestamp.
DEFAULT_MODEL_AGE_LIMIT_HOURS = 18
LONG_RANGE_MODEL_AGE_LIMIT_HOURS = 24
LONG_RANGE_MODELS = frozenset(('GFS', 'IFS'))


def model_age_limit(name):
    return (LONG_RANGE_MODEL_AGE_LIMIT_HOURS if name in LONG_RANGE_MODELS
            else DEFAULT_MODEL_AGE_LIMIT_HOURS)


def stamp(value, assume_utc=False):
    if not isinstance(value, str):
        raise ValueError('missing timestamp')
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        if not assume_utc:
            raise ValueError('timestamp without timezone')
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def time_axis(values, assume_utc=False):
    if not isinstance(values, list) or not values:
        raise ValueError('empty time axis')
    ts = [stamp(x, assume_utc=assume_utc) for x in values]
    if any(b <= a for a, b in zip(ts, ts[1:])):
        raise ValueError('duplicate or unordered times')
    return ts


def plausible(hourly, assume_utc=False):
    times = time_axis(hourly.get('time'), assume_utc=assume_utc)
    problems = []
    bounds = {'temperature_2m': (-90, 65), 'precipitation': (0, 500),
              'wind_speed_10m': (0, 300), 'wind_gusts_10m': (0, 400),
              'pressure_msl': (800, 1100), 'relative_humidity_2m': (0, 100),
              'cloud_cover': (0, 100), 'cloud_cover_low': (0, 100),
              'snowfall': (0, 300), 'snow_depth': (0, 30),
              'freezing_level_height': (-1000, 25000)}
    for field, (low, high) in bounds.items():
        vals = hourly.get(field)
        if vals is None:
            continue  # optional fields differ between models
        if not isinstance(vals, list) or len(vals) != len(times):
            problems.append(f'{field}: length mismatch')
            continue
        for value in vals:
            if value is None:
                continue
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or not low <= value <= high):
                problems.append(f'{field}: implausible value')
                break
    return problems


def check_winter(rec, model):
    """Check optional derived daily values without invalidating core models."""
    warnings = []
    rows = rec.get('winter_daily')
    if rows is None:
        return [f'{model}: winter_daily unavailable']
    if not isinstance(rows, list):
        return [f'{model}: winter_daily malformed']
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('date'), str):
            warnings.append(f'{model}: malformed winter day')
            break
        for key in ('precipitation_mm', 'snowfall_cm', 'snow_depth_max_cm', 'snow_depth_end_cm'):
            value = row.get(key)
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)
                                      or not math.isfinite(value) or value < 0):
                warnings.append(f'{model}: invalid winter {key}')
                break
        if row.get('tmin_c') is not None and row.get('tmax_c') is not None:
            if row['tmin_c'] > row['tmax_c']:
                warnings.append(f'{model}: winter Tmin above Tmax')
        if row.get('snow_depth_change_cm') is not None:
            change = row['snow_depth_change_cm']
            if not isinstance(change, (int, float)) or not math.isfinite(change):
                warnings.append(f'{model}: invalid snow depth change')
    return warnings


def check_parameter_fallback(rec, name, now, times):
    """Check per-model 18 h/24 h source-run ceiling, not fetch time."""
    warnings=[]
    target_run=stamp(rec.get('model_run_utc'))
    for field, entry in rec.get('parameter_fallback',{}).items():
        if not isinstance(entry,dict):
            warnings.append(f'{name}/{field}: malformed fallback metadata')
            continue
        indices=entry.get('recovered_indices',[])
        sources=entry.get('sources',[])
        values=rec.get('hourly',{}).get(field,[])
        if (not isinstance(indices,list) or not isinstance(sources,list) or
            not isinstance(values,list) or any(not isinstance(i,int) or isinstance(i,bool)
                    or not 0 <= i < len(times) for i in indices) or
            len(set(indices)) != len(indices) or
            entry.get('recovered_hours') != len(indices) or
            any(i >= len(values) or not isinstance(values[i],(int,float)) or
                    isinstance(values[i],bool) or not math.isfinite(values[i])
                    for i in indices)):
            warnings.append(f'{name}/{field}: inconsistent fallback indices')
            continue
        if sum(item.get('recovered_hours',0) for item in sources if isinstance(item,dict)) != len(indices):
            warnings.append(f'{name}/{field}: inconsistent fallback source counts')
        if not sources and indices:
            warnings.append(f'{name}/{field}: missing fallback source')
        for source in sources:
            if not isinstance(source,dict):
                warnings.append(f'{name}/{field}: malformed fallback source')
                continue
            try:
                run=stamp(source.get('source_run_utc'))
                age=(now-run).total_seconds()/3600
                if age < 0 or age > model_age_limit(name):
                    warnings.append(f'{name}/{field}: fallback older than {model_age_limit(name)}h ({age:.1f}h)')
                if run > target_run:
                    warnings.append(f'{name}/{field}: fallback from newer run')
                if source.get('source_model') != rec.get('api_model'):
                    warnings.append(f'{name}/{field}: fallback model mismatch')
            except (KeyError,TypeError,ValueError):
                warnings.append(f'{name}/{field}: invalid fallback run timestamp')
    return warnings


def summarize_fallback_warnings(rec, name, now):
    """One actionable warning per model; keep provenance defects separate.

    The detailed per-field records remain unchanged in mering_models.json.
    Grouping affects only GitHub Actions warning output and its count.
    """
    fallback = rec.get('parameter_fallback', {})
    if not isinstance(fallback, dict):
        return None
    active = []
    runs = set()
    ages = []
    for field, entry in fallback.items():
        if not isinstance(entry, dict):
            continue
        hours = entry.get('recovered_hours', 0)
        if not isinstance(hours, int) or isinstance(hours, bool) or hours <= 0:
            continue
        active.append((field, hours))
        for source in entry.get('sources', []):
            if not isinstance(source, dict):
                continue
            run = source.get('source_run_utc')
            try:
                dt = stamp(run)
                ages.append((now - dt).total_seconds() / 3600)
                runs.add(dt.strftime('%d.%m. %H:%M UTC'))
            except (ValueError, TypeError):
                pass  # invalid provenance is reported separately by the quality gate
    if not active:
        return None
    important = {'temperature_2m': 'T', 'precipitation': 'rain',
                 'wind_speed_10m': 'wind', 'wind_gusts_10m': 'gusts',
                 'pressure_msl': 'pressure'}
    top = ', '.join(f'{label}={hours}h' for field, hours in active
                    if (label := important.get(field)))
    other_count = sum(field not in important for field, _ in active)
    if other_count:
        top += f'; {other_count} other fields' if top else f'{other_count} other fields'
    summary = (f'{name}: fallback in {len(active)} parameters '
               f'({sum(n for _, n in active)} hourly values; {top})')
    if runs:
        summary += '; source run(s): ' + ', '.join(sorted(runs))
    if ages:
        summary += f'; oldest age {max(ages):.1f}h'
    return summary


def check_models(doc, now):
    critical, warnings = [], []
    models = doc.get('models', {})
    for name in ('ICON-D2', 'ICON-EU', 'AROME', 'GFS', 'IFS', 'UKMO'):
        rec = models.get(name)
        if not isinstance(rec, dict) or rec.get('status') not in ('ok', 'stale'):
            warnings.append(f'{name}: missing/unverified')
            continue
        if rec['status'] != 'ok':
            warnings.append(f'{name}: cached')
        try:
            times = time_axis(rec['hourly']['time'], assume_utc=True)
            future = sum(t > now for t in times)
            if not future:
                warnings.append(f'{name}: no future timestamps')
            elif name in ('ICON-D2', 'AROME') and future < 12:
                warnings.append(f'{name}: short remaining forecast coverage ({future}h)')
            elif name in ('ICON-EU', 'IFS', 'GFS', 'UKMO') and future < 24:
                warnings.append(f'{name}: short remaining forecast coverage ({future}h)')
            if not any(v is not None for v in rec['hourly'].get('temperature_2m', [])):
                warnings.append(f'{name}: no valid temperatures')
            warnings.extend(f'{name}: {p}' for p in plausible(rec['hourly'], assume_utc=True))
            warnings.extend(check_winter(rec, name))
            units = rec.get('hourly_units', {})
            for field, unit in (('snowfall', 'cm'), ('snow_depth', 'm'), ('precipitation', 'mm')):
                if field in rec['hourly'] and units.get(field) != unit:
                    warnings.append(f'{name}: unexpected unit for {field}: {units.get(field)}')
            # Monitor explicitly the five mandatory fields, even if the model status is 'ok'.
            mandatory = ('temperature_2m', 'precipitation', 'wind_speed_10m',
                         'wind_gusts_10m', 'pressure_msl')
            for field in mandatory:
                values = rec['hourly'].get(field, [])
                if not isinstance(values, list) or len(values) != len(times):
                    warnings.append(f'{name}/{field}: missing or wrong-length array')
                    continue
                count = sum(isinstance(v, (int, float)) and not isinstance(v, bool)
                            and math.isfinite(v) for v in values)
                if count == 0:
                    warnings.append(f'{name}/{field}: not available (0/{len(times)})')
            # Keep every invalid-source warning; only valid replacement notices
            # are consolidated to a single warning per model.
            warnings.extend(check_parameter_fallback(rec, name, now, times))
            fallback_notice = summarize_fallback_warnings(rec, name, now)
            if fallback_notice:
                warnings.append(fallback_notice)
            run_age = (now-stamp(rec.get('model_run_utc'))).total_seconds()/3600
            if run_age > model_age_limit(name):
                warnings.append(f'{name}: model run older than {model_age_limit(name)}h ({run_age:.1f}h)')
        except (KeyError, TypeError, ValueError) as e:
            critical.append(f'{name}: malformed time series: {e}')
    if not any(isinstance(models.get(n), dict) and models[n].get('status') == 'ok'
               for n in ('ICON-D2', 'ICON-EU', 'AROME', 'GFS', 'IFS')):
        critical.append('no fresh main model')
    if isinstance(models.get('IFS'), dict):
        ext = models['IFS'].get('extended_forecast')
        if ext:
            try:
                time_axis(ext['hourly']['time'], assume_utc=True)
                warnings.extend(f'IFS extended_forecast: {p}' for p in plausible(ext['hourly'], assume_utc=True))
                if not any(v is not None for v in ext['hourly'].get('temperature_2m', [])):
                    warnings.append('IFS extended_forecast: no temperatures')
            except (KeyError, TypeError, ValueError) as e:
                warnings.append(f'IFS extended_forecast: invalid: {e}')
        # No warning if the optional extension is not needed and not stored.
    return critical, warnings


def check_mosmix(doc):
    warnings = []
    for name in ('Augsburg', 'Lechfeld'):
        station = doc.get('stations', {}).get(name)
        if not isinstance(station, dict):
            warnings.append(f'MOSMIX {name}: missing')
            continue
        try:
            times = time_axis(station['forecast_times_utc'])
            if station.get('status') != 'ok':
                warnings.append(f'MOSMIX {name}: {station.get("status")}')
            for key, values in station.get('parameters', {}).items():
                if not isinstance(values, list) or len(values) != len(times):
                    warnings.append(f'MOSMIX {name}/{key}: length mismatch')
        except (KeyError, TypeError, ValueError) as e:
            warnings.append(f'MOSMIX {name}: {e}')
    return warnings


def check_ensembles(doc):
    warnings = []
    for name in ('GFS-ENS', 'IFS-ENS'):
        rec = doc.get('models', {}).get(name)
        if not isinstance(rec, dict) or not isinstance(rec.get('daily'), (dict, list)) or not rec['daily']:
            warnings.append(f'{name}: missing/empty')
        elif rec.get('data_quality', {}).get('invalid_arrays'):
            warnings.append(f'{name}: invalid arrays')
    if doc.get('errors'):
        warnings.append('Ensemble collector reported errors')
    return warnings


def main():
    now = datetime.now(UTC)
    critical, warnings = [], []
    for filename, checker in (('mering_models.json', check_models),
                              ('mering_mosmix.json', check_mosmix),
                              ('mering_ensembles.json', check_ensembles)):
        try:
            path = ROOT / filename
            doc = json.loads(path.read_text(encoding='utf-8'))
            age = (now - stamp(doc['retrieved_at_utc'])).total_seconds() / 3600
            if age < -1 or age > 30:
                warnings.append(f'{filename}: collection timestamp age {age:.1f}h')
            if filename == 'mering_models.json':
                c, w = checker(doc, now)
                critical.extend(c)
                warnings.extend(w)
            else:
                warnings.extend(checker(doc))
        except (OSError, ValueError, TypeError, KeyError) as e:
            if filename == 'mering_models.json':
                critical.append(f'{filename}: {e}')
            else:
                warnings.append(f'{filename}: {e}')
    for issue in warnings:
        print('::warning::' + issue)
    for issue in critical:
        print('::error::' + issue)
    print('QUALITY:', 'failed' if critical else ('degraded' if warnings else 'ok'),
          f'critical={len(critical)} warnings={len(warnings)}')
    return 1 if critical else 0


if __name__ == '__main__':
    sys.exit(main())
