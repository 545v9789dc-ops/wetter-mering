#!/usr/bin/env python3
"""Add independent availability/cache indicators without weakening existing DWD status."""
import json
from pathlib import Path

PATH = Path('data/dwd_synoptik.json')


def annotate(doc):
    reports = doc.get('reports', {})
    key = doc.get('latest_short_term')
    latest = reports.get(key, {}) if key else {}
    medium = reports.get('mittelfrist', {})
    doc['quality_details'] = {
        'latest_short_term_freshness': latest.get('freshness', 'unknown'),
        'latest_short_term_source_status': latest.get('status', 'unknown'),
        'latest_short_term_usable': bool(doc.get('availability', {}).get('short_term')),
        'medium_term_usable': bool(doc.get('availability', {}).get('medium_term')),
        'cached_reports': [k for k in ('kurzfrist_frueh', 'kurzfrist_abend', 'mittelfrist')
                           if reports.get(k, {}).get('used_cache', False)],
        'note': 'collection_status retains its original conservative meaning; '
                'availability and cached reports are evaluated separately',
    }
    return doc


def main():
    doc = json.loads(PATH.read_text(encoding='utf-8'))
    annotate(doc)
    PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('DWD quality:', doc['quality_details'])


if __name__ == '__main__':
    main()
