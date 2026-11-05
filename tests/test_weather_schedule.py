import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path
spec = importlib.util.spec_from_file_location('weather_schedule', Path(__file__).resolve().parents[1] / 'scripts' / 'weather_schedule.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)
class ScheduleTests(unittest.TestCase):
    def test_winter_slot(self):
        self.assertTrue(s.should_run('schedule', '0 4 * * *', datetime(2026, 11, 5, 4, 45, tzinfo=timezone.utc)))
        self.assertFalse(s.should_run('schedule', '0 3 * * *', datetime(2026, 11, 5, 3, 45, tzinfo=timezone.utc)))
    def test_summer_slot(self):
        self.assertTrue(s.should_run('schedule', '0 3 * * *', datetime(2026, 7, 5, 3, 30, tzinfo=timezone.utc)))
    def test_manual(self):
        self.assertTrue(s.should_run('workflow_dispatch'))
if __name__ == '__main__':
    unittest.main()
