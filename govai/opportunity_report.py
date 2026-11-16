"""A short qualitative rubric and a management report over verified Python results."""

import re
from copy import deepcopy
from datetime import date
from decimal import Decimal

from govai.processes import ProcessError

PROSPECT_VERSION = 'prospect-1.1'
PROSPECT_RULES = {
    'insufficient': 'Механизм добавочной ценности неясен или возможность ограниченного пилота неизвестна.',
    'low': 'Добавочная ценность слаба либо ограниченный пилот пока не обоснован.',
    'medium': 'Механизм конкретен, но перед пилотом нужно устранить существенную неопределённость.',
    'high': 'Механизм конкретен; предложен ограниченный измеримый пилот с контролем рисков.',
    'unknown_data': 'Неизвестная готовность данных отражается в неопределённости, а не автоматически в низкой категории.',
    'scope': 'Перспективность дальнейшей проработки не означает доказанную окупаемость или готовность к внедрению.',
    'consistency': 'Противоречивый или неполный механизм, пилот либо протокол измерения означает недостаток оснований; отсутствие числового прогноза само по себе категорию не снижает.',
}
CATEGORIES = {'high': 'Высокая перспективность', 'medium': 'Средняя перспективность',
              'low': 'Низкая перспективность', 'insufficient': 'Недостаточно оснований для оценки'}
SCENARIO_LABELS = {'conservative': 'Консервативный', 'base': 'Базовый', 'optimistic': 'Оптимистичный'}
ORIGINS = {'document': 'Подтверждено документом', 'user': 'Предоставлено пользователем',
           'illustrative': 'Сценарное допущение', 'unknown': 'Неизвестно'}
PARAMETERS = {'annual_cases': 'Обращений за год', 'case_count': 'Число случаев в расчёте',
    'applicability': 'Доля подходящих случаев', 'adoption': 'Доля использования',
    'before_minutes': 'Работа до', 'after_minutes': 'Работа после, с проверкой и исправлением',
    'before_elapsed_minutes': 'Длительность этапа до', 'after_elapsed_minutes': 'Длительность этапа после',
    'realization': 'Коэффициент достижения эффекта', 'hour_cost': 'Полная стоимость часа',
    'annual_fixed_opex': 'Постоянные расходы за год', 'variable_cost_per_case': 'Расходы на случай',
    'implementation_cost': 'Единовременное внедрение'}
UNIT_LABELS = {'minutes/case': 'человеко-минут на случай', 'hours': 'человеко-часов',
    'minutes/process': 'минут на этап', 'share': 'доля', 'cases': 'случаев', 'cases/year': 'случаев/год',
    'RUB/hour': 'руб./час', 'RUB/year': 'руб./год', 'RUB/case': 'руб./случай', 'RUB': 'руб.'}


def meaningful_text(value):
    """Check explanatory structure, not truth or semantic quality of the hypothesis."""
    return isinstance(value, str) and len(value.strip()) >= 20 and len(re.findall(r'\w+', value)) >= 3


def assess_prospects(initiatives, *, incomplete_input=False):
    """Apply a visible nonnumeric rubric; preserve order within each category.

    Source uncertainty is retained by the report, not converted to a penalty for
    every hypothesis. The assessment's substantive claims still require review.
    """
    assessed = []
    for initiative in initiatives:
        a = initiative['prospect_assessment']
        if a['value_case'] not in ('concrete', 'weak', 'unclear') or a['pilot_feasibility'] not in (
                'bounded', 'needs_preparation', 'not_justified', 'unknown'):
            raise ValueError('Неизвестное основание качественной оценки.')
        if a['value_case'] == 'unclear' or a['pilot_feasibility'] == 'unknown':
            category = 'insufficient'
        elif a['value_case'] == 'weak' or a['pilot_feasibility'] == 'not_justified':
            category = 'low'
        elif a['pilot_feasibility'] == 'needs_preparation':
            category = 'medium'
        else:
            category = 'high'
        issues = []
        if initiative.get('ai_necessity') == 'uncertain' or initiative.get('novelty') == 'unknown':
            issues.append('Добавочная ценность ИИ пока не обоснована относительно обычного или существующего решения.')
        if not all(meaningful_text(initiative.get(k)) for k in ('why_ai', 'non_ai_alternative')):
            issues.append('Не раскрыто преимущество ИИ относительно обычной автоматизации.')
        elif initiative['why_ai'].strip().casefold() == initiative['non_ai_alternative'].strip().casefold():
            issues.append('Описание пользы ИИ повторяет обычную альтернативу; добавочная ценность не раскрыта.')
        m = initiative.get('measurement', {})
        if not all(meaningful_text(a.get(k)) for k in ('value_reason', 'pilot_reason', 'pilot', 'success_criterion', 'change_condition', 'main_uncertainty')):
            issues.append('Не определены основания, условия пересмотра оценки или проверяемый ограниченный пилот.')
        if (len(str(m.get('case_unit') or '').strip()) < 3 or
                not all(meaningful_text(m.get(k)) for k in ('work', 'boundary', 'method', 'success_criterion'))):
            issues.append('Нужно уточнить измеряемую работу, границы, единицу случая и критерий пилота.')
        if not meaningful_text(initiative.get('human_role')) or not initiative.get('human_approval_required'):
            issues.append('Не определена проверка и ответственность уполномоченного человека.')
        if not initiative.get('risks') or not all(meaningful_text(x) for x in initiative['risks']):
            issues.append('Риски и ограничения пилота не раскрыты; допустимость проверки требует уточнения.')
        quality = any(metric and metric.get('category') == 'quality' for metric in (m.get('primary'), m.get('secondary')))
        if quality and not all(meaningful_text((m.get('quality_protocol') or {}).get(k))
                               for k in ('calculation', 'expert_reference', 'unacceptable_errors')):
            issues.append('Для показателя качества нужны способ расчёта, экспертный эталон и перечень недопустимых ошибок.')
        issues.extend(m.get('validation_issues', []))
        if issues:
            category = 'insufficient'
        assessed.append({**initiative, 'prospect': {
            'category': category, 'label': CATEGORIES[category],
            'reason': ' '.join((a['value_reason'].strip(), a['pilot_reason'].strip(), *issues)).strip(),
            'main_uncertainty': a['main_uncertainty'], 'change_condition': a['change_condition'],
            'pilot': a['pilot'], 'success_criterion': a['success_criterion'], 'preliminary': True,
            'assessment_issues': issues,
        }})
    # These are named groups, not scores or ordinal places within a group.
    return [a for category in CATEGORIES for a in assessed if a['prospect']['category'] == category]



