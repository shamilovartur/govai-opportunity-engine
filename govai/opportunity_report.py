"""Deterministic, inspectable prioritization and a compact executive Markdown report."""

import re
from decimal import Decimal

from govai.economics import FORMULAS, UNITS
from govai.analysis_io import digest

PRIORITY_VERSION = 'priority-1.1'
PRIORITY_RULES = {
    'weights': {'economic': 25, 'feasibility': 20, 'data': 15, 'delivery': 10, 'risk': 20, 'reliability': 10},
    'feasibility': {'low': 5, 'medium': 3, 'high': 1, 'unknown': 0},
    'data': {'ready': 5, 'partial': 2, 'missing': 0, 'unknown': 0},
    'delivery': {'small': 5, 'medium': 3, 'large': 1, 'unknown': 0},
    'risk': {'low': 5, 'medium': 2, 'high': 0, 'unknown': 0},
    'reliability': {'document': 4, 'user': 3, 'illustrative': 0, 'unknown': 0},
    'economic_metric': 'annual_net_capacity_equivalent',
    'economic_thresholds_RUB_per_year': [0, 100000, 300000, 1000000, 3000000],
    'high_threshold': 75, 'preparation_threshold': 30,
    'high_minimum_economic_score': 3, 'high_requires_positive_first_year': True,
    'unknown_policy': 'zero_without_weight_redistribution',
    'illustrative_economic_score': 0,
}
CATEGORIES = {'pilot_candidate': 'Кандидат на быстрый пилот после проверки предпосылок',
              'preparation': 'Перспективная инициатива, требующая подготовки',
              'deferred': 'Отложить до подтверждения предпосылок'}
SCENARIO_LABELS = {'conservative': 'Консервативный', 'base': 'Базовый', 'optimistic': 'Оптимистичный'}
ORIGINS = {'document': 'Указано в документе', 'user': 'Предоставлено пользователем',
           'illustrative': 'Иллюстративное допущение', 'unknown': 'Неизвестно'}
PARAMETERS = {'annual_cases': 'Объём за год', 'applicability': 'Доля применимых случаев',
    'adoption': 'Доля использования', 'before_minutes': 'Минут работы до', 'after_minutes': 'Минут работы после, включая проверку',
    'realization': 'Коэффициент достижения эффекта', 'hour_cost': 'Полная стоимость часа',
    'annual_fixed_opex': 'Постоянные расходы в год', 'variable_cost_per_case': 'Расходы на обработанный случай',
    'implementation_cost': 'Единовременное внедрение'}


def rank_initiatives(initiatives, *, incomplete_input=False):
    ranked = []
    for initiative in initiatives:
        a = initiative['priority_assessment']; econ = initiative['economics']
        base = next((s for s in econ['scenarios'] if s['name'] == 'base'), None)
        economic, reliability = 0, 0
        if base and base['status'] == 'conditional_estimate' and econ['status'] == 'conditional_estimate':
            amount = base['results']['annual_net_capacity_equivalent']['value']
            economic = sum(Decimal(amount) > Decimal(t) for t in PRIORITY_RULES['economic_thresholds_RUB_per_year']) if amount is not None else 0
            reliability = min(PRIORITY_RULES['reliability'][p['origin']] for p in base['parameters'].values())
        scores = {'economic': economic, 'feasibility': PRIORITY_RULES['feasibility'][a['integration_complexity']],
                  'data': PRIORITY_RULES['data'][a['data_readiness']], 'delivery': PRIORITY_RULES['delivery'][a['delivery_effort']],
                  'risk': PRIORITY_RULES['risk'][a['risk']], 'reliability': reliability}
        contributions = {k: float(Decimal(v) * Decimal(PRIORITY_RULES['weights'][k]) / 5) for k, v in scores.items()}
        total = round(sum(contributions.values()), 2)
        unknown = [k for k, v in a.items() if v == 'unknown']
        first_year = base['results']['first_year_net_capacity_equivalent']['value'] if base else None
        if (total >= PRIORITY_RULES['high_threshold'] and reliability >= 3
                and economic >= PRIORITY_RULES['high_minimum_economic_score']
                and first_year is not None and Decimal(first_year) > 0
                and not incomplete_input and not unknown and a['risk'] == 'low'):
            category = 'pilot_candidate'
        elif total >= PRIORITY_RULES['preparation_threshold']:
            category = 'preparation'
        else:
            category = 'deferred'
        ranked.append({**initiative, 'priority': {'score': total, 'category': category,
            'label': CATEGORIES[category], 'scores_0_to_5': scores, 'weighted_contributions': contributions,
            'weights': PRIORITY_RULES['weights'], 'unknown_criteria': unknown, 'preliminary': True,
            'reason': a['priority_basis'],
            'limitations': (['Количественная часть не оценена: финансовых оснований или индивидуальных параметров недостаточно.']
                           if econ['status'] == 'needs_data' else []) + ['Оценки интеграции, готовности данных, сроков пилота и рисков — гипотезы модели.',
                           'Иллюстративная экономика не повышает экономический балл; неизвестные критерии дают 0.']}})
    ranked.sort(key=lambda a: (-a['priority']['score'], a['id']))
    return [{**a, 'rank': i+1} for i, a in enumerate(ranked)]


