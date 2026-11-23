"""Validate Europe/Berlin cron events and report GitHub scheduler delays."""
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo('Europe/Berlin')
CRONS = {f'20 {h} * * *': (h, 20) for h in (4, 9, 14, 18)}

def scheduled_time(cron, created_at):
    """Find latest scheduled Berlin wall-clock slot, including DST changes."""
    if created_at.tzinfo is None:
        raise ValueError('created_at must include a timezone')
    hour, minute = CRONS[cron]
    created_utc = created_at.astimezone(timezone.utc)
    local_date = created_utc.astimezone(BERLIN).date()
    candidates = []
    for days_back in (0, 1, 2):
        day = local_date - timedelta(days=days_back)
        for fold in (0, 1):
            local = datetime(day.year, day.month, day.day, hour, minute,
                             tzinfo=BERLIN, fold=fold)
            candidate = local.astimezone(timezone.utc)
            # Reject nonexistent local times during spring DST transition.
            if candidate.astimezone(BERLIN).replace(tzinfo=None) != local.replace(tzinfo=None):
                continue
            if candidate <= created_utc:
                candidates.append(candidate)
    if not candidates:
        raise ValueError('No valid schedule candidate found')
    return max(candidates)

def should_run(event_name, cron='', created_at=None):
    if event_name == 'workflow_dispatch':
        return True
    if event_name != 'schedule':
        raise ValueError(f'Unsupported event: {event_name}')
    return cron in CRONS

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
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        output.write(f'run={str(run).lower()}\n')

if __name__ == '__main__':
    main()
