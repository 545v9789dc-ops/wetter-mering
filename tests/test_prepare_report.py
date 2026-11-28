import importlib.util
import unittest
from pathlib import Path
spec = importlib.util.spec_from_file_location('prepare_report', Path(__file__).resolve().parents[1] / 'scripts' / 'prepare_report.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

class PrepareTests(unittest.TestCase):
    def test_partial_sunshine_is_not_full_day(self):
        station = {'forecast_times_utc':['2026-11-14T09:00:00Z','2026-11-14T10:00:00Z'],'parameters':{'SunD1':[1800,1200],'RR1c':[0.5,None]}}
        row=p.mosmix_days(station)[0]
        self.assertEqual(row['sunshine_hours']['available_sum'],round(3000/3600,3))
        self.assertIsNone(row['sunshine_hours']['sum'])
        self.assertEqual(row['precipitation_mm']['valid_hours'],1)
        self.assertEqual(len(row['sunshine_hours']['missing_interval_starts_utc']),22)
    def test_ukmo_report_is_included_and_missing_is_visible(self):
        doc=p.prepare({'models':{}},{'stations':{}},{'models':{}},'2026-11-14T10:00:00Z')
        self.assertEqual(len(doc['models']),6)
        self.assertIn('UKMO',doc['models'])
        self.assertIn('Main models available: 0/6',doc['report_quality']['gaps'])
    def test_ensemble_distributions_reused(self):
        ens={'models':{'IFS-ENS':{'member_count':50,'daily':[{'date':'2026-11-14','daily_distributions':{'temperature_2m_max':{'p90':12}}}]}}}
        doc=p.prepare({}, {}, ens,'2026-11-14T10:00:00Z')
        self.assertEqual(doc['ensembles']['IFS-ENS']['daily'][0]['daily_distributions']['temperature_2m_max']['p90'],12)
    def test_signals_and_metadata(self):
        signal={'icing_periods':[{'kind':'explicit_freezing_precipitation'}]}
        meta={'forecast_run_verification':'unverified'}
        doc=p.prepare({'models':{'ICON-D2':{'hourly_signals':signal}}},{},{'models':{'IFS-ENS':{'run_metadata':meta}}})
        self.assertEqual(doc['models']['ICON-D2']['hourly_signals'],signal)
        self.assertEqual(doc['ensembles']['IFS-ENS']['run_metadata'],meta)
    def test_matching_quality_and_events(self):
        t='2026-11-14T09:00:00Z'; now='2026-11-14T10:00:00Z'
        source={'retrieved_at_utc':t}
        q={'status':'ok','checked_at_utc':now,'sources':{f'mering_{k}.json':{'readable':True,'retrieved_at_utc':t} for k in ('models','mosmix','ensembles')}}
        e={'source_retrieved_at_utc':t,'events':[]}
        doc=p.prepare(source,source,source,now,q,e)
        self.assertEqual(doc['data_validation']['status'],'matched')
        self.assertEqual(doc['winter_events']['events'],[])
        self.assertEqual(doc['source_age_hours_at_generation']['models'],1)
        q['sources']['mering_models.json']['retrieved_at_utc']='2026-11-13T09:00:00Z'
        doc=p.prepare(source,source,source,now,q,{'source_retrieved_at_utc':None,'events':[]})
        self.assertIsNone(doc['data_validation']['snapshot'])
        self.assertIsNone(doc['winter_events']['events'])
    def test_missing_not_empty(self):
        doc=p.prepare({}, {}, {})
        self.assertEqual(doc['data_validation']['status'],'missing')
        self.assertIsNone(doc['winter_events']['events'])
    def test_duplicate_hour_not_complete(self):
        row=p.mosmix_days({'forecast_times_utc':['2026-11-14T09:00:00Z']*2,'parameters':{'SunD1':[0,3600]}})[0]
        self.assertEqual(row['sunshine_hours']['valid_hours'],0)
        self.assertIsNone(row['sunshine_hours']['available_sum'])

if __name__=='__main__': unittest.main()
