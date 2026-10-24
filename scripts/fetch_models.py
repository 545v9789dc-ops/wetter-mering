#!/usr/bin/env python3
"""Run from repository root to patch scripts/fetch_models.py."""
from pathlib import Path
p=Path("scripts/fetch_models.py")
s=p.read_text(encoding="utf-8")
if "TRANSIENT_RETRIES = 2" in s:
    print("Already patched")
    raise SystemExit
assert "REQUEST_TIMEOUT_SECONDS = 12" in s
start=s.index("def fetch_json(url):")
end=s.index("\ndef api_url(",start)
replacement = """def fetch_json(url):
    for attempt in range(TRANSIENT_RETRIES + 1):
        try:
            with urlopen(Request(url, headers={"User-Agent": "wetter-mering/4.0"}),
                         timeout=REQUEST_TIMEOUT_SECONDS) as res:
                return json.load(res)
        except HTTPError as exc:
            try:
                body = exc.read(2048).decode("utf-8", errors="replace")
                reason = json.loads(body).get("reason", body)
            except (OSError, ValueError, TypeError):
                reason = "API did not return a readable error reason"
            if exc.code not in (429, 500, 502, 503, 504) or attempt == TRANSIENT_RETRIES:
                raise ValueError(f"HTTP {exc.code}: {str(reason)[:240]}") from exc
        except (URLError, TimeoutError, OSError):
            if attempt == TRANSIENT_RETRIES:
                raise
        time.sleep(2 ** attempt)
"""
s=s[:start]+replacement+s[end:]
s=s.replace("REQUEST_TIMEOUT_SECONDS = 12","REQUEST_TIMEOUT_SECONDS = 25\nTRANSIENT_RETRIES = 2")
compile(s,str(p),"exec")
p.write_text(s,encoding="utf-8")
print("Updated",p)
