#!/usr/bin/env python3
"""Winter metrics and hourly hazard indicators; no network."""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = timezone.utc
BERLIN = ZoneInfo('Europe/Berlin')
OUTPUT = Path('data/mering_models.json')
WINDOW_HOURS = (1, 6, 12, 24, 48, 72)
HOUR = timedelta(hours=1)


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
    days = sorted({(t - HOUR).astimezone(BERLIN).date() for t in times})
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
        rows.append({'date': day.isoformat(), 'expected_hours': hours,
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
            'snow_depth_change_cm': round((depth[-1] - depth[0]) * 100, 2) if depth else None})
    return rows


def rolling_totals(record, reference):
    hourly = record.get('hourly', {})
    times = [timestamp(v) for v in hourly.get('time', [])]
    lookup = {t: i for i, t in enumerate(times)}
    start = reference.replace(minute=0, second=0, microsecond=0)
    units = record.get('hourly_units', {})
    output = {}
    for hours in (24, 48, 72):
        row = {'hours': hours, 'start_utc': start.isoformat()}
        indices = [lookup.get(start + timedelta(hours=i)) for i in range(1, hours + 1)]
        for field, unit, result_name in (('precipitation', 'mm', 'precipitation_mm'), ('snowfall', 'cm', 'snowfall_cm')):
            vals = hourly.get(field)
            valid = (units.get(field) == unit and isinstance(vals, list) and len(vals) == len(times) and all(i is not None for i in indices))
            values = [vals[i] for i in indices] if valid else []
            row[result_name] = round(sum(values), 3) if values and all(numeric(v) for v in values) else None
        output[str(hours)] = row
    return output


def sliding_maxima(record, reference, hours_ahead=168):
    """Complete hourly windows only; gusts use maximum, never sum."""
    hourly = record.get('hourly', {})
    times = [timestamp(v) for v in hourly.get('time', [])]
    start = reference.replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=hours_ahead)
    units = record.get('hourly_units', {})
    output = {}
    for field, unit, label in (('precipitation', 'mm', 'precipitation_mm'), ('snowfall', 'cm', 'snowfall_cm'), ('wind_gusts_10m', 'km/h', 'wind_gusts_kmh')):
        values = hourly.get(field)
        series = {}
        if units.get(field) == unit and isinstance(values, list) and len(values) == len(times) and all(a < b for a,b in zip(times,times[1:])):
            series = {t: v for t,v in zip(times,values) if start < t <= end and numeric(v) and v >= 0}
        dense = [series.get(start + timedelta(hours=i)) for i in range(1,hours_ahead+1)]
        prefix, bad = [0.0], [0]
        for v in dense:
            prefix.append(prefix[-1] + (v if v is not None else 0))
            bad.append(bad[-1] + (v is None))
        for window in WINDOW_HOURS:
            best = None
            complete = 0
            for stop in range(window,hours_ahead+1):
                begin = stop-window
                if bad[stop] != bad[begin]:
                    continue
                complete += 1
                total = max(dense[begin:stop]) if field == 'wind_gusts_10m' else prefix[stop]-prefix[begin]
                if best is None or total > best[0]:
                    best = (total,begin,stop)
            output.setdefault(str(window), {'hours':window})[label] = ({'value':round(best[0],3), 'start_utc':(start+timedelta(hours=best[1])).isoformat(), 'end_utc':(start+timedelta(hours=best[2])).isoformat(), 'complete_windows':complete} if best else {'value':None,'start_utc':None,'end_utc':None,'complete_windows':0})
    return output


