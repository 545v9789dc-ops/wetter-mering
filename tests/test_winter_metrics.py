import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location('winter_metrics', Path(__file__).resolve().parents[1] / 'scripts' / 'winter_metrics.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

class WinterTests(unittest.TestCase):
    def record(self,start,n=48):
        times=[(start+timedelta(hours=i)).strftime('%Y-%m-%dT%H:%M') for i in range(1,n+1)]
        return {'hourly_units':{'temperature_2m':'°C','precipitation':'mm','snowfall':'cm','snow_depth':'m'},'hourly':{'time':times,'temperature_2m':[-2]*n,'precipitation':[0.5]*n,'snowfall':[0.1]*n,'snow_depth':[0.04]*n}}
    def test_full_day(self):
        r=self.record(datetime(2026,1,1,tzinfo=timezone.utc)); days=w.winter_summary(r)
        d=next(x for x in days if x['date']=='2026-01-02')
        self.assertEqual(d['precipitation_mm'],12); self.assertEqual(d['snowfall_cm'],2.4); self.assertEqual(d['snow_depth_max_cm'],4); self.assertTrue(d['ice_day']); self.assertIsNone(days[0]['precipitation_mm'])
    def test_missing_depth_not_zero(self):
        r=self.record(datetime(2026,1,1)); del r['hourly']['snow_depth']; self.assertIsNone(w.winter_summary(r)[1]['snow_depth_max_cm'])
    def test_depth_change(self):
        r=self.record(datetime(2026,1,1)); self.assertEqual(next(x for x in w.winter_summary(r) if x['date']=='2026-01-02')['snow_depth_change_cm'],0)
    def test_rolling_window(self):
        r=self.record(datetime(2026,1,1)); result=w.rolling_totals(r,datetime(2026,1,1,tzinfo=timezone.utc)); self.assertEqual(result['24']['precipitation_mm'],12); self.assertEqual(result['48']['snowfall_cm'],4.8); self.assertIsNone(result['72']['precipitation_mm'])
    def test_sliding_maxima(self):
        r=self.record(datetime(2026,1,1)); r['hourly']['precipitation'][10:16]=[10]*6; result=w.sliding_maxima(r,datetime(2026,1,1,tzinfo=timezone.utc),48); self.assertEqual(result['6']['precipitation_mm']['value'],60); self.assertEqual(result['6']['precipitation_mm']['complete_windows'],43)
    def test_sliding_gap_is_not_zero(self):
        r=self.record(datetime(2026,1,1),6); r['hourly']['precipitation'][3]=None; self.assertIsNone(w.sliding_maxima(r,datetime(2026,1,1,tzinfo=timezone.utc),6)['6']['precipitation_mm']['value'])
    def test_no_frost_hours_field(self):
        self.assertNotIn('frost_hours',w.winter_summary(self.record(datetime(2026,1,1)))[0])
    def test_dst_spring_day_has_23_hours(self):
        self.assertEqual(next(x for x in w.winter_summary(self.record(datetime(2026,3,28),72)) if x['date']=='2026-03-29')['expected_hours'],23)
    def test_dst_autumn_day_has_25_hours(self):
        self.assertEqual(next(x for x in w.winter_summary(self.record(datetime(2026,10,24),72)) if x['date']=='2026-10-25')['expected_hours'],25)
    def test_gusts_max_not_sum(self):
        t=datetime(2026,1,1,tzinfo=timezone.utc); r=self.record(t,6); r['hourly_units']['wind_gusts_10m']='km/h'; r['hourly']['wind_gusts_10m']=[10,20,90,30,40,50]; self.assertEqual(w.sliding_maxima(r,t,6)['6']['wind_gusts_kmh']['value'],90); r['hourly']['wind_gusts_10m'][2]=None; self.assertIsNone(w.sliding_maxima(r,t,6)['6']['wind_gusts_kmh']['value'])
    def test_icing_distinguishes_explicit_and_heuristic(self):
        t=datetime(2026,1,1,tzinfo=timezone.utc); r=self.record(t,6); r['hourly_units']['weather_code']='wmo code'; r['hourly']['weather_code']=[66,67,61,71,56,57]; r['hourly']['precipitation'][4]=None; rows=w.hourly_signals(r,t,6)['icing_periods']; self.assertEqual([x['kind'] for x in rows],['explicit_freezing_precipitation','near_freezing_liquid_precipitation','explicit_freezing_precipitation']); self.assertIsNone(rows[-1]['precipitation_mm'])
    def test_missing_hour_splits_period_and_marks_fallback(self):
        t=datetime(2026,1,1,tzinfo=timezone.utc); r=self.record(t,3); r['hourly']['precipitation']=[1,None,2]; r['parameter_fallback']={'precipitation':{'recovered_indices':[2]}}; rows=w.hourly_signals(r,t,3)['precipitation_periods']; self.assertEqual(len(rows),2); self.assertEqual(rows[1]['fallback_fields'],['precipitation']); self.assertEqual(rows[1]['temperature_at_interval_start_c'],-2)
    def test_missing_code_not_assumed_liquid(self):
        t=datetime(2026,1,1,tzinfo=timezone.utc); r=self.record(t,2); self.assertEqual(w.hourly_signals(r,t,2)['icing_periods'],[]); r['hourly']['snowfall']=[0,0]; self.assertEqual(w.hourly_signals(r,t,2)['icing_periods'][0]['kind'],'near_freezing_phase_uncertain')
    def test_wrong_units_not_used(self):
        t=datetime(2026,1,1,tzinfo=timezone.utc); r=self.record(t,2); r['hourly_units']['precipitation']='cm'; result=w.hourly_signals(r,t,2); self.assertEqual(result['valid_hours']['precipitation'],0); self.assertEqual(result['precipitation_periods'],[])

if __name__=='__main__':
    unittest.main()
