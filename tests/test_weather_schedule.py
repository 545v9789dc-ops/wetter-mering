import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'weather_schedule',
    Path(__file__).resolve().parents[1] / 'scripts' / 'weather_schedule.py',
)
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


class ScheduleTests(unittest.TestCase):
    def test_fixed_utc_slots_in_winter(self):
        for hour in (4, 9, 14, 18):
            with self.subTest(hour=hour):
                cron = f'20 {hour} * * *'
                created = datetime(2026, 11, 5, hour, 45, tzinfo=timezone.utc)
                self.assertTrue(s.should_run('schedule', cron, created))

    def test_fixed_utc_slots_in_summer(self):
        for hour in (4, 9, 14, 18):
            with self.subTest(hour=hour):
                cron = f'20 {hour} * * *'
                created = datetime(2026, 7, 5, hour, 45, tzinfo=timezone.utc)
                self.assertTrue(s.should_run('schedule', cron, created))

    def test_old_seasonal_slots_are_rejected(self):
        for cron in ('0 3 * * *', '0 4 * * *', '30 20 * * *'):
            with self.subTest(cron=cron):
                self.assertFalse(s.should_run('schedule', cron, datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)))

    def test_manual(self):
        self.assertTrue(s.should_run('workflow_dispatch'))

    def test_scheduled_time_and_delay(self):
        created = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)
        expected = datetime(2026, 11, 5, 4, 20, tzinfo=timezone.utc)
        self.assertEqual(s.scheduled_time('20 4 * * *', created), expected)


if __name__ == '__main__':
    unittest.main()
