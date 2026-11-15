"""Pinned, user-approved reference data; never a substitute for an unmatched NPA.

This adapter does not extract, normalize or generate recommendations. The transfer
specification is a distinct artifact, not an invented OpportunityArtifact or an old
model response. Its complete PDF is returned unchanged after resource validation.
"""

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unicodedata

from govai.economics import calculate_prototype_economics
from govai.processes import ProcessError

KIND = 'govai_verified_reference'
SCHEMA_VERSION = '1'
REFERENCE_VERSION = 'reference-v3-final6'
SOURCE_SHA256 = '8d9445aca3666c6605c4c38a5baef54a2d8c042462447f7f6fe14c0d5727b6d4'
NORMALIZED_SHA256 = 'cffad20d3cded12cce2e036f3872e587516e8ee552e9e5efb243bee607fbaa7f'
PDF_SHA256 = '0d3edf893a2fa63265d88a70b52b496495dffaf94d89dd96270f6fed89d45da6'
_RESOURCES = Path(__file__).with_name('reference_data') / 'rosim261-v3'
_SOURCE = Path(__file__).resolve().parent.parent / 'examples/rosimushchestvo_261/source.txt'
_HASHES = {
    'manifest.json': '9153e9143c1fe80818557a6829e1876056df192039c2e2b031f820187d9cf5dd',
    'report.json': 'e5d538e36d9ae4751c38134fb6ab713e2d0aa5be216a5a099b076b8819992e9d',
    'final.pdf': PDF_SHA256,
}


def normalized_text(text):
    """Only the conservative normalization explicitly approved in the manifest."""
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', text.removeprefix('\ufeff'))).strip()


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':')).encode('utf-8')).hexdigest()


def _resource(name):
    try:
        data = (_RESOURCES / name).read_bytes()
    except OSError as exc:
        raise ProcessError('Не удалось прочитать сохранённый проверенный разбор. Владелец приложения должен восстановить ресурсы эталона.') from exc
    if sha256(data).hexdigest() != _HASHES[name]:
        raise ProcessError('Контрольная сумма сохранённого разбора не совпала. Эталон не подставлен; владелец приложения должен проверить ресурсы.')
    return data


def _approved():
    manifest = json.loads(_resource('manifest.json'))
    report = json.loads(_resource('report.json'))
    _resource('final.pdf')
    if (manifest['version'] != REFERENCE_VERSION or manifest['source_sha256'] != SOURCE_SHA256
            or manifest['full_text_normalized_sha256'] != NORMALIZED_SHA256
            or manifest['reference_pdf_sha256'] != PDF_SHA256
            or report['document']['revision_date'] != manifest['edition']):
        raise ProcessError('Ресурсы утверждённого разбора не согласованы между собой.')
    return manifest, report


def example_document_bytes():
    """A known full public source for an explicit sample download, not a UI override."""
    try:
        data = _SOURCE.read_bytes()
    except OSError as exc:
        raise ProcessError('Полный текст примера недоступен. Владелец приложения должен восстановить source.txt.') from exc
    if sha256(data).hexdigest() != SOURCE_SHA256:
        raise ProcessError('Контрольная сумма полного текста примера не совпала.')
    return data


def _binding(document):
    if not isinstance(document, dict) or not isinstance(document.get('text'), str):
        return None
    text = document['text']
    if sha256(normalized_text(text).encode('utf-8')).hexdigest() != NORMALIZED_SHA256:
        return None
    source = document.get('source')
    if (not isinstance(source, dict) or source.get('format') not in ('txt', 'docx', 'pdf', 'html')
            or not re.fullmatch(r'[0-9a-f]{64}', str(source.get('sha256', '')))):
        return None
    text_hash = sha256(text.encode('utf-8')).hexdigest()
    if source.get('text_sha256') != text_hash:
        return None
    # Additional unreadable PDF pages may contain material outside the matched text.
    if any(str(warning).startswith('pdf_page_without_text:') for warning in document.get('warnings', [])):
        return None
    revision = document.get('metadata', {}).get('revision_date')
    if revision and (not isinstance(revision, dict) or revision.get('value') != '2023-05-30'):
        return None
    return {'source_sha256': source['sha256'], 'text_sha256': text_hash,
            'normalized_sha256': NORMALIZED_SHA256, 'format': source['format']}


