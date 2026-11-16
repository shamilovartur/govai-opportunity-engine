from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document

from govai.extraction import extract_document, extract_text


def parse(text):
    return extract_document('input.txt', text.encode())


def check_provenance(document):
    text = document['text']
    cursor = 0
    for fragment in document['fragments']:
        assert fragment['start'] == cursor
        assert fragment['text'] == text[fragment['start']:fragment['end']]
        cursor = fragment['end']
    assert cursor == len(text)
    assert ''.join(f['text'] for f in document['fragments']) == text
    assert len({f['id'] for f in document['fragments']}) == len(document['fragments'])
    for c in document['clauses']:
        assert text[c['marker_start']:c['marker_end']] == c['marker']
        assert c['fragment_ids']
    for evidence in document['metadata'].values():
        if evidence:
            assert text[evidence['start']:evidence['end']] == evidence['text']


def test_metadata_with_evidence_and_edition():
    text = ('Приказ Росимущества от 11.09.2020 N 261 (ред. от 30.05.2023) '
            '"Об утверждении регламента"\n\nI. Общие положения\n1. Текст.\n')
    doc = parse(text)
    assert doc['metadata']['number']['value'] == '261'
    assert doc['metadata']['adoption_date']['value'] == '2020-09-11'
    assert doc['metadata']['revision_date']['value'] == '2023-05-30'
    assert doc['metadata']['title']['value'] == 'Об утверждении регламента'
    check_provenance(doc)


def test_multiline_heading_and_word_date():
    doc = parse('ПРИКАЗ\nот 11 сентября 2020 г. № 261\nО ТЕСТОВОМ ПОРЯДКЕ\n\n1. Текст.')
    assert doc['metadata']['number']['value'] == '261'
    assert doc['metadata']['adoption_date']['value'] == '2020-09-11'
    assert doc['metadata']['title']['value'] == 'О ТЕСТОВОМ ПОРЯДКЕ'
    assert doc['metadata']['revision_date'] is None


def test_do_not_infer_metadata_from_citations_or_filename():
    doc = extract_document('Приказ_261_ред_30.05.2023.txt',
        '1. Основание:\nПриказ от 11.09.2020 N 261 "Об утверждении регламента".'.encode())
    assert all(v is None for v in doc['metadata'].values())


def test_amending_act_date_is_not_edition_date():
    doc = parse('ПРИКАЗ\nот 11 сентября 2020 г. N 261\n'
                '(в ред. Приказа Росимущества от 30.05.2023 N 108)\n1. Текст.')
    assert doc['metadata']['revision_date'] is None
    assert doc['metadata']['number']['value'] == '261'


def test_conflicting_metadata_is_not_selected_arbitrarily():
    doc = parse('Приказ от 11.09.2020 N 261\nПриказ от 30.05.2023 N 108\n1. Текст.')
    assert doc['metadata']['number'] is None
    assert doc['metadata']['adoption_date'] is None


def test_edition_of_cited_act_in_preamble_is_not_current_edition():
    doc = parse('ПРИКАЗ\nот 11 сентября 2020 г. N 261\n'
                'В соответствии с Положением (ред. от 30.05.2023), приказываю:\n1. Текст.')
    assert doc['metadata']['revision_date'] is None


def test_invalid_date_is_not_normalized_or_invented():
    doc = parse('Приказ от 32.13.2020 N 261 (ред. от 32.05.2023)\n1. Текст.')
    assert doc['metadata']['adoption_date'] is None
    assert doc['metadata']['revision_date'] is None


def test_hierarchy_order_and_no_inline_reference_boundaries():
    doc = parse('I. Общие положения\n1. Первый пункт.\n1.1. Условия.\n'
                'Продолжение со ссылкой на пункт 2.3 и дату 30.05.2023.\n'
                'а) Подпункт.\nб) Второй подпункт.\n2. Следующий пункт.\n')
    clauses = doc['clauses']
    assert [c['marker'] for c in clauses] == ['1.', '1.1.', 'а)', 'б)', '2.']
    assert clauses[1]['parent_id'] == clauses[0]['id']
    assert clauses[2]['parent_id'] == clauses[1]['id']
    assert clauses[3]['parent_id'] == clauses[1]['id']
    assert clauses[4]['parent_id'] is None
    continuation = next(f for f in doc['fragments'] if f['text'].startswith('Продолжение'))
    assert continuation['clause_id'] == clauses[1]['id']
    check_provenance(doc)


def test_unobserved_parent_is_not_created():
    doc = parse('3.3.1. Текст подпункта без родителя.')
    assert len(doc['clauses']) == 1
    assert doc['clauses'][0]['parent_id'] is None


def test_separate_marker_line_belongs_to_same_clause():
    doc = parse('1.1.\nТекст первого пункта.\n1.2.\nТекст второго пункта.')
    assert len(doc['clauses']) == 2
    assert doc['fragments'][1]['clause_id'] == doc['clauses'][0]['id']
    assert doc['fragments'][3]['clause_id'] == doc['clauses'][1]['id']
    check_provenance(doc)


