"""Rendering regressions, not claims about the quality of a real model's analysis."""

from copy import deepcopy
from io import BytesIO
import json
import re
from zipfile import ZipFile

from docx import Document
from pypdf import PdfReader

from govai.opportunity_report import management_view, render_markdown, render_details, report_outcome
from govai.report_export import render_pdf, render_docx, render_bundle, pdf_layout_status
from test_opportunities import _management_fixture, _editorial_inputs, _service_map_input


def _pdf_text(data):
    # Page furniture can interrupt a paragraph that legitimately spans pages.
    lines = [line for page in PdfReader(BytesIO(data)).pages for line in page.extract_text().splitlines()
             if line.strip() != 'GovAI · Предварительные рекомендации' and not re.fullmatch(r'Страница \d+', line.strip())]
    return ' '.join(' '.join(lines).split())


def test_service_overview_is_shared_by_documents_and_keeps_exact_sources_in_details():
    from govai.opportunities import rebuild_opportunities, restore_opportunities
    original,normalized,analysis,_=_management_fixture(1)
    editorial=_editorial_inputs(original['report']['initiatives'],['case-1'])
    editorial['ordering_basis']='Первым проверить ограниченный участок: для него доступны контрольные примеры, а интеграция пока не нужна.'
    editorial['service_map']=_service_map_input(normalized)
    artifact=rebuild_opportunities(json.dumps(original).encode(),normalized,analysis,'test-model',
        editorial_inputs=editorial,reason='Проверенный обзор услуги и основание редакционной очередности без нового запроса.')
    before=deepcopy(artifact)
    view=management_view(artifact)
    assert editorial['ordering_basis'] in view['ordering_note']
    assert 'сохранён порядок' not in view['ordering_note']
    stage=view['service_map']['stages'][0]
    pdf=render_pdf(artifact); docx=render_docx(artifact)
    texts=[render_markdown(artifact).replace('\\.','.'),_pdf_text(pdf),
           ' '.join(node.text or '' for node in Document(BytesIO(docx)).element.iter() if node.tag.endswith('}t'))]
    for text in texts:
        assert 'Как проходит услуга' in text
        assert all(stage[key] in text for key in ('stage','participant','result','clauses'))
        assert editorial['ordering_basis'] in text
    details=render_details(artifact)
    assert 'это не новый ответ модели' in details
    for proof in artifact['report']['presentation']['service_map']['evidence'].values():
        assert proof['quote'] in details
    with ZipFile(BytesIO(render_bundle(artifact,pdf_data=pdf,docx_data=docx))) as archive:
        data=archive.read('opportunities.json')
        assert restore_opportunities(data,normalized,analysis,'test-model')==artifact
        assert archive.read('opportunity-report.pdf')==pdf
    assert artifact==before and not original['editorial_inputs']


def test_incomplete_grouping_does_not_claim_that_source_text_is_incomplete():
    artifact,_,_,_=_management_fixture(1)
    artifact['report']['normalization_status']='incomplete'
    view=management_view(artifact)
    assert 'разграничить часть действий и участников' in view['input_limitation']
    assert 'Числовые оценки допустимы только для отдельно определённой работы и её участника' in view['input_limitation']
    assert 'охват' not in view['input_limitation'] and 'полнот' not in view['input_limitation']
    # Source completeness is stated separately; it cannot be inferred from grouping.
    assert view['scope']


