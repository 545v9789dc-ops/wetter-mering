import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("fetch", Path(__file__).parents[1] / "scripts/fetch_models.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
UTC = timezone.utc
NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)

def response(start=NOW, n=48):
    return {"latitude": f.LAT, "longitude": f.LON,
            "hourly_units": {"precipitation": "mm"},
            "hourly": {"time": [(start+timedelta(hours=i)).isoformat() for i in range(n)],
                       "temperature_2m": [10]*n, "precipitation": [1]*n}}

def record(run=NOW, n=48):
    obj = response(run, n)
    rec = {"status": "ok", "model_run_utc": run.isoformat(),
           "api_model": "ecmwf_ifs025", "grid_latitude": f.LAT,
           "grid_longitude": f.LON, "data_retrieved_at_utc": NOW.isoformat(),
           "hourly": obj["hourly"], "hourly_units": obj["hourly_units"]}
    f.enrich(rec, NOW)
    return rec

class WeatherTests(unittest.TestCase):
    def test_dst_and_midnight(self):
        for day, hours in [("2026-10-25", 25), ("2026-03-29", 23), ("2026-10-24", 24)]:
            start = datetime.fromisoformat(day).replace(tzinfo=f.BERLIN).astimezone(UTC)
            obj = response(start+timedelta(hours=1), hours)
            rows = f.daily_totals(obj["hourly"], obj["hourly_units"])
            self.assertEqual(rows[0]["precipitation_mm"]["sum"], hours)
            self.assertEqual(rows[0]["expected_hours"], hours)

    def test_missing_snow_is_not_zero(self):
        obj = response()
        rows = f.daily_totals(obj["hourly"], obj["hourly_units"])
        self.assertIsNone(rows[0]["snowfall_cm"]["sum"])

    def test_snow_sum_requires_full_day(self):
        start = datetime(2026, 10, 24, 22, tzinfo=UTC)
        obj = response(start, 27)
        obj["hourly"]["snowfall"] = [2]*27
        obj["hourly_units"]["snowfall"] = "cm"
        row = next(r for r in f.daily_totals(obj["hourly"], obj["hourly_units"])
                   if r["date"] == "2026-10-25")
        self.assertEqual(row["snowfall_cm"]["sum"], 50)
        obj["hourly"]["snowfall"][5] = None
        row = next(r for r in f.daily_totals(obj["hourly"], obj["hourly_units"])
                   if r["date"] == "2026-10-25")
        self.assertIsNone(row["snowfall_cm"]["sum"])

    def test_invalid_values(self):
        hourly = {"time": ["x"]*4, "snowfall": [-1, float("nan"), 0, 2],
                  "snow_depth": [-1, 0.1, 1, float("inf")]}
        f.clean_values(hourly)
        self.assertEqual(hourly["snowfall"], [None, None, 0, 2])
        self.assertEqual(hourly["snow_depth"], [None, .1, 1, None])

    def test_optional_failure_preserves_base(self):
        rec = record()
        def fetch(url):
            from urllib.parse import parse_qs, urlparse
            key = parse_qs(urlparse(url).query)["hourly"][0]
            if key == "snowfall_height":
                raise ValueError("unsupported")
            obj = response()
            obj["hourly"] = {"time": obj["hourly"]["time"],
                             key: [0.1 if key == "snow_depth" else 2]*48}
            obj["hourly_units"] = {key: "m" if key == "snow_depth" else "cm"}
            return obj
        with patch.object(f, "fetch_json", side_effect=fetch):
            f.add_optional_variables(rec, "icon_d2", NOW, 3,
                                     keys=("snowfall", "snow_depth", "snowfall_height"))
        self.assertEqual(rec["hourly"]["temperature_2m"][0], 10)
        self.assertEqual(rec["hourly"]["snowfall"][0], 2)
        self.assertEqual(rec["hourly"]["snow_depth"][0], .1)
        self.assertEqual(rec["optional_variable_coverage"]["snowfall_height"]["status"], "unavailable")

    def test_wrong_snow_units_rejected(self):
        rec = record()
        obj = response()
        obj["hourly"]["snow_depth"] = [10]*48
        obj["hourly_units"]["snow_depth"] = "cm"
        with patch.object(f, "fetch_json", return_value=obj):
            f.add_optional_variables(rec, "icon_d2", NOW, 3, keys=("snow_depth",))
        self.assertNotIn("snow_depth", rec["hourly"])

    def test_ifs_longer_run_kept_separate(self):
        newest = record(NOW, 147)
        older = record(NOW-timedelta(hours=6), 360)
        with patch.object(f, "candidate_runs", return_value=[NOW, NOW-timedelta(hours=6)]), \
             patch.object(f, "request_run", side_effect=[newest, older]):
            result = f.select_model("IFS", f.MODELS["IFS"], NOW, {})
        self.assertEqual(result["model_run_utc"], NOW.isoformat())
        self.assertEqual(result["extended_forecast"]["model_run_utc"], older["model_run_utc"])

    def test_total_outage_preserves_last_json(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)/"data.json"
            output.write_text('{"existing":"valid"}')
            with patch.object(f, "OUTPUT", output), \
                 patch.object(f, "request_run", side_effect=OSError("offline")):
                with self.assertRaises(SystemExit):
                    f.main()
            self.assertEqual(json.loads(output.read_text()), {"existing": "valid"})

if __name__ == "__main__":
    unittest.main()
