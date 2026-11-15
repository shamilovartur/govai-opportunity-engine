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
    text = ' '.join(str(value).split())
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


def _first_sentence(value):
    return re.split(r'(?<=[.!?])\s+', business_text(value), maxsplit=1)[0]


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
    # A short discussion agenda, not a ranking within the qualitative category.
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
                      '; ' + ORIGINS[parameter['origin']] + '. ' + _business(parameter['note'])]
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


def management_view(artifact):
    """One concise, evidence-linked presentation shared by site and every main export.

    Only presentation is selected here. Categories, IDs, participants, calculated
    values and the original analytic artifact are not changed. Full explanations
    and proofs are emitted separately by render_details.
    """
    report = _report(artifact); initiatives = report['initiatives']; details = report['document']
    selected = _pilot_candidates(initiatives)
    if len(initiatives) > 12:
        # A longer catalogue leaves room for two detailed pilots. This changes
        # presentation only: every initiative and its original order stay listed.
        selected = selected[:2]
    requisites = str(details.get('act_type') or '')
    if details.get('number'):
        requisites += ' № ' + str(details['number'])
    if details.get('adoption_date'):
        requisites += ' от ' + _display_date(details['adoption_date'])
    requisites = requisites.strip() or 'Реквизиты не определены'
    if details.get('revision_date'):
        requisites += ' · редакция ' + _display_date(details['revision_date'])
    short_categories = {'high': 'Высокая', 'medium': 'Средняя', 'low': 'Низкая', 'insufficient': 'Недостаточно оснований'}
    rows = []
    for initiative in initiatives:
        p = initiative.get('presentation', {}); prospect = initiative['prospect']
        rows.append({'id': initiative['id'], 'number': initiative['sequence_number'],
                     'title': business_text(p.get('short_title') or initiative['title']),
                     'category': prospect['category'], 'category_label': short_categories[prospect['category']],
                     'benefit': business_text(p.get('expected_effect_short') or _first_sentence(initiative['expected_effect'])),
                     'condition': business_text(p.get('main_condition_short') or _first_sentence(prospect['main_uncertainty'])),
                     'clauses': _clauses(initiative, report['evidence'])})
    by_id = {row['id']: row for row in rows}
    numeric = [effect for initiative in initiatives if (effect := _main_effect(initiative)) is not None]
    editorial_summary = report.get('presentation', {}).get('executive_summary')
    if editorial_summary:
        recommendation = business_text(editorial_summary)
    elif selected:
        recommendation = 'Начать с ограниченной проверки: ' + initiative_title(selected[0]) + '.'
        if len(selected) > 1:
            recommendation += ' Следующие кандидаты — ' + ', '.join('№ ' + str(a['sequence_number']) for a in selected[1:]) + '.'
        recommendation += ' Сравнить помощь ИИ с текущим способом работы до решения о внедрении.'
    elif initiatives:
        recommendation = ('Обоснованных кандидатов для первого ограниченного пилота пока нет. '
                          'Сначала подтвердить добавочную пользу и условия безопасной проверки предложений.')
    else:
        recommendation = business_text(report['explanation'])
    changes_by_sentence = {}
    for initiative in selected:
        sentence = _first_sentence(initiative.get('presentation', {}).get('business_summary') or initiative['ai_role'])
        changes_by_sentence.setdefault(sentence, []).append('№ ' + str(initiative['sequence_number']))
    changes = '\n'.join(', '.join(numbers) + '. ' + sentence for sentence, numbers in changes_by_sentence.items())
    if not changes:
        changes = ('Принятые идеи сохранены в перечне; порядок их применения зависит от уточнения условий.' if initiatives
                   else 'По представленным процессам подходящих AI-инициатив не принято. Новая технология не предлагается без конкретной задачи.')
    if numeric:
        first = next(a for a in initiatives if a['sequence_number'] == numeric[0]['number'])
        base = next(scenario for scenario in first['economics']['scenarios'] if scenario['name'] == 'base')
        params = base['parameters']; results = base['results']
        illustrative = any(parameter['origin'] == 'illustrative' for parameter in params.values() if parameter['value'] is not None)
        if first['economics']['model'] == 'labor_time':
            benefits = ('В иллюстративном сценарии' if illustrative else 'В условном расчёте') + ' время на случай: '
            benefits += _number(params['before_minutes']['value']) + ' минут до и ' + _number(params['after_minutes']['value']) + ' после, включая проверку и исправления специалистом.'
            hours = results.get('released_hours', {}).get('value')
            count = params.get('case_count', {}).get('value')
            if hours is not None and count is not None:
                population = 'подходящих случаев' if first['measurement']['population'] == 'eligible_cases' else 'обращений с указанной долей применимости'
                count_text = f'{int(Decimal(count)):,}'.replace(',', ' ') if Decimal(count) == Decimal(count).to_integral_value() else _number(count)
                benefits += ' Это ' + _number(hours) + ' человеко-часа на ' + count_text + ' ' + population + '; не годовая или бюджетная экономия.'
            else:
                benefits += ' ' + (_result_summary(first, base) or 'Для общего эффекта нужно уточнить применимость и число случаев.')
            benefits += ' ' + business_text(first['measurement']['work'])
        else:
            benefits = economic_summary(first) + '. ' + business_text(first['measurement']['work'])
        if illustrative:
            benefits += ' Числа не измерены в ведомстве; их нужно проверить на пилоте.'
    else:
        benefits = (' '.join(dict.fromkeys(_first_sentence(a['expected_effect']) for a in selected)) if selected else
                    'Пользу предстоит установить на конкретных примерах; отсутствие числовой оценки не означает отсутствие проблемы.')
    decision = ('Выбрать первый пилот, определить ответственного за проверку, подтвердить доступ к обезличенным примерам '
                'и сравнить результаты с текущим способом работы. Это предлагаемые действия; готовность ресурсов пока не установлена.' if initiatives else
                'Не запускать AI-пилот только ради технологии. При необходимости уточнить реальные затруднения и данные по операциям.')
    pilots = []
    for initiative in selected:
        p = initiative.get('presentation', {}); row = by_id[initiative['id']]; prospect = initiative['prospect']
        solution = business_text(p.get('business_summary') or initiative['ai_role'])
        # Human role is a specific legal distinction, not a generic repeated warning.
        if not p.get('business_summary'):
            solution += ' ' + _first_sentence(initiative['human_role'])
        reason = _first_sentence(p.get('ordering_reason') or prospect['reason'])
        blocks = [
            {'label': 'Работа инструмента', 'text': solution},
            {'label': 'Почему начать здесь', 'text': reason},
            {'label': 'Почему ИИ', 'text': _first_sentence(initiative['why_ai']) + ' Без ИИ: ' + _first_sentence(initiative['non_ai_alternative'])},
            {'label': 'Ожидаемая польза', 'text': row['benefit']},
            {'label': 'Как проверить', 'text': _first_sentence(prospect['pilot']) + ' ' + _first_sentence(initiative['measurement']['success_criterion'])},
            {'label': 'Условие и риск', 'text': row['condition'] + ' ' + _first_sentence(initiative['risks'][0])},
        ]
        pilots.append({'number': row['number'], 'title': row['title'], 'category': row['category'],
                       'category_label': row['category_label'], 'blocks': blocks, 'clauses': row['clauses']})
    conditions = [
        'Подготовить разрешённые к использованию примеры и контрольные ответы специалистов; определить правила доступа и обработки персональных данных.',
        'Продолжать пилот, если он улучшает заявленный результат относительно текущего способа работы, не добавляя критических ошибок. Если обычные правила дают сопоставимый результат, предпочесть их.',
        'Юридически значимые решения и утверждение документов остаются уполномоченному человеку. Анализ описывает возможные улучшения нормативных операций; реальные данные, трудозатраты и готовность систем ещё нужно проверить.',
        'Эффекты разных инициатив не суммируются без независимых замеров. Часы, деньги, длительность и качество не складываются. Неизвестные расходы не равны нулю.',
    ]
    if report['normalization_status'] != 'completed':
        conditions.append('До количественных выводов по остальным операциям уточнить их границы и участников; часть исходной группировки ещё не подтверждена.')
    if any(a['prospect'].get('assessment_issues') for a in initiatives):
        conditions.append('Для отдельных предложений пока недостаточно оснований даже для выбора пилота; это не доказательство отсутствия полезной задачи.')
    return {'title': 'ИИ в административных процедурах: предложения для пилота',
            'requisites': requisites, 'full_title': business_text(details.get('title') or ''),
            'summary_blocks': [{'label': 'Что рекомендуем', 'text': recommendation},
                               {'label': 'Что изменится', 'text': changes},
                               {'label': 'Какая польза ожидается', 'text': benefits},
                               {'label': 'Что нужно решить или проверить', 'text': decision}],
            'initiatives': rows, 'pilots': pilots, 'effects': numeric,
            'effect_note': ('Для остальных инициатив проверим практическую пользу на пилоте: полноту ответа, точность сверки и отсутствие неподтверждённых выводов. Проценты улучшения без наблюдений не задаются.' if numeric else
                            'Числовые предпосылки не подтверждены. Вместо денежного прогноза проверить заявленную пользу по контрольным примерам и критериям пилотов.'),
            'conditions': conditions,
            'source_index': [{'number': row['number'], 'clauses': row['clauses']} for row in rows],
            'ordering_note': 'Номер обозначает предварительную очередь проверки. Внутри категории при отсутствии сопоставимых оснований сохранён исходный порядок.',
            'detail_note': 'Полные цитаты, цепочки источников, протоколы измерения и диагностические ограничения — в opportunity-details.txt и полном opportunities.json внутри ZIP.'}


