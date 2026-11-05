"""Identify scheduled UTC slot by original run creation, not runner start time."""
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo('Europe/Berlin')
TARGETS = {(2, 0), (5, 0), (10, 0), (15, 0), (18, 0), (21, 30)}
CRONS = {f'0 {h} * * *': (h, 0) for h in (0, 1, 3, 4, 8, 9, 13, 14, 16, 17)}
CRONS.update({f'30 {h} * * *': (h, 30) for h in (19, 20)})

def scheduled_time(cron, created_at):
    if created_at.tzinfo is None:
        raise ValueError('created_at must include a timezone')
    hour, minute = CRONS[cron]
    created_at = created_at.astimezone(timezone.utc)
    planned = created_at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if planned > created_at:
        planned -= timedelta(days=1)
    return planned

def should_run(event_name, cron='', created_at=None):
    if event_name == 'workflow_dispatch':
        return True
    if event_name != 'schedule':
        raise ValueError(f'Unsupported event: {event_name}')
    local = scheduled_time(cron, created_at).astimezone(BERLIN)
    return (local.hour, local.minute) in TARGETS and local.fold == 0

def run_created_at():
    url = (f"{os.environ['GITHUB_API_URL']}/repos/{os.environ['GITHUB_REPOSITORY']}"
           f"/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    request = Request(url, headers={
        'Authorization': f"Bearer {os.environ['GH_TOKEN']}",
        'Accept': 'application/vnd.github+json',
    })
    for attempt in range(3):
        try:
            with urlopen(request, timeout=15) as response:
                return datetime.fromisoformat(json.load(response)['created_at'].replace('Z', '+00:00'))
        except (OSError, ValueError, KeyError):
            if attempt == 2:
                raise
            import time
            time.sleep(2 * (attempt + 1))

def main():
    event = os.environ['EVENT_NAME']
    cron = os.environ.get('EVENT_SCHEDULE', '')
    created = run_created_at() if event == 'schedule' else None
    run = should_run(event, cron, created)
    if created is not None:
        planned = scheduled_time(cron, created)
        delay = round((created - planned).total_seconds() / 60, 1)
        print(f'Cron: {cron}; created: {created.isoformat()}; planned UTC: {planned.isoformat()}; '
              f'Berlin: {planned.astimezone(BERLIN).isoformat()}; queue delay: {delay} min')
        if delay > 30:
            print(f'::warning::GitHub schedule created {delay} minutes after UTC cron slot.')
        if delay > 90:
            print('::warning::Very late GitHub schedule; verify data collection and downstream reporting deadlines.')
    print(f'Event: {event}; run={str(run).lower()}')
    if not run:
        print('Seasonal UTC duplicate skipped (based on ORIGINAL scheduled UTC slot, not current local time).')
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        output.write(f'run={str(run).lower()}\n')

if __name__ == '__main__':
    main()
