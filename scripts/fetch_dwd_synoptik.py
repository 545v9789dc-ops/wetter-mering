#!/usr/bin/env python3
"""DWD synoptic reports: text-based publication dating, robust headings, 3-day history."""
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
HISTORY_DAYS = 3
UTC = dt.timezone.utc


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
    header = re.search(r'SXEU31\s+DWAV\s+\d{6}|S\s*Y\s*N\s*O\s*P\s*T\s*I\s*S\s*C\s*H\s*E', text, re.I)
    if header:
        text = text[header.start():].strip()
    return text


def normalized_heading(text):
    """Collapse typography differences for heading checks, without altering stored text."""
    opening = unescape(text[:3000]).replace('\xa0', ' ').casefold()
    # Only match recognizable heading words; allow whitespace between each letter.
    def present(word):
        pattern = r'(?<!\w)' + r'\s*'.join(map(re.escape, word)) + r'(?!\w)'
        return bool(re.search(pattern, opening, re.I))
    return {'mittelfrist': present('mittelfrist'), 'kurzfrist': present('kurzfrist')}


def issuance(text):
    """Read issue time from the bulletin itself; never use fetch time as issue time."""
    # Accept spaces/newlines in the label, punctuation and month/day separators.
    opening = unescape(text[:3000]).replace('\xa0', ' ')
    pattern = (r'ausgegeben\s+am\s+(?:[A-Za-zÄÖÜäöüß]+,?\s*(?:den\s+)?)?'
               r'([0-3]?\d)\s*[.]\s*([01]?\d)\s*[.]\s*(20\d{2})\s+'
               r'um\s+([0-2]?\d)(?:\s*[:.]\s*([0-5]\d))?\s*(UTC|MESZ|MEZ)\b')
    match = re.search(pattern, opening, re.I)
    if not match:
        return None
    day, month, year, hour, minute, zone = match.groups()
    hour, minute, zone = int(hour), int(minute or '0'), zone.upper()
    if hour > 23:
        return None
    try:
        offset = {'UTC': 0, 'MEZ': 1, 'MESZ': 2}[zone]
        published = dt.datetime(int(year), int(month), int(day), hour, minute,
                                tzinfo=dt.timezone(dt.timedelta(hours=offset)))
        return {'date': f'{int(day):02d}.{int(month):02d}.{year}',
                'time': f'{hour:02d}:{minute:02d}', 'timezone': zone,
                'raw': match.group(0), 'issued_at_utc': published.astimezone(UTC).isoformat()}
    except ValueError:
        return None


def actual_short_key(pub):
    issued = dt.datetime.fromisoformat(pub['issued_at_utc']).astimezone(UTC)
    return 'kurzfrist_frueh' if issued.hour < 12 else 'kurzfrist_abend'


def classify_report(text, pub):
    headings = normalized_heading(text)
    if headings['mittelfrist'] and not headings['kurzfrist']:
        return 'mittelfrist'
    if headings['kurzfrist'] and not headings['mittelfrist']:
        return actual_short_key(pub)
    raise ValueError('Report heading ambiguous or missing')


def age_status(entry, key, now):
    publication = entry.get('publication')
    if not publication or not publication.get('issued_at_utc'):
        entry['freshness'], entry['age_hours'] = 'unknown', None
        return
    try:
        issued = dt.datetime.fromisoformat(publication['issued_at_utc'])
        hours = (now - issued).total_seconds() / 3600
    except (ValueError, TypeError):
        entry['freshness'], entry['age_hours'] = 'unknown', None
        return
    entry['age_hours'] = round(hours, 2)
    entry['freshness'] = ('invalid_future_timestamp' if hours < -1 else
                          'stale' if hours > MAX_AGE_HOURS[key] else 'current')


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
    actual = classify_report(report, pub)
    if key == 'mittelfrist' and actual != 'mittelfrist':
        raise ValueError('Expected medium-term synoptic report')
    if key != 'mittelfrist' and actual == 'mittelfrist':
        raise ValueError('Expected short-term synoptic report')
    return actual


