#!/usr/bin/env python3
"""Winter summaries and efficient sliding precipitation/snowfall maxima; no network."""
import json
import math
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = timezone.utc
BERLIN = ZoneInfo('Europe/Berlin')
OUTPUT = Path('data/mering_models.json')
WINDOW_HOURS = (1, 6, 12, 24, 48, 72)


def numeric(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def timestamp(v):
    t = datetime.fromisoformat(v.replace('Z', '+00:00'))
    return (t.replace(tzinfo=UTC) if t.tzinfo is None else t).astimezone(UTC)


def winter_summary(record):
    hourly = record.get('hourly', {})
    times = [timestamp(s) for s in hourly.get('time', [])]
    if not times or any(b <= a for a, b in zip(times, times[1:])):
        return []
    lookup = {t: i for i, t in enumerate(times)}
    units = record.get('hourly_units', {})
    days = sorted({(t - timedelta(hours=1)).astimezone(BERLIN).date() for t in times})
    rows = []
    for day in days:
        start = datetime.combine(day, datetime.min.time(), BERLIN).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), BERLIN).astimezone(UTC)
        hours = int((end - start).total_seconds() / 3600)
        idx = [lookup.get(start + timedelta(hours=i)) for i in range(1, hours + 1)]
        complete_axis = all(i is not None for i in idx)

        def series(field, unit=None):
            values = hourly.get(field)
            if unit is not None and units.get(field) != unit:
                return None
            if not complete_axis or not isinstance(values, list) or len(values) != len(times):
                return None
            result = [values[i] for i in idx]
            return result if all(numeric(v) for v in result) else None

        temp = series('temperature_2m', '°C')
        precip = series('precipitation', 'mm')
        snow = series('snowfall', 'cm')
        depth = series('snow_depth', 'm')
        rows.append({
            'date': day.isoformat(), 'expected_hours': hours,
            'complete_time_axis': complete_axis,
            'tmin_c': round(min(temp), 2) if temp else None,
            'tmax_c': round(max(temp), 2) if temp else None,
            'frost_day': min(temp) < 0 if temp else None,
            'ice_day': max(temp) < 0 if temp else None,
            'severe_frost': min(temp) <= -10 if temp else None,
            'snow_cover_present': max(depth) > 0 if depth else None,
            'precipitation_mm': round(sum(precip), 3) if precip else None,
            'snowfall_cm': round(sum(snow), 3) if snow else None,
            'snow_depth_max_cm': round(max(depth) * 100, 2) if depth else None,
            'snow_depth_end_cm': round(depth[-1] * 100, 2) if depth else None,
            'snow_depth_change_cm': round((depth[-1] - depth[0]) * 100, 2) if depth else None,
        })
    return rows


def rolling_totals(record, reference):
    """Backward-compatible next 24/48/72 UTC hours, only complete intervals."""
    hourly = record.get('hourly', {})
    times = [timestamp(v) for v in hourly.get('time', [])]
    lookup = {t: i for i, t in enumerate(times)}
    start = reference.replace(minute=0, second=0, microsecond=0)
    units = record.get('hourly_units', {})
    output = {}
    for hours in (24, 48, 72):
        row = {'hours': hours, 'start_utc': start.isoformat()}
        indices = [lookup.get(start + timedelta(hours=i)) for i in range(1, hours + 1)]
        for field, unit, result_name in (('precipitation', 'mm', 'precipitation_mm'),
                                         ('snowfall', 'cm', 'snowfall_cm')):
            vals = hourly.get(field)
            valid = (units.get(field) == unit and isinstance(vals, list)
                     and len(vals) == len(times) and all(i is not None for i in indices))
            values = [vals[i] for i in indices] if valid else []
            row[result_name] = round(sum(values), 3) if values and all(numeric(v) for v in values) else None
        output[str(hours)] = row
    return output


def sliding_maxima(record, reference, hours_ahead=168):
    """O(N) per field/window using prefix sums and consecutive-hour coverage.

    Values are hourly interval-ending amounts. Only windows starting at/after
    the current UTC hour and ending within the next `hours_ahead` are considered.
    A missing hour/value invalidates its window; missing never means zero.
    """
    hourly = record.get('hourly', {})
    raw_times = hourly.get('time', [])
    times = [timestamp(v) for v in raw_times]
    start = reference.replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=hours_ahead)
    units = record.get('hourly_units', {})
    output = {}
    for field, unit, label in (('precipitation', 'mm', 'precipitation_mm'),
                               ('snowfall', 'cm', 'snowfall_cm')):
        values = hourly.get(field)
        series = {}
        if units.get(field) == unit and isinstance(values, list) and len(values) == len(times):
            series = {t: v for t, v in zip(times, values) if start < t <= end and numeric(v) and v >= 0}
        # Build one dense hourly axis; invalid/missing values break a window.
        dense = [series.get(start + timedelta(hours=i)) for i in range(1, hours_ahead + 1)]
        prefix, bad = [0.0], [0]
        for v in dense:
            prefix.append(prefix[-1] + (v if v is not None else 0))
            bad.append(bad[-1] + (v is None))
        for window in WINDOW_HOURS:
            best = None
            complete = 0
            if window <= hours_ahead:
                for stop in range(window, hours_ahead + 1):
                    begin = stop - window
                    if bad[stop] != bad[begin]:
                        continue
                    complete += 1
                    total = prefix[stop] - prefix[begin]
                    if best is None or total > best[0]:
                        best = (total, begin, stop)
            output.setdefault(str(window), {'hours': window})[label] = (
                {'value': round(best[0], 3),
                 'start_utc': (start + timedelta(hours=best[1])).isoformat(),
                 'end_utc': (start + timedelta(hours=best[2])).isoformat(),
                 'complete_windows': complete} if best else
                {'value': None, 'start_utc': None, 'end_utc': None,
                 'complete_windows': 0})
    return output


def enrich(doc):
    reference = timestamp(doc.get('retrieved_at_utc')) if doc.get('retrieved_at_utc') else datetime.now(UTC)
    for rec in doc.get('models', {}).values():
        if isinstance(rec, dict) and isinstance(rec.get('hourly'), dict):
            rec['winter_daily'] = winter_summary(rec)
            rec['winter_rolling'] = rolling_totals(rec, reference)
            rec['winter_rolling_max'] = sliding_maxima(rec, reference)
    return doc


def main():
    doc = json.loads(OUTPUT.read_text(encoding='utf-8'))
    enrich(doc)
    temp = OUTPUT.with_suffix('.winter.tmp')
    temp.write_text(json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temp.replace(OUTPUT)
    print('Winter metrics generated; incomplete windows retained as null.')


if __name__ == '__main__':
    main()
