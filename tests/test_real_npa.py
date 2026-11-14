"""Regression checks against the retrieved HTML text, not an original PDF."""

import json
from hashlib import sha256
from pathlib import Path

from govai.extraction import extract_document

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/rosimushchestvo_261'


def test_retrieved_source_integrity_and_all_toc_parts():
    data = (EXAMPLE / 'source.txt').read_bytes()
    manifest = json.loads((EXAMPLE / 'sources.json').read_text())
    assert sha256(data).hexdigest() == '8d9445aca3666c6605c4c38a5baef54a2d8c042462447f7f6fe14c0d5727b6d4'
    assert manifest['source_sha256'] == sha256(data).hexdigest()
    assert [s['toc_id'] for s in manifest['sections']] == list(range(53))
    cursor = 0
    for section in manifest['sections']:
        assert section['start'] == cursor
        assert section['url'].startswith('https://www.consultant.ru/document/cons_doc_LAW_362388/')
        fragment = data.decode()[section['start']:section['end']]
        if 'sha256' in section:
            assert sha256(fragment.encode()).hexdigest() == section['sha256']
        cursor = section['end']
    assert cursor == len(data.decode())


def test_real_2023_edition_structure_and_amended_clause():
    data = (EXAMPLE / 'source.txt').read_bytes()
    doc = extract_document('source.txt', data)
    assert doc['metadata']['number']['value'] == '261'
    assert doc['metadata']['adoption_date']['value'] == '2020-09-11'
    assert doc['metadata']['revision_date']['value'] == '2023-05-30'
    roman = [s['text'].split('.')[0] for s in doc['sections']
             if s['kind'] == 'section' and not s['text'].startswith('Приложение')]
    assert roman == ['I', 'II', 'III', 'IV', 'V', 'VI']
    by_number = {c['number']: c for c in doc['clauses'] if '.' in c['number']}
    fragments = {f['id']: f for f in doc['fragments']}
    clause26 = ''.join(fragments[fid]['text'] for fid in by_number['2.6']['fragment_ids'])
    assert 'не должен превышать двадцати дней' in clause26
    assert 'Приказа Росимущества от 30.05.2023 N 108' not in clause26
    assert '(п. 2.6 в ред. Приказа Росимущества от 30.05.2023 N 108)' in doc['text']
    assert by_number['3.3.1']['parent_id'] == by_number['3.3']['id']
    assert by_number['3.3']['end'] <= by_number['3.3.1']['start']
    assert by_number['2.13']['end'] <= by_number['2.13.1']['start']
    assert by_number['1.1']['start'] < by_number['2.6']['start'] < by_number['6.8']['start']
    assert ''.join(f['text'] for f in doc['fragments']) == data.decode()
    assert len(doc['text']) == 113889
    assert len(doc['clauses']) == 160
    assert all(f['page_number'] is None for f in doc['fragments'])
    for f in doc['fragments']:
        assert doc['text'][f['start']:f['end']] == f['text']


def test_committed_structured_example_matches_current_extractor():
    from scripts.build_npa_example import build_example

    _, example = build_example()
    assert example == json.loads((EXAMPLE / 'structured-example.json').read_text())
