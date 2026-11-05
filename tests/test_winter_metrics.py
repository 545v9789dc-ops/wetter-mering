import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location('winter_metrics', Path(__file__).resolve().parents[1] / 'scripts' / 'winter_metrics.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

class WinterTests(unittest.TestCase):
    def record(self, start, n=48):
        times = [(start + timedelta(hours=i)).strftime('%Y-%m-%dT%H:%M') for i in range(1, n + 1)]
        return {'hourly_units': {'temperature_2m':'°C','precipitation':'mm','snowfall':'cm','snow_depth':'m'},
                'hourly': {'time': times, 'temperature_2m':[-2]*n, 'precipitation':[0.5]*n,
                           'snowfall':[0.1]*n, 'snow_depth':[0.04]*n}}

    def test_full_day(self):
        r = self.record(datetime(2026, 1, 1, tzinfo=timezone.utc), 48)
        days = w.winter_summary(r)
        # Local winter days are UTC+1; only Jan 2 is complete.
        d = next(x for x in days if x['date'] == '2026-01-02')
        self.assertEqual(d['precipitation_mm'], 12)
        self.assertEqual(d['snowfall_cm'], 2.4)
        self.assertEqual(d['snow_depth_max_cm'], 4)
        self.assertTrue(d['ice_day'])
        self.assertIsNone(days[0]['precipitation_mm'])

    def test_missing_depth_not_zero(self):
        r = self.record(datetime(2026, 1, 1), 48)
        del r['hourly']['snow_depth']
        self.assertIsNone(w.winter_summary(r)[1]['snow_depth_max_cm'])

    def test_depth_change(self):
        r = self.record(datetime(2026, 1, 1), 48)
        day = next(x for x in w.winter_summary(r) if x['date']=='2026-01-02')
        self.assertEqual(day['snow_depth_change_cm'], 0)

    def test_rolling_window(self):
        r = self.record(datetime(2026, 1, 1), 48)
        result = w.rolling_totals(r, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(result['24']['precipitation_mm'], 12)
        self.assertEqual(result['48']['snowfall_cm'], 4.8)
        self.assertIsNone(result['72']['precipitation_mm'])

    def test_no_frost_hours_field(self):
        r = self.record(datetime(2026, 1, 1), 48)
        self.assertNotIn('frost_hours', w.winter_summary(r)[0])

    def test_dst_spring_day_has_23_hours(self):
        r = self.record(datetime(2026, 3, 28), 72)
        day = next(x for x in w.winter_summary(r) if x['date'] == '2026-03-29')
        self.assertEqual(day['expected_hours'], 23)

if __name__ == '__main__':
    unittest.main()