def _reference_calculations(report):
    """Recompute approved illustrative premises using the shared Decimal calculator."""
    a = report['model']['assumptions']
    results = {}
    for item in report['model']['initiatives']:
        if 'before' not in item:
            continue
        values = {
            'case_count': (a['case_count'], 'cases', 'Условная база сравнения из утверждённого отчёта; не годовой поток.'),
            'before_minutes': (item['before'], 'minutes/case', 'Сценарное время до изменения; границы: ' + item['scope']),
            'after_minutes': (item['after'], 'minutes/case', 'Сценарное время после, включая проверку и правки сотрудника.'),
            'person_days_min': (item['days'][0], 'person_days', item['team']),
            'person_days_max': (item['days'][1], 'person_days', item['team']),
            'hours_per_day': (a['hours_per_person_day'], 'hours/person_day', 'Условное число рабочих часов в человеко-дне.'),
            'team_hour_rate': (a['team_rub_per_hour'], 'RUB/hour', 'Иллюстративная стоимость часа команды; не цена поставщика.'),
            'staff_hour_rate': (a['staff_rub_per_hour'], 'RUB/hour', 'Иллюстративная стоимость часа сотрудника; не ставка ведомства.'),
            'cash_realization': (a['cash_realization'], 'share', 'Условный денежный сценарий: весь выигрыш времени сокращает расходы; это не подтверждённый факт.'),
            'variable_cost_per_case': (a['variable_rub_per_case'], 'RUB/case', 'Условные затраты обработки одного случая; не тариф провайдера.'),
        }
        parameters = {key: {'value': str(value), 'origin': 'illustrative', 'unit': unit,
                            'note': note, 'evidence_refs': []} for key, (value, unit, note) in values.items()}
        calculated = calculate_prototype_economics(parameters)
        checks = {'saved_minutes': item['saved_minutes'], 'saved_hours': item['hours_exact'],
                  'prototype_cost_min': item['prototype_cost'][0], 'prototype_cost_max': item['prototype_cost'][1],
                  'net_value_per_case': item['per_case_value'], 'net_value_for_cases': item['value_1000'],
                  'threshold_cases_min': item['break_even_cases'][0], 'threshold_cases_max': item['break_even_cases'][1]}
        for key, expected in checks.items():
            actual = calculated['results'][key]['value']
            if Decimal(str(expected)).quantize(Decimal('.01'), rounding=ROUND_HALF_EVEN) != Decimal(actual):
                raise ProcessError('Сохранённые числа эталона не подтверждены действующим калькулятором.')
        if Decimal(calculated['results']['saved_hours']['exact_value']).quantize(Decimal('1')) != item['hours']:
            raise ProcessError('Округлённые часы не совпали с утверждённым отчётом.')
        results[f'rosim261-initiative-{item["rank"]}'] = calculated
        if item['rank'] == 1:
            half = deepcopy(parameters)
            half['cash_realization'].update(value='0.5', note='Второй утверждённый сценарий: расходы сокращает половина выигрыша времени.')
            results['rosim261-initiative-1-half-cash'] = calculate_prototype_economics(half)
    return results


def _artifact(binding):
    manifest, report = _approved()
    artifact = {'kind': KIND, 'schema_version': SCHEMA_VERSION, 'reference_version': REFERENCE_VERSION,
                'matched_document': binding, 'document': deepcopy(report['document']),
                'provenance': {'origin': report['origin'], 'approved_date': manifest['approved_date'],
                               'reference_report_sha256': _HASHES['report.json'], 'pdf_sha256': PDF_SHA256,
                               'source_sha256': SOURCE_SHA256, 'api_requests': 0},
                'approved_report': report, 'calculations': _reference_calculations(report)}
    artifact['artifact_id'] = _digest(artifact)
    return artifact


