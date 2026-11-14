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
        'priority': {'integration_complexity': 'low', 'data_readiness': 'unknown', 'delivery_effort': 'small',
                     'risk': 'medium', 'priority_basis': 'Пилот без записи в ведомственные системы; готовность корпуса неизвестна.'},
        'uncertainties': ['Реальный объём, рабочие минуты, наличие аналогичных систем и качество корпуса неизвестны.']}
    value.update(changes)
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
    items[0]['priority'].update(integration_complexity='medium', delivery_effort='medium')
    items[1]['priority'].update(integration_complexity='medium', delivery_effort='medium', risk='high')
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
