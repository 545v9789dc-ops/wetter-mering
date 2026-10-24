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

    def test_ifs_candidates_no_older_than_18_hours(self):
        now = datetime(2026, 10, 24, 5, 32, tzinfo=UTC)
        runs = f.candidate_runs(now, [0, 6, 12, 18], limit=4, max_age_hours=18)
        self.assertEqual([r.hour for r in runs], [0, 18, 12])
        self.assertTrue(all((now-r).total_seconds() <= 18*3600 for r in runs))

    def test_http_400_reason_retained(self):
        from io import BytesIO
        from urllib.error import HTTPError
        error = HTTPError("https://example.org", 400, "Bad Request", {},
                          BytesIO(b'{"error":true,"reason":"Run is not available"}'))
        with patch.object(f, "urlopen", side_effect=error):
            with self.assertRaisesRegex(ValueError, "Run is not available"):
                f.fetch_json("https://example.org")

    def test_ifs_rejects_old_cached_extended(self):
        now = NOW
        latest = record(now, 147)
        cached = record(now-timedelta(hours=24), 360)
        with patch.object(f, "candidate_runs", return_value=[now]), \
             patch.object(f, "request_run", return_value=latest), \
             patch.object(f, "previous_record", return_value=cached):
            result = f.select_model("IFS", f.MODELS["IFS"], now, {})
        self.assertNotIn("extended_forecast", result)

    def test_gfs_core_recovery_fills_missing_values(self):
        rec = record()
        rec["hourly"]["wind_gusts_10m"] = [None]*48
        rec["hourly"]["pressure_msl"] = [None]*48
        rec["hourly_units"].update({"wind_gusts_10m": "km/h", "pressure_msl": "hPa"})
        def fetch(url):
            from urllib.parse import parse_qs, urlparse
            key = parse_qs(urlparse(url).query)["hourly"][0]
            obj = response()
            obj["hourly"] = {"time": obj["hourly"]["time"],
                             key: [30 if key == "wind_gusts_10m" else 1013]*48}
            obj["hourly_units"] = {key: "km/h" if key == "wind_gusts_10m" else "hPa"}
            return obj
        with patch.object(f, "fetch_json", side_effect=fetch):
            f.repair_gfs_core(rec, NOW, 16)
        self.assertEqual(rec["hourly"]["wind_gusts_10m"], [30]*48)
        self.assertEqual(rec["hourly"]["pressure_msl"], [1013]*48)
        self.assertEqual(rec["gfs_core_recovery"]["wind_gusts_10m"]["status"], "recovered")

    def test_gfs_rejects_different_grid(self):
        rec = record()
        rec["hourly"]["wind_gusts_10m"] = [None]*48
        rec["hourly"]["pressure_msl"] = [1010]*48
        obj = response()
        obj["latitude"] += 0.1
        obj["hourly"] = {"time": obj["hourly"]["time"], "wind_gusts_10m": [30]*48}
        obj["hourly_units"] = {"wind_gusts_10m": "km/h"}
        with patch.object(f, "fetch_json", return_value=obj):
            f.repair_gfs_core(rec, NOW, 16)
        self.assertTrue(all(v is None for v in rec["hourly"]["wind_gusts_10m"]))
        self.assertEqual(rec["gfs_core_recovery"]["wind_gusts_10m"]["status"], "retry_failed")

    def test_gfs_no_retry_if_complete(self):
        rec = record()
        rec["hourly"]["wind_gusts_10m"] = [20]*48
        rec["hourly"]["pressure_msl"] = [1010]*48
        with patch.object(f, "fetch_json") as fetch:
            f.repair_gfs_core(rec, NOW, 16)
        fetch.assert_not_called()

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