def render_markdown(artifact, *, view=None):
    """The management report only; detailed proofs are a separate ZIP member."""
    v = management_view(artifact) if view is None else view
    lines = ['# ' + _safe(v['title']), '', _safe(v['requisites']), '', '## Главное для руководителя', '']
    for block in v['summary_blocks']:
        lines += ['**' + _safe(block['label']) + '**', '', _safe(block['text']), '']
    if v['initiatives']:
        lines += ['## Предлагаемые инициативы', '',
                  '| № и название | Перспективность | Практическая польза | Главное условие |',
                  '| --- | --- | --- | --- |']
        for row in v['initiatives']:
            lines += ['| ' + ' | '.join(_safe(value) for value in
                      (f"№ {row['number']}. {row['title']}", row['category_label'], row['benefit'], row['condition'])) + ' |']
        lines += ['', _safe(v['ordering_note']), '']
    if v['pilots']:
        lines += ['## С чего начать', '']
        for pilot in v['pilots']:
            lines += ['### ' + _safe(f"№ {pilot['number']}. {pilot['title']}") + ' · ' + _safe(pilot['category_label']), '']
            for block in pilot['blocks']:
                lines += ['**' + _safe(block['label']) + ':** ' + _safe(block['text']), '']
            lines += ['Основание: ' + _safe(pilot['clauses']), '']
    if v['initiatives']:
        lines += ['## Какой эффект можно получить', '']
        for effect in v['effects']:
            lines += ['### ' + _safe(f"№ {effect['number']}. {effect['title']}"), '', _safe(effect['boundary']), '',
                      '**' + _safe(effect['notice']) + '**', '',
                      '| ' + ' | '.join(_safe(cell) for cell in effect['headers']) + ' |',
                      '| ' + ' | '.join('---' for _ in effect['headers']) + ' |']
            lines += ['| ' + ' | '.join(_safe(cell) for cell in row) + ' |' for row in effect['rows']]
            lines += ['', _safe(effect['formula']), '']
        lines += [_safe(v['effect_note']), '']
    lines += ['## Условия запуска и основания', '']
    lines += ['- ' + _safe(condition) for condition in v['conditions']]
    lines += ['']
    for source in v['source_index']:
        lines += ['- № ' + str(source['number']) + ' → ' + _safe(source['clauses'])]
    if v['full_title']:
        lines += ['', 'Документ: ' + _safe(v['full_title'])]
    lines += ['', _safe(v['detail_note'])]
    return '\n'.join(lines)


def render_details(artifact):
    """Complete readable technical appendix: exact proofs are deduplicated, not cut."""
    report = _report(artifact)
    lines = ['# Подробные материалы к управленческому отчёту', '',
             'Этот файл дополняет краткий отчёт. Он не заменяет полноценный opportunities.json для восстановления.', '',
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
    lines += ['## Полные подтверждающие фрагменты', '']
    for reference, proof in report['evidence'].items():
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
