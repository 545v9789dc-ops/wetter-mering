import copy
import json
import tempfile
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path

spec = importlib.util.spec_from_file_location('fetch', Path(__file__).parents[1]/'scripts/fetch_models.py')
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
UTC = timezone.utc
NOW = datetime(2026,10,23,12,tzinfo=UTC)


def response(start=NOW, n=48):
    return {'latitude':f.LAT,'longitude':f.LON,'hourly_units':{'precipitation':'mm'},
            'hourly':{'time':[(start+timedelta(hours=i)).isoformat() for i in range(n)],
                      'temperature_2m':[10]*n,'precipitation':[1]*n}}


def record(run=NOW, n=48):
    obj=response(run,n)
    rec={'status':'ok','model_run_utc':run.isoformat(),'api_model':'ecmwf_ifs025',
         'grid_latitude':f.LAT,'grid_longitude':f.LON,'data_retrieved_at_utc':NOW.isoformat(),
         'hourly':obj['hourly'],'hourly_units':obj['hourly_units']}
    f.enrich(rec,NOW)
    return rec


class WeatherTests(unittest.TestCase):
    def test_dst_and_midnight(self):
        for day,hours in [('2026-10-25',25),('2026-03-29',23),('2026-10-24',24)]:
            start=datetime.fromisoformat(day).replace(tzinfo=f.BERLIN).astimezone(UTC)
            obj=response(start+timedelta(hours=1),hours)
            rows=f.daily_totals(obj['hourly'],obj['hourly_units'])
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['date'],day)
            self.assertEqual(rows[0]['precipitation_mm']['sum'],hours)
            self.assertEqual(rows[0]['expected_hours'],hours)

    def test_missing_rain_not_zero(self):
        obj=response(datetime(2026,10,24,22,tzinfo=UTC),26)
        obj['hourly']['precipitation'][5]=None
        row=next(r for r in f.daily_totals(obj['hourly'],obj['hourly_units']) if r['date']=='2026-10-25')
        self.assertIsNone(row['precipitation_mm']['sum'])
        self.assertEqual(row['precipitation_mm']['available_sum'],24)

    def test_duplicates_lengths_and_gaps(self):
        for kind in ['duplicate','length','gap']:
            obj=response()['hourly']
            if kind=='length': obj['precipitation'].pop()
            else: obj['time'][1]=obj['time'][0] if kind=='duplicate' else (NOW+timedelta(hours=3)).isoformat()
            with self.assertRaises(ValueError): f.validate_hourly(obj)

    def test_invalid_values(self):
        hourly={'time':['x']*4,'precipitation':[-1,float('nan'),0,2], 'sunshine_duration':[3601,-1,0,3600]}
        f.clean_values(hourly)
        self.assertEqual(hourly['precipitation'],[None,None,0,2])
        self.assertEqual(hourly['sunshine_duration'],[None,None,0,3600])

    def test_optional_failures_and_minutes(self):
        rec=record(); rec['api_model']='icon_d2'
        def fetch(url):
            from urllib.parse import parse_qs,urlparse
            key=parse_qs(urlparse(url).query)['hourly'][0]
            if key=='visibility': raise ValueError('unsupported')
            obj=response()
            obj['hourly']={'time':obj['hourly']['time'],key:[1800 if key=='sunshine_duration' else None]*48}
            obj['hourly_units']={key:'s' if key=='sunshine_duration' else '%'}
            return obj
        with patch.object(f,'fetch_json',side_effect=fetch): f.add_optional_variables(rec,'icon_d2',NOW,3)
        self.assertEqual(rec['hourly']['sunshine_duration_minutes'][0],30)
        self.assertEqual(rec['near_surface_variable_coverage']['visibility']['status'],'unavailable')
        self.assertEqual(rec['near_surface_variable_coverage']['cloud_cover']['status'],'unavailable')
        self.assertEqual(rec['hourly']['temperature_2m'][0],10)

    def test_wrong_sunshine_unit(self):
        rec=record(); obj=response(); obj['hourly']['sunshine_duration']=[30]*48
        obj['hourly_units']['sunshine_duration']='min'
        with patch.object(f,'fetch_json',return_value=obj): f.add_optional_variables(rec,'icon_d2',NOW,3)
        self.assertNotIn('sunshine_duration_minutes',rec['hourly'])

    def test_ifs_latest_and_longer_separate(self):
        newest=record(NOW,147); older=record(NOW-timedelta(hours=6),360)
        with patch.object(f,'candidate_runs',return_value=[NOW,NOW-timedelta(hours=6)]), patch.object(f,'request_run',side_effect=[newest,older]):
            result=f.select_model('IFS',f.MODELS['IFS'],NOW,{})
        self.assertEqual(result['model_run_utc'],NOW.isoformat())
        self.assertEqual(result['extended_forecast']['model_run_utc'],older['model_run_utc'])
        self.assertEqual(len(result['hourly']['time']),147)

    def test_model_outage_repeated_age(self):
        old=record(); previous={'retrieved_at_utc':NOW.isoformat(),'models':{'IFS':old}}
        with patch.object(f,'request_run',side_effect=OSError('offline')):
            first=f.select_model('IFS',f.MODELS['IFS'],NOW+timedelta(hours=2),previous)
            second=f.select_model('IFS',f.MODELS['IFS'],NOW+timedelta(hours=4),{'retrieved_at_utc':(NOW+timedelta(hours=2)).isoformat(),'models':{'IFS':first}})
        self.assertEqual(second['status'],'stale')
        self.assertEqual(second['data_age_hours'],4)

    def test_expired_and_distant_cache(self):
        for old in [record(NOW-timedelta(hours=100),200),record()]:
            if old['run_age_hours']==0: old['grid_latitude']=40
            self.assertIsNone(f.previous_record({'models':{'IFS':old}},'IFS',f.MODELS['IFS'],NOW))

    def test_outage_without_cache(self):
        with patch.object(f,'request_run',side_effect=OSError('offline')):
            result=f.select_model('IFS',f.MODELS['IFS'],NOW,{})
        self.assertEqual(result['status'],'unverified')

    def test_remote_grid_rejected(self):
        obj=response(); obj['latitude']=45
        with patch.object(f,'fetch_json',return_value=obj):
            with self.assertRaises(ValueError): f.request_run('ecmwf_ifs025',NOW,16,now=NOW)

    def test_total_outage_saves_explicit_status(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'data.json'
            with patch.object(f,'OUTPUT',output), patch.object(f,'request_run',side_effect=OSError('offline')):
                with self.assertRaises(SystemExit): f.main()
            data=json.loads(output.read_text())
            self.assertEqual(data['collection_status'],'degraded')
            self.assertTrue(all(r['status']=='unverified' for r in data['models'].values()))
            self.assertFalse(output.with_suffix('.tmp').exists())

    def test_wrong_precipitation_units(self):
        obj=response()
        rows=f.daily_totals(obj['hourly'],{'precipitation':'inch'})
        self.assertTrue(all(r['precipitation_mm']['sum'] is None for r in rows))

if __name__=='__main__': unittest.main()
