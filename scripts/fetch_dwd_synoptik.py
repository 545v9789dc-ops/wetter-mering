#!/usr/bin/env python3
"""Fetch DWD synoptic reports, classify by issuance text and retain valid cache."""
import datetime as dt
import json
import re
import time
import urllib.error
import urllib.request
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

ROOT = 'https://www.dwd.de/DE/fachnutzer/hobbymet/wetter_deutschland/_functions/PlainTeaser_synUebersichten/'
SOURCES = {
    'kurzfrist_frueh': 'nas_bericht_syn_ueb_kurzfrist_frueh.html',
    'kurzfrist_abend': 'nas_bericht_syn_ueb_kurzfrist_abd.html',
    'mittelfrist': 'nas_bericht_syn_ueb_mittelfrist.html',
}
OUT = Path('data/dwd_synoptik.json')
MAX_AGE_HOURS = {'kurzfrist_frueh': 20, 'kurzfrist_abend': 20, 'mittelfrist': 30}
RETRY_TIMEOUTS = (15, 20, 30)
RETRY_DELAYS = (3, 6)


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'nav'):
            self.skip += 1
        elif tag in ('p', 'div', 'h1', 'h2', 'h3', 'br', 'li') and not self.skip:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'nav') and self.skip:
            self.skip -= 1
        elif tag in ('p', 'div', 'h1', 'h2', 'h3') and not self.skip:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def extract_report(html):
    parser = TextParser()
    parser.feed(html)
    text = unescape(' '.join(parser.parts)).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n', text).strip()
    for marker in ('Diese Seite teilen', 'Service-Navigations- Menü'):
        pos = text.find(marker)
        if pos > 700:
            text = text[:pos].strip()
            break
    header = re.search(r'SXEU31\s+DWAV\s+\d{6}|S\s+Y\s+N\s+O\s+P\s+T\s+I\s+S\s+C\s+H\s+E', text, re.I)
    if header:
        text = text[header.start():].strip()
    return text


def issuance(text):
    pattern = (r'ausgegeben\s+am\s+(?:[A-Za-zÄÖÜäöüß]+,\s*den\s+)?'
               r'([0-3]?\d\.[01]?\d\.20\d{2})\s+um\s+'
               r'([0-2]?\d)(?:[:.]([0-5]\d))?\s*(UTC|MESZ|MEZ)\b')
    match = re.search(pattern, text[:2500], re.I)
    if not match:
        return None
    date, hour, minute, zone = match.group(1), int(match.group(2)), int(match.group(3) or '0'), match.group(4).upper()
    try:
        day = dt.datetime.strptime(date, '%d.%m.%Y')
        if hour > 23:
            return None
        offset = {'UTC': 0, 'MEZ': 1, 'MESZ': 2}[zone]
        published = day.replace(hour=hour, minute=minute, tzinfo=dt.timezone(dt.timedelta(hours=offset)))
        return {'date': date, 'time': f'{hour:02d}:{minute:02d}', 'timezone': zone,
                'raw': match.group(0), 'issued_at_utc': published.astimezone(dt.timezone.utc).isoformat()}
    except ValueError:
        return None


def actual_short_key(pub):
    """Identify early/late bulletin from publication time, not source URL."""
    issued = dt.datetime.fromisoformat(pub['issued_at_utc']).astimezone(dt.timezone.utc)
    return 'kurzfrist_frueh' if issued.hour < 12 else 'kurzfrist_abend'


def age_status(entry, key, now):
    publication = entry.get('publication')
    if not publication or not publication.get('issued_at_utc'):
        entry['freshness'] = 'unknown'
        entry['age_hours'] = None
        return
    try:
        issued = dt.datetime.fromisoformat(publication['issued_at_utc'])
        hours = (now - issued).total_seconds() / 3600
    except (ValueError, TypeError):
        entry['freshness'] = 'unknown'
        entry['age_hours'] = None
        return
    entry['age_hours'] = round(hours, 2)
    entry['freshness'] = 'stale' if hours > MAX_AGE_HOURS[key] else 'current'
    if hours < -1.0:
        entry['freshness'] = 'invalid_future_timestamp'


def download(request, entry):
    for attempt, timeout in enumerate(RETRY_TIMEOUTS, 1):
        entry['attempts'] = attempt
        if attempt > 1:
            time.sleep(RETRY_DELAYS[attempt - 2])
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                entry['http_status'] = response.status
                payload = response.read(600_000)
                return payload.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')
        except (TimeoutError, ConnectionError, urllib.error.URLError) as exc:
            entry['last_error'] = f'{type(exc).__name__}: {exc}'
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == len(RETRY_TIMEOUTS):
                raise
    raise RuntimeError('All download attempts exhausted')