def test_full_legal_title_is_preserved_in_appendix_with_requisites_in_main_report():
    artifact,_,_,_=_management_fixture(1)
    title='Об утверждении порядка проверки поступивших материалов и подготовки проектов ответов заявителям'
    artifact['report']['document'].update(title=title,act_type='Приказ',number='17',
        adoption_date='2025-01-20',revision_date='2025-02-01')
    view=management_view(artifact)
    assert title in render_details(artifact)
    assert '2025-01-20' in render_details(artifact)
    texts=[render_markdown(artifact).replace('\\.','.'),_pdf_text(render_pdf(artifact)),
           ' '.join(node.text or '' for node in Document(BytesIO(render_docx(artifact))).element.iter() if node.tag.endswith('}t'))]
    for text in texts:
        assert title not in text
        assert view['requisites'] in text
        assert 'opportunity-details.txt' in text
    assert artifact['report']['document']['title']==title


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
    assert view['conclusion']['text'] == summary
    assert render_markdown(artifact).count(summary.replace('.', '\\.')) == 1
    assert _pdf_text(render_pdf(artifact)).count(summary) == 1
    body = '\n'.join(node.text or '' for node in Document(BytesIO(render_docx(artifact))).element.iter() if node.tag.endswith('}t'))
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
    check = view['pilots'][0]['check']
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
    assert view['priority_groups'][0]['title'] == 'В первую очередь'
    assert 'ресурсы' in view['priority_groups'][0]['explanation'].lower()
    assert any('№ 1' in text and 'исполнитель измеряемой работы не установлен' in text for text in view['conditions'])
    assert any('№ 1' in text and 'только часть работы' in text for text in view['conditions'])
    assert 'В первую очередь' in render_markdown(artifact)
    assert 'В первую очередь' in _pdf_text(render_pdf(artifact))


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


def test_rejected_source_plan_never_returns_as_a_recommendation_in_any_format():
    """Controlled reconstruction of the reported failure, not the absent live ZIP."""
    artifact, _, _, _ = _management_fixture(1)
    original = deepcopy(artifact)
    refused = artifact['report']['initiatives'].pop()
    artifact['report']['explanation'] = 'Выделена одна содержательная гипотеза о маршрутизации.'
    artifact['report']['presentation']['executive_summary'] = 'Начать с отвергнутой маршрутизации.'
    artifact['report']['rejected'] = [{'id': refused['id'], 'title': refused['title'],
        'origin': 'integrity_validator', 'reason': 'Контекстная ссылка не была допущена проверкой источников.'}]
    view = management_view(artifact)
    assert view['outcome']['state'] == 'rejected'
    assert not view['initiatives'] and not view['pilots'] and not view['highlights'] and not view['priority_groups']
    assert 'Это не означает' in view['conclusion']['text']
    assert 'не прошли проверку' in view['conclusion']['text']
    texts = [render_markdown(artifact), _pdf_text(render_pdf(artifact)),
             '\n'.join(node.text or '' for node in Document(BytesIO(render_docx(artifact))).element.iter() if node.tag.endswith('}t'))]
    for text in texts:
        assert 'Выделена одна содержательная гипотеза' not in text
        assert 'Начать с отвергнутой маршрутизации' not in text
        assert 'Обоснованных вариантов применения ИИ не найдено' not in text
        assert 'Сначала подтвердить основания предложений' in text
    assert artifact['plan'] == original['plan']


def test_outcome_distinguishes_normal_exclusions_partial_and_unfinished_results():
    artifact, _, _, _ = _management_fixture(2)
    artifact['report']['rejected'] = [{'title': 'Обычная автоматизация', 'origin': 'model', 'reason': 'Достаточно правил.'}]
    artifact['report']['normalization_status'] = 'incomplete'
    assert report_outcome(artifact)['state'] == 'ready'
    refused = artifact['report']['initiatives'].pop()
    artifact['report']['rejected'].append({'id': refused['id'], 'title': refused['title'], 'origin': 'validator', 'reason': 'Нет добавочной пользы.'})
    artifact['report']['presentation']['executive_summary'] = 'Продвигать отклонённый вариант.'
    view = management_view(artifact)
    assert view['outcome']['state'] == 'partial' and len(view['initiatives']) == 1
    assert 'Продвигать отклонённый вариант' not in render_markdown(artifact)
    artifact['stage_status'] = 'failed'
    unfinished = management_view(artifact)
    assert unfinished['outcome']['state'] == 'unfinished'
    assert not unfinished['highlights'] and not unfinished['pilots']
    assert unfinished['priority_groups'][0]['title'] == 'Пока не запускать'


def test_no_opportunity_result_does_not_reuse_unchecked_model_explanation():
    artifact, _, _, _ = _management_fixture(0)
    artifact['report']['explanation'] = 'Выделена одна содержательная гипотеза.'
    artifact['report']['rejected'] = [{'title': 'Проверка формата', 'origin': 'model', 'reason': 'Достаточно обычных правил.'}]
    assert report_outcome(artifact)['state'] == 'none'
    text = render_markdown(artifact)
    assert 'Обоснованных вариантов применения ИИ не найдено' in text
    assert 'Выделена одна' not in text
    assert not management_view(artifact)['highlights']


