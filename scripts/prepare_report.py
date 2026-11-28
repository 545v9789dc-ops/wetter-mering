#!/usr/bin/env python3
"""Compact report index; original hourly arrays and DWD remain separate."""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from collections import defaultdict
ROOT = Path('data')
OUT = ROOT / 'mering_report.json'
BERLIN = ZoneInfo('Europe/Berlin')
UTC = timezone.utc


def read(name):
    try:
        doc = json.loads((ROOT / name).read_text(encoding='utf-8'))
        if not isinstance(doc, dict):
            raise ValueError('Expected JSON object')
        return doc, None
    except (OSError, ValueError) as exc:
        return {}, f'{name}: {type(exc).__name__}: {exc}'


def number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def stamp(v):
    if not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v.replace('Z', '+00:00'))
        return t.astimezone(UTC) if t.tzinfo else None
    except ValueError:
        return None


def age_hours(value, now):
    t, n = stamp(value), stamp(now)
    return round((n-t).total_seconds()/3600, 2) if t and n else None


def mosmix_days(station):
    times = station.get('forecast_times_utc', [])
    parameters = station.get('parameters', {})
    grouped = defaultdict(dict)
    duplicates = defaultdict(set)
    for i, value in enumerate(times):
        moment = stamp(value)
        if moment is None:
            continue
        start = moment - timedelta(hours=1)
        day = start.astimezone(BERLIN).date().isoformat()
        if start in grouped[day]:
            duplicates[day].add(start)
        grouped[day][start] = i
    result = []
    for day, indices in sorted(grouped.items()):
        midnight = datetime.fromisoformat(day).replace(tzinfo=BERLIN)
        first = midnight.astimezone(UTC)
        expected = int(((midnight + timedelta(days=1)).astimezone(UTC)-first).total_seconds()/3600)
        slots = [first+timedelta(hours=i) for i in range(expected)]
        row = {'date': day, 'hours_available': sum(t in indices for t in slots), 'calendar_day_hours': expected}
        for key, field, factor in (('sunshine_hours','SunD1',3600), ('precipitation_mm','RR1c',1)):
            values = parameters.get(field)
            valid, missing = [], []
            for t in slots:
                i = indices.get(t)
                v = values[i] if isinstance(values,list) and i is not None and i < len(values) else None
                if t not in duplicates[day] and number(v) and v >= 0:
                    valid.append(v)
                else:
                    missing.append(t.isoformat())
            complete = len(valid) == expected
            total = round(sum(valid)/factor,3) if valid else None
            row[key] = {'available_sum': total, 'sum': total if complete else None,
                        'valid_hours':len(valid), 'complete':complete, 'missing_interval_starts_utc':missing}
        result.append(row)
    return result


def model_summary(rec):
    return {'status': rec.get('status'), 'model_run_utc': rec.get('model_run_utc'),
            'run_age_hours': rec.get('run_age_hours'), 'data_age_hours': rec.get('data_age_hours'),
            **{k:rec.get(k, []) for k in ('daily','winter_daily','recent_errors')},
            **{k:rec.get(k, {}) for k in ('winter_rolling','winter_rolling_max','coverage','optional_variable_coverage','parameter_fallback','valid_hours_by_variable','parameter_availability','hourly_signals')}}


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
            'data_quality': rec.get('data_quality', {}), 'quality_note': rec.get('quality_note'),
            'run_metadata':rec.get('run_metadata', {})}


def matching(a,b):
    return stamp(a) is not None and stamp(a)==stamp(b)


