#!/usr/bin/env python3
"""Fetch DWD MOSMIX_L station KML/KMZ forecasts for Augsburg and Lechfeld.

This is a separate, non-critical enrichment of the existing Open-Meteo models.
All forecast times and issue times are UTC. Missing values remain null.
"""
import json
import math
import os
import sys
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

BASE = 'https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/single_stations'
STATIONS = {'Augsburg': {'id': '10852', 'elevation_m': 463},
            'Lechfeld': {'id': '10856', 'elevation_m': 555}}
OUTPUT = Path('data/mering_mosmix.json')
MAX_BYTES = 8_000_000
TIMEOUT = 25
# Store original DWD element names without claiming unverified derived meanings.
# Also preserve all available parameters, allowing subsequent model validation.
PREFERRED = ('N', 'Nl', 'Nm', 'Nh', 'SunD1', 'Rad1h', 'VV10', 'wwM', 'wwP',
             'TTT', 'Td', 'FF', 'DD', 'PPPP')

def local(tag):
    return tag.rsplit('}', 1)[-1].rsplit(':', 1)[-1]

def iso_utc(value):
    value = value.strip()
    if not value:
        raise ValueError('Empty forecast timestamp')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Forecast timestamp lacks timezone')
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

def values(raw, count):
    parts = (raw or '').split()
    if len(parts) != count:
        raise ValueError(f'Parameter has {len(parts)} values, expected {count}')
    out = []
    for item in parts:
        if item in ('-', '--', 'NaN', 'nan'):
            out.append(None)
        else:
            number = float(item)
            if not math.isfinite(number):
                raise ValueError('Nonfinite forecast parameter')
            out.append(number)
    return out

def parse_kml(xml_data, expected_id):
    root = ET.fromstring(xml_data)
    timesteps = [iso_utc((el.text or '')) for el in root.iter()
                 if local(el.tag) == 'TimeStep']
    if not timesteps or len(timesteps) != len(set(timesteps)) or timesteps != sorted(timesteps):
        raise ValueError('Missing, repeated or unordered forecast times')
    issue = next((iso_utc(el.text or '') for el in root.iter()
                  if local(el.tag) == 'IssueTime' and el.text), None)
    # Every station file is requested from its station-specific DWD URL.
    # Check a station identifier in the placemark if supplied by the file.
    placemarks = [el for el in root.iter() if local(el.tag) == 'Placemark']
    if len(placemarks) != 1:
        raise ValueError('Expected exactly one station placemark')
    pm = placemarks[0]
    station_names = [(el.text or '').strip() for el in pm.iter()
                     if local(el.tag) == 'name' and el.text]
    if station_names and expected_id not in station_names and not any(expected_id in x for x in station_names):
        # DWD names may be text labels rather than numeric IDs; retain and expose name.
        pass
    parameters = {}
    for forecast in pm.iter():
        if local(forecast.tag) != 'Forecast':
            continue
        key = next((v for k, v in forecast.attrib.items() if local(k) == 'elementName'), None)
        if not key or key in parameters:
            continue
        value_el = next((el for el in forecast.iter() if local(el.tag) == 'value'), None)
        if value_el is None:
            continue
        parameters[key] = values(value_el.text, len(timesteps))
    if not parameters:
        raise ValueError('No MOSMIX forecast parameters in station file')
    return {'issue_time_utc': issue, 'forecast_times_utc': timesteps,
            'station_label': station_names[0] if station_names else None,
            'parameters': parameters,
            'parameter_valid_hours': {k: sum(x is not None for x in v)
                                      for k, v in parameters.items()},
            'available_preferred_parameters': [k for k in PREFERRED if k in parameters]}

def fetch_station(name, info):
    station_id = info['id']
    url = f'{BASE}/{station_id}/kml/MOSMIX_L_LATEST_{station_id}.kmz'
    req = Request(url, headers={'User-Agent': 'mering-weather-mosmix/1.0 (GitHub Actions)'})
    with urlopen(req, timeout=TIMEOUT) as response:
        content = response.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError('DWD KMZ exceeds size limit')
    with zipfile.ZipFile(BytesIO(content)) as archive:
        candidates = [entry for entry in archive.infolist()
                      if entry.filename.lower().endswith('.kml') and not entry.is_dir()]
        if len(candidates) != 1:
            raise ValueError('Expected one KML in DWD KMZ')
        if candidates[0].file_size > 20_000_000:
            raise ValueError('KML uncompressed size limit exceeded')
        kml = archive.read(candidates[0])
    parsed = parse_kml(kml, station_id)
    parsed.update({'status': 'ok', 'station_id': station_id,
                   'reference_elevation_m': info['elevation_m'], 'source_url': url})
    return parsed

def main():
    now = datetime.now(timezone.utc)
    result = {'schema_version': 1, 'source': 'DWD MOSMIX_L station KMZ/KML',
              'retrieved_at_utc': now.isoformat(),
              'interpretation_note': ('Raw DWD forecast parameter codes; missing values are null. '
                  'Do not interpret wwM as a fog probability without verifying DWD parameter definition. '
                  'MOSMIX station values are not direct observations in Mering.'),
              'stations': {}}
    for name, info in STATIONS.items():
        try:
            station = fetch_station(name, info)
            if station['issue_time_utc']:
                age = (now - datetime.fromisoformat(station['issue_time_utc'].replace('Z', '+00:00'))).total_seconds()/3600
                station['issue_age_hours'] = round(age, 2)
                if age < -1 or age > 24:
                    station['status'] = 'stale'
            result['stations'][name] = station
            print(f'{name}: {station["status"]}; {len(station["parameters"])} parameters')
        except (OSError, ValueError, TimeoutError, zipfile.BadZipFile, ET.ParseError) as exc:
            result['stations'][name] = {'status': 'error', 'station_id': info['id'],
                                        'error': str(exc)[:250]}
            print(f'{name}: {exc}', file=sys.stderr)
    result['collection_status'] = ('ok' if all(s['status'] == 'ok' for s in result['stations'].values())
                                    else 'degraded')
    # Publish diagnostics even on DWD outages. Never affect the main model JSON.
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix('.tmp')
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    os.replace(tmp, OUTPUT)

if __name__ == '__main__':
    main()