def validate_report(key, report, pub):
    if len(report) < 900 or not re.search(r'(synopt|trog|hochdruck|tiefdruck|vorhersage|wetterlage)', report, re.I):
        raise ValueError(f'No plausible synoptic report: {len(report)} characters')
    if not pub:
        raise ValueError('DWD issuance timestamp missing or unparseable')
    opening = report[:2500].lower()
    normal = 'mittelfrist' in opening
    spaced = bool(re.search(r'm\s+i\s+t\s+t\s+e\s+l\s+f\s+r\s+i\s+s\s+t', opening))
    short = bool(re.search(r'k\s+u\s+r\s+z\s+f\s+r\s+i\s+s\s+t', opening)) or 'kurzfrist' in opening
    if key == 'mittelfrist' and not (normal or spaced):
        raise ValueError('Expected medium-term synoptic report')
    if key != 'mittelfrist' and not short:
        raise ValueError('Expected short-term synoptic report')


def effective_status(reports):
    short_keys = ('kurzfrist_frueh', 'kurzfrist_abend')
    candidates = [k for k in short_keys if reports[k].get('publication', {}).get('issued_at_utc')
                  and reports[k].get('freshness') != 'invalid_future_timestamp']
    latest = max(candidates, key=lambda k: reports[k]['publication']['issued_at_utc']) if candidates else None
    short_ok = bool(latest and reports[latest]['freshness'] == 'current'
                    and reports[latest]['status'] in ('ok', 'stale_cached'))
    medium = reports['mittelfrist']
    medium_ok = medium.get('freshness') == 'current' and medium['status'] in ('ok', 'stale_cached')
    if short_ok and medium_ok:
        status = 'ok' if reports[latest]['status'] == 'ok' and medium['status'] == 'ok' else 'degraded'
    elif short_ok or medium_ok:
        status = 'degraded'
    else:
        status = 'error'
    return status, latest, {'short_term': short_ok, 'medium_term': medium_ok}


def main():
    now_dt = dt.datetime.now(dt.timezone.utc)
    now = now_dt.isoformat()
    old = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding='utf-8')).get('reports', {})
        except (OSError, ValueError):
            pass
    reports = {}
    fetched = []
    mismatched = False
    for source_key, filename in SOURCES.items():
        url = ROOT + filename
        entry = {'url': url, 'source_key': source_key, 'checked_at_utc': now,
                 'status': 'error', 'attempts': 0, 'used_cache': False}
        try:
            request = urllib.request.Request(url, headers={
                'User-Agent': 'MeringWeatherResearch/1.0 (noncommercial weather report check)',
                'Accept': 'text/html,text/plain;q=0.9,*/*;q=0.5',
            })
            html = download(request, entry)
            report = extract_report(html)
            pub = issuance(report)
            validate_report(source_key, report, pub)
            actual_key = source_key if source_key == 'mittelfrist' else actual_short_key(pub)
            entry.update(status='ok', fetched_at_utc=now, publication=pub,
                         text_length=len(report), report_text=report)
            if actual_key != source_key:
                mismatched = True
                entry['quality_note'] = f'Source {source_key} delivered {actual_key}; reassigned by issuance timestamp'
                print(f'WARNING: {entry["quality_note"]}')
            fetched.append((actual_key, entry))
        except Exception as exc:
            print(f'{source_key}: download/validation error: {type(exc).__name__}: {exc}')

    # The newest successfully downloaded bulletin wins within each actual type.
    for actual_key, entry in fetched:
        previous = reports.get(actual_key)
        if previous is None or entry['publication']['issued_at_utc'] > previous['publication']['issued_at_utc']:
            reports[actual_key] = entry

    # Reclassify previous cache entries by their actual text, too.
    for original_key, previous in old.items():
        if original_key not in SOURCES or not previous.get('report_text'):
            continue
        try:
            pub = issuance(previous['report_text'])
            validate_report(original_key, previous['report_text'], pub)
            actual_key = original_key if original_key == 'mittelfrist' else actual_short_key(pub)
        except (ValueError, TypeError, KeyError):
            continue
        current = reports.get(actual_key)
        if current is None or pub['issued_at_utc'] > current['publication']['issued_at_utc']:
            cached = dict(previous)
            cached.update(status='stale_cached', used_cache=True, checked_at_utc=now,
                          publication=pub, source_key=previous.get('source_key', original_key))
            reports[actual_key] = cached
            print(f'{actual_key}: previous correctly classified report retained as cache')

    for key in SOURCES:
        if key not in reports:
            reports[key] = {'url': ROOT + SOURCES[key], 'checked_at_utc': now,
                            'status': 'error', 'used_cache': False,
                            'error': 'No correctly classified report available'}
        age_status(reports[key], key, now_dt)
        print(f'{key}: {reports[key]["status"]}, publication={reports[key].get("publication")}')

    status, latest_short, availability = effective_status(reports)
    if mismatched and status == 'ok':
        status = 'degraded'
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({'retrieved_at_utc': now, 'collection_status': status,
                               'latest_short_term': latest_short, 'availability': availability,
                               'reports': reports}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'collection_status={status}; latest_short_term={latest_short}; output={OUT}')
    return 0 if status != 'error' else 1


if __name__ == '__main__':
    raise SystemExit(main())
