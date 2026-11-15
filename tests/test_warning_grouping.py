"""Offline regression tests: compact GitHub warnings and per-model age ceilings."""
import unittest
from datetime import datetime, timedelta, timezone
from scripts.validate_data_quality import check_models, summarize_fallback_warnings

UTC = timezone.utc
NOW = datetime(2026, 11, 15, 15, tzinfo=UTC)


def fixture():
    times = [(NOW + timedelta(hours=i + 1)).isoformat() for i in range(24)]
    vals = {'temperature_2m': [8] * 24,
            'precipitation': [0] * 24,
            'wind_speed_10m': [12] * 24,
            'wind_gusts_10m': [25] * 24,
            'pressure_msl': [1011] * 24}
    fields = list(vals)
    return {'status': 'ok', 'api_model': 'gfs_seamless',
            'model_run_utc': (NOW - timedelta(hours=3)).isoformat(),
            'winter_daily': [], 'winter_rolling_max': {'1': {}},
            'hourly_units': {'precipitation': 'mm'},
            'hourly': {'time': times, **vals},
            'parameter_fallback': {
                name: {'status': 'fallback', 'recovered_hours': 2,
                       'recovered_indices': [0, 1],
                       'sources': [{'source_model': 'gfs_seamless',
                                    'source_run_utc': (NOW - timedelta(hours=6)).isoformat(),
                                    'recovered_hours': 2}]}
                for name in fields}}


class CompactWarningsTests(unittest.TestCase):
    def test_five_parameters_produce_one_grouped_warning(self):
        rec = fixture()
        critical, warnings = check_models({'models': {'GFS': rec}}, NOW)
        self.assertEqual(critical, [])
        groups = [w for w in warnings if w.startswith('GFS: fallback in')]
        self.assertEqual(len(groups), 1, warnings)
        self.assertIn('5 parameters', groups[0])
        self.assertIn('10 hourly values', groups[0])
        self.assertIn('gusts=2h', groups[0])
        self.assertFalse(any('replacement hours from' in w for w in warnings))

    def test_bad_age_still_has_specific_warning(self):
        rec = fixture()
        rec['parameter_fallback']['wind_gusts_10m']['sources'][0]['source_run_utc'] = (
            NOW - timedelta(hours=25)).isoformat()
        _, warnings = check_models({'models': {'GFS': rec}}, NOW)
        self.assertTrue(any('GFS/wind_gusts_10m: fallback older than 24h' in w
                            for w in warnings), warnings)
        self.assertTrue(any('GFS: fallback in 5 parameters' in w for w in warnings))

    def test_missing_parameter_still_warns(self):
        rec = fixture()
        rec['hourly']['wind_gusts_10m'] = [None] * 24
        rec['parameter_fallback'].pop('wind_gusts_10m')
        _, warnings = check_models({'models': {'GFS': rec}}, NOW)
        self.assertTrue(any('GFS/wind_gusts_10m: not available' in w
                            for w in warnings), warnings)

    def test_no_replacements_no_group_warning(self):
        rec = fixture()
        rec['parameter_fallback'] = {}
        self.assertIsNone(summarize_fallback_warnings(rec, 'GFS', NOW))


if __name__ == '__main__':
    unittest.main()