def number_initiatives(report, editorial_inputs=None):
    """Number the discussion agenda without inventing scores or changing categories.

    An explicit editorial order is a reviewable input, not an inferred objective
    superiority. In its absence, equal-category initiatives retain their order.
    The original report and source IDs are never mutated.
    """
    result = deepcopy(report)
    rows = result['initiatives']
    ids = [row['id'] for row in rows]
    if len(set(ids)) != len(ids):
        raise ProcessError('Повторяющиеся ID инициатив не позволяют определить очередность.')
    rows = [row for category in CATEGORIES for row in rows if row['prospect']['category'] == category]
    if len(rows) != len(ids):
        raise ProcessError('Неизвестная категория перспективности.')
    editorial = {} if editorial_inputs is None else editorial_inputs
    allowed = {'order', 'ordering_basis', 'executive_summary', 'initiatives'}
    if not isinstance(editorial, dict) or set(editorial) - allowed:
        raise ProcessError('Неизвестные поля редакционных предпосылок.')
    presentation = {
        'version': 'numbered-1', 'origin': 'stable',
        'ordering_basis': 'Сначала высокая, затем средняя и низкая перспективность; далее инициативы с недостаточными основаниями. '
            'Внутри категории сохранён исходный порядок: сопоставимых оснований утверждать превосходство одной идеи над другой нет.',
    }
    if editorial:
        order = editorial.get('order')
        entries = editorial.get('initiatives')
        if (not isinstance(order, list) or not all(isinstance(key, str) for key in order)
                or len(order) != len(ids) or set(order) != set(ids) or len(set(order)) != len(order)):
            raise ProcessError('Редакционная очередность должна учитывать каждую существующую инициативу ровно один раз.')
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise ProcessError('Редакционные пояснения инициатив имеют неверный формат.')
        entry_ids = [entry.get('id') for entry in entries]
        if (not all(isinstance(key, str) for key in entry_ids) or len(entry_ids) != len(ids)
                or len(set(entry_ids)) != len(entry_ids) or set(entry_ids) != set(ids)):
            raise ProcessError('Редакционные пояснения должны ссылаться на все существующие инициативы без повторов.')
        lookup = {row['id']: row for row in rows}
        ordered = [lookup[key] for key in order]
        grouped = [row for category in CATEGORIES for row in ordered if row['prospect']['category'] == category]
        if [row['id'] for row in grouped] != order:
            raise ProcessError('Редакционная очередность не может менять порядок категорий перспективности.')
        fields = {'short_title', 'expected_effect_short', 'main_condition_short', 'business_summary', 'ordering_reason'}
        for entry in entries:
            if set(entry) != fields | {'id'}:
                raise ProcessError('Нужны короткие формулировки и объяснение очередности каждой инициативы.')
            for field in fields:
                value = entry[field]
                limit = 180 if field in ('short_title', 'expected_effect_short', 'main_condition_short') else 1200
                if not isinstance(value, str) or not value.strip() or len(value) > limit:
                    raise ProcessError('Редакционное пояснение пустое или превышает допустимую длину.')
            lookup[entry['id']]['presentation'] = {field: entry[field].strip() for field in fields}
        for field in ('ordering_basis', 'executive_summary'):
            value = editorial.get(field)
            if field == 'executive_summary' and value is None:
                continue
            if not isinstance(value, str) or not value.strip() or len(value) > 1800:
                raise ProcessError('Нужно содержательное объяснение редакционной очередности.')
            presentation[field] = value.strip()
        presentation['origin'] = 'editorial'
        rows = ordered
    for number, row in enumerate(rows, start=1):
        row['sequence_number'] = number
    result['initiatives'] = rows
    result['presentation'] = presentation
    return result


def initiative_title(initiative):
    """One numbered caption shared by the main UI and document presentations."""
    title = initiative.get('presentation', {}).get('short_title') or initiative['title']
    number = initiative.get('sequence_number')
    return (f'№ {number}. ' if number is not None else '') + business_text(title)


def _safe(value):
    # Escape untrusted text; never embed active HTML or model-generated Markdown links.
    return re.sub(r'([\\`*_{}\[\]()#+.!<>|])', r'\\\1', ' '.join(str(value).split()))


def business_text(value):
    """Translate diagnostic spans without discarding the surrounding business meaning."""
    text = re.sub(r'\bИИ\b', 'AI', ' '.join(str(value).split()))
    modes = {'automation_explicit': 'автоматизация прямо предусмотрена НПА',
             'manual_explicit': 'ручное действие прямо предусмотрено НПА',
             'mixed': 'предусмотрен смешанный способ выполнения',
             'unknown': 'способ выполнения не определён'}
    text = re.sub(r'\bmode=([A-Za-z_]+)', lambda match: modes.get(match.group(1),
                  'способ выполнения требует уточнения'), text)
    text = re.sub(r'не объедин[её]н[оаы]?\s+с\s+(?:action[_-][\w-]+|a\d+)\b',
                  'Границы операции и распределение ответственности требуют уточнения', text, flags=re.I)
    translations = {'review_reasons': 'замечания к границам операции', 'requires_review': 'требуется содержательная проверка',
                    'review': 'группировка требует проверки', 'incomplete': 'часть группировки ещё не подтверждена',
                    'context_action_ids': 'ссылки на вспомогательный контекст', 'source_action_ids': 'ссылки на исходные действия'}
    for technical, business in translations.items():
        text = re.sub(r'\b' + technical + r'\b', business, text, flags=re.I)
    text = re.sub(r'\b(?:action[_-][\w-]+|a\d+)\b', 'указанная операция', text, flags=re.I)
    text = re.sub(r'\b(?:sha256|hash|хеш)\s*[:=]?\s*[a-f0-9]{32,64}\b', 'идентификатор исходного документа', text, flags=re.I)
    return text.strip()


def _business(value):
    return _safe(business_text(value))


def _number(value):
    if value is None:
        return 'неизвестно'
    return format(Decimal(value), ',.2f').replace(',', ' ').replace('.', ',')


def _metric_name(initiative):
    primary = initiative['measurement']['primary']
    if initiative['measurement'].get('validation_issues'):
        return business_text(primary['name']) + ' (единицу и способ измерения нужно уточнить)'
    return business_text(primary['name']) + ' (' + UNIT_LABELS.get(primary['unit'], primary['unit']) + ')'


def _result_summary(initiative, scenario):
    """Display only calculated, unit-specific results; never invent a missing operand."""
    results = scenario['results']; model = initiative['economics']['model']
    measurement = initiative['measurement']
    if model == 'labor_time':
        hours = results.get('released_hours', {}).get('value')
        minutes = results.get('saved_minutes', {}).get('value')
        population = 'подходящих случаев' if measurement['population'] == 'eligible_cases' else 'обращений'
        count = scenario['parameters'].get('case_count', {}).get('value')
        if measurement['primary']['unit'] == 'hours':
            if hours is not None:
                per_case = ('; ' + _number(minutes) + ' человеко-минут на ' +
                            ('подходящий случай' if measurement['population'] == 'eligible_cases' else 'обращение')) if minutes is not None else ''
                return f"{_number(hours)} человеко-часа на {_number(count)} {population}" + per_case
            if minutes is not None:
                return (_number(minutes) + ' человеко-минут на ' +
                        ('подходящий случай' if measurement['population'] == 'eligible_cases' else 'обращение') +
                        '; размер фактической выборки не задан')
        elif minutes is not None:
            summary = f"{_number(minutes)} человеко-минут на " + ('подходящий случай' if measurement['population'] == 'eligible_cases' else 'обращение')
            eligible = results.get('eligible_saved_minutes', {}).get('value')
            if measurement['population'] == 'all_cases' and eligible is not None:
                summary += '; ' + _number(eligible) + ' человеко-минут на подходящий случай'
            return summary
        eligible = results.get('eligible_saved_minutes', {}).get('value')
        if measurement['population'] == 'all_cases' and eligible is not None:
            return _number(eligible) + ' человеко-минут на подходящий случай; применимость к общему потоку неизвестна, общий эффект не оценён'
    elif model == 'elapsed_time':
        minutes = results.get('saved_elapsed_minutes', {}).get('value')
        if minutes is not None:
            return f"{_number(minutes)} минут на измеряемый этап"
    elif model == 'time_capacity':
        value = results.get('capacity_equivalent', {}).get('value')
        if value is not None:
            return f"{_number(value)} руб./год эквивалента мощности"
    return None


