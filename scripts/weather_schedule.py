"""Resolve a daily UTC cron occurrence before applying Berlin's local schedule."""
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
TARGETS = {(2, 0), (5, 0), (10, 0), (15, 0), (18, 0), (21, 30)}
CRONS = {f"0 {h} * * *": (h, 0) for h in (0, 1, 3, 4, 8, 9, 13, 14, 16, 17)}
CRONS.update({f"30 {h} * * *": (h, 30) for h in (19, 20)})


def scheduled_time(cron, created_at):
    """Latest occurrence of this daily cron at/before original run creation."""
    if created_at.tzinfo is None:
        raise ValueError("created_at must include a timezone")
    hour, minute = CRONS[cron]  # Unknown triggers must fail, never silently skip.
    created_at = created_at.astimezone(timezone.utc)
    planned = created_at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if planned > created_at:
        planned -= timedelta(days=1)
    return planned


def should_run(event_name, cron="", created_at=None):
    if event_name == "workflow_dispatch":
        return True
    if event_name != "schedule":
        raise ValueError(f"Unsupported event: {event_name}")
    local = scheduled_time(cron, created_at).astimezone(BERLIN)
    # fold=1 identifies only the repeated hour on the actual fallback day.
    return (local.hour, local.minute) in TARGETS and local.fold == 0


def run_created_at():
    """Original creation time is stable across runner queues and re-runs."""
    url = (f"{os.environ['GITHUB_API_URL']}/repos/{os.environ['GITHUB_REPOSITORY']}"
           f"/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    request = Request(url, headers={
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json",
    })
    with urlopen(request, timeout=30) as response:
        value = json.load(response)["created_at"]
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main():
    event = os.environ["EVENT_NAME"]
    cron = os.environ.get("EVENT_SCHEDULE", "")
    created_at = run_created_at() if event == "schedule" else None
    run = should_run(event, cron, created_at)
    if created_at is not None:
        planned = scheduled_time(cron, created_at)
        print(f"Cron: {cron}; created: {created_at.isoformat()}; "
              f"planned UTC: {planned.isoformat()}; Berlin: {planned.astimezone(BERLIN).isoformat()}")
    print(f"Event: {event}; run={str(run).lower()}")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"run={str(run).lower()}\n")


if __name__ == "__main__":
    main()
