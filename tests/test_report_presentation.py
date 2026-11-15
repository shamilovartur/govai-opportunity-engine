"""Rendering regressions, not claims about the quality of a real model's analysis."""

from copy import deepcopy
from io import BytesIO
import json
import re
from zipfile import ZipFile

from docx import Document
from pypdf import PdfReader

from govai.opportunity_report import management_view, render_markdown
from govai.report_export import render_pdf, render_docx, render_bundle, pdf_layout_status
from test_opportunities import _management_fixture


def _pdf_text(data):
    # Page furniture can interrupt a paragraph that legitimately spans pages.
    lines = [line for page in PdfReader(BytesIO(data)).pages for line in page.extract_text().splitlines()
             if line.strip() != 'GovAI · Предварительные рекомендации' and not re.fullmatch(r'Страница \d+', line.strip())]
    return ' '.join(' '.join(lines).split())


def test_full_pdf_survives_layout_overflow_without_losing_initiatives():
    artifact, _, _, _ = _management_fixture(20)
    unchanged = deepcopy(artifact)
    view = management_view(artifact)
    # Legacy analytic fields can be much longer than the new concise fields.
    # This view-only layout fixture deliberately does not alter the source artifact.
    for row in view['initiatives']:
        row['benefit'] = ('Помочь специалисту сопоставить доводы с первоисточниками и подготовить проверяемый ответ. ' * 9).strip()
        row['condition'] = ('До пилота подтвердить доступ к материалам и правила человеческой проверки. ' * 6).strip()
    data = render_pdf(artifact, view=view)
    status = pdf_layout_status(data)
    assert status['pages'] > 6 and status['exceeded'] and status['brief_limit'] == 6
    assert 'полный PDF доступен' in status['message']
    text = _pdf_text(data)
    for row in view['initiatives']:
        assert f"№ {row['number']}. {row['title']}" in text
        assert row['condition'] in text
        assert row['benefit'] in text
    docx = render_docx(artifact, view=view)
    assert Document(BytesIO(docx)).paragraphs
    bundle = render_bundle(artifact, view=view, pdf_data=data, docx_data=docx)
    with ZipFile(BytesIO(bundle)) as archive:
        assert archive.testzip() is None
        assert archive.read('opportunity-report.pdf') == data
        assert json.loads(archive.read('opportunities.json')) == artifact
        assert status['message'] in archive.read('README.txt').decode('utf-8')
    assert artifact == unchanged


def test_a_very_long_condition_is_split_not_clipped_or_rejected():
    artifact, _, _, _ = _management_fixture(1)
    view = management_view(artifact)
    view['initiatives'][0]['condition'] = (
        'Проверить допустимость работы с материалами и порядок проверки специалистом. ' * 180
        + 'Последнее условие сохраняется целиком.')
    data = render_pdf(artifact, view=view)
    text = _pdf_text(data)
    assert text.count('Проверить допустимость работы с материалами и порядок проверки специалистом.') == 180
    assert 'Последнее условие сохраняется целиком.' in text
    assert pdf_layout_status(data)['pages'] > 1


def test_model_executive_summary_is_shared_without_mechanical_repetition():
    artifact, _, _, _ = _management_fixture(1)
    original = deepcopy(artifact)
    # Presentation data is a controlled fixture for the new generation contract.
    summary = ('Начать с проверки материалов одного вида и сравнить помощь ИИ с обычным поиском. '
               'Специалист проверяет каждую ссылку и принимает решение о дальнейшем использовании. '
               'Сначала нужно подтвердить доступ к обезличенным примерам.')
    artifact['report']['presentation'].update(origin='model', executive_summary=summary)
    view = management_view(artifact)
    assert view['summary_blocks'] == [{'label': 'Рекомендация и ожидаемая польза', 'text': summary}]
    assert render_markdown(artifact).count(summary.replace('.', '\\.')) == 1
    assert _pdf_text(render_pdf(artifact)).count(summary) == 1
    body = '\n'.join(p.text for p in Document(BytesIO(render_docx(artifact))).paragraphs)
    assert body.count(summary) == 1
    # No source or numeric field was changed by any renderer.
    for key in ('plan', 'scenario_inputs'):
        assert artifact[key] == original[key]


def test_pilot_names_the_quality_indicator_instead_of_an_unresolved_reference():
    artifact, _, _, _ = _management_fixture(1)
    initiative = artifact['report']['initiatives'][0]
    indicator = 'Доля ответов с подтверждёнными выводами'
    initiative['measurement']['primary']['name'] = indicator
    initiative['measurement']['success_criterion'] = (
        'Превосходство над обычным поиском по основной метрике при отсутствии критических ошибок.')
    view = management_view(artifact)
    check = next(block['text'] for block in view['pilots'][0]['blocks'] if block['label'] == 'Как проверить')
    assert indicator in check and 'по основной метрике' not in check
    assert initiative['measurement']['method'] in check
    assert 'при отсутствии критических ошибок' in check
    assert 'по основной метрике' in initiative['measurement']['success_criterion']


def test_category_meanings_and_known_participant_limit_are_visible():
    artifact, _, _, _ = _management_fixture(1)
    initiative = artifact['report']['initiatives'][0]
    initiative['measurement']['participant']['category'] = 'unknown'
    initiative['measurement']['partial_initiative'] = True
    view = management_view(artifact)
    assert 'Высокая — есть конкретная польза' in view['category_note']
    assert any('№ 1' in text and 'исполнитель измеряемой работы не установлен' in text for text in view['conditions'])
    assert any('№ 1' in text and 'только часть работы' in text for text in view['conditions'])
    assert view['category_note'] in render_markdown(artifact).replace('\\.', '.')
    assert view['category_note'] in _pdf_text(render_pdf(artifact))


def test_a_short_pdf_status_does_not_report_an_overflow():
    artifact, _, _, _ = _management_fixture(0)
    data = render_pdf(artifact)
    assert pdf_layout_status(data) == {'pages': 1, 'brief_limit': 6, 'exceeded': False, 'message': None}
    assert 'Предлагаемые инициативы' not in _pdf_text(data)


def test_unreadable_pdf_does_not_destroy_the_complete_backup_bundle():
    artifact, _, _, _ = _management_fixture(1)
    unchanged = deepcopy(artifact)
    docx = render_docx(artifact)
    markdown = render_markdown(artifact).encode('utf-8')
    bundle = render_bundle(artifact, pdf_data=b'not a valid PDF',
                           docx_data=docx, markdown_data=markdown)
    with ZipFile(BytesIO(bundle)) as archive:
        assert archive.testzip() is None
        assert 'opportunity-report.pdf' not in archive.namelist()
        assert archive.read('opportunity-report.docx') == docx
        assert Document(BytesIO(archive.read('opportunity-report.docx'))).paragraphs
        assert archive.read('opportunity-report.md') == markdown
        assert json.loads(archive.read('opportunities.json')) == artifact
        assert archive.read('opportunity-details.txt')
        note = archive.read('README.txt').decode('utf-8')
        assert 'PDF не включён: не удалось проверить' in note
        assert 'ВНИМАНИЕ: не удалось подготовить opportunity-report.pdf' in note
    assert artifact == unchanged