def economic_summary(initiative):
    """Kept as the UI's single effect-summary function, now covering four metric types."""
    e = initiative['economics']
    scope = 'На выбранном участке: ' if initiative['measurement'].get('partial_initiative') else ''
    if e['model'] in ('labor_time', 'time_capacity'):
        participant = initiative['measurement'].get('participant', {})
        scope += participant.get('label', 'Участник измерения не определён') + '. '
    base = next((s for s in e['scenarios'] if s['name'] == 'base'), None)
    if base:
        result = _result_summary(initiative, base)
        if result:
            assumed = any(p['origin'] == 'illustrative' for p in base['parameters'].values())
            summary = scope + ('Сценарная оценка: ' if assumed else 'Условная оценка: ') + result
            comparison = base['results'].get('standardized_hours', {})
            if comparison.get('value') is not None and (initiative['measurement']['primary']['unit'] != 'hours'
                    or base['results'].get('released_hours', {}).get('value') is None):
                population = 'подходящих случаев' if initiative['measurement']['population'] == 'eligible_cases' else 'всех обращений с указанной применимостью'
                summary += '. Для сравнения: ' + _number(comparison['value']) + ' человеко-часа на условные 1 000 ' + population + '; не годовая нагрузка'
            return summary
    return scope + _metric_name(initiative) + '. Прогноз не задан; требуется измерение на пилоте.'


def _pilot_candidates(initiatives):
    # Current plans carry a validated comparative queue. Older plans retain the
    # previous conservative selection without pretending to have new ranking data.
    if any(a.get('priority') for a in initiatives):
        return [a for a in initiatives if a.get('priority', {}).get('queue') == 'first'
                and a['prospect']['category'] == 'high' and not a['prospect'].get('assessment_issues')][:3]
    return [a for a in initiatives if a['prospect']['category'] == 'high'][:3]


def _assumption_lines(initiative):
    """Exact provenance, with identical premises stated once for all scenarios."""
    scenarios = initiative['economics']['scenarios']
    if not scenarios:
        return []
    common = {name: parameter for name, parameter in scenarios[0]['parameters'].items()
              if parameter['value'] is not None and
              all(scenario['parameters'].get(name) == parameter for scenario in scenarios[1:])}
    groups = [('Общие предпосылки для всех сценариев', common)] if common else []
    groups += [(SCENARIO_LABELS[scenario['name']],
                {name: parameter for name, parameter in scenario['parameters'].items()
                 if parameter['value'] is not None and name not in common}) for scenario in scenarios]
    lines = []
    for label, parameters in groups:
        if not parameters:
            continue
        lines += ['**' + label + ':**', '']
        for name, parameter in parameters.items():
            unit = UNIT_LABELS.get(parameter['unit'], parameter['unit'])
            lines += ['- ' + _safe(PARAMETERS.get(name, name)) + ': ' + _number(parameter['value']) + ' ' + _safe(unit) +
                      '; ' + ORIGINS[parameter['origin']] + '. ' + _safe(parameter['note'])]
        lines += ['']
    return lines


def _report(artifact):
    original = artifact['report']
    return original if all(a.get('sequence_number') for a in original['initiatives']) else number_initiatives(original)


def _clauses(initiative, registry):
    """Only existing direct references, with measured-operation references first."""
    measured = set(initiative['measurement']['source_action_ids'])
    refs = [ref for grounding in initiative['grounding'] for candidate in grounding['source_candidates']
            if candidate['source_action_id'] in measured for ref in candidate['evidence_refs']]
    refs += initiative['evidence_refs']
    clauses = list(dict.fromkeys(str(registry[ref]['clause_number']) for ref in refs
                                if ref in registry and registry[ref].get('clause_number')))
    return 'пп. ' + ', '.join(clauses) if clauses else 'Пункт не определён; см. подтверждающий фрагмент в подробных материалах.'


