"""Reproduce the example offline: python -m scripts.build_npa_example [--full PATH]."""

import argparse
import json
from pathlib import Path

from govai.extraction import extract_document

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples/rosimushchestvo_261'


def build_example():
    document = extract_document('source.txt', (EXAMPLE / 'source.txt').read_bytes())
    sources = json.loads((EXAMPLE / 'sources.json').read_text())
    selected = [c for c in document['clauses'] if c['number'] in {'2.6', '3.3', '3.3.1'}]
    ids = {fid for c in selected for fid in c['fragment_ids']}
    fragments = []
    for f in document['fragments']:
        if f['id'] in ids:
            origin = next(s for s in sources['sections'] if s['start'] <= f['start'] < s['end'])
            fragments.append({**f, 'source_url': origin['url']})
    return document, {
        'scope': 'Excerpt of structured output; full input is source.txt. Not an original PDF.',
        'schema_version': document['schema_version'],
        'source': document['source'],
        'metadata': document['metadata'],
        'summary': {'characters': len(document['text']), 'fragments': len(document['fragments']),
                    'observed_numbered_items': len(document['clauses']), 'toc_pages_retrieved': 52},
        'clauses': selected,
        'fragments': fragments,
        'warnings': document['warnings'],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full', type=Path, help='Optional destination for the complete structure JSON')
    args = parser.parse_args()
    document, example = build_example()
    (EXAMPLE / 'structured-example.json').write_text(
        json.dumps(example, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.full:
        args.full.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(example['summary'], ensure_ascii=False))
