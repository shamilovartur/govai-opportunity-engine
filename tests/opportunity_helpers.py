"""Small real-source excerpts and explicitly hand-authored model plans, never live results."""
from copy import deepcopy
from govai.economics import REQUIRED, illustrative_scenarios
from govai.normalization import normalize_analysis
from govai.opportunities import OpportunityPlan
from test_normalization import real_case, simple_case, node, plan_for, Normalizer, ACCEPT, FIX, NO_ERROR


def map_case(incomplete=False):
    doc, raw, analysis = real_case()
    nodes = [node(analysis, ACCEPT, key='receipt', process='intake', title='Принять заявительные документы'),
             node(analysis, FIX, key='correction', process='errors', title='Исправить опечатки и ошибки'),
             node(analysis, NO_ERROR, key='answer', process='errors', title='Подготовить ответ об отсутствии ошибки')]
    used = set(ACCEPT + FIX + NO_ERROR)
    if not incomplete:
        for i, sid in enumerate(analysis.actions):
            if sid not in used:
                nodes.append(node(analysis, [sid], key=f'context-{i}', kind='context', process='context'))
    normalized = normalize_analysis(analysis, Normalizer(plan_for(analysis, nodes,
        titles={'intake': 'Приём документов', 'errors': 'Исправление ошибок', 'context': 'Контекст'})))
    return doc, raw, analysis, normalized


def simple_map(lines=('Специалист анализирует заявление и готовит проект ответа.',)):
    doc, raw, analysis = simple_case(lines)
    nodes = [node(analysis, [sid], key=f'op-{i}', process=f'proc-{i}', title=a['description'])
             for i, (sid, a) in enumerate(analysis.actions.items())]
    normalized = normalize_analysis(analysis, Normalizer(plan_for(analysis, nodes)))
    return doc, raw, analysis, normalized


def prospect_assessment(**changes):
    value = {'value_case': 'concrete',
        'value_reason': 'Есть содержательное сопоставление документов, которое не сводится к проверке реквизитов.',
        'pilot_feasibility': 'needs_preparation',
        'pilot_reason': 'Для ограниченного пилота сначала подтвердить доступ к обезличенным примерам одной процедуры.',
        'main_uncertainty': 'Готовность корпуса и состав документов неизвестны.',
        'change_condition': 'Если доступен экспертно размеченный корпус, можно начать ограниченный пилот.',
        'pilot': 'Сравнить помощь ИИ и обычный поиск на обезличенных законченных делах одной процедуры без отправки решений.',
        'success_criterion': 'Меньше времени на проверенный проект, без пропуска существенных обстоятельств и неподтверждённых выводов.'}
    value.update(changes)
    return value


def measurement(normalized, aids, model='labor_time', **changes):
    category, unit, name = {
        'time_capacity': ('money', 'RUB/year', 'Годовой денежный эквивалент высвобождённой мощности'),
        'labor_time': ('labor', 'minutes/case', 'Рабочие минуты на проверенный проект'),
        'elapsed_time': ('elapsed_time', 'minutes/process', 'Время прохождения выбранного этапа'),
        'qualitative': ('quality', 'share', 'Доля выводов с корректным подтверждением'),
    }[model]
    value = {'source_action_ids': list(dict.fromkeys(sid for a in normalized['map']['actions'] if a['id'] in aids for sid in a['source_action_ids'])),
        'work': 'Поиск оснований, сопоставление материалов и проверка проекта ответа.',
        'case_unit': 'Один законченный комплект документов выбранного вида.',
        'boundary': 'От начала чтения до проверенного проекта; отправка и административное решение не включаются.',
        'population': 'all_cases' if model == 'time_capacity' else 'eligible_cases',
        'primary': {'category': category, 'name': name, 'unit': unit}, 'secondary': None,
        'method': 'Сравнить контрольную обработку и работу с ИИ на одинаковых обезличенных материалах; эксперт проверяет результат.',
        'success_criterion': 'Проверенный результат без дополнительных существенных ошибок при меньших трудозатратах.',
        'human_review_included': True, 'time_scenarios': []}
    value.update(changes)
    return value


def labor_assumptions():
    rows = []
    for name, before, after, note in [
        ('conservative', '20', '16', 'Сложные формулировки требуют полной перепроверки и значительных правок проекта.'),
        ('base', '20', '12', 'Читаемый типовой комплект; проверка извлечения и исправление проекта остаются специалисту.'),
        ('optimistic', '20', '10', 'Однотипный читаемый комплект, но источники и проект всё равно проверяет специалист.')]:
        rows.append({'name': name, 'rationale': note, **{key: {
            'value': value, 'origin': 'illustrative', 'unit': 'minutes/case',
            'note': note, 'evidence_refs': []} for key, value in [('before_minutes', before), ('after_minutes', after)]}})
    return rows