def _main_effect(initiative):
    """Present already calculated values; no new assumptions or arithmetic."""
    economics = initiative['economics']; cases = economics['scenarios']; measurement = initiative['measurement']
    if not cases or not any(_result_summary(initiative, scenario) is not None for scenario in cases):
        return None
    model = economics['model']; by_name = {scenario['name']: scenario for scenario in cases}
    origins = {parameter['origin'] for scenario in cases for parameter in scenario['parameters'].values()
               if parameter['value'] is not None}
    notice = ('Иллюстративный сценарий, не замер ведомства. Происхождение чисел: Сценарное допущение.'
              if 'illustrative' in origins else
              'Условный расчёт по предоставленным параметрам. Источники: ' + ', '.join(ORIGINS[o] for o in sorted(origins)) + '.')
    rows = []; headers = []; formula = ''
    if model == 'labor_time':
        counts = {scenario['parameters'].get('case_count', {}).get('value') for scenario in cases}
        population = 'подходящих случаев' if measurement['population'] == 'eligible_cases' else 'обращений'
        base = next(iter(counts)) if len(counts) == 1 and None not in counts else None
        basis = ('на ' + _number(base).removesuffix(',00') + ' ' + population) if base is not None else 'на заданную выборку'
        headers = ['Сценарий', 'Минуты до / после',
                   'Человеко-часы ' + basis if base is not None else 'Эффект и база сравнения']
        for name, label in SCENARIO_LABELS.items():
            scenario = by_name[name]; params = scenario['parameters']; results = scenario['results']
            before = _number(params['before_minutes']['value']); after = _number(params['after_minutes']['value'])
            hours = results.get('released_hours', {}).get('value')
            if hours is not None:
                effect = _number(hours) + ' человеко-часа'
                if base is None:
                    effect += ' на ' + _number(params['case_count']['value']).removesuffix(',00') + ' ' + population
            else:
                minutes = results.get('saved_minutes', {}).get('value')
                effect = (_number(minutes) + ' человеко-минут на случай; размер выборки неизвестен'
                          if minutes is not None else 'Применимость или размер выборки неизвестны')
            rows.append([label, before + ' / ' + after, effect])
        factor = 'число подходящих случаев' if measurement['population'] == 'eligible_cases' else 'число обращений × доля подходящих случаев'
        formula = 'Человеко-часы = ' + factor + ' × (минуты до − после) / 60. Результат округлён до сотых часа.'
        notice += ' Время после включает проверку и исправления специалистом.'
        assumed_base = any(scenario['parameters'].get('case_count', {}).get('origin') == 'illustrative' for scenario in cases)
        notice += ' База условная, не годовая нагрузка.' if assumed_base else ' База расчёта — указанная выборка, не годовая нагрузка.'
        notice += ' Это не бюджетная экономия.'
        if measurement['population'] == 'all_cases':
            shares = '; '.join(SCENARIO_LABELS[name] + ': ' + _number(by_name[name]['parameters'].get('applicability', {}).get('value'))
                               for name in SCENARIO_LABELS)
            notice += ' Доля подходящих случаев: ' + shares + '.'
    elif model == 'elapsed_time':
        headers = ['Сценарий', 'Минуты этапа до / после', 'Изменение, минут на этап']
        for name, label in SCENARIO_LABELS.items():
            scenario = by_name[name]; params = scenario['parameters']
            rows.append([label, _number(params['before_elapsed_minutes']['value']) + ' / ' + _number(params['after_elapsed_minutes']['value']),
                         _number(scenario['results'].get('saved_elapsed_minutes', {}).get('value'))])
        formula = 'Изменение длительности этапа = минуты до − после.'
        notice += ' Измеряется указанный этап, а не рабочее время сотрудника или срок всей услуги.'
    elif model == 'time_capacity':
        headers = ['Сценарий', 'Эквивалент мощности, руб./год', 'Эксплуатация, руб./год', 'Первый год, руб.']
        for name, label in SCENARIO_LABELS.items():
            results = by_name[name]['results']
            rows.append([label] + [_number(results.get(key, {}).get('value')) for key in
                ('capacity_equivalent', 'annual_opex', 'first_year_net_capacity_equivalent')])
        formula = 'Эквивалент мощности = высвобождённые часы × стоимость часа. Первый год = эквивалент − эксплуатация − внедрение.'
        notice += ' Неизвестные расходы не считаются нулевыми. Эквивалент времени не доказывает сокращение расходов бюджета.'
    if not rows:
        return None
    if any(value['value'] is not None and Decimal(value['value']) < 0 for scenario in cases for value in scenario['results'].values()):
        notice += ' Отрицательное значение означает рост затрат или длительности.'
    boundary = business_text(measurement['work']) + ' ' + business_text(measurement['boundary'])
    boundary += ' Один случай: ' + business_text(measurement['case_unit'])
    if model in ('labor_time', 'time_capacity'):
        boundary += ' ' + business_text(measurement.get('participant', {}).get('label', 'Участник не определён')) + '.'
    return {'number': initiative['sequence_number'],
            'title': business_text(initiative.get('presentation', {}).get('short_title') or initiative['title']),
            'boundary': boundary, 'notice': notice, 'headers': headers, 'rows': rows, 'formula': formula}


def _display_date(value):
    """Format a known ISO date without inferring or replacing unknown requisites."""
    try:
        return date.fromisoformat(str(value)).strftime('%d.%m.%Y')
    except ValueError:
        return str(value)


def report_outcome(artifact):
    """Describe the checked result, never the model's pre-validation narrative."""
    report = artifact['report']
    accepted = report.get('initiatives', [])
    refused = [item for item in report.get('rejected', [])
               if item.get('origin') in ('validator', 'integrity_validator')]
    if artifact.get('stage_status', 'completed') != 'completed':
        state, title = 'unfinished', 'Завершить проверку перед выбором инициатив'
        message = 'Анализ не завершён. Сохранённые предложения нельзя считать полным результатом.'
        action = 'Продолжить с сохранённого этапа и проверить итоговые источники перед решением о запуске.'
    elif not accepted and refused:
        state, title = 'rejected', 'Сначала подтвердить основания предложений'
        message = ('Предложения найдены, но не прошли проверку. Они не включены в рекомендации. '
                   'Это не означает, что в работе нет полезной задачи для ИИ.')
        action = ('Связать каждую предлагаемую операцию с её подтверждением и уточнить спорные параметры; '
                  'до этого не принимать решение о пилоте.')
    elif not accepted:
        state, title = 'none', 'Обоснованных вариантов применения ИИ не найдено'
        message = 'В описанных операциях не найдено достаточно оснований для рекомендации ИИ.'
        action = ('Сначала проверить, можно ли улучшить работу обычными правилами, шаблонами или поиском. '
                  'Вернуться к ИИ, если на конкретных примерах обнаружится задача, которую эти способы не решают.')
    elif refused:
        state, title = 'partial', 'Часть предложений прошла проверку'
        message = ('Ниже приведены только принятые инициативы. Остальные предложения не прошли проверку '
                   'и не участвуют в расчёте эффекта или выборе первых проверок.')
        action = 'До расширения перечня уточнить основания отложенных предложений; причины сохранены в подробных материалах.'
    else:
        state, title = 'ready', 'Рекомендации подготовлены для обсуждения'
        message = 'Принятые инициативы связаны с операциями документа; ожидаемую пользу ещё нужно проверить на практике.'
        action = 'Выбрать ограниченную проверку, подтвердить доступ к данным и сравнить результат с обычным способом работы.'
    return {'state': state, 'title': title, 'message': message, 'action': action,
            'accepted_count': len(accepted), 'rejected_count': len(refused)}


def _pilot_check(initiative):
    measurement = initiative['measurement']
    indicator = business_text(measurement['primary']['name'])
    criterion = business_text(measurement['success_criterion'])
    criterion = criterion.replace('по основной метрике', 'по показателю «' + indicator + '»')
    return business_text(measurement['method']) + ' ' + criterion


def _resource_summary(initiative, *, compact=False):
    """Render validated engineering assumptions; unknown is neither cheap nor free."""
    resources = initiative.get('resources') or {}
    effort = resources.get('effort') or {}
    if resources.get('status') == 'illustrative' and effort.get('min') is not None and effort.get('max') is not None:
        text = ('Ориентир реализации: ' + _number(effort['min']).removesuffix(',00') + '–' +
                _number(effort['max']).removesuffix(',00') + ' человеко-дней суммарно. Это инженерное допущение, не календарный срок.')
        if resources.get('scope'):
            text += ' Объём: ' + business_text(resources['scope'])
        if compact:
            return text
        if resources.get('roles'):
            text += ' Участие: ' + ', '.join(business_text(role) for role in resources['roles']) + '.'
    else:
        text = 'Трудоёмкость реализации, календарный срок и расходы пока не оценены.'
    dependencies = resources.get('dependencies') or []
    if dependencies:
        text += ' Главная зависимость: ' + business_text(dependencies[0])
    elif resources.get('scope'):
        text += ' Объём работ: ' + business_text(resources['scope'])
    else:
        text += ' Для оценки нужно определить подготовку данных, необходимые доступы, участие разработчика и проверку предметным специалистом.'
    if effort.get('min') is not None:
        text += ' Стоимость внедрения и использования неизвестна; без ставок её не рассчитываем.'
    return text


