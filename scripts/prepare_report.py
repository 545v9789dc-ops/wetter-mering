#!/usr/bin/env python3
"""Compact, lossless-reference report index. Reuses precomputed daily/ensemble data.

Does not fetch, recompute ensemble distributions or duplicate full hourly arrays.
Missing and partial data are explicitly marked. DWD synoptics are kept separate
because they update in a different workflow.
"""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path('data')
OUT = ROOT / 'mering_report.json'
BERLIN = ZoneInfo('Europe/Berlin')
UTC = timezone.utc


def read(name):
    try:
        return json.loads((ROOT / name).read_text(encoding='utf-8')), None
    except (OSError, ValueError) as exc:
        return {}, f'{name}: {type(exc).__name__}: {exc}'


def number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def mosmix_days(station):
    """Local interval-ending SunD1 coverage; partial sum never masquerades as full."""
    times = station.get('forecast_times_utc', [])
    parameters = station.get('parameters', {})
    sun = parameters.get('SunD1')
    rain = parameters.get('RR1c')
    from collections import defaultdict
    grouped = defaultdict(list)
    for i, stamp in enumerate(times):
        try:
            moment = datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(UTC)
            # Hourly SunD1/RR1c describe the *preceding* interval.
            day = (moment - timedelta(hours=1)).astimezone(BERLIN).date().isoformat()
            grouped[day].append(i)
        except (ValueError, TypeError):
            continue
    result = []
    for day, idx in sorted(grouped.items()):
        midnight = datetime.fromisoformat(day).replace(tzinfo=BERLIN)
        expected = int(((midnight + timedelta(days=1)).astimezone(UTC) -
                        midnight.astimezone(UTC)).total_seconds() / 3600)
        row = {'date': day, 'hours_available': len(idx), 'calendar_day_hours': expected}
        for key, values, factor in (('sunshine_hours', sun, 3600), ('precipitation_mm', rain, 1)):
            valid = [values[i] for i in idx if isinstance(values, list) and i < len(values)
                     and number(values[i]) and values[i] >= 0]
            complete = len(idx) == expected and len(valid) == expected
            row[key] = {'available_sum': round(sum(valid) / factor, 3) if valid else None,
                        'sum': round(sum(valid) / factor, 3) if complete else None,
                        'valid_hours': len(valid), 'complete': complete}
        result.append(row)
    return result


def model_summary(rec):
    return {'status': rec.get('status'), 'model_run_utc': rec.get('model_run_utc'),
            'run_age_hours': rec.get('run_age_hours'),
            'data_age_hours': rec.get('data_age_hours'),
            'daily': rec.get('daily', []), 'winter_daily': rec.get('winter_daily', []),
            'winter_rolling': rec.get('winter_rolling', {}),
            'winter_rolling_max': rec.get('winter_rolling_max', {}),
            'coverage': rec.get('coverage', {}),
            'optional_variable_coverage': rec.get('optional_variable_coverage', {}),
            'recent_errors': rec.get('recent_errors', [])}


def ensemble_summary(rec):
    daily = []
    for row in rec.get('daily', []) if isinstance(rec.get('daily'), list) else []:
        daily.append({'date': row.get('date'),
                      'full_calendar_day': row.get('data_quality', {}).get('full_calendar_day', False),
                      'daily_distributions': row.get('daily_distributions', {}),
                      'probabilities': row.get('probabilities', {}),
                      'available_members': row.get('available_members', {}),
                      'wind_gusts_10m_max': row.get('wind_gusts_10m_max'),
                      'snow_depth_max': row.get('snow_depth_max'),
                      'precipitation_threshold_probabilities': row.get('precipitation_threshold_probabilities', {})})
    return {'member_count': rec.get('member_count'), 'daily': daily,
            'data_quality': rec.get('data_quality', {}), 'quality_note': rec.get('quality_note')}


def prepare(models, mosmix, ensembles, generated_at=None):
    output = {'schema_version': 1,
              'generated_at_utc': generated_at or datetime.now(UTC).isoformat(),
              'source_timestamps': {key: doc.get('retrieved_at_utc') for key, doc in
                                    (('models', models), ('mosmix', mosmix), ('ensembles', ensembles))},
              'source_status': {'models': models.get('collection_status'),
                                'mosmix': mosmix.get('collection_status'),
                                'ensembles': 'degraded' if ensembles.get('errors') else
                                             ('ok' if ensembles.get('models') else 'missing')},
              'models': {}, 'mosmix': {}, 'ensembles': {},
              'notes': ['Daily partial sums are not full-day totals.',
                        'Ensemble percentiles copied from existing member distributions.',
                        'DWD synoptic reports are maintained separately and must be read from dwd_synoptik.json.',
                        'Original hourly arrays remain in source JSON files.']}
    for name in ('ICON-D2', 'ICON-EU', 'AROME', 'GFS', 'IFS'):
        rec = models.get('models', {}).get(name, {})
        output['models'][name] = model_summary(rec)
    for name in ('Augsburg', 'Lechfeld'):
        station = mosmix.get('stations', {}).get(name, {})
        output['mosmix'][name] = {'status': station.get('status', 'missing'),
                                  'issue_time_utc': station.get('issue_time_utc'),
                                  'issue_age_hours': station.get('issue_age_hours'),
                                  'daily': mosmix_days(station)}
    for name in ('GFS-ENS', 'IFS-ENS'):
        output['ensembles'][name] = ensemble_summary(ensembles.get('models', {}).get(name, {}))
    model_count = sum(v.get('status') in ('ok', 'stale') for v in output['models'].values())
    mosmix_count = sum(v.get('status') in ('ok', 'stale') for v in output['mosmix'].values())
    ensemble_count = sum(bool(v.get('daily')) for v in output['ensembles'].values())
    gaps = []
    if model_count < 5:
        gaps.append(f'Main models available: {model_count}/5')
    if mosmix_count < 2:
        gaps.append(f'MOSMIX stations available: {mosmix_count}/2')
    if ensemble_count < 2:
        gaps.append(f'Ensembles available: {ensemble_count}/2')
    for name, rec in output['models'].items():
        if rec.get('status') in ('ok', 'stale') and not rec.get('winter_rolling_max'):
            gaps.append(f'{name}: sliding maxima unavailable')
    output['report_quality'] = {
        'status': 'complete' if not gaps else 'complete_with_gaps',
        'model_count_available': model_count,
        'mosmix_station_count_available': mosmix_count,
        'ensemble_count_available': ensemble_count, 'gaps': gaps}
    return output


def main():
    models, m_error = read('mering_models.json')
    mosmix, s_error = read('mering_mosmix.json')
    ensembles, e_error = read('mering_ensembles.json')
    if m_error:
        raise SystemExit(m_error)
    result = prepare(models, mosmix, ensembles)
    result['input_errors'] = [x for x in (s_error, e_error) if x]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix('.tmp')
    tmp.write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n', encoding='utf-8')
    tmp.replace(OUT)
    print('Report index prepared:', result['report_quality'])


if __name__ == '__main__':
    main()
