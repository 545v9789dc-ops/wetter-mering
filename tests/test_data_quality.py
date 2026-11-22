"""Offline regression tests for the Mering post-fetch quality checks."""
import unittest
from scripts.validate_data_quality import plausible, time_axis, check_models, check_mosmix, check_ensembles
from scripts.annotate_dwd_quality import annotate
from datetime import datetime, timezone


class DataQualityTests(unittest.TestCase):
    def test_reject_duplicate_times(self):
        with self.assertRaises(ValueError):
            time_axis(['2026-11-05T00:00:00Z', '2026-11-05T00:00:00Z'])

    def test_reject_non_utc_naive_time(self):
        with self.assertRaises(ValueError):
            time_axis(['2026-11-05T00:00:00'])

    def test_accept_naive_utc_for_model_times(self):
        times = time_axis(['2026-11-05T00:00:00', '2026-11-05T01:00:00'], assume_utc=True)
        self.assertEqual(times[0].utcoffset().total_seconds(), 0)

    def test_reject_duplicate_naive_model_times(self):
        with self.assertRaises(ValueError):
            time_axis(['2026-11-05T00:00:00', '2026-11-05T00:00:00'], assume_utc=True)

    def test_ensemble_daily_list_is_supported(self):
        doc = {'models': {'GFS-ENS': {'daily': [{'date': '2026-11-05'}]},
                          'IFS-ENS': {'daily': [{'date': '2026-11-05'}]}}}
        self.assertEqual(check_ensembles(doc), [])

    def test_detect_negative_rain(self):
        h = {'time': ['2026-11-05T00:00:00Z'], 'precipitation': [-1]}
        self.assertTrue(plausible(h))

    def test_null_is_allowed(self):
        h = {'time': ['2026-11-05T00:00:00Z'], 'precipitation': [None]}
        self.assertEqual(plausible(h), [])

    def test_no_models_is_critical(self):
        critical, warnings = check_models({'models': {}}, datetime.now(timezone.utc))
        self.assertTrue(critical)
        self.assertTrue(warnings)

    def test_ukmo_missing_is_warning_not_critical(self):
        critical, warnings = check_models({'models': {'ICON-D2': {'status': 'ok',
            'hourly': {'time': ['2026-11-22T23:00:00Z'],
                       'temperature_2m': [1], 'precipitation': [0],
                       'wind_speed_10m': [5], 'wind_gusts_10m': [10],
                       'pressure_msl': [1010]},
            'model_run_utc': '2026-11-22T18:00:00Z'}}},
            datetime(2026, 11, 22, 20, tzinfo=timezone.utc))
        self.assertTrue(any('UKMO: missing/unverified' in w for w in warnings))
        self.assertFalse(any('no fresh main model' in c for c in critical))

    def test_mosmix_missing_is_warning(self):
        self.assertTrue(check_mosmix({'stations': {}}))

    def test_ensembles_missing_is_warning(self):
        self.assertTrue(check_ensembles({'models': {}}))

    def test_dwd_cache_keeps_collection_status(self):
        doc = {'collection_status': 'degraded', 'latest_short_term': 'kurzfrist_abend',
               'availability': {'short_term': True, 'medium_term': True},
               'reports': {'kurzfrist_abend': {'freshness': 'current', 'status': 'ok'},
                           'kurzfrist_frueh': {'used_cache': True}}}
        annotate(doc)
        self.assertEqual(doc['collection_status'], 'degraded')
        self.assertTrue(doc['quality_details']['latest_short_term_usable'])
        self.assertEqual(doc['quality_details']['cached_reports'], ['kurzfrist_frueh'])


if __name__ == '__main__':
    unittest.main()