def match_reference(document):
    """Return the approved artifact only for the complete, matching extracted text."""
    binding = _binding(document)
    return _artifact(binding) if binding is not None else None


def validate_reference(artifact):
    if not isinstance(artifact, dict) or artifact.get('kind') != KIND:
        raise ProcessError('Это не результат сохранённого проверенного разбора.')
    binding = artifact.get('matched_document')
    if (not isinstance(binding, dict) or set(binding) != {'source_sha256', 'text_sha256', 'normalized_sha256', 'format'}
            or binding['normalized_sha256'] != NORMALIZED_SHA256 or binding['format'] not in ('txt', 'docx', 'pdf', 'html')
            or any(not re.fullmatch(r'[0-9a-f]{64}', str(binding[key])) for key in ('source_sha256', 'text_sha256'))):
        raise ProcessError('Привязка сохранённого разбора к документу повреждена.')
    expected = _artifact(binding)
    if artifact != expected:
        raise ProcessError('Сохранённый разбор изменён или несовместим с утверждённой версией.')
    return artifact


def restore_reference_result(data, document):
    try:
        value = json.loads(data)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ProcessError('Некорректный файл reference-result.json.') from exc
    expected = match_reference(document)
    if expected is None or value != expected:
        raise ProcessError('Сохранённый разбор не соответствует полному загруженному документу или его редакции.')
    return validate_reference(value)


def reference_pdf(artifact):
    validate_reference(artifact)
    return _resource('final.pdf')


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links = [], []

    def handle_starttag(self, tag, attrs):
        if tag == 'br':
            self.parts.append('\n')
        elif tag == 'link':
            self.links.append(dict(attrs).get('href', ''))

    def handle_endtag(self, tag):
        if tag == 'link' and self.links:
            self.parts.append(' (' + self.links.pop() + ')')

    def handle_data(self, data):
        self.parts.append(data)


def _plain(value):
    parser = _PlainText(); parser.feed(value)
    return ''.join(parser.parts)


def reference_full_blocks(artifact):
    validate_reference(artifact)
    return [{'page': page['page'], 'kind': row['kind'], 'text': _plain(row['text'])}
            for page in artifact['approved_report']['pages'] for row in page['paragraphs']]