def _numeric_highlights(initiatives):
    """Use existing scenario outputs and the shared pure arithmetic helper."""
    from govai.economics import labor_change
    highlights = []
    for initiative in initiatives:
        scenarios = initiative['economics']['scenarios']
        base = next((scenario for scenario in scenarios if scenario['name'] == 'base'), None)
        if not base or initiative['economics']['model'] != 'labor_time':
            continue
        change = labor_change(base)
        if change.get('before_minutes') is None or change.get('after_minutes') is None:
            continue
        measurement = initiative['measurement']
        status = 'Сценарий' if 'illustrative' in change.get('origins', []) else 'Расчёт'
        scope = business_text(measurement['work'])
        common = {'number': initiative['sequence_number'], 'status': status}
        highlights.append({**common, 'value': _number(change['before_minutes']).removesuffix(',00') + ' → ' +
                           _number(change['after_minutes']).removesuffix(',00'),
                           'label': 'минут на подходящий случай', 'basis': business_text(measurement['case_unit']),
                           'note': 'Время после включает проверку и исправления специалистом. ' + scope})
        percent = change.get('reduction_percent')
        if percent is not None:
            highlights.append({**common, 'value': _number(percent).removesuffix(',00') + '%',
                               'label': 'снижение времени на случай' if Decimal(percent) >= 0 else 'изменение времени на случай',
                               'basis': 'Относительно времени до, в том же сценарии',
                               'note': 'Отрицательное значение означает рост трудозатрат.' if Decimal(percent) < 0 else 'Расчёт из указанных предпосылок; на другие операции его не переносим.'})
        hours = base['results'].get('released_hours', {}).get('value')
        count = base['parameters'].get('case_count', {}).get('value')
        if hours is None:
            hours = base['results'].get('standardized_hours', {}).get('value')
            count = '1000' if hours is not None else None
        if hours is not None and count is not None:
            population = 'подходящих случаев' if measurement['population'] == 'eligible_cases' else 'обращений с указанной долей применимости'
            count_text = f'{int(Decimal(count)):,}'.replace(',', ' ') if Decimal(count) == Decimal(count).to_integral_value() else _number(count)
            assumed = (base['results'].get('released_hours', {}).get('value') is None or
                       any(parameter['origin'] == 'illustrative' for parameter in base['parameters'].values() if parameter['value'] is not None))
            highlights.append({**common, 'status': 'Сценарий' if assumed else 'Расчёт', 'value': _number(hours), 'label': 'человеко-часа',
                               'basis': 'На ' + count_text + ' ' + population,
                               'note': ('Условная база сравнения' if base['parameters'].get('case_count', {}).get('origin') == 'illustrative' or base['results'].get('released_hours', {}).get('value') is None else 'Указанная база расчёта') + ', не годовой объём или бюджетная экономия. ' + scope})
        break  # One comparable operation, never a sum of overlapping effects.
    if len(highlights) < 3:
        for initiative in initiatives:
            resources = initiative.get('resources') or {}; effort = resources.get('effort') or {}
            if resources.get('status') == 'illustrative' and effort.get('min') is not None and effort.get('max') is not None:
                highlights.append({'number': initiative['sequence_number'],
                    'value': _number(effort['min']).removesuffix(',00') + '–' + _number(effort['max']).removesuffix(',00'),
                    'label': 'человеко-дней реализации', 'basis': business_text(resources.get('scope') or 'Указанный объём работ'),
                    'status': 'Инженерное допущение', 'note': 'Сумма работ, не календарный срок; стоимость без ставок неизвестна.'})
                break
    return highlights[:3]


def _short_resources(initiative):
    resources = initiative.get('resources') or {}; effort = resources.get('effort') or {}
    if resources.get('status') == 'illustrative' and effort.get('min') is not None and effort.get('max') is not None:
        return (_number(effort['min']).removesuffix(',00') + '–' + _number(effort['max']).removesuffix(',00') +
                ' человеко-дней. Сценарная оценка отдельного прототипа, не календарный срок.')
    return 'Ресурс прототипа пока не оценён.'


def _process_groups_view(report, ranks):
    """Display every verified group; do not promote context records into operations."""
    process_map = report.get('process_map') or {}
    evidence = process_map.get('evidence') or {}
    groups = []
    for group in process_map.get('groups', []):
        actions = group.get('actions', [])
        operations = [action for action in actions if action.get('kind') in ('operation', 'review')]
        participants, outputs, inputs = [], [], []
        for action in operations:
            actors = action.get('actors') or []
            for actor in actors:
                text = actor.get('value') if isinstance(actor, dict) else actor
                if text and text not in participants: participants.append(text)
            for entry in action.get('inputs') or []:
                text = entry.get('value') if isinstance(entry, dict) else entry
                if text and text not in inputs: inputs.append(text)
            for output in action.get('outputs') or []:
                text = output.get('value') if isinstance(output, dict) else output
                if text and text not in outputs: outputs.append(text)
        refs = group.get('evidence_refs') or []
        clauses = list(dict.fromkeys(str(evidence[ref]['clause_number']) for ref in refs
                                    if ref in evidence and evidence[ref].get('clause_number')))
        linked = [ranks[key] for key in group.get('initiative_ids', []) if key in ranks]
        groups.append({'id': group['id'], 'title': business_text(group['title']),
            'summary': business_text(group.get('summary') or ''), 'lane': group.get('lane'),
            'operations': [business_text(action['title']) for action in operations],
            'participants': [business_text(text) for text in participants],
            'inputs': [business_text(text) for text in inputs],
            'results': [business_text(text) for text in outputs], 'initiative_numbers': linked,
            'non_ai_reasons': [business_text(text) for text in group.get('non_ai_reasons', [])],
            'clauses': 'пп. ' + ', '.join(clauses) if clauses else 'Пункт не определён; источники сохранены в подробных материалах.',
            'issues': list(dict.fromkeys(business_text(text) for text in group.get('classification_issues', []))),
            'requires_review': bool(group.get('classification_issues') or group.get('lane') == 'review'),
            'context_records': len(actions) - len(operations)})
    return groups


