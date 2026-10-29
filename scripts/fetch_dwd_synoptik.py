#!/usr/bin/env python3
"""Fetch DWD synoptic reports with cached fallback and effective freshness checks."""
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
    if key == 'mittelfrist' and 'mittelfrist' not in opening:
        raise ValueError('Expected medium-term synoptic report')
    if key != 'mittelfrist' and 'mittelfrist' in opening and 'kurzfrist' not in opening:
        raise ValueError('Unexpected medium-term report on short-term source')


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
    for key, filename in SOURCES.items():
        url = ROOT + filename
        entry = {'url': url, 'checked_at_utc': now, 'status': 'error',
                 'attempts': 0, 'used_cache': False}
        try:
            request = urllib.request.Request(url, headers={
                'User-Agent': 'MeringWeatherResearch/1.0 (noncommercial weather report check)',
                'Accept': 'text/html,text/plain;q=0.9,*/*;q=0.5',
            })
            html = download(request, entry)
            report = extract_report(html)
            pub = issuance(report)
            validate_report(key, report, pub)
            entry.update(status='ok', fetched_at_utc=now, publication=pub,
                         text_length=len(report), report_text=report)
            previous = old.get(key, {})
            previous_length = previous.get('text_length')
            if isinstance(previous_length, int) and previous_length > 0 and len(report) < previous_length * 0.5:
                entry['quality_note'] = 'Report text less than half the previous length; review recommended'
        except Exception as exc:
            entry['error'] = f'{type(exc).__name__}: {exc}'
            entry['last_error'] = entry['error']
            previous = old.get(key, {})
            if previous.get('report_text'):
                entry['status'] = 'stale_cached'
                entry['used_cache'] = True
                entry['report_text'] = previous['report_text']
                entry['publication'] = issuance(previous['report_text']) or previous.get('publication')
                entry['fetched_at_utc'] = previous.get('fetched_at_utc')
                entry['text_length'] = len(previous['report_text'])
        age_status(entry, key, now_dt)
        reports[key] = entry
        print(f'{key}: {entry["status"]}, HTTP {entry.get("http_status", "n/a")}, publication={entry.get("publication")}, attempts={entry["attempts"]}')
    status, latest_short, availability = effective_status(reports)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({'retrieved_at_utc': now, 'collection_status': status,
                               'latest_short_term': latest_short, 'availability': availability,
                               'reports': reports}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'collection_status={status}; latest_short_term={latest_short}; output={OUT}')
    return 0 if status != 'error' else 1


if __name__ == '__main__':
    raise SystemExit(main())
