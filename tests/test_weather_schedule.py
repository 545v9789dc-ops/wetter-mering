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
    def test_local_slots_winter_and_summer(self):
        for month in (1, 7, 11):
            for hour in (4, 9, 14, 18):
                with self.subTest(month=month, hour=hour):
                    cron = f'20 {hour} * * *'
                    local = datetime(2026, month, 5, hour, 20, tzinfo=s.BERLIN)
                    created = local.astimezone(timezone.utc)
                    self.assertTrue(s.should_run('schedule', cron, created))
                    self.assertEqual(s.scheduled_time(cron, created), created)

    def test_old_utc_slots_rejected(self):
        for cron in ('20 3 * * *', '20 8 * * *', '20 13 * * *', '20 17 * * *'):
            with self.subTest(cron=cron):
                self.assertFalse(s.should_run('schedule', cron))

    def test_manual(self):
        self.assertTrue(s.should_run('workflow_dispatch'))

    def test_delay_across_dst(self):
        for month in (1, 7):
            with self.subTest(month=month):
                local = datetime(2026, month, 5, 4, 20, tzinfo=s.BERLIN)
                expected = local.astimezone(timezone.utc)
                self.assertEqual(s.scheduled_time('20 4 * * *', expected.replace(minute=expected.minute+5)), expected)

    def test_previous_day(self):
        created = datetime(2026, 11, 5, 0, 0, tzinfo=timezone.utc)
        expected = datetime(2026, 11, 4, 17, 20, tzinfo=timezone.utc)
        self.assertEqual(s.scheduled_time('20 18 * * *', created), expected)


if __name__ == '__main__':
    unittest.main()
