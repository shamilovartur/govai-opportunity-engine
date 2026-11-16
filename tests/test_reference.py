"""Approved reference matching is independent of LLM generation and file names."""

from copy import deepcopy
from hashlib import sha256
from html import escape
from io import BytesIO
import json
import re
import unicodedata
from zipfile import ZipFile

from docx import Document
from pypdf import PdfReader
import pytest

from govai.extraction import extract_document
from govai.processes import ProcessError
from govai import reference
from govai.url_input import _html_document


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx.Client, 'send', lambda *a, **k: pytest.fail('Reference must not access the network'))


@pytest.fixture(scope='module')
def source():
    return reference.example_document_bytes()


@pytest.fixture(scope='module')
def document(source):
    return extract_document('source.txt', source)


def test_exact_full_source_returns_seven_approved_initiatives_and_pdf(document):
    saved = reference.match_reference(document)
    assert saved['kind'] == 'govai_verified_reference'
    assert saved['schema_version'] == '1'
    assert saved['provenance']['api_requests'] == 0
    view = reference.reference_view(saved)
    assert view['reference_label'] == 'Проверенный разбор · редакция 30.05.2023'
    assert [row['rank'] for row in view['cards']] == list(range(1, 8))
    assert len({row['id'] for row in view['cards']}) == 7
    assert [row['title'] for row in view['cards']] == [row['process'] for row in saved['approved_report']['model']['initiatives']]
    assert [row['title'] for row in view['cards']][3:5] == ['Исправления в документах', 'Обоснование отказа']
    pdf = reference.reference_pdf(saved)
    assert sha256(pdf).hexdigest() == reference.PDF_SHA256
    pages = PdfReader(BytesIO(pdf)).pages
    assert len(pages) == 6
    assert all(tuple(page.mediabox[2:]) == (450, 800) for page in pages)
    assert all(page.extract_text().strip() for page in pages)


@pytest.mark.parametrize('variation', ['renamed', 'bom', 'unicode_whitespace', 'nfd'])
def test_only_approved_full_text_normalization_variants_match(source, variation):
    text = source.decode('utf-8')
    name = 'unrelated_filename.txt'
    if variation == 'bom':
        text = '\ufeff' + text
    elif variation == 'unicode_whitespace':
        text = '\n\t' + re.sub(r'\s+', '\u00a0 \n\t', text) + '\u2003'
    elif variation == 'nfd':
        text = unicodedata.normalize('NFD', text)
    saved = reference.match_reference(extract_document(name, text.encode()))
    assert saved is not None
    assert saved['matched_document']['normalized_sha256'] == reference.NORMALIZED_SHA256
    assert reference.reference_pdf(saved) == reference.reference_pdf(reference.match_reference(extract_document('source.txt', source)))


