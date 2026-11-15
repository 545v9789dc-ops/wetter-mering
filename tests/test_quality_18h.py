"""Offline tests of the 18h provenance quality gate and report annotations."""
import importlib.util
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def load(path, name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/path)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
q=load('validate_data_quality.py','quality_fallback_18h')
p=load('prepare_report.py','report_fallback_18h')
NOW=datetime(2026,11,15,18,tzinfo=timezone.utc)


def model(gusts=None):
    times=[(NOW+timedelta(hours=i+1)).isoformat() for i in range(24)]
    return {'status':'ok','api_model':'gfs_seamless','model_run_utc':NOW.isoformat(),
            'winter_daily':[], 'winter_rolling_max':{'1':{}},
            'hourly_units':{'precipitation':'mm'},
            'hourly':{'time':times,'temperature_2m':[8]*24,
                      'precipitation':[0]*24,'wind_speed_10m':[12]*24,
                      'wind_gusts_10m':([None]*24 if gusts is None else list(gusts)),
                      'pressure_msl':[1012]*24}}


class Quality18HourTests(unittest.TestCase):
    def test_fully_missing_gusts_are_warned_even_if_model_ok(self):
        _,warn=q.check_models({'models':{'GFS':model()}},NOW)
        self.assertTrue(any('GFS/wind_gusts_10m: not available' in s for s in warn),warn)

    def test_valid_source_age_is_accepted(self):
        r=model([25]*24)
        r['parameter_fallback']={'wind_gusts_10m':{
            'recovered_hours':2,'recovered_indices':[0,1],
            'sources':[{'source_model':'gfs_seamless',
                        'source_run_utc':(NOW-timedelta(hours=17)).isoformat(),
                        'recovered_hours':2}]}}
        self.assertEqual(q.check_parameter_fallback(r,'GFS',NOW,
            q.time_axis(r['hourly']['time'])),[])

    def test_19h_source_is_rejected(self):
        r=model([22]*24)
        r['parameter_fallback']={'wind_gusts_10m':{
            'recovered_hours':1,'recovered_indices':[0],
            'sources':[{'source_model':'gfs_seamless',
                        'source_run_utc':(NOW-timedelta(hours=19)).isoformat(),
                        'recovered_hours':1}]}}
        messages=q.check_parameter_fallback(r,'GFS',NOW,
            q.time_axis(r['hourly']['time']))
        self.assertTrue(any('older than 18h' in s for s in messages),messages)

    def test_inconsistent_indices_are_warned(self):
        r=model([22]*24)
        r['parameter_fallback']={'wind_gusts_10m':{
            'recovered_hours':2,'recovered_indices':[0,0],
            'sources':[{'source_model':'gfs_seamless',
                        'source_run_utc':(NOW-timedelta(hours=6)).isoformat(),
                        'recovered_hours':2}]}}
        messages=q.check_parameter_fallback(r,'GFS',NOW,
            q.time_axis(r['hourly']['time']))
        self.assertTrue(any('inconsistent fallback indices' in s for s in messages),messages)

    def test_model_over_18h_is_warned(self):
        r=model([25]*24)
        r['model_run_utc']=(NOW-timedelta(hours=19)).isoformat()
        _,warnings=q.check_models({'models':{'GFS':r}},NOW)
        self.assertTrue(any('model run older than 18h' in s for s in warnings),warnings)

    def test_report_quality_exposes_missing_parameter(self):
        r=model()
        r['parameter_availability']={'wind_gusts_10m':{
            'status':'not_available','valid_hours':0,'total_hours':24}}
        doc=p.prepare({'models':{'GFS':r}}, {'stations':{}}, {'models':{}},NOW.isoformat())
        self.assertTrue(any('GFS/wind_gusts_10m' in s for s in doc['report_quality']['gaps']))
        self.assertEqual(doc['report_quality']['status'],'complete_with_gaps')

    def test_report_quality_exposes_substitution(self):
        r=model([22]*24)
        r['parameter_availability']={'wind_gusts_10m':{
            'status':'fallback','valid_hours':24,'replaced_hours':12}}
        doc=p.prepare({'models':{'GFS':r}}, {'stations':{}}, {'models':{}},NOW.isoformat())
        self.assertTrue(any('12 from previous' in s for s in doc['report_quality']['gaps']))

    def test_absent_optional_ifs_extension_is_not_automatic_error(self):
        r=model([20]*24)
        _,warnings=q.check_models({'models':{'IFS':r}},NOW)
        self.assertFalse(any('extended_forecast' in s for s in warnings),warnings)


if __name__=='__main__':
    unittest.main()
