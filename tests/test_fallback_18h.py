"""Regression tests for regional 18h and long-range 24h run fallback."""
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fetch_models_18h', ROOT/'scripts/fetch_models.py')
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
NOW = datetime(2026, 11, 15, 18, tzinfo=timezone.utc)


def make_record(run=NOW, n=24, gust=None):
    times = [(NOW + timedelta(hours=i)).isoformat() for i in range(n)]
    return {
        'status': 'ok', 'api_model': 'gfs_seamless', 'model_run_utc': run.isoformat(),
        'data_retrieved_at_utc': NOW.isoformat(), 'grid_latitude': f.LAT,
        'grid_longitude': f.LON,
        'hourly': {'time': times, 'temperature_2m': [8]*n,
                   'precipitation': [0]*n, 'wind_speed_10m': [12]*n,
                   'wind_gusts_10m': [gust]*n, 'pressure_msl': [1012]*n},
        'hourly_units': {'temperature_2m': '°C', 'precipitation': 'mm',
                         'wind_speed_10m': 'km/h', 'wind_gusts_10m': 'km/h',
                         'pressure_msl': 'hPa'}
    }


class EighteenHourFallbackTests(unittest.TestCase):
    def test_17_hour_previous_run_recovers_exact_hours(self):
        current = make_record()
        previous = make_record(NOW-timedelta(hours=17), gust=38)
        count = f.supplement_from_source(current, previous, NOW, 'previous_json')
        self.assertEqual(count, 24)
        self.assertEqual(current['hourly']['wind_gusts_10m'], [38]*24)
        info = current['parameter_fallback']['wind_gusts_10m']
        self.assertEqual(info['sources'][0]['source_age_hours'], 17)
        self.assertEqual(info['recovered_indices'], list(range(24)))

    def test_18_hour_boundary_is_allowed(self):
        self.assertEqual(f.supplement_from_source(make_record(),
            make_record(NOW-timedelta(hours=18), gust=24), NOW, 'previous_json'), 24)

    def test_anything_over_18_hours_is_rejected_for_icon(self):
        current = make_record()
        previous = make_record(NOW-timedelta(hours=18, minutes=1), gust=35)
        current['api_model'] = previous['api_model'] = 'icon_d2'
        self.assertEqual(f.supplement_from_source(current,previous,NOW,'previous_json'),0)
        self.assertTrue(all(x is None for x in current['hourly']['wind_gusts_10m']))

    def test_wrong_model_and_grid_are_rejected(self):
        for change in ({'api_model':'icon_d2'}, {'grid_latitude':f.LAT+0.1}):
            with self.subTest(change=change):
                current = make_record()
                previous = make_record(NOW-timedelta(hours=6), gust=30)
                previous.update(change)
                self.assertEqual(f.supplement_from_source(current,previous,NOW,'previous_json'),0)

    def test_missing_optional_parameter_key_is_recovered(self):
        current=make_record()
        previous=make_record(NOW-timedelta(hours=6),gust=18)
        previous['hourly']['cloud_cover_low']=[50]*24
        previous['hourly_units']['cloud_cover_low']='%'
        with patch.object(f,'candidate_runs',return_value=[]):
            f.complete_missing_parameters(current,f.MODELS['GFS'],NOW,previous)
        self.assertEqual(current['hourly']['cloud_cover_low'],[50]*24)
        self.assertEqual(current['parameter_fallback']['cloud_cover_low']['recovered_hours'],24)

    def test_timestamp_alignment_does_not_shift_forecasts(self):
        current = make_record(n=5)
        previous = make_record(NOW-timedelta(hours=6),n=5,gust=27)
        previous['hourly']['time'] = [
            (NOW+timedelta(hours=i+1)).isoformat() for i in range(5)]
        self.assertEqual(f.supplement_from_source(current,previous,NOW,'previous_json'),4)
        self.assertEqual(current['hourly']['wind_gusts_10m'],[None,27,27,27,27])

    def test_no_chained_fallback_values(self):
        previous = make_record(NOW-timedelta(hours=6), gust=22)
        previous['parameter_fallback'] = {'wind_gusts_10m': {
            'recovered_indices':[0,1], 'recovered_hours':2,
            'sources':[{'source_run_utc':(NOW-timedelta(hours=17)).isoformat(),
                        'source_model':'gfs_seamless','recovered_hours':2}]}}
        f.strip_previous_fallbacks(previous)
        self.assertEqual(previous['hourly']['wind_gusts_10m'][:3],[None,None,22])
        self.assertNotIn('parameter_fallback',previous)
        current=make_record(n=24)
        self.assertEqual(f.supplement_from_source(current,previous,NOW,'previous_json'),22)
        self.assertEqual(current['hourly']['wind_gusts_10m'][:3],[None,None,22])

    def test_missing_main_field_can_be_fetched_from_an_older_run(self):
        current = make_record()
        older=NOW-timedelta(hours=6)
        def payload(url):
            return {'latitude':f.LAT,'longitude':f.LON,
                    'hourly_units':{'wind_gusts_10m':'km/h'},
                    'hourly':{'time':current['hourly']['time'],
                              'wind_gusts_10m':[41]*24}}
        with patch.object(f,'candidate_runs',return_value=[older]), \
             patch.object(f,'fetch_json',side_effect=payload) as fetch:
            f.complete_missing_parameters(current,f.MODELS['GFS'],NOW,None)
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(current['hourly']['wind_gusts_10m'],[41]*24)
        self.assertEqual(current['parameter_availability']['wind_gusts_10m']['status'],'fallback')

    def test_previous_record_older_than_18h_is_not_accepted_for_icon(self):
        older=make_record(NOW-timedelta(hours=19),gust=30)
        older['api_model']='icon_d2'
        previous={'models':{'ICON-D2':older},'retrieved_at_utc':NOW.isoformat()}
        self.assertIsNone(f.previous_record(previous,'ICON-D2',f.MODELS['ICON-D2'],NOW))

    def test_values_expire_at_write_time_after_18h_for_icon(self):
        current=make_record()
        older=make_record(NOW-timedelta(hours=18),gust=31)
        current['api_model']=older['api_model']='icon_d2'
        self.assertEqual(f.supplement_from_source(current,older,NOW,'previous_json'),24)
        expired=f.expire_fallback_points(current,NOW+timedelta(seconds=1))
        self.assertEqual(expired,['wind_gusts_10m'])
        self.assertTrue(all(x is None for x in current['hourly']['wind_gusts_10m']))
        self.assertEqual(current['parameter_availability']['wind_gusts_10m']['status'],
                         'not_available')
        self.assertEqual(current['hourly']['temperature_2m'],[8]*24)

    def test_current_values_are_never_overwritten(self):
        current=make_record(n=4)
        current['hourly']['wind_gusts_10m'][1]=50
        previous=make_record(NOW-timedelta(hours=6),n=4,gust=20)
        self.assertEqual(f.supplement_from_source(current,previous,NOW,'previous_json'),3)
        self.assertEqual(current['hourly']['wind_gusts_10m'],[20,50,20,20])



    def test_long_range_gfs_and_ifs_accept_exactly_24h(self):
        for api_model in ('gfs_seamless', 'ecmwf_ifs025'):
            with self.subTest(model=api_model):
                current=make_record()
                older=make_record(NOW-timedelta(hours=24),gust=37)
                current['api_model']=older['api_model']=api_model
                self.assertEqual(f.supplement_from_source(current,older,NOW,'previous_json'),24)
                self.assertEqual(current['hourly']['wind_gusts_10m'],[37]*24)

    def test_long_range_rejects_over_24h(self):
        for api_model in ('gfs_seamless', 'ecmwf_ifs025'):
            with self.subTest(model=api_model):
                current=make_record()
                older=make_record(NOW-timedelta(hours=24,seconds=1),gust=37)
                current['api_model']=older['api_model']=api_model
                self.assertEqual(f.supplement_from_source(current,older,NOW,'previous_json'),0)
                self.assertTrue(all(v is None for v in current['hourly']['wind_gusts_10m']))

    def test_long_range_expire_at_save_after_24h(self):
        for api_model in ('gfs_seamless', 'ecmwf_ifs025'):
            with self.subTest(model=api_model):
                current=make_record()
                older=make_record(NOW-timedelta(hours=24),gust=37)
                current['api_model']=older['api_model']=api_model
                self.assertEqual(f.supplement_from_source(current,older,NOW,'previous_json'),24)
                self.assertEqual(f.expire_fallback_points(current,NOW+timedelta(seconds=1)),
                                 ['wind_gusts_10m'])

    def test_previous_record_24h_boundary(self):
        for label in ('GFS', 'IFS'):
            with self.subTest(label=label):
                config=f.MODELS[label]
                older=make_record(NOW-timedelta(hours=24),gust=37)
                older['api_model']=config['model']
                previous={'models':{label:older},'retrieved_at_utc':NOW.isoformat()}
                self.assertIsNotNone(f.previous_record(previous,label,config,NOW))
                older['model_run_utc']=(NOW-timedelta(hours=25)).isoformat()
                self.assertIsNone(f.previous_record(previous,label,config,NOW))

    def test_18h_regional_vs_24h_long_range(self):
        for label,expected in [('ICON-D2',18),('ICON-EU',18),('AROME',18),
                               ('GFS',24),('IFS',24)]:
            with self.subTest(model=label):
                self.assertEqual(f.fallback_age_limit(f.MODELS[label]['model']),expected)

    def test_24h_requery_accepts_older_than_18h(self):
        current=make_record()
        previous_run=NOW-timedelta(hours=21)
        def payload(url):
            return {'latitude':f.LAT,'longitude':f.LON,
                    'hourly_units':{'wind_gusts_10m':'km/h'},
                    'hourly':{'time':current['hourly']['time'],
                              'wind_gusts_10m':[44]*24}}
        with patch.object(f,'candidate_runs',return_value=[previous_run]), \
             patch.object(f,'fetch_json',side_effect=payload) as fetch:
            f.complete_missing_parameters(current,f.MODELS['GFS'],NOW)
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(current['hourly']['wind_gusts_10m'],[44]*24)


if __name__=='__main__':
    unittest.main()