@pytest.mark.parametrize('variation', ['other_file', 'truncated', 'changed_rule', 'edition', 'case', 'punctuation', 'extra_text'])
def test_title_filename_number_or_similarity_never_matches(source, variation):
    text = source.decode('utf-8')
    if variation == 'other_file':
        text = 'Приказ Росимущества от 11.09.2020 №261, редакция 30.05.2023. Другой документ.'
    elif variation == 'truncated':
        text = text[:len(text)//2]
    elif variation == 'changed_rule':
        assert 'без проведения торгов' in text
        text = text.replace('без проведения торгов', 'по результатам проведения торгов', 1)
    elif variation == 'edition':
        text = text.replace('30.05.2023', '30.05.2024', 1)
    elif variation == 'case':
        text = text.replace('Росимущества', 'РОСИМУЩЕСТВА', 1)
    elif variation == 'punctuation':
        text = text.replace('.', '', 1)
    else:
        text += '\nДополнительная норма: документы не принимаются.'
    assert reference.match_reference(extract_document('source.txt', text.encode())) is None


def test_complete_equivalent_docx_matches_only_after_text_extraction(source):
    docx = Document()
    for line in source.decode('utf-8').splitlines():
        docx.add_paragraph(line)
    stream = BytesIO(); docx.save(stream)
    extracted = extract_document('full-regulation.docx', stream.getvalue())
    saved = reference.match_reference(extracted)
    assert saved is not None
    assert saved['matched_document']['format'] == 'docx'


def test_complete_html_matches_only_the_whole_extracted_text(source):
    def extract(text):
        data = ('<!doctype html><html><head><meta charset="utf-8"></head><body><article>'
                + ''.join('<p>' + escape(line) + '</p>' for line in text.splitlines())
                + '</article></body></html>').encode()
        return _html_document(data, 'https://example.org/full-regulation', 'text/html; charset=utf-8')

    text = source.decode()
    saved = reference.match_reference(extract(text))
    assert saved is not None
    assert saved['matched_document']['format'] == 'html'
    assert reference.reference_pdf(saved) == (reference._RESOURCES / 'final.pdf').read_bytes()
    assert reference.match_reference(extract(text + '\nДополнительная норма: документы не принимаются.')) is None
    assert reference.match_reference(extract(text.replace('30.05.2023', '30.05.2024', 1))) is None


def test_unknown_pdf_page_or_corrupt_extraction_does_not_use_reference(document):
    partial_pdf = deepcopy(document)
    partial_pdf['source']['format'] = 'pdf'
    partial_pdf['warnings'].append('pdf_page_without_text:12')
    assert reference.match_reference(partial_pdf) is None
    corrupted = deepcopy(document)
    corrupted['source']['text_sha256'] = '0' * 64
    assert reference.match_reference(corrupted) is None
    contradictory = deepcopy(document)
    contradictory['metadata']['revision_date']['value'] = '2024-05-30'
    assert reference.match_reference(contradictory) is None


def test_reference_restore_is_bound_to_full_document_and_immutable_approval(document):
    saved = reference.match_reference(document)
    data = json.dumps(saved, ensure_ascii=False).encode()
    assert reference.restore_reference_result(data, document) == saved
    assert reference.match_reference(document) == saved  # no volatile timestamp/cache identity
    wrong = extract_document('source.txt', b'Unrelated new document.')
    with pytest.raises(ProcessError, match='не соответствует'):
        reference.restore_reference_result(data, wrong)
    for path in [('approved_report', 'warning'), ('provenance', 'origin')]:
        changed = deepcopy(saved)
        changed[path[0]][path[1]] = 'tampered'
        with pytest.raises(ProcessError, match='изменён'):
            reference.validate_reference(changed)
    changed = deepcopy(saved)
    changed['calculations']['rosim261-initiative-1']['results']['saved_hours']['value'] = '999.00'
    with pytest.raises(ProcessError, match='изменён'):
        reference.reference_pdf(changed)


def test_reference_does_not_trust_a_forged_kind_or_version(document):
    with pytest.raises(ProcessError):
        reference.reference_pdf({'kind': 'govai_verified_reference'})
    changed = reference.match_reference(document)
    changed['schema_version'] = '2.5'
    with pytest.raises(ProcessError, match='изменён'):
        reference.validate_reference(changed)


def test_all_original_approved_paragraphs_remain_in_markdown_and_details(document):
    saved = reference.match_reference(document)
    markdown = reference.render_reference_markdown(saved)
    for row in reference.reference_full_blocks(saved):
        assert row['text'] in markdown
    assert '<b>' not in markdown and '<link' not in markdown and '<br' not in markdown
    assert 'https://www.consultant.ru/document/cons_doc_LAW_362388/' in markdown
    details = reference.render_reference_details(saved)
    assert 'scenario_assumptions' not in markdown
    assert 'illustrative' in details
    assert 'не отражают расходы ведомства' in markdown
    assert reference.SOURCE_SHA256 in details


def test_shared_exports_use_one_reference_and_restore_without_api(document):
    from govai.opportunity_report import management_view, render_markdown
    from govai.report_export import render_bundle, render_docx, render_pdf

    saved = reference.match_reference(document)
    assert management_view(saved)['cards'] == reference.reference_view(saved)['cards']
    assert render_pdf(saved) == reference.reference_pdf(saved)
    assert render_markdown(saved) == reference.render_reference_markdown(saved)
    editable = Document(BytesIO(render_docx(saved)))
    paragraphs = [p.text for p in editable.paragraphs]
    assert all(row['text'] in paragraphs for row in reference.reference_full_blocks(saved))
    with ZipFile(BytesIO(render_bundle(saved))) as archive:
        assert archive.read('opportunity-report.pdf') == reference.reference_pdf(saved)
        assert archive.read('opportunity-report.md').decode() == reference.render_reference_markdown(saved)
        assert 'opportunities.json' not in archive.namelist()
        restored = reference.restore_reference_result(archive.read('reference-result.json'), document)
    assert restored == saved


def test_shared_calculator_confirms_frozen_approved_numbers_without_defaults(document):
    saved = reference.match_reference(document)
    expected = [('133.33', '120000.00', '220000.00', '128.33'),
                ('166.67', '160000.00', '280000.00', '161.67'),
                ('33.33', '140000.00', '240000.00', '28.33')]
    for rank, values in enumerate(expected, 1):
        calc = saved['calculations'][f'rosim261-initiative-{rank}']
        assert tuple(calc['results'][key]['value'] for key in
                     ('saved_hours', 'prototype_cost_min', 'prototype_cost_max', 'net_value_per_case')) == values
        assert all(p['origin'] == 'illustrative' for p in calc['parameters'].values())
    assert 'rosim261-initiative-4' not in saved['calculations']
    half = saved['calculations']['rosim261-initiative-1-half-cash']['results']
    assert half['threshold_cases_min']['value'] == '1945.95'
    assert half['threshold_cases_max']['value'] == '3567.57'


def test_damaged_reference_resources_fail_closed(document, tmp_path, monkeypatch):
    for name in reference._HASHES:
        (tmp_path / name).write_bytes((reference._RESOURCES / name).read_bytes())
    (tmp_path / 'report.json').write_text('{}')
    monkeypatch.setattr(reference, '_RESOURCES', tmp_path)
    with pytest.raises(ProcessError, match='Контрольная сумма'):
        reference.match_reference(document)
    # An unrelated document still takes the ordinary path; missing reference is not its source.
    assert reference.match_reference(extract_document('other.txt', b'Other regulation.')) is None


def test_approved_sources_and_pdf_are_unmodified_resources(source):
    assert sha256(source).hexdigest() == reference.SOURCE_SHA256
    assert sha256(reference.normalized_text(source.decode()).encode()).hexdigest() == reference.NORMALIZED_SHA256
    assert sha256((reference._RESOURCES / 'final.pdf').read_bytes()).hexdigest() == reference.PDF_SHA256
