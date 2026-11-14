import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location('fetch_dwd_synoptik', Path(__file__).resolve().parents[1] / 'scripts' / 'fetch_dwd_synoptik.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def bulletin(kind, date, time, spacing=False):
    head = 'M I T T E L F R I S T' if kind == 'mittelfrist' else 'K U R Z F R I S T'
    if not spacing:
        head = kind.upper()
    return f'SYNOPTISCHE ÜBERSICHT {head}\nAusgegeben am {date} um {time} UTC\n' + ('Wetterlage Tiefdruck und Hochdruck. ' * 50)


def item(kind, date, time, spacing=False):
    text = bulletin(kind, date, time, spacing)
    pub = d.issuance(text)
    actual = d.classify_report(text, pub)
    return actual, pub, {'report_text': text, 'status': 'ok', 'publication': pub}


class DwdTests(unittest.TestCase):
    def test_spaced_heading_and_newlines(self):
        text = bulletin('mittelfrist', '14.11.2026', '10:30', True).replace('M I T T E L F R I S T', 'M I T T E L\nF R I S T')
        self.assertEqual(d.classify_report(text, d.issuance(text)), 'mittelfrist')

    def test_publication_zone(self):
        pub = d.issuance(bulletin('mittelfrist', '14.11.2026', '11:30').replace('UTC', 'MEZ'))
        self.assertEqual(pub['issued_at_utc'], '2026-11-14T10:30:00+00:00')

    def test_newest_from_text_not_source_order(self):
        now = datetime(2026, 11, 14, 20, tzinfo=timezone.utc)
        old = {'reports': {'mittelfrist': item('mittelfrist', '13.11.2026', '10:30')[2]}}
        fetched = [item('mittelfrist', '14.11.2026', '10:30')]
        reports, history = d.select_reports(fetched, old, now)
        self.assertEqual(reports['mittelfrist']['publication']['date'], '14.11.2026')
        self.assertEqual(len(history), 2)

    def test_three_days_and_dedup(self):
        now = datetime(2026, 11, 14, 20, tzinfo=timezone.utc)
        fetched = [item('mittelfrist', '14.11.2026', '10:30')]
        old = {'history': [item('mittelfrist', '14.11.2026', '10:30')[2],
                           item('mittelfrist', '12.11.2026', '10:30')[2],
                           item('mittelfrist', '11.11.2026', '10:30')[2]]}
        _, history = d.select_reports(fetched, old, now)
        self.assertEqual(len(history), 2)

    def test_older_cache_never_overwrites_newer(self):
        now = datetime(2026, 11, 14, 20, tzinfo=timezone.utc)
        fetched = [item('mittelfrist', '13.11.2026', '10:30')]
        old = {'reports': {'mittelfrist': item('mittelfrist', '14.11.2026', '10:30')[2]}}
        reports, _ = d.select_reports(fetched, old, now)
        self.assertEqual(reports['mittelfrist']['publication']['date'], '14.11.2026')
        self.assertTrue(reports['mittelfrist']['used_cache'])


if __name__ == '__main__':
    unittest.main()