def management_view(artifact):
    """A compact publication view; source records and calculations stay unchanged."""
    if artifact.get('kind') == 'govai_verified_reference':
        from govai.reference import reference_view
        return reference_view(artifact)
    report = _report(artifact); initiatives = report['initiatives']; document = report['document']
    outcome = report_outcome(artifact)
    requisites = str(document.get('act_type') or 'Документ')
    if document.get('number'): requisites += ' № ' + str(document['number'])
    title = requisites
    if document.get('adoption_date'): requisites += ' от ' + _display_date(document['adoption_date'])
    if document.get('revision_date'): requisites += ' · редакция ' + _display_date(document['revision_date'])
    cards, details, effects = [], [], []
    ready = outcome['state'] in ('ready', 'partial')
    by_number = {a['id']: a.get('rank', a['sequence_number']) for a in initiatives}
    categories = {'high': 'Высокая', 'medium': 'Средняя', 'low': 'Низкая', 'insufficient': 'Недостаточно оснований'}
    for item in initiatives:
        presentation = item.get('presentation') or {}; priority = item.get('priority') or {}
        prospect = item['prospect']; measurement = item['measurement']
        description = presentation.get('business_summary') or (item['ai_role'] + ' ' + item['human_role'])
        # Prefer the existing complete short statements, not character clipping or
        # a second model-generated edition. Full reasoning remains in the artifact.
        card = {'id': item['id'], 'rank': item.get('rank', item['sequence_number']),
            'number': item.get('rank', item['sequence_number']),
            'title': business_text(presentation.get('short_title') or item['title']),
            'description': re.sub(r'^Инструмент\b', 'AI', business_text(description)),
            'benefit': business_text(presentation.get('expected_effect_short') or item['expected_effect']),
            'effect': business_text(economic_summary(item)) if ready and item['economics']['scenarios'] else (business_text(item['expected_effect']) if not presentation.get('business_summary') else ''),
            'resources': _short_resources(item),
            'priority_reason': business_text(presentation.get('ordering_reason') or priority.get('reason') or prospect['reason']),
            'condition': business_text(presentation.get('main_condition_short') or prospect['main_uncertainty']),
            'category': prospect['category'], 'category_label': categories[prospect['category']],
            'clauses': _clauses(item, report['evidence']), 'queue': priority.get('queue', 'reserve'),
            'dependencies': [f'№ {by_number[key]}' if key in by_number else 'Зависимая доработка ещё не подтверждена' for key in priority.get('dependencies', [])],
            'assessment_limit': ('Недостаточно оснований для оценки. ' + business_text(' '.join(prospect.get('assessment_issues', [])))) if prospect['category'] == 'insufficient' else ''}
        cards.append(card)
        details.append({**card,
            'why_ai': business_text(item['why_ai']), 'alternative': business_text(item['non_ai_alternative']),
            'check': business_text(prospect['pilot']), 'criterion': _pilot_check(item),
            'input_materials': [business_text(x) for x in item.get('input_materials', [])],
            'staff_result': business_text(item.get('staff_result', '')),
            'uncertainty': business_text(prospect['main_uncertainty']),
            'change_condition': business_text(priority.get('change_condition') or prospect['change_condition']),
            'risks': list(dict.fromkeys(business_text(x) for x in item['risks'])),
            'resource_tasks': [business_text(t['role'] + ': ' + t['work']) for t in (item.get('resources') or {}).get('tasks', [])],
            'resource_scope': business_text((item.get('resources') or {}).get('scope', '')),
            'boundary': business_text(measurement.get('boundary', '')),
            'metric': business_text(measurement['primary']['name']),
            'method': business_text(measurement['method']),
            'quality_protocol': {k: business_text(v) for k,v in (measurement.get('quality_protocol') or {}).items()},
            'human_role': business_text(item['human_role'])})
        effect = _main_effect(item) if ready else None
        if effect: effects.append(effect)
    ranks = {row['id']: row['rank'] for row in cards}
    groups = _process_groups_view(report, ranks)
    # Compact original groups are safer than guessing legal mergers from titles.
    # Pure context and deferred records are not independent business processes.
    operations = [g for g in groups if g['operations'] and g['id'] != '__review__']
    context = [g for g in groups if not g['operations'] and g['id'] != '__review__']
    participants = list(dict.fromkeys(actor for g in operations for actor in g['participants']))
    conditions = []
    if initiatives:
        conditions.append('AI помогает подготовить и проверить материалы. Решение и подписание остаются уполномоченному специалисту. Нужны разрешённые данные и защищённая среда обработки.')
        if not effects:
            conditions.append('Экономия пока не рассчитана. Для оценки нужны замеры обычного способа и работы с AI, включая проверку и исправления сотрудником, а также число подходящих случаев. Стоимость труда, внедрения и эксплуатации неизвестна, а не равна нулю.')
        else:
            conditions.append('Высвобождённое время не означает сокращения бюджетных расходов. Пересекающиеся эффекты разных доработок не суммируются.')
    limitation = None
    if report.get('normalization_status') != 'completed':
        limitation = 'Для части операций ещё нужно уточнить исполнителя и границы работы. Рекомендации относятся к подтверждённым действиям; общий объём эффекта не оценён.'
        conditions.append(limitation)
    partial = [str(a['sequence_number']) for a in initiatives if a['measurement'].get('partial_initiative')]
    if partial:
        conditions.append('В инициативах № ' + ', '.join(partial) + ' измеряется только часть работы. Эффект этого участка нельзя переносить на остальные действия или весь процесс.')
    unknown = [str(a['sequence_number']) for a in initiatives if a['measurement'].get('participant', {}).get('category') == 'unknown']
    if unknown:
        conditions.append('Для инициатив № ' + ', '.join(unknown) + ' исполнитель измеряемой работы не установлен. Пока он не определён, нельзя приписывать это время сотрудникам ведомства.')
    presentation = report.get('presentation') or {}
    service = presentation.get('service_map')
    service_view = {'scope': business_text(service['scope']), 'stages': [
        {**{k: business_text(stage[k]) for k in ('stage', 'participant', 'result')},
         'clauses': 'пп. ' + ', '.join(stage['clause_numbers'])} for stage in service['stages']]} if service else None
    ordering = ('Очередь предварительная: польза, ресурсы, готовность данных, зависимости и последствия ошибки ещё требуют проверки.'
        if any(a.get('priority', {}).get('origin') == 'model_comparison_checked' for a in initiatives) else
        'Внутри категории сохранён порядок: сопоставимых оснований утверждать превосходство одной идеи над другой пока нет.')
    if presentation.get('origin') == 'editorial' and presentation.get('ordering_basis'):
        ordering = business_text(presentation['ordering_basis'])
    return {'title': title, 'requisites': requisites, 'outcome': outcome,
        'cards': cards, 'initiatives': cards, 'details': details, 'effects': effects,
        'process_groups': groups, 'operational_groups': operations, 'context_groups': context, 'participants': participants,
        'service_map': service_view,
        'conditions': conditions, 'source_index': [{'number': r['rank'], 'clauses': r['clauses']} for r in cards],
        'scope': business_text(document.get('analysis_scope') or ('Рассмотрен загруженный фрагмент документа.' if document.get('is_fragment') else 'Область анализа — загруженный текст; полнота нормативного акта отдельно не подтверждена.')),
        'input_limitation': limitation,
        'conclusion': {'heading': 'Рекомендации', 'text': outcome['message'] if not cards else ''},
        'summary_heading': 'Рекомендации', 'summary_intro': '',
        'highlights': _numeric_highlights(initiatives) if ready else [], 'pilots': [d for d in details if d['id'] in {a['id'] for a in _pilot_candidates(initiatives)}] if ready else [],
        'ordering_note': ordering,
        'full_title': business_text(document.get('title') or ''),
        'detail_note': 'Основания приведены по пунктам анализируемого документа.'}