def prepare(models, mosmix, ensembles, generated_at=None, quality=None, events=None):
    now = generated_at or datetime.now(UTC).isoformat()
    sources = {'models':models,'mosmix':mosmix,'ensembles':ensembles}
    timestamps = {k:v.get('retrieved_at_utc') for k,v in sources.items()}
    output = {'schema_version': 1, 'generated_at_utc':now, 'source_timestamps':timestamps,
              'source_age_hours_at_generation':{k:age_hours(v,now) for k,v in timestamps.items()},
              'source_status': {'models': models.get('collection_status'), 'mosmix': mosmix.get('collection_status'),
                                'ensembles': 'degraded' if ensembles.get('errors') else ('ok' if ensembles.get('models') else 'missing')},
              'models': {}, 'mosmix': {}, 'ensembles': {},
              'notes': ['Daily partial sums are not full-day totals.', 'Ensemble percentiles copied from existing member distributions.',
                        'DWD synoptic reports are maintained separately and must be read from dwd_synoptik.json.',
                        'Original hourly arrays remain in source JSON files.', 'Published ensemble run metadata is not proof of the actual forecast run.',
                        'Ages are snapshots at generation; recalculate at report time.']}
    for name in ('ICON-D2','ICON-EU','AROME','GFS','IFS','UKMO'):
        rec = model_summary(models.get('models',{}).get(name,{}))
        rec['run_age_hours_at_generation'] = age_hours(rec['model_run_utc'],now)
        output['models'][name] = rec
    for name in ('Augsburg','Lechfeld'):
        station = mosmix.get('stations',{}).get(name,{})
        output['mosmix'][name] = {'status':station.get('status','missing'), 'issue_time_utc':station.get('issue_time_utc'),
                                  'issue_age_hours':station.get('issue_age_hours'), 'issue_age_hours_at_generation':age_hours(station.get('issue_time_utc'),now), 'daily':mosmix_days(station)}
    for name in ('GFS-ENS','IFS-ENS'):
        output['ensembles'][name] = ensemble_summary(ensembles.get('models',{}).get(name,{}))
    model_count = sum(v.get('status') in ('ok','stale') for v in output['models'].values())
    mosmix_count = sum(v.get('status') in ('ok','stale') for v in output['mosmix'].values())
    ensemble_count = sum(bool(v.get('daily')) for v in output['ensembles'].values())
    gaps = []
    for count,total,label in ((model_count,6,'Main models'),(mosmix_count,2,'MOSMIX stations'),(ensemble_count,2,'Ensembles')):
        if count<total:
            gaps.append(f'{label} available: {count}/{total}')
    for name,rec in output['models'].items():
        if rec.get('status') in ('ok','stale'):
            if not rec.get('winter_rolling_max'): gaps.append(f'{name}: sliding maxima unavailable')
            if not rec.get('hourly_signals'): gaps.append(f'{name}: hourly signals unavailable')
        for field,meta in rec.get('parameter_availability',{}).items():
            if meta.get('status') in ('not_available','partial','fallback'):
                desc = f'{name}/{field}: {meta["status"]} ({meta.get("valid_hours",0)} valid hours)'
                if meta.get('status')=='fallback': desc += f'; {meta.get("replaced_hours",0)} from previous model run'
                gaps.append(desc)
    q = quality or {}
    mismatches = [k for k,t in timestamps.items() if not matching(t,q.get('sources',{}).get(f'mering_{k}.json',{}).get('retrieved_at_utc')) or q.get('sources',{}).get(f'mering_{k}.json',{}).get('readable') is not True]
    checked = stamp(q.get('checked_at_utc'))
    chronology = checked is not None and stamp(now) is not None and checked <= stamp(now) and all(stamp(t) is not None and stamp(t)<=checked for t in timestamps.values())
    q_ok = bool(q) and not mismatches and chronology and q.get('status') in ('ok','degraded','failed')
    output['data_validation'] = {'status':'matched' if q_ok else ('mismatch' if q else 'missing'), 'mismatched_sources':mismatches, 'snapshot':q if q_ok else None}
    if not q_ok: gaps.append('Matching data validation unavailable')
    ev = events or {}
    ev_ok = matching(ev.get('source_retrieved_at_utc'),timestamps['models']) and isinstance(ev.get('events'),list)
    output['winter_events'] = {'status':'matched' if ev_ok else ('mismatch' if ev else 'missing'), 'events':ev['events'] if ev_ok else None, 'source_retrieved_at_utc':ev.get('source_retrieved_at_utc'), 'note':ev.get('note')}
    if not ev_ok: gaps.append('Matching winter events unavailable')
    output['report_quality'] = {'status':'complete' if not gaps else 'complete_with_gaps','model_count_available':model_count,'mosmix_station_count_available':mosmix_count,'ensemble_count_available':ensemble_count,'gaps':gaps}
    return output


def main():
    models,m_error = read('mering_models.json')
    mosmix,s_error = read('mering_mosmix.json')
    ensembles,e_error = read('mering_ensembles.json')
    quality,q_error = read('mering_data_quality.json')
    events,w_error = read('mering_winter_events.json')
    if m_error: raise SystemExit(m_error)
    result = prepare(models,mosmix,ensembles,quality=quality,events=events)
    result['input_errors'] = [x for x in (s_error,e_error,q_error,w_error) if x]
    OUT.parent.mkdir(parents=True,exist_ok=True)
    tmp = OUT.with_suffix('.tmp')
    tmp.write_text(json.dumps(result,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n',encoding='utf-8')
    tmp.replace(OUT)
    print('Report index prepared:',result['report_quality'])


if __name__ == '__main__':
    main()