def proposal(normalized, aids=None, key='pilot', **changes):
    aids = aids or [a['id'] for a in normalized['map']['actions'] if a['kind'] == 'operation'][:1]
    refs = list(dict.fromkeys(e for a in normalized['map']['actions'] if a['id'] in aids for e in a['evidence_refs']))
    value = {'key': key, 'title': 'Подготовка проекта ответа с нормативным поиском',
        'problem': 'Проверить гипотезу трудоёмкости сопоставления материалов и подготовки проекта.',
        'ai_role': 'Находит релевантные основания и готовит черновик с проверяемыми ссылками.',
        'human_role': 'Уполномоченный специалист проверяет источники и утверждает документ.',
        'human_approval_required': True, 'task_type': 'rag_drafting',
        'why_ai': 'Содержательный поиск по неструктурированным материалам, а не только фиксированный шаблон.',
        'non_ai_alternative': 'Шаблоны и поиск по реквизитам; сравнить с ними на пилоте.',
        'ai_necessity': 'justified_hypothesis', 'expected_effect': 'Возможно сократить время поиска и подготовки проекта.',
        'conditions': ['Проверенный корпус документов, контроль доступа, пилот с замером рабочего времени.'],
        'risks': ['Ошибки генерации, персональные данные и неверные нормативные основания.'],
        'action_ids': aids, 'context_action_ids': [], 'evidence_refs': refs,
        'novelty': 'new_support', 'incremental_value': None, 'economic_model': 'time_capacity',
        'effect_mechanism': 'Сокращение времени поиска и чернового составления за вычетом проверки человеком.',
        'required_parameters': list(REQUIRED),
        'prospect_assessment': prospect_assessment(),
        'uncertainties': ['Реальный объём, рабочие минуты, наличие аналогичных систем и качество корпуса неизвестны.']}
    value.update(changes)
    if 'measurement' not in changes:
        value['measurement'] = measurement(normalized, aids, value['economic_model'])
    return value


def three_proposals(normalized):
    items = [proposal(normalized, ['receipt'], key='documents', title='Извлечение сведений из заявительных документов',
        task_type='information_extraction', ai_role='Предзаполняет сведения из неструктурированных документов; специалист сверяет оригинал.',
        problem='Проверить, возникают ли повторный ввод и поиск реквизитов в разноформатных заявительных документах при приёме.',
        human_role='Сверяет извлечённые сведения с оригиналом; решение о приёме документов остаётся специалисту.',
        why_ai='Если документы имеют разные формулировки и структуры, фиксированная схема полей может быть недостаточна.',
        non_ai_alternative='Электронные формы и детерминированные проверки полей; сначала сравнить с этим вариантом.',
        expected_effect='Возможное сокращение повторного ввода и поиска сведений, если такие трудозатраты подтвердятся.',
        effect_mechanism='Снижение времени чтения и переноса реквизитов за вычетом сверки специалистом.',
        conditions=['Доступ к образцам входных документов, перечень нужных реквизитов, разметка и замеры на пилоте.']),
        proposal(normalized, ['correction'], key='correction-help', title='Сверка материалов при исправлении ошибок',
        task_type='semantic_review', ai_role='Сопоставляет исходные материалы и выданный документ, предлагает кандидаты расхождений.',
        problem='Проверить трудоёмкость сопоставления материалов при рассмотрении обращения об ошибке.',
        human_role='Устанавливает наличие ошибки и утверждает исправление либо ответ об отсутствии ошибки.',
        why_ai='Содержательные расхождения и переформулировки могут не выявляться обычным посимвольным сравнением.',
        non_ai_alternative='Посимвольное сравнение, проверка реквизитов и версий документов.',
        expected_effect='Возможно сократить поиск расхождений, сохранив оба исхода проверки.',
        effect_mechanism='Меньше времени на сопоставление версий; экспертная квалификация ошибки остаётся человеку.',
        conditions=['Связанные версии исходных и выданных документов, проверенные примеры ошибок, защищённый доступ.']),
        proposal(normalized, ['answer'], key='grounded-reply', title='Проект обоснованного ответа заявителю')]
    items[0]['prospect_assessment']['main_uncertainty'] = 'Подтвердить доступ к обезличенным комплектам документов.'
    items[1]['prospect_assessment']['main_uncertainty'] = 'Уточнить границу исправления технической ошибки и пересмотра решения.'
    return OpportunityPlan(proposals=items, rejected=[], relationships=[], explanation='Ручной тестовый план на реальных фрагментах приказа, не вызов LLM.')


class OpportunityMock:
    name, model = 'openai', 'test-model'
    def __init__(self, plan): self.plan, self.calls = plan, []
    def opportunities(self, payload, **kw):
        self.calls.append(deepcopy(payload))
        return self.plan, {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}
    def close(self): pass


def supplied_scenarios():
    cases = [s.model_dump() for s in illustrative_scenarios()]
    for s in cases:
        for p in s['parameters'].values():
            p['origin'] = 'user'; p['note'] = 'Тестовые данные пользователя, не факты ведомства.'
    return cases