def management_blocks(view):
    """One short edition for PDF, DOCX and Markdown; no diagnostic record dump."""
    rows = view['cards']
    yield 'heading', 'Рекомендации' if rows else business_text(view['outcome']['title'])
    yield 'requisites', view['requisites']
    if not rows:
        yield 'paragraph', business_text(view['outcome']['message'])
        if view['outcome'].get('action'): yield 'paragraph', business_text(view['outcome']['action'])
    if rows and view['outcome']['state'] in ('partial', 'unfinished'):
        yield 'notice', {'text': business_text(view['outcome']['message'])}
    for row in rows: yield 'ranked_card', row
    if rows: yield 'paragraph', view['ordering_note']
    page_sections = len(rows) >= 2
    if view.get('process_groups') or view.get('context_groups'):
        if page_sections: yield 'page_break', ''
        yield 'heading', 'Карта процессов'
        if view.get('participants'):
            yield 'paragraph', 'В документе названы следующие участники: ' + '; '.join(view['participants']) + '.'
        if view.get('service_map'):
            yield 'paragraph', view['service_map']['scope']
            for stage in view['service_map']['stages']:
                yield 'paragraph', stage['stage'] + '. ' + stage['participant'] + ': ' + stage['result'] + ' ' + stage['clauses'] + '.'
        else:
            for group in view['operational_groups']: yield 'process_group', group
        for group in view.get('context_groups', []):
            yield 'paragraph', group['title'] + '. Это общее условие, не подтверждённый самостоятельный бизнес-процесс. ' + group['summary'] + ' ' + ' '.join(group['non_ai_reasons']) + ' ' + group['clauses'] + '.'
        # Specific measurement limits are stated once with resources and effects.
    if view['details']:
        if page_sections: yield 'page_break', ''
        yield 'heading', 'Как работают инструменты'
        # Every recommendation remains in the overview; the first three receive
        # full business cases regardless of a high/medium category label.
        for detail in view['details'][:3]: yield 'card', detail
    if rows:
        if page_sections: yield 'page_break', ''
        yield 'heading', 'Ресурсы и эффект'
        for detail in view['details']:
            yield 'subheading', f"№ {detail['rank']}. {detail['title']}"
            yield 'paragraph', detail['resources']
            if detail.get('resource_tasks'):
                yield 'paragraph', '; '.join(detail['resource_tasks']) + '.'
            if detail.get('resource_scope'): yield 'paragraph', detail['resource_scope']
        for effect in view['effects']:
            yield 'subheading', f"№ {effect['number']}. Сценарии"
            yield 'notice', {'text': effect['notice']}
            yield 'paragraph', effect['boundary']
            yield 'table', {'headers': effect['headers'], 'rows': effect['rows']}
            yield 'paragraph', effect['formula']
        for condition in view['conditions']: yield 'paragraph', condition
        # Citation pointers appear once, while full quotes and source IDs stay
        # unchanged in the saved artifact and the developer-only appendix.
        yield 'paragraph', 'Основания: ' + '; '.join(f"№ {r['number']} — {r['clauses']}" for r in view['source_index']) + '.'
    if view.get('scope'): yield 'paragraph', view['scope']


def _block_paragraphs(kind, value):
    if kind == 'ranked_card':
        yield f"№ {value['number']}. {value['title']}"
        yield value['description']
        if value.get('effect'): yield value['effect']
        yield value['resources']
        if value.get('assessment_limit'): yield value['assessment_limit']
        if value.get('dependencies'): yield 'Зависит от: ' + ', '.join(value['dependencies'])
        if value.get('priority_reason'): yield value['priority_reason']
        if value.get('condition') and value['condition'] not in value['description']: yield value['condition']
    elif kind == 'process_group':
        linked = ', '.join('№ ' + str(n) for n in value['initiative_numbers'])
        yield value['title'] + (f' · {linked}' if linked else '')
        yield value['summary'] + ' ' + value['clauses'] + '.'
    elif kind == 'card':
        yield f"№ {value['number']}. {value['title']}"
        yield value['why_ai'] + ' ' + value['alternative']
        yield value['criterion']
        safety = value.get('quality_protocol', {}).get('unacceptable_errors')
        if safety:
            yield 'Недопустимы: ' + safety[0].lower() + safety[1:]
        elif value.get('risks'):
            yield ' '.join(value['risks'])


def render_markdown(artifact, *, view=None):
    """Full readable report from the same semantic blocks as PDF and DOCX."""
    if artifact.get('kind') == 'govai_verified_reference':
        from govai.reference import reference_markdown
        return reference_markdown(artifact)
    v = management_view(artifact) if view is None else view
    lines = []
    for kind, value in management_blocks(v):
        if kind == 'conclusion':
            lines += ['# ' + _safe(value['heading']), '', _safe(value['text']), '']
        elif kind in ('heading', 'subheading'):
            lines += [('## ' if kind == 'heading' else '### ') + _safe(value), '']
        elif kind in ('ranked_card', 'process_group', 'card'):
            paragraphs = list(_block_paragraphs(kind, value))
            lines += ['### ' + _safe(paragraphs[0]), '']
            for paragraph in paragraphs[1:]: lines += [_safe(paragraph), '']
        elif kind == 'table':
            lines += ['| ' + ' | '.join(_safe(x) for x in value['headers']) + ' |',
                      '| ' + ' | '.join('---' for _ in value['headers']) + ' |']
            lines += ['| ' + ' | '.join(_safe(x) for x in row) + ' |' for row in value['rows']]
            lines += ['']
        elif kind == 'notice': lines += ['> ' + _safe(value['text']), '']
        elif kind == 'page_break': continue
        else: lines += [('− ' if kind == 'bullet' else '') + _safe(value), '']
    return '\n'.join(lines).rstrip()


