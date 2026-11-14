import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location('prepare_report', Path(__file__).resolve().parents[1] / 'scripts' / 'prepare_report.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class PrepareTests(unittest.TestCase):
    def test_partial_sunshine_is_not_full_day(self):
        station = {'forecast_times_utc': ['2026-11-14T09:00:00Z', '2026-11-14T10:00:00Z'],
                   'parameters': {'SunD1': [1800, 1200], 'RR1c': [0.5, None]}}
        row = p.mosmix_days(station)[0]
        self.assertEqual(row['sunshine_hours']['available_sum'], round(3000/3600, 3))
        self.assertIsNone(row['sunshine_hours']['sum'])
        self.assertEqual(row['precipitation_mm']['valid_hours'], 1)

    def test_ensemble_distributions_reused(self):
        ens = {'models': {'IFS-ENS': {'member_count': 50, 'daily': [
            {'date': '2026-11-14', 'daily_distributions': {'temperature_2m_max': {'p90': 12}}}]}}}
        doc = p.prepare({'models': {}}, {'stations': {}}, ens, '2026-11-14T10:00:00Z')
        self.assertEqual(doc['ensembles']['IFS-ENS']['daily'][0]['daily_distributions']['temperature_2m_max']['p90'], 12)


if __name__ == '__main__':
    unittest.main()
