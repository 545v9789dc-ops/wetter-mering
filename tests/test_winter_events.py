import unittest
from scripts.winter_events import identify

class WinterEventTests(unittest.TestCase):
    def test_requires_two_models(self):
        row = {'date':'2026-12-01','complete_time_axis':True,'snowfall_cm':3}
        self.assertEqual(identify({'ICON-D2':{'status':'ok','winter_daily':[row]}}), [])
        models = {n:{'status':'ok','winter_daily':[row]} for n in ('ICON-D2','ICON-EU')}
        self.assertTrue(any(e['kind']=='snowfall' for e in identify(models)))
    def test_agreement_fraction_with_ukmo(self):
        row = {'date':'2026-12-01','complete_time_axis':True,'snowfall_cm':3}
        dry = {'date':'2026-12-01','complete_time_axis':True,'snowfall_cm':0}
        models = {n:{'status':'ok','winter_daily':[r]} for n,r in
                  (('ICON-D2',row),('ICON-EU',row),('UKMO',dry))}
        event = next(e for e in identify(models) if e['kind']=='snowfall')
        self.assertEqual(event['supporting_model_count'], 2)
        self.assertEqual(event['available_model_count'], 3)
        self.assertEqual(event['agreement_fraction'], 0.667)

    def test_no_partial_days(self):
        row = {'date':'2026-12-01','complete_time_axis':False,'snowfall_cm':10}
        models = {n:{'status':'ok','winter_daily':[row]} for n in ('ICON-D2','ICON-EU')}
        self.assertEqual(identify(models), [])
    def test_stale_excluded(self):
        row = {'date':'2026-12-01','complete_time_axis':True,'snowfall_cm':5}
        models = {n:{'status':'stale','winter_daily':[row]} for n in ('ICON-D2','ICON-EU')}
        self.assertEqual(identify(models), [])