def _safe(value):
    # Escape untrusted text; never embed active HTML or model-generated Markdown links.
    return re.sub(r'([\\`*_{}\[\]()#+.!<>|])', r'\\\1', ' '.join(str(value).split()))


def _number(value):
    return 'Требуются данные' if value is None else format(Decimal(value), ',.2f').replace(',', ' ')


def economic_summary(initiative):
    e = initiative['economics']
    base = next((s for s in e['scenarios'] if s['name'] == 'base'), None)
    if not base or base['results']['released_hours']['value'] is None:
        return 'Требуются дополнительные данные'
    prefix = 'Иллюстрация: ' if base['status'] == 'illustrative' else 'Условная оценка: '
    return prefix + _number(base['results']['released_hours']['value']) + ' ч/год; ' + _number(
        base['results']['capacity_equivalent']['value']) + ' руб./год эквивалента мощности'


def render_markdown(artifact):
    r = artifact['report']; initiatives = r['initiatives']; d = r['document']
    assumption_sets = {}
    for a in initiatives:
        cases = a['economics']['scenarios']
        if any(p['value'] is not None for case in cases for p in case['parameters'].values()):
            parameters = {s['name']: s['parameters'] for s in cases}
            assumption_sets.setdefault(digest(parameters), {'label': f'Набор {len(assumption_sets)+1}', 'parameters': parameters})
    document = ' '.join(str(d.get(k) or '') for k in ('act_type', 'number', 'adoption_date')).strip()
    lines = ['# AI-возможности и экономика', '', '## Резюме для руководителя', '',
        _safe(document or 'Нормативный акт'), '',
        'Предварительный отчёт. Это предложения для обсуждения и пилота, а не инвестиционное заключение.', '',
        f"Инициатив: {len(initiatives)}. Отклонённых сценариев: {len(r['rejected'])}. "
        + f"Карта процессов: {"требует проверки" if r['normalization_status'] != "completed" else "обработана; содержательная проверка необходима"}.", '']
    if d.get('revision_date'): lines += ['Редакция: ' + _safe(d['revision_date']), '']
    for warning in r['warnings']: lines += ['- ' + _safe(warning)]
    lines += ['', 'Наиболее перспективные направления по текущим предпосылкам:', '']
    for a in initiatives[:3]:
        lines += [f"- **{_safe(a['title'])}** — {_safe(a['expected_effect'])} {_safe(a['priority']['label'])}."]
    if not initiatives: lines += [_safe(r['explanation'])]
    lines += ['', '## Рейтинг инициатив', '',
              '| Место | Инициатива и процесс | Решение | Механизм эффекта | Индивидуальная оценка | Приоритет | Главное ограничение |',
              '| --- | --- | --- | --- | --- | --- | --- |']
    for a in initiatives:
        processes = '; '.join(dict.fromkeys(g['process_title'] for g in a['grounding']))
        lines += [f"| {a['rank']} | {_safe(a['title'])}; {_safe(processes)} | {_safe(a['ai_role'])} | "
                  f"{_safe(a['effect_mechanism'])} | {_safe(economic_summary(a))} | {a['priority']['score']}/100; {_safe(a['priority']['label'])} | {_safe(a['uncertainties'][0])} |"]
    lines += ['', '**Совокупная экономия не рассчитана:** независимость эффектов не подтверждена. '
              'Высвобождение часов и его стоимостный эквивалент не являются гарантированной экономией бюджета.', '']
    if r['portfolio']['conflicts']:
        lines += ['Пересечения, требующие проверки:', '']
        by_id = {a['id']: a['title'] for a in initiatives}
        for c in r['portfolio']['conflicts']:
            lines += ['- ' + ' / '.join(_safe(by_id[k]) for k in c['initiative_ids']) + ': ' + _safe(c['reason'])]
    lines += ['', '## Краткие business cases', '']
    for a in initiatives:
        lines += [f"### {a['rank']}. {_safe(a['title'])}", '',
                  '**Проблема — гипотеза:** ' + _safe(a['problem']), '',
                  '**Модель:** ' + _safe(a['ai_role']), '', '**Человек:** ' + _safe(a['human_role']), '',
                  'Утверждение юридически значимых результатов остаётся уполномоченному человеку.', '',
                  '**Почему ИИ:** ' + _safe(a['why_ai']), '',
                  '**Обычная автоматизация:** ' + _safe(a['non_ai_alternative']), '',
                  '**Механизм эффекта:** ' + _safe(a['effect_mechanism']), '',
                  '**Ожидаемый эффект — гипотеза:** ' + _safe(a['expected_effect']), '',
                  '**Данные и внедрение:** ' + '; '.join(_safe(x) for x in a['conditions']), '',
                  '**Ограничения и риски:** ' + '; '.join(_safe(x) for x in a['risks'] + a['uncertainties']), '',
                  '**Основание рейтинга:** ' + _safe(a['priority']['reason']), '',
                  'Вклад критериев в балл: ' + ', '.join(f'{k}={v:g}' for k, v in a['priority']['weighted_contributions'].items()) + '.', '']
        if a['incremental_value']: lines += ['**Добавочная ценность:** ' + _safe(a['incremental_value']), '']
        e = a['economics']
        for reason in e['reasons']: lines += [_safe(reason), '']
        if any(v['value'] is not None for case in e['scenarios'] for v in case['results'].values()):
            lines += ['| Сценарий | Часы/год | Эквивалент мощности, руб./год | OPEX, руб./год | Эквивалент за вычетом OPEX | Первый год за вычетом внедрения |',
                      '| --- | --- | --- | --- | --- | --- |']
            for s in e['scenarios']:
                v = s['results']; label = {'illustrative': ' — иллюстрация', 'conditional_estimate': ' — условная оценка',
                                         'needs_data': ' — неполные данные'}[s['status']]
                lines += ['| ' + SCENARIO_LABELS[s['name']] + label + ' | ' + ' | '.join(_number(v[k]['value']) for k in (
                    'released_hours', 'capacity_equivalent', 'annual_opex', 'annual_net_capacity_equivalent', 'first_year_net_capacity_equivalent')) + ' |']
        if any(p['value'] is not None for case in e['scenarios'] for p in case['parameters'].values()):
            set_key = digest({s['name']: s['parameters'] for s in e['scenarios']})
            lines += ['', assumption_sets[set_key]['label'] + ' предпосылок приведён в методике ниже.', '']
        if e['status'] == 'needs_data':
            lines += ['Количественная часть рейтинга не оценена. Нулевой вклад в балл не означает нулевую экономию.', '']
        if e['required_parameters']:
            lines += ['Для инвестиционного решения нужны: ' + ', '.join(PARAMETERS[k] for k in e['required_parameters']) + '.', '']
    lines += ['## Экономическая методика и приоритизация', '',
        'Экономика рассчитывается Python с десятичной арифметикой. Неизвестное значение — null, не ноль. '
        'Отрицательный эффект сохраняется. Единицы: часы/год и рубли; доли — от 0 до 1.', '']
    for key, formula in FORMULAS.items(): lines += [f'- `{key} = {formula}`']
    lines += ['', 'Рабочие минуты относятся к одному случаю в границах инициативы, '
              'суммарно по её операциям. На количество исходных ID они не умножаются.', '']
    for group in assumption_sets.values():
        lines += ['### ' + group['label'] + ' предпосылок', '',
                  'Одинаковые иллюстративные предпосылки не делают эффекты независимыми.', '',
                  '| Параметр и единица | Консервативный | Базовый | Оптимистичный |', '| --- | --- | --- | --- |']
        for key, unit in UNITS.items():
            values = []
            for name in ('conservative', 'base', 'optimistic'):
                p = group['parameters'][name][key]
                values.append(_safe((p['value'] if p['value'] is not None else 'неизвестно') + ' — ' + ORIGINS[p['origin']]))
            lines += ['| ' + PARAMETERS[key] + ' (' + unit + ') | ' + ' | '.join(values) + ' |']
        lines += ['']
    lines += ['', 'Время после внедрения должно включать человеческую проверку. Нормативный срок услуги не заменяет рабочие минуты.', '',
        'Баллы: сумма вес × оценка / 5. Веса: экономика 25, реализуемость 20, данные 15, '
        'срок пилота 10, риски 20, надёжность предпосылок 10. Неизвестное даёт 0 без '
        'перераспределения веса. Иллюстративные числа не дают экономических баллов. '
        'Операционные оценки модели — предварительные гипотезы; точные шкалы сохранены в документации и коде.', '',
        '## Доказательная база', '',
        'Цитаты подтверждают операции и их расположение. Они не подтверждают экономический эффект или готовность ИТ.', '']
    for a in initiatives:
        lines += ['**' + _safe(a['title']) + '**', '',
                  'Действия карты: ' + ', '.join(_safe(x) for x in a['action_ids']) + '.',
                  'Исходные кандидаты: ' + ', '.join(_safe(x) for x in a['source_action_ids']) + '.', '']
        # A short report uses one primary proof per initiative; the full chain remains in JSON.
        eid = a['grounding'][0]['source_candidates'][0]['evidence_refs'][0]
        proof = r['evidence'][eid]
        lines += ['Пункт ' + _safe(proof['clause_number'] or 'не определён') +
                  (f"; страница PDF {proof['page_number']}" if proof['page_number'] else '') + ':', '']
        fence = '`' * max(3, 1 + max((len(x) for x in re.findall(r'`+', proof['quote'])), default=0))
        lines += [fence + 'text', proof['quote'], fence, '',
                  'Остальные точные цитаты, координаты и связи сохранены в opportunities.json.', '']
    if r['rejected']:
        lines += ['## Отклонённые сценарии', '']
        lines += ['- ' + _safe(x['title']) + ': ' + _safe(x['reason']) for x in r['rejected']]
    lines += ['', '## Что проверить перед инвестиционным решением', '',
        '- Качество и законность доступа к данным, действующие системы и интеграции.',
        '- Реальный объём, рабочие минуты до/после с проверкой человеком, долю использования, качество и стоимость эксплуатации.',
        '- Независимость эффектов и отсутствие повторного учёта; бюджетное сокращение требует отдельных доказанных предпосылок.',
        '- Спорные связи исходной карты и пропуски первого этапа. Этот отчёт не заменяет проверку НПА.', '',
        'Метрики этапа: ' + str(artifact['metrics']['requests']) + ' запросов; ' +
        f"{artifact['metrics']['elapsed_seconds']:.2f} с. Токены: " + _safe(artifact['metrics']['usage'] or 'неизвестны') + '.', '']
    return '\n'.join(lines)
