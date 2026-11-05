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
