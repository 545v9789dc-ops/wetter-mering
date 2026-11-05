#!/usr/bin/env python3
"""Conservative, deterministic winter signals from complete daily summaries."""
import json
from pathlib import Path

INPUT = Path('data/mering_models.json')
OUTPUT = Path('data/mering_winter_events.json')

def identify(models):
    by_day = {}
    for model, rec in models.items():
        if not isinstance(rec, dict) or rec.get('status') != 'ok':
            continue
        for day in rec.get('winter_daily', []):
            if day.get('complete_time_axis'):
                by_day.setdefault(day['date'], {})[model] = day
    events = []
    for date, records in sorted(by_day.items()):
        # Evidence-based signals, not official warnings or calibrated probabilities.
        rules = (
            ('snowfall', 'snowfall_cm', lambda v: v >= 2, 'At least 2 cm forecast snowfall'),
            ('snow_cover', 'snow_depth_end_cm', lambda v: v >= 2, 'At least 2 cm forecast snow cover'),
            ('ice_day', 'ice_day', lambda v: v is True, 'Daily maximum below 0 C'),
            ('severe_frost', 'tmin_c', lambda v: v <= -10, 'Daily minimum at or below -10 C'),
            ('heavy_precipitation', 'precipitation_mm', lambda v: v >= 10, 'At least 10 mm daily precipitation'),
        )
        for kind, field, predicate, explanation in rules:
            available = {m: r[field] for m, r in records.items() if r.get(field) is not None}
            hits = [m for m, value in available.items() if predicate(value)]
            if len(hits) >= 2:
                events.append({'date': date, 'kind': kind, 'description': explanation,
                               'supporting_models': hits, 'available_models': sorted(available),
                               'confidence_note': 'Model agreement is not a calibrated probability'})
    return events

def main():
    doc = json.loads(INPUT.read_text(encoding='utf-8'))
    events = identify(doc.get('models', {}))
    out = {'source_retrieved_at_utc': doc.get('retrieved_at_utc'),
           'note': 'Indicative model signals, not official weather warnings',
           'events': events}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix('.tmp')
    temp.write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temp.replace(OUTPUT)
    print(f'Winter event signals: {len(events)}')

if __name__ == '__main__':
    main()
