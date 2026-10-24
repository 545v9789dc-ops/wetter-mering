import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('fetch_mosmix', Path(__file__).parents[1] / 'scripts/fetch_mosmix.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

SAMPLE = b'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:dwd="https://opendata.dwd.de/weather/lib/pointforecast_dwd_extension_V1_0.xsd">
<ExtendedData><dwd:ProductDefinition><dwd:IssueTime>2026-10-24T03:00:00.000Z</dwd:IssueTime>
<dwd:ForecastTimeSteps><dwd:TimeStep>2026-10-24T09:00:00.000Z</dwd:TimeStep><dwd:TimeStep>2026-10-24T10:00:00.000Z</dwd:TimeStep></dwd:ForecastTimeSteps>
</dwd:ProductDefinition></ExtendedData>
<Document><Placemark><name>10856</name><ExtendedData>
<dwd:Forecast dwd:elementName="N"><dwd:value>90 30</dwd:value></dwd:Forecast>
<dwd:Forecast dwd:elementName="SunD1"><dwd:value>- 1200</dwd:value></dwd:Forecast>
</ExtendedData></Placemark></Document></kml>'''

class MosmixTests(unittest.TestCase):
    def test_parse_values_and_missing(self):
        r = m.parse_kml(SAMPLE, '10856')
        self.assertEqual(r['parameters']['N'], [90, 30])
        self.assertEqual(r['parameters']['SunD1'], [None, 1200])
        self.assertEqual(r['parameter_valid_hours']['SunD1'], 1)
        self.assertEqual(r['issue_time_utc'], '2026-10-24T03:00:00Z')
    def test_reject_mismatched_count(self):
        with self.assertRaises(ValueError):
            m.parse_kml(SAMPLE.replace(b'90 30', b'90'), '10856')
    def test_reject_bad_timestamp(self):
        with self.assertRaises(ValueError):
            m.parse_kml(SAMPLE.replace(b'2026-10-24T10:00:00.000Z', b'bad'), '10856')
    def test_missing_marker(self):
        self.assertEqual(m.values('- -- NaN', 3), [None, None, None])

if __name__ == '__main__':
    unittest.main()
