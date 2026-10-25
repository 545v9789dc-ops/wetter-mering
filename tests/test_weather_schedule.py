import os
from datetime import datetime, timedelta
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from scripts import weather_schedule as clock


def utc(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class ScheduleTests(unittest.TestCase):
    def test_three_years_all_crons_and_delays(self):
        day = utc('2025-01-01T00:00:00Z')
        while day.year < 2028:
            accepted = []
            for cron, (hour, minute) in clock.CRONS.items():
                planned = day.replace(hour=hour, minute=minute)
                local = planned.astimezone(clock.BERLIN)
                expected = (local.hour, local.minute) in clock.TARGETS and not local.fold
                for delay in (0, 31, 95, 360, 1439):
                    created = planned + timedelta(minutes=delay)
                    self.assertEqual(clock.scheduled_time(cron, created), planned)
                    self.assertEqual(clock.should_run('schedule', cron, created), expected)
                if expected:
                    accepted.append((local.hour, local.minute))
            spring_day = day.month == 3 and day.weekday() == 6 and (day + timedelta(days=7)).month == 4
            self.assertEqual(len(accepted), 5 if spring_day else 6, day)
            self.assertEqual(len(accepted), len(set(accepted)), day)
            day += timedelta(days=1)

    def test_fallback_delayed_first_and_second_occurrence(self):
        self.assertTrue(clock.should_run('schedule', '0 0 * * *', utc('2026-10-25T02:45:00Z')))
        self.assertFalse(clock.should_run('schedule', '0 1 * * *', utc('2026-10-25T02:45:00Z')))
        self.assertTrue(clock.should_run('schedule', '0 1 * * *', utc('2026-10-26T02:45:00Z')))

    def test_spring_gap(self):
        for cron in ('0 0 * * *', '0 1 * * *'):
            self.assertFalse(clock.should_run('schedule', cron, utc('2026-03-29T04:00:00Z')))
        self.assertTrue(clock.should_run('schedule', '0 3 * * *', utc('2026-03-29T04:00:00Z')))

    def test_midnight_delay(self):
        self.assertTrue(clock.should_run('schedule', '30 20 * * *', utc('2026-01-16T01:00:00Z')))

    def test_dispatch_without_api(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            with patch.dict(os.environ, {'EVENT_NAME': 'workflow_dispatch', 'GITHUB_OUTPUT': str(output)}, clear=True), patch.object(clock, 'run_created_at', side_effect=AssertionError('API must not be used')):
                clock.main()
            self.assertEqual(output.read_text(), 'run=true\n')

    def test_schedule_uses_original_creation_time(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            env = {'EVENT_NAME': 'schedule', 'EVENT_SCHEDULE': '0 0 * * *', 'GITHUB_OUTPUT': str(output)}
            with patch.dict(os.environ, env, clear=True), patch.object(clock, 'run_created_at', return_value=utc('2026-10-25T02:45:00Z')):
                clock.main()
            self.assertEqual(output.read_text(), 'run=true\n')

    def test_api_timestamp(self):
        env = {'GITHUB_API_URL': 'https://api.github.com', 'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '123', 'GH_TOKEN': 'test'}
        with patch.dict(os.environ, env, clear=True), patch.object(clock, 'urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = b'{"created_at":"2026-10-25T00:45:00Z"}'
            self.assertEqual(clock.run_created_at(), utc('2026-10-25T00:45:00Z'))
            self.assertEqual(request.call_args.args[0].full_url, 'https://api.github.com/repos/owner/repo/actions/runs/123')

    def test_bad_inputs_fail(self):
        with self.assertRaises(KeyError):
            clock.should_run('schedule', '0 0,1 * * *', utc('2026-01-01T02:00:00Z'))
        with self.assertRaises(ValueError):
            clock.should_run('push')
        with self.assertRaises(ValueError):
            clock.scheduled_time('0 0 * * *', datetime(2026, 1, 1))

    def test_workflow_wiring(self):
        workflow = Path('.github/workflows/weather.yml').read_text()
        self.assertEqual(set(re.findall(r'cron: "([^"]+)"', workflow)), set(clock.CRONS))
        self.assertIn('EVENT_SCHEDULE: ${{ github.event.schedule }}', workflow)
        self.assertIn('workflow_dispatch:', workflow)
        self.assertIn('actions: read', workflow)


if __name__ == '__main__':
    unittest.main()
