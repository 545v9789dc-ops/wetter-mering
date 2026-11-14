#!/usr/bin/env python3
"""Independent DWD availability, archive and cache diagnostics."""
import json
from pathlib import Path

PATH = Path('data/dwd_synoptik.json')


def annotate(doc):
    reports = doc.get('reports', {})
    key = doc.get('latest_short_term')
    latest = reports.get(key, {}) if key else {}
    medium = reports.get('mittelfrist', {})
    history = doc.get('history', [])
    doc['quality_details'] = {
        'latest_short_term_freshness': latest.get('freshness', 'unknown'),
        'latest_short_term_source_status': latest.get('status', 'unknown'),
        'latest_short_term_usable': bool(doc.get('availability', {}).get('short_term')),
        'medium_term_usable': bool(doc.get('availability', {}).get('medium_term')),
        'cached_reports': [k for k in ('kurzfrist_frueh', 'kurzfrist_abend', 'mittelfrist')
                           if reports.get(k, {}).get('used_cache', False)],
        'history_days': doc.get('history_days'),
        'history_report_count': len(history),
        'history_medium_term_count': sum(item.get('type') == 'mittelfrist' for item in history),
        'medium_term_previous_available': bool(doc.get('previous_medium_term')),
        'note': 'Collection status remains conservative; cache, publication freshness and history are separate.'
    }
    return doc


def main():
    doc = json.loads(PATH.read_text(encoding='utf-8'))
    annotate(doc)
    PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('DWD quality:', doc['quality_details'])


if __name__ == '__main__':
    main()