def render_details(artifact):
    """Complete readable technical appendix: exact proofs are deduplicated, not cut."""
    if artifact.get('kind') == 'govai_verified_reference':
        from govai.reference import reference_details
        return reference_details(artifact)
    report = _report(artifact)
    lines = ['# Подробные материалы к управленческому отчёту', '',
             'Этот файл дополняет краткий отчёт. Он не заменяет полноценный opportunities.json для восстановления.', '',
             '## Анализируемый документ', '',
             _safe(report['document'].get('title') or 'Название в исходных данных не определено.'), '',
             _safe(' · '.join(str(report['document'][key]) for key in
                             ('act_type', 'number', 'adoption_date', 'revision_date') if report['document'].get(key))), '',
             'Расчёты выполняются Decimal. Результаты округляются до сотых по правилу ROUND_HALF_EVEN; исходные числовые параметры сохранены в полном JSON.', '',
             '## Инициативы, условия и протоколы', '']
    for initiative in report['initiatives']:
        measurement = initiative['measurement']; prospect = initiative['prospect']
        lines += ['### ' + _business(initiative_title(initiative)), '',
                  'ID инициативы: ' + _safe(initiative['id']), '',
                  'Полное название: ' + _safe(initiative['title']), '']
        for key, label in [('problem', 'Проблема'), ('ai_role', 'Работа ИИ'), ('human_role', 'Работа человека'),
                           ('why_ai', 'Добавочная польза ИИ'), ('non_ai_alternative', 'Обычная альтернатива'),
                           ('expected_effect', 'Ожидаемый эффект'), ('effect_mechanism', 'Механизм эффекта')]:
            lines += ['**' + label + ':** ' + _safe(initiative[key]), '']
        for key, label in [('conditions', 'Условия'), ('risks', 'Риски'), ('uncertainties', 'Неопределённости')]:
            lines += ['**' + label + ':**', ''] + ['- ' + _safe(item) for item in initiative[key]] + ['']
        for key, label in [('reason', 'Категория и основание'), ('pilot', 'Пилот'), ('success_criterion', 'Критерий успеха'),
                           ('main_uncertainty', 'Главная неопределённость'), ('change_condition', 'Условие пересмотра оценки')]:
            lines += ['**' + label + ':** ' + _safe(prospect[key]), '']
        for key, label in [('work', 'Измеряемая работа'), ('boundary', 'Границы'), ('case_unit', 'Один случай'),
                           ('method', 'Метод измерения'), ('success_criterion', 'Критерий измерения')]:
            lines += ['**' + label + ':** ' + _safe(measurement[key]), '']
        for key, label in [('primary', 'Основная метрика'), ('secondary', 'Дополнительная метрика')]:
            metric = measurement.get(key)
            if metric:
                lines += ['**' + label + ':** ' + _safe(metric['name']) + ' (' +
                          _safe(UNIT_LABELS.get(metric['unit'], metric['unit'])) +
                          '; категория ' + _safe(metric['category']) + ').', '']
        if measurement.get('participant'):
            lines += ['Участник измерения: ' + _safe(str(measurement['participant'])), '']
        if measurement.get('quality_protocol'):
            for key, label in [('calculation', 'Расчёт метрики качества'), ('expert_reference', 'Экспертный эталон'),
                               ('unacceptable_errors', 'Недопустимые ошибки')]:
                lines += ['**' + label + ':** ' + _safe(measurement['quality_protocol'][key]), '']
        resources = initiative.get('resources') or {}
        if resources:
            lines += ['**Ресурсы и инженерные предпосылки:**', '', _safe(_resource_summary(initiative)), '']
            for task in resources.get('tasks', []):
                lines += ['- ' + _safe(task.get('work', '')) + ' — ' + _safe(task.get('role', '')) + ': ' +
                          _number(task.get('lower_days')) + '–' + _number(task.get('upper_days')) +
                          ' человеко-дней суммарно; ' + _safe(ORIGINS.get(task.get('origin'), 'Неизвестно')) + '. ' +
                          _safe(task.get('basis', ''))]
                lines += ['  - Ограничение: ' + _safe(issue) for issue in task.get('issues', [])]
            lines += ['', 'Формула диапазона: ' + _safe(resources.get('effort', {}).get('formula', 'Не определена')), '']
            lines += ['- Зависимость: ' + _safe(item) for item in resources.get('dependencies', [])]
            lines += ['', 'Регулярные расходы: ' + _safe(resources.get('operating_costs', {}).get('basis', 'Неизвестны.')), '']
            lines += ['- ' + _safe(item) for item in resources.get('limitations', []) + resources.get('issues', [])]
            lines += ['']
        if initiative.get('proposed_flow'):
            lines += ['Предлагаемый порядок работы, не утверждение о действующей системе: ' +
                      ' → '.join(_safe(step) for step in initiative['proposed_flow']), '']
        lines += _assumption_lines(initiative)
        for scenario in initiative['economics']['scenarios']:
            lines += ['**Результаты: ' + SCENARIO_LABELS[scenario['name']] + '**', '']
            for name, result in scenario['results'].items():
                lines += ['- ' + _safe(name) + ': ' + _number(result['value']) + ' ' + _safe(result['unit']) +
                          '; формула: ' + _safe(result.get('formula', 'Не указана'))]
            lines += ['']
        lines += ['**Цепочки источников:**', '']
        for kind in ('grounding', 'context_grounding'):
            for grounding in initiative.get(kind, []):
                lines += ['- ' + _safe(kind + ': ' + str(grounding.get('action_id')) + ' / ' + str(grounding.get('process_title')))]
                for candidate in grounding['source_candidates']:
                    lines += ['  - ' + _safe(candidate['source_action_id'] + ' → ' + ', '.join(candidate['evidence_refs']))]
        lines += ['', '**Ограничения расчёта и проверки:**', '',
                  _safe(str(initiative['economics'].get('reasons', []))),
                  _safe(str(measurement.get('validation_issues', []))),
                  _safe(str(prospect.get('assessment_issues', []))), '']
    service_map = report.get('presentation', {}).get('service_map')
    if service_map:
        lines += ['## Основания краткой карты услуги', '',
                  'Карта подготовлена при аналитической проверке исходного текста; это не новый ответ модели.', '',
                  _safe(service_map['scope']), '']
        for stage in service_map['stages']:
            lines += ['- ' + _safe(stage['stage'] + ': ' + stage['participant'] + ' → ' + stage['result']) +
                      ' — ' + _safe(', '.join(stage['evidence_refs']))]
        lines += ['']
    process_map = report.get('process_map') or {}
    if process_map:
        lines += ['## Полная карта исходных процессов', '']
        for group in process_map.get('groups', []):
            lines += ['### ' + _safe(group['title']), '', 'ID группы: ' + _safe(group['id']), '',
                      _safe(group.get('summary') or ''), '',
                      'Связанные инициативы: ' + _safe(', '.join(group.get('initiative_ids', []))), '']
            for action in group.get('actions', []):
                lines += ['- ' + _safe(str(action))]
            lines += ['']
    lines += ['## Полные подтверждающие фрагменты', '']
    proofs = {**report['evidence'], **(service_map['evidence'] if service_map else {}), **process_map.get('evidence', {})}
    for reference, proof in proofs.items():
        lines += ['### ' + _safe(reference), '',
                  'Пункт ' + _safe(proof.get('clause_number') or 'не определён') +
                  (f"; страница PDF {proof['page_number']}" if proof.get('page_number') else ''), '',
                  'Координаты: ' + _safe(str({key: value for key, value in proof.items() if key != 'quote'})), '']
        quote = proof['quote']
        fence = '`' * max(3, 1 + max((len(value) for value in re.findall(r'`+', quote)), default=0))
        lines += [fence + 'text', quote, fence, '']
    if report['rejected']:
        lines += ['## Отклонённые сценарии', '']
        lines += ['- **' + _safe(row['title']) + ':** ' + _safe(row['reason']) for row in report['rejected']]
    lines += ['', '## Диагностические ограничения', '']
    for key in ('normalization_status', 'warnings', 'portfolio', 'explanation'):
        if key in report:
            lines += ['**' + key + ':** ' + _safe(str(report[key])), '']
    return '\n'.join(lines)
