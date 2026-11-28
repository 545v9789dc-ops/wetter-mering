import unittest
from scripts.winter_events import identify


class WinterEventTests(unittest.TestCase):
    def test_single_model_screen(self):
        row = {'date':'2026-12-01','complete_time_axis':True,'snowfall_cm':3}
        event = identify({'ICON-D2':{'status':'ok','winter_daily':[row]}})[0]
        self.assertEqual(event['supporting_model_count'], 1)

    def test_agreement_fraction_with_ukmo(self):
        row = {'date':'2026-12-01','complete_time_axis':True,'snowfall_cm':3}
        dry = {**row, 'snowfall_cm':0}
        models = {n:{'status':'ok','winter_daily':[r]} for n,r in [('ICON-D2',row),('ICON-EU',row),('UKMO',dry)]}
        event = next(e for e in identify(models) if e['kind']=='snowfall')
        self.assertEqual(event['supporting_model_count'], 2)
        self.assertEqual(event['available_model_count'], 3)
        self.assertEqual(event['agreement_fraction'], 0.667)

    def test_no_partial_daily_totals(self):
        row = {'date':'2026-12-01','complete_time_axis':False,'snowfall_cm':10}
        self.assertEqual(identify({'ICON-D2':{'status':'ok','winter_daily':[row]}}), [])

    def test_stale_excluded(self):
        self.assertEqual(identify({'ICON-D2':{'status':'stale','winter_daily':[{'date':'2026-12-01','complete_time_axis':True,'snowfall_cm':5}]}}), [])

    def period(self):
        return {'start_utc':'2026-11-29T23:00:00Z','end_utc':'2026-11-30T00:00:00Z'}

    def test_explicit_icing_partial_day(self):
        row = {**self.period(), 'kind':'explicit_freezing_precipitation','fallback_fields':['weather_code']}
        event = identify({'ICON-D2':{'status':'ok','hourly_signals':{'icing_periods':[row]}}})[0]
        self.assertEqual(event['date'], '2026-11-30')
        self.assertEqual(event['fallback_fields'], ['weather_code'])
        self.assertNotIn('agreement_fraction', event)

    def test_indirect_icing(self):
        row = {**self.period(), 'kind':'near_freezing_liquid_precipitation'}
        self.assertEqual(identify({'IFS':{'status':'ok','hourly_signals':{'icing_periods':[row]}}})[0]['kind'], row['kind'])

    def test_rolling_gust(self):
        row = {**self.period(), 'value':85,'complete_windows':1}
        event = identify({'GFS':{'status':'ok','winter_rolling_max':{'1':{'wind_gusts_kmh':row}}}})[0]
        self.assertEqual(event['kind'], 'rolling_wind_gusts_kmh')

    def test_missing_window(self):
        row = {**self.period(), 'value':85,'complete_windows':0}
        self.assertEqual(identify({'GFS':{'status':'ok','winter_rolling_max':{'1':{'wind_gusts_kmh':row}}}}), [])

    def test_invalid_window_duration(self):
        row = {**self.period(), 'value':30,'complete_windows':1}
        self.assertEqual(identify({'GFS':{'status':'ok','winter_rolling_max':{'6':{'precipitation_mm':row}}}}), [])

    def test_null_not_zero(self):
        row = {'date':'2026-12-01','complete_time_axis':True,'snowfall_cm':None}
        self.assertEqual(identify({'GFS':{'status':'ok','winter_daily':[row]}}), [])


if __name__ == '__main__':
    unittest.main()