def hourly_signals(record, reference, hours_ahead=168):
    """Forecast-only screening, not a road-temperature forecast or official warning.

    WMO 56/57/66/67 explicitly indicate freezing drizzle/rain. Ordinary
    liquid precipitation near freezing is a separate, heuristic signal.
    Missing hours split events; interval boundaries are not exact onset times.
    """
    hourly = record.get('hourly', {})
    times = [timestamp(v) for v in hourly.get('time', [])]
    units = record.get('hourly_units', {})
    start = reference.replace(minute=0,second=0,microsecond=0)
    end = start + timedelta(hours=hours_ahead)
    fields = {'precipitation':'mm','temperature_2m':'°C','wet_bulb_temperature_2m':'°C','weather_code':'wmo code','snowfall':'cm','wind_gusts_10m':'km/h'}
    result = {'start_utc':start.isoformat(),'end_utc':end.isoformat(),'valid_hours':dict.fromkeys(fields,0),'precipitation_periods':[],'icing_periods':[], 'note':'Screening only; road temperature unknown. Missing values are not zero.'}
    if any(b <= a for a,b in zip(times,times[1:])):
        result['note'] += ' Invalid time axis.'
        return result
    lookup = {t:i for i,t in enumerate(times)}
    fallback = record.get('parameter_fallback', {})
    recovered = {f:set(m.get('recovered_indices',[])) for f,m in fallback.items() if isinstance(m,dict)}
    def get(field,i):
        vals = hourly.get(field)
        if i is None or units.get(field)!=fields[field] or not isinstance(vals,list) or len(vals)!=len(times):
            return None
        v=vals[i]
        if not numeric(v) or (field in ('precipitation','snowfall','wind_gusts_10m') and v<0):
            return None
        return v
    def append_period(key,kind,t,vals,i,prev):
        rows=result[key]
        if not rows or rows[-1]['kind']!=kind or rows[-1]['end_utc']!=(t-HOUR).isoformat():
            rows.append({'kind':kind,'start_utc':(t-HOUR).isoformat(),'end_utc':t.isoformat(),'first_valid_time_utc':t.isoformat(),'temperature_at_first_valid_time_c':vals['temperature_2m'],'temperature_at_interval_start_c':prev,'precipitation_mm':0.0,'valid_precipitation_hours':0,'interval_count':0,'weather_codes':[],'fallback_fields':[]})
        row=rows[-1]
        row['end_utc']=t.isoformat()
        row['interval_count']+=1
        if vals['precipitation'] is not None:
            row['precipitation_mm']+=vals['precipitation']; row['valid_precipitation_hours']+=1
        code=vals['weather_code']
        if code is not None and code not in row['weather_codes']: row['weather_codes'].append(code)
        row['fallback_fields']=sorted(set(row['fallback_fields']) | {f for f in fields if i in recovered.get(f,set()) or (f=='temperature_2m' and lookup.get(t-HOUR) in recovered.get(f,set()))})
    for h in range(1,hours_ahead+1):
        t=start+timedelta(hours=h); i=lookup.get(t)
        vals={f:get(f,i) for f in fields}
        for f,v in vals.items(): result['valid_hours'][f]+=v is not None
        p,temp,code=vals['precipitation'],vals['temperature_2m'],vals['weather_code']
        prev=get('temperature_2m',lookup.get(t-HOUR))
        if p is not None and p>0:
            append_period('precipitation_periods','precipitation',t,vals,i,prev)
        if code in (56,57,66,67):
            append_period('icing_periods','explicit_freezing_precipitation',t,vals,i,prev)
        elif p is not None and p>0 and temp is not None and -2<=temp<=1 and code in (51,53,55,61,63,65,80,81,82):
            append_period('icing_periods','near_freezing_liquid_precipitation',t,vals,i,prev)
        elif p is not None and p>0 and temp is not None and -2<=temp<=1 and code is None and vals['snowfall']==0:
            append_period('icing_periods','near_freezing_phase_uncertain',t,vals,i,prev)
    for key in ('precipitation_periods','icing_periods'):
        for row in result[key]:
            row['precipitation_mm']=round(row['precipitation_mm'],3) if row['valid_precipitation_hours']==row['interval_count'] else None
    return result


def enrich(doc):
    reference=timestamp(doc.get('retrieved_at_utc')) if doc.get('retrieved_at_utc') else datetime.now(UTC)
    for rec in doc.get('models',{}).values():
        if isinstance(rec,dict) and isinstance(rec.get('hourly'),dict):
            rec['winter_daily']=winter_summary(rec)
            rec['winter_rolling']=rolling_totals(rec,reference)
            rec['winter_rolling_max']=sliding_maxima(rec,reference)
            rec['hourly_signals']=hourly_signals(rec,reference)
    return doc


def main():
    doc=json.loads(OUTPUT.read_text(encoding='utf-8'))
    enrich(doc)
    temp=OUTPUT.with_suffix('.winter.tmp')
    temp.write_text(json.dumps(doc,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    temp.replace(OUTPUT)
    print('Winter metrics generated; incomplete windows retained as null.')


if __name__=='__main__':
    main()