def history_items(old):
    """Normalize legacy and archived entries, rejecting invalid text and timestamps."""
    candidates = []
    for entry in list(old.get('history', [])) + list(old.get('reports', {}).values()):
        if not isinstance(entry, dict) or not entry.get('report_text'):
            continue
        try:
            pub = issuance(entry['report_text'])
            if not pub:
                continue
            actual = classify_report(entry['report_text'], pub)
            if len(entry['report_text']) < 900:
                continue
            candidates.append((actual, pub, entry))
        except (TypeError, ValueError, KeyError):
            continue
    return candidates


def select_reports(fetched, old, now):
    """Newest *text-dated* bulletin always wins, including valid cached history."""
    candidates = list(fetched) + history_items(old)
    selected, unique = {}, {}
    # Local calendar days: today and the two previous days.
    from zoneinfo import ZoneInfo
    today = now.astimezone(ZoneInfo('Europe/Berlin')).date()
    for key, pub, entry in candidates:
        try:
            issued = dt.datetime.fromisoformat(pub['issued_at_utc']).astimezone(UTC)
        except (TypeError, ValueError):
            continue
        if issued > now + dt.timedelta(hours=1):
            continue
        day = issued.astimezone(ZoneInfo('Europe/Berlin')).date()
        if not (today - dt.timedelta(days=HISTORY_DAYS - 1) <= day <= today):
            continue
        ident = (key, issued.isoformat())
        prior = unique.get(ident)
        if prior is None or (prior[2].get('status') != 'ok' and entry.get('status') == 'ok'):
            unique[ident] = (key, pub, entry)
    for key, pub, entry in unique.values():
        prev = selected.get(key)
        if prev is None or pub['issued_at_utc'] > prev[0]['issued_at_utc']:
            selected[key] = (pub, entry)
    reports = {}
    for key in SOURCES:
        if key not in selected:
            reports[key] = {'url': ROOT + SOURCES[key], 'checked_at_utc': now.isoformat(),
                            'status': 'error', 'used_cache': False,
                            'error': 'No correctly classified report available'}
            continue
        pub, source = selected[key]
        current_fetch = any(k == key and p['issued_at_utc'] == pub['issued_at_utc']
                            for k, p, _ in fetched)
        entry = dict(source)
        entry.update(publication=pub, status='ok' if current_fetch else 'stale_cached',
                     used_cache=not current_fetch, checked_at_utc=now.isoformat())
        age_status(entry, key, now)
        reports[key] = entry
    history = []
    for key, pub, entry in sorted(unique.values(), key=lambda row: row[1]['issued_at_utc'], reverse=True):
        history.append({'type': key, 'publication': pub, 'report_text': entry['report_text']})
    return reports, history


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
    now_dt = dt.datetime.now(UTC)
    now = now_dt.isoformat()
    old = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
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
            actual = validate_report(source_key, report, pub)
            entry.update(status='ok', fetched_at_utc=now, publication=pub,
                         text_length=len(report), report_text=report)
            if actual != source_key:
                mismatched = True
                entry['quality_note'] = f'Source {source_key} delivered {actual}; reassigned by issuance text'
                print(f'WARNING: {entry["quality_note"]}')
            fetched.append((actual, pub, entry))
        except Exception as exc:
            print(f'{source_key}: download/validation error: {type(exc).__name__}: {exc}')

    reports, history = select_reports(fetched, old, now_dt)
    for key, entry in reports.items():
        print(f'{key}: {entry["status"]}, publication={entry.get("publication")}')
    status, latest_short, availability = effective_status(reports)
    if mismatched and status == 'ok':
        status = 'degraded'
    mediums = [item for item in history if item['type'] == 'mittelfrist']
    previous_medium = None
    if mediums:
        current_day = mediums[0]['publication']['date']
        previous_medium = next((item['publication']['issued_at_utc'] for item in mediums[1:]
                                if item['publication']['date'] != current_day), None)
    result = {'retrieved_at_utc': now, 'collection_status': status,
              'latest_short_term': latest_short, 'availability': availability,
              'latest_medium_term': reports['mittelfrist'].get('publication', {}).get('issued_at_utc'),
              'previous_medium_term': previous_medium, 'history_days': HISTORY_DAYS,
              'history': history, 'reports': reports}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'collection_status={status}; latest_short_term={latest_short}; history={len(history)}')
    return 0 if status != 'error' else 1


if __name__ == '__main__':
    raise SystemExit(main())
