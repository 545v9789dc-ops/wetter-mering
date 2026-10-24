#!/usr/bin/env python3
"""Test the DWD morning short-range synoptic overview from GitHub Actions or locally.

Exit 0 if the response contains plausible synoptic report text; exit 1 otherwise.
Does not bypass access controls or write credentials.
"""
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.request
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

URL = "https://www.dwd.de/DE/fachnutzer/hobbymet/wetter_deutschland/_functions/PlainTeaser_synUebersichten/nas_bericht_syn_ueb_kurzfrist_frueh.html"
OUT = Path("data/dwd_synoptik_test.json")

class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

now = dt.datetime.now(dt.timezone.utc).isoformat()
result = {"source": "DWD Kurzfristsynoptik Vormittag", "url": URL, "checked_at_utc": now, "status": "error"}
try:
    request = urllib.request.Request(URL, headers={
        "User-Agent": "MeringWeatherResearch/1.0 (noncommercial weather report check)",
        "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5",
    })
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = response.read(500_000)
        result["http_status"] = response.status
        result["final_url"] = response.url
        encoding = response.headers.get_content_charset() or "utf-8"
        html = payload.decode(encoding, errors="replace")
    parser = TextParser()
    parser.feed(html)
    text = re.sub(r"\s+", " ", unescape(" ".join(parser.parts))).strip()
    result["text_length"] = len(text)
    result["excerpt"] = text[:1500]
    if len(text) >= 500 and re.search(r"synopt|vorhersage|wetterlage|trog|hochdruck|tiefdruck", text, re.I):
        result["status"] = "ok"
        result["report_text"] = text
    else:
        result["status"] = "invalid_content"
except urllib.error.HTTPError as exc:
    result["http_status"] = exc.code
    result["error"] = f"HTTP {exc.code}: {exc.reason}"
except Exception as exc:
    result["error"] = f"{type(exc).__name__}: {exc}"
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({k: v for k, v in result.items() if k != "report_text"}, ensure_ascii=False, indent=2))
sys.exit(0 if result["status"] == "ok" else 1)
