import unittest
from scripts.winter_events import identify

class WinterEventTests(unittest.TestCase):
    def test_requires_two_models(self):
        row = {'date':'2026-12-01','complete_time_axis':True,'snowfall_cm':3}
        self.assertEqual(identify({'ICON-D2':{'status':'ok','winter_daily':[row]}}), [])
        models = {n:{'status':'ok','winter_daily':[row]} for n in ('ICON-D2','ICON-EU')}
        self.assertTrue(any(e['kind']=='snowfall' for e in identify(models)))
    def test_no_partial_days(self):
        row = {'date':'2026-12-01','complete_time_axis':False,'snowfall_cm':10}
        models = {n:{'status':'ok','winter_daily':[row]} for n in ('ICON-D2','ICON-EU')}
        self.assertEqual(identify(models), [])
    def test_stale_excluded(self):
        row = {'date':'2026-12-01','complete_time_axis':True,'snowfall_cm':5}
        models = {n:{'status':'stale','winter_daily':[row]} for n in ('ICON-D2','ICON-EU')}
        self.assertEqual(identify(models), [])