def reference_view(artifact):
    validate_reference(artifact)
    report = artifact['approved_report']; pages = report['pages']; cards = []
    inputs = {1: _plain(pages[2]['paragraphs'][2]['text']).replace('\n', ' '),
              2: 'пояснительная записка, материалы дела и утверждённый шаблон одного вида акта',
              3: 'вопрос заявителя и утверждённые материалы о порядке услуги'}
    human = {1: 'Исполнитель проверяет выводы и определяет дальнейшее действие.',
             2: 'Специалист проверяет правовые основания и исправляет текст. Затем документ проходит обычные согласование и подпись.',
             3: 'Сотрудник МФЦ проверяет ответ и объясняет заявителю порядок действий.'}
    for item in report['model']['initiatives']:
        rank = item['rank']; key = f'rosim261-initiative-{rank}'
        description = item['ai']
        if rank <= 3:
            description = 'На входе — ' + inputs[rank].lower() + '. ' + item['ai']
            description += ' Сотрудник получает ' + item['result'][0].lower() + item['result'][1:]
            effect = f"{item['hours']} ч на 1 000 операций · {item['before']} → {item['after']} мин на случай. Сценарий, включая проверку и правки сотрудника."
            resources = f"{item['days'][0]}–{item['days'][1]} человеко-дней. Сценарий для отдельного прототипа, не полного внедрения."
            condition = item['scope']
        else:
            description += ' ' + item['needed']
            effect = item['needed']; resources = 'Ресурс пока не оценён.'; condition = item['needed']
        cards.append({'id': key, 'number': rank, 'rank': rank, 'title': item['process'], 'process': item['process'],
                      'description': description, 'input_materials': inputs.get(rank, ''), 'ai_work': item['ai'],
                      'staff_result': item.get('result', ''), 'human_role': human.get(rank, ''),
                      'benefit': item.get('result', item['ai']), 'effect': effect, 'resources': resources,
                      'priority_reason': item.get('why', item.get('reason', '')), 'dependencies': [1] if rank == 2 else [],
                      'change_condition': ('Готовая база и большой поток могут поднять приоритет.' if rank == 3 else item.get('needed', 'Сравнить результат с обычным способом на одинаковых материалах.')),
                      'condition': condition, 'clauses': 'пп. ' + item['clauses'],
                      'queue': 'first' if rank <= 3 else 'reserve', 'category_label': 'В первую очередь' if rank <= 3 else 'Резерв',
                      'color': item['fill']})
    heading = _plain(pages[0]['paragraphs'][0]['text']).replace('\n', ' ')
    intro = _plain(pages[0]['paragraphs'][1]['text'])
    view = {'title': heading, 'summary_heading': heading, 'summary_intro': intro,
            'reference_label': 'Проверенный разбор · редакция 30.05.2023',
            'outcome': {'state': 'ready', 'title': 'Проверенный разбор', 'message': intro, 'action': ''},
            'conclusion': {'heading': heading, 'text': intro}, 'cards': cards, 'initiatives': cards,
            'approved_pages': [{**p, 'paragraphs': [{**row, 'text': _plain(row['text'])} for row in p['paragraphs']]} for p in pages],
            'process_groups': deepcopy(report['process_groups']),
            'scope': 'Полный текст приказа №261 · редакция 30.05.2023 · сохранённый одобренный разбор.',
            'requisites': 'Росимущество · Приказ №261 от 11.09.2020 · редакция 30.05.2023',
            'full_title': 'Административный регламент предоставления федеральных земельных участков без торгов',
            'source_index': [{'number': row['number'], 'clauses': row['clauses']} for row in cards],
            'conditions': [_plain(pages[3]['paragraphs'][-1]['text']), report['economics_scope']],
            'detail_note': 'В полном отчёте — карта процессов, подробные предложения и расчёт экономики',
            'input_limitation': None, 'highlights': [], 'priority_groups': [], 'pilots': [], 'effects': [],
            'effect_note': 'Сценарные предпосылки не являются измерениями ведомства или доказанной бюджетной экономией.',
            'ordering_note': intro}
    return view


def render_reference_markdown(artifact):
    blocks = reference_full_blocks(artifact)
    lines = []
    for row in blocks:
        lines += [('## ' if row['kind'] == 'heading' else '') + row['text'], '']
    return '\n'.join(lines).strip() + '\n'


def render_reference_details(artifact):
    validate_reference(artifact)
    return ('# Сохранённый проверенный разбор\n\n'
            'Утверждённая версия: ' + REFERENCE_VERSION + '. Новых вызовов модели нет.\n'
            'Это адаптация одобренного отчёта; исходный пакет не является OpportunityArtifact и не выдаётся за восстановленный план LLM.\n\n'
            'SHA-256 полного исходника: ' + SOURCE_SHA256 + '\n'
            'SHA-256 утверждённого PDF: ' + PDF_SHA256 + '\n\n'
            '## Проверяемые программные расчёты\n\n'
            'Все числовые входы этого эталона — явно иллюстративные предпосылки. Они не используются как значения по умолчанию для других документов.\n\n'
            + json.dumps(artifact['calculations'], ensure_ascii=False, indent=2) + '\n\n'
            '## Полный утверждённый текст отчёта\n\n' + render_reference_markdown(artifact))


verified_reference_pdf = reference_pdf
reference_markdown = render_reference_markdown
reference_details = render_reference_details