def test_real_arithmetic_highlights_are_scoped_and_do_not_create_resource_numbers():
    artifact, _, _, _ = _management_fixture(1, scenario=True)
    before = deepcopy(artifact)
    view = management_view(artifact)
    by_label = {h['label']: h for h in view['highlights']}
    assert by_label['минут на подходящий случай']['value'] == '20 → 12'
    assert by_label['снижение времени на случай']['value'] == '40%'
    assert by_label['человеко-часа']['value'] == '133,33'
    assert '1 000' in by_label['человеко-часа']['basis']
    assert all(h['status'] == 'Сценарий' and h['number'] == 1 for h in view['highlights'])
    assert 'не оценены' in view['pilots'][0]['resources']
    assert not view['pilots'][0]['flow']
    assert artifact == before


def test_new_validated_resources_and_proposed_flow_are_visible_without_summing_initiatives():
    from govai.economics import assess_resources
    artifact, _, _, _ = _management_fixture(2)
    row = artifact['report']['initiatives'][0]
    row['resources'] = assess_resources({'scope': 'Ограниченный прототип без подключения к рабочей системе.',
        'tasks': [{'work': 'Подготовить контрольную сверку документов.', 'role': 'Разработчик и предметный специалист',
                   'lower_days': '4', 'upper_days': '8', 'unit': 'person_days', 'origin': 'illustrative',
                   'basis': 'Диапазон зависит от числа форматов; поиск работает только по подготовленным текстам.'}],
        'dependencies': ['Нужен утверждённый справочник.'],
        'operating_cost_basis': 'Нужно измерить число обращений и определить тариф; цена неизвестна.'})
    row['proposed_flow'] = ['Материалы поступают', 'ИИ готовит сверку', 'Специалист проверяет результат']
    view = management_view(artifact)
    assert '4–8 человеко-дней' in view['pilots'][0]['resources']
    assert 'не календарный срок' in view['pilots'][0]['resources']
    assert 'утверждённый справочник' in view['pilots'][0]['resources']
    assert len(view['pilots'][0]['flow']) == 3
    assert len([h for h in view['highlights'] if h['label'] == 'человеко-дней реализации']) == 1
    assert 'Предлагаемая работа' in render_markdown(artifact)
    assert row['proposed_flow'][1] in _pdf_text(render_pdf(artifact))
    appendix = render_details(artifact).replace('\\.', '.')
    assert row['resources']['tasks'][0]['basis'] in appendix
    assert row['resources']['tasks'][0]['work'] in appendix
    assert row['resources']['operating_costs']['basis'] in appendix
    assert '4,00–8,00 человеко-дней суммарно; Сценарное допущение' in appendix


def test_ready_medium_preserves_checked_summary_without_promoting_it_to_first_pilot():
    artifact, _, _, _ = _management_fixture(1)
    artifact['report']['initiatives'][0]['prospect']['category'] = 'medium'
    summary = 'Сначала подготовить обезличенные материалы, затем сравнить помощь ИИ с обычным поиском и проверить ссылки специалистом.'
    artifact['report']['presentation'].update(origin='model', executive_summary=summary)
    view = management_view(artifact)
    assert view['outcome']['state'] == 'ready'
    assert summary in view['conclusion']['text'] and summary.replace('.', '\\.') in render_markdown(artifact)
    assert not view['pilots']
    assert [group['title'] for group in view['priority_groups']] == ['После подготовки данных или интеграций']
    assert 'До запуска' in view['conclusion']['text']


def test_summary_with_downgraded_assessment_cannot_promote_an_unjustified_pilot():
    artifact, _, _, _ = _management_fixture(1)
    artifact['report']['initiatives'][0]['prospect'].update(category='insufficient', assessment_issues=['Не определён безопасный пилот.'])
    artifact['report']['presentation'].update(origin='model', executive_summary='Немедленно внедрить неподтверждённое решение.', recommendation_title='Начать немедленное внедрение')
    view = management_view(artifact)
    assert 'Немедленно' not in view['conclusion']['text']
    assert 'немедленное' not in view['conclusion']['heading']
    assert not view['pilots'] and view['priority_groups'][0]['title'] == 'Пока не запускать'
