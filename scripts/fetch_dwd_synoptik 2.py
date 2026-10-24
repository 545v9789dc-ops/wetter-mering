#!/usr/bin/env python3
"""Fetch the three public DWD synoptic reports for Mering weather analysis."""
import datetime as dt
import json
import re
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
MAX_AGE_HOURS = {'kurzfrist_frueh': 36, 'kurzfrist_abend': 42, 'mittelfrist': 60}

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
    # Exclude page footer when present; retain source text before it.
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
    issued = dt.datetime.fromisoformat(publication['issued_at_utc'])
    hours = round((now - issued).total_seconds() / 3600, 2)
    entry['age_hours'] = hours
    entry['freshness'] = 'stale' if hours > MAX_AGE_HOURS[key] else 'current'
    if hours < -0.5:
        entry['freshness'] = 'invalid_future_timestamp'

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
        entry = {'url': url, 'checked_at_utc': now, 'status': 'error'}
        try:
            request = urllib.request.Request(url, headers={
                'User-Agent': 'MeringWeatherResearch/1.0 (noncommercial weather report check)',
                'Accept': 'text/html,text/plain;q=0.9,*/*;q=0.5',
            })
            with urllib.request.urlopen(request, timeout=30) as response:
                entry['http_status'] = response.status
                payload = response.read(600_000)
                html = payload.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')
            report = extract_report(html)
            if len(report) < 900 or not re.search(r'(synopt|trog|hochdruck|tiefdruck|vorhersage|wetterlage)', report, re.I):
                raise ValueError(f'No plausible synoptic report: {len(report)} characters')
            pub = issuance(report)
            if not pub:
                raise ValueError('DWD issuance timestamp missing or unparseable')
            entry.update(status='ok', fetched_at_utc=now, publication=pub,
                         text_length=len(report), report_text=report)
        except Exception as exc:
            entry['error'] = f'{type(exc).__name__}: {exc}'
            previous = old.get(key, {})
            if previous.get('report_text'):
                entry['status'] = 'stale_cached'
                entry['report_text'] = previous['report_text']
                entry['publication'] = issuance(previous['report_text']) or previous.get('publication')
                entry['fetched_at_utc'] = previous.get('fetched_at_utc')
                entry['text_length'] = len(previous['report_text'])
        age_status(entry, key, now_dt)
        reports[key] = entry
        print(f'{key}: {entry["status"]}, HTTP {entry.get("http_status", "n/a")}, publication={entry.get("publication")}')
    statuses = [r['status'] for r in reports.values()]
    status = 'ok' if all(x == 'ok' for x in statuses) and all(r['freshness'] == 'current' for r in reports.values()) else 'degraded' if any(x in ('ok', 'stale_cached') for x in statuses) else 'error'
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({'retrieved_at_utc': now, 'collection_status': status, 'reports': reports}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'collection_status={status}; output={OUT}')
    return 0 if status != 'error' else 1

if __name__ == '__main__':
    raise SystemExit(main())
