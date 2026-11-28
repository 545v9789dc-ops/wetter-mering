#!/usr/bin/env python3
"""Model screening signals, not official warnings or calibrated probabilities."""
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

INPUT = Path('data/mering_models.json')
OUTPUT = Path('data/mering_winter_events.json')
BERLIN = ZoneInfo('Europe/Berlin')
# Internal screening thresholds, deliberately not DWD warning criteria.
SCREEN = {'precipitation_mm': {1: 10, 6: 15, 12: 20, 24: 25, 48: 40, 72: 60},
          'snowfall_cm': {1: 2, 6: 3, 12: 5, 24: 5, 48: 10, 72: 15},
          'wind_gusts_kmh': {1: 60}}


def number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def interval(row):
    try:
        a, b = (datetime.fromisoformat(row[k].replace('Z', '+00:00')) for k in ('start_utc', 'end_utc'))
        if a.tzinfo is None or b.tzinfo is None or b <= a:
            return None
        return a, b
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def identify(models):
    by_day, events = {}, []
    for model, rec in sorted(models.items()):
        if not isinstance(rec, dict) or rec.get('status') != 'ok':
            continue
        for day in rec.get('winter_daily', []):
            if day.get('complete_time_axis'):
                by_day.setdefault(day['date'], {})[model] = day
        signals = rec.get('hourly_signals') or {}
        for row in signals.get('icing_periods', []):
            if row.get('kind') not in ('explicit_freezing_precipitation', 'near_freezing_liquid_precipitation') or not interval(row):
                continue
            events.append({**row, 'date': interval(row)[0].astimezone(BERLIN).date().isoformat(),
                           'supporting_models': [model], 'supporting_model_count': 1,
                           'model_run_utc': rec.get('model_run_utc'),
                           'evidence_type': 'hourly_screen', 'confidence_note': 'Road temperature unknown; not an official warning'})
        for field, thresholds in SCREEN.items():
            for hours, threshold in thresholds.items():
                row = (rec.get('winter_rolling_max') or {}).get(str(hours), {}).get(field) or {}
                value = row.get('value')
                if not number(value) or value < threshold or not interval(row) or not number(row.get('complete_windows')) or row['complete_windows'] < 1:
                    continue
                if (interval(row)[1] - interval(row)[0]).total_seconds() != hours * 3600:
                    continue
                events.append({**row, 'date': interval(row)[0].astimezone(BERLIN).date().isoformat(),
                               'kind': 'rolling_' + field, 'window_hours': hours, 'screen_threshold': threshold,
                               'supporting_models': [model], 'supporting_model_count': 1,
                               'model_run_utc': rec.get('model_run_utc'), 'evidence_type': 'rolling_screen',
                               'fallback_metadata': (rec.get('parameter_fallback') or {}).get({'precipitation_mm':'precipitation','snowfall_cm':'snowfall','wind_gusts_kmh':'wind_gusts_10m'}[field]),
                               'confidence_note': 'Internal screening threshold, not official warning criteria'})
    rules = (
        ('snowfall', 'snowfall_cm', lambda v: number(v) and v >= 2, 'At least 2 cm forecast snowfall'),
        ('snow_cover', 'snow_depth_end_cm', lambda v: number(v) and v >= 2, 'At least 2 cm forecast snow cover'),
        ('ice_day', 'ice_day', lambda v: v is True, 'Daily maximum below 0 C'),
        ('severe_frost', 'tmin_c', lambda v: number(v) and v <= -10, 'Daily minimum at or below -10 C'),
        ('heavy_precipitation', 'precipitation_mm', lambda v: number(v) and v >= 10, 'At least 10 mm daily precipitation'),
    )
    for date, records in sorted(by_day.items()):
        for kind, field, predicate, explanation in rules:
            available = {m: r[field] for m, r in records.items() if r.get(field) is not None and (isinstance(r[field], bool) if field == 'ice_day' else number(r[field]))}
            hits = sorted(m for m, value in available.items() if predicate(value))
            if hits:
                events.append({'date': date, 'kind': kind, 'description': explanation,
                               'supporting_models': hits, 'available_models': sorted(available),
                               'supporting_model_count': len(hits), 'available_model_count': len(available),
                               'agreement_fraction': round(len(hits) / len(available), 3),
                               'evidence_type': 'daily_screen',
                               'confidence_note': 'Model agreement is not a calibrated probability'})
    return sorted(events, key=lambda e: (e['date'], e.get('start_utc', ''), e['kind'], e['supporting_models']))


def main():
    doc = json.loads(INPUT.read_text(encoding='utf-8'))
    events = identify(doc.get('models', {}))
    out = {'source_retrieved_at_utc': doc.get('retrieved_at_utc'),
           'note': 'Indicative model signals, not official weather warnings; absence of signals does not establish safety',
           'events': events}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix('.tmp')
    temp.write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temp.replace(OUTPUT)
    print(f'Winter event signals: {len(events)}')


if __name__ == '__main__':
    main()