def test_heading_and_appendix_end_previous_clause_and_reset_parent():
    doc = parse('I. Порядок\n1. Текст.\n\nСрок предоставления услуги\n\n'
                '2. Новый пункт.\n\nПриложение N 1\n1. Другой пункт.\n1.1. Условие.')
    header = next(f for f in doc['fragments'] if f['text'].startswith('Срок'))
    assert header['clause_id'] is None
    assert header['attribution'] == 'uncertain'
    assert doc['clauses'][0]['end'] <= header['start']
    assert doc['clauses'][-1]['parent_id'] == doc['clauses'][-2]['id']
    assert doc['clauses'][0]['section_id'] != doc['clauses'][-1]['section_id']


def test_unknown_structure_preserves_every_character_and_repetitions():
    text = 'Произвольный текст\r\n\r\nТа же строка.\r\nТа же строка.\n\tДанные\fКонец'
    doc = parse(text)
    assert doc['text'] == text
    assert not doc['clauses']
    assert all(f['clause_id'] is None for f in doc['fragments'])
    assert 'numbered_clauses_not_recognized' in doc['warnings']
    assert doc['source']['sha256'] == sha256(text.encode()).hexdigest()
    assert all(p['number'] is None for p in doc['pages'])
    check_provenance(doc)


def test_editorial_notes_are_not_part_of_normative_clause():
    doc = parse('2.6. Срок составляет двадцать дней.\n'
                '(п. 2.6 в ред. Приказа от 30.05.2023 N 108)\n(см. текст в предыдущей редакции)')
    assert doc['fragments'][1]['kind'] == 'annotation'
    assert doc['fragments'][1]['clause_id'] is None
    assert doc['fragments'][2]['clause_id'] is None
    check_provenance(doc)


def test_ambiguous_list_continuation_stays_unassigned():
    doc = parse('1. Условия:\nа) Первый вариант.\n\nПоследующий абзац\nс продолжением.')
    assert doc['fragments'][-1]['clause_id'] is None
    assert doc['fragments'][-2]['clause_id'] is None


def test_cyrillic_pdf_multpage_continuation_and_blank_page():
    data = (Path(__file__).parent / 'fixtures/cyrillic-pages.pdf').read_bytes()
    doc = extract_document('cyrillic-pages.pdf', data)
    assert 'Заявитель направляет обращение.' in doc['text']
    assert 'десять дней.' in doc['text']
    assert '\ufffd' not in doc['text']
    assert [p['number'] for p in doc['pages']] == [1, 2, 3]
    assert doc['pages'][2]['start'] == doc['pages'][2]['end']
    assert doc['clauses'][0]['page_numbers'] == [1, 2]
    assert [c['number'] for c in doc['clauses']] == ['1', '2']
    headers = [f for f in doc['fragments'] if 'Тестовый колонтитул' in f['text']]
    assert len(headers) == 2
    assert all(f['kind'] == 'layout_candidate' and f['clause_id'] is None for f in headers)
    assert 'pdf_page_without_text:3' in doc['warnings']
    assert extract_text('test.pdf', data) == doc['text']
    check_provenance(doc)


def test_docx_heading_styles_cyrillic_and_no_invented_pages():
    word = Document()
    word.add_heading('Общие положения', level=1)
    word.add_paragraph('1. Условия участия.')
    word.add_page_break()
    word.add_paragraph('2. Порядок рассмотрения.')
    buffer = BytesIO()
    word.save(buffer)
    doc = extract_document('document.docx', buffer.getvalue())
    assert doc['sections'][0]['kind'] == 'section'
    assert [c['number'] for c in doc['clauses']] == ['1', '2']
    assert all(f['page_number'] is None for f in doc['fragments'])
    assert 'docx_automatic_numbering_not_resolved' in doc['warnings']
    assert extract_text('document.docx', buffer.getvalue()) == doc['text']
    check_provenance(doc)


@pytest.mark.parametrize('encoding', ['utf-8-sig', 'utf-16', 'cp1251'])
def test_structured_txt_encodings(encoding):
    text = '1. Заявление принято.\n2. Ответ направлен.'
    doc = extract_document('test.txt', text.encode(encoding), encoding=encoding)
    assert doc['text'] == text
    check_provenance(doc)


def test_docx_vertical_merge_does_not_duplicate_but_real_repetition_survives():
    word = Document()
    table = word.add_table(rows=2, cols=2)
    table.cell(0, 0).merge(table.cell(1, 0)).text = 'Объединённая ячейка'
    table.cell(0, 1).text = table.cell(1, 1).text = 'Повтор в разных ячейках'
    buffer = BytesIO()
    word.save(buffer)
    doc = extract_document('table.docx', buffer.getvalue())
    assert doc['text'].count('Объединённая ячейка') == 1
    assert doc['text'].count('Повтор в разных ячейках') == 2
    check_provenance(doc)
