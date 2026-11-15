"""A short qualitative rubric and a management report over verified Python results."""

import re
from copy import deepcopy
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


def compact_effect(initiative):
    """Keep the overview short; all measurement boundaries remain in the body/JSON."""
    economics = initiative['economics']
    base = next((scenario for scenario in economics['scenarios'] if scenario['name'] == 'base'), None)
    if base and economics['model'] == 'labor_time':
        hours = base['results'].get('released_hours', {}).get('value')
        count = base['parameters'].get('case_count', {}).get('value')
        if hours is not None and count is not None:
            population = 'подходящих случаев' if initiative['measurement']['population'] == 'eligible_cases' else 'обращений'
            count_text = f'{int(Decimal(count)):,}'.replace(',', ' ') if Decimal(count) == Decimal(count).to_integral_value() else _number(count)
            scope = ''
            if initiative['measurement'].get('partial_initiative'):
                scope = (initiative.get('presentation', {}).get('expected_effect_short')
                         or 'Только выбранный измеряемый участок.') + ' '
            return scope + 'Иллюстрация: ' + _number(hours) + ' чел.-ч на ' + count_text + ' ' + population + '.'
    short = initiative.get('presentation', {}).get('expected_effect_short')
    if short:
        return business_text(short)
    result = _result_summary(initiative, base) if base else None
    return ('Сценарно: ' + result) if result else _metric_name(initiative) + '; прогноз требует измерения.'

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


def _scenario_lines(initiative):
    """Unit-specific outputs with explicit scenario premises, not repeated annual-money tables."""
    cases = initiative['economics']['scenarios']
    if not any(_result_summary(initiative, s) is not None for s in cases):
        return []
    by_name = {s['name']: s for s in cases}
    origins = {p['origin'] for s in cases for p in s['parameters'].values() if p['value'] is not None}
    common_origin = next(iter(origins)) if len(origins) == 1 else None
    model = initiative['economics']['model']
    lines = []
    if common_origin:
        lines += ['Происхождение чисел: **' + ORIGINS[common_origin] + '**. '
                  + ('Это не показатели ведомства.' if common_origin == 'illustrative' else ''), '']
    # Distinct sentences make scenario differences visible even when notes repeat a common introduction.
    note_sets = {}
    for s in cases:
        notes = list(dict.fromkeys(business_text(p['note']) for p in s['parameters'].values() if p['value'] is not None))
        note_sets[s['name']] = list(dict.fromkeys(sentence for note in notes for sentence in re.split(r'(?<=[.!?])\s+', note)))
    common = set.intersection(*(set(sentences) for sentences in note_sets.values())) if note_sets else set()
    if model in ('labor_time', 'elapsed_time'):
        before, after = ('before_minutes','after_minutes') if model == 'labor_time' else ('before_elapsed_minutes','after_elapsed_minutes')
        count_values = {s['parameters'].get('case_count', {}).get('value') for s in cases}
        if model == 'labor_time' and len(count_values) == 1 and None not in count_values:
            pop = 'подходящих случаев' if initiative['measurement']['population'] == 'eligible_cases' else 'обращений'
            lines += ['Расчётная база: ' + _number(next(iter(count_values))) + ' ' + pop + '; не годовая нагрузка.', '']
        unit = 'рабочих минут на случай' if model == 'labor_time' else 'минут на этап'
        if model == 'labor_time':
            lines += ['| Сценарий | До / после (' + unit + ') | Изменение на случай | Человеко-часы на расчётной базе |',
                      '| --- | --- | --- | --- |']
        else:
            lines += ['| Сценарий | До / после (' + unit + ') | Изменение длительности этапа |', '| --- | --- | --- |']
        for name, label in SCENARIO_LABELS.items():
            scenario = by_name[name]
            values = []
            for key in (before, after):
                parameter = scenario['parameters'][key]
                values.append(_number(parameter['value']) + ('' if common_origin or parameter['value'] is None else '; ' + ORIGINS[parameter['origin']]))
            if model == 'labor_time':
                results = scenario['results']
                minutes = results.get('saved_minutes', {}).get('value')
                hours = results.get('released_hours', {}).get('value')
                effect = _number(hours) + ' человеко-часа' if hours is not None else 'Размер выборки не задан'
                change = _number(minutes) + ' человеко-минут' if minutes is not None else 'Требуются данные'
                cells = [label, _safe(' / '.join(values)), _safe(change), _safe(effect)]
            else:
                cells = [label, _safe(' / '.join(values)), _safe(_result_summary(initiative, scenario) or 'Требуются дополнительные данные')]
            lines += ['| ' + ' | '.join(cells) + ' |']
        lines += ['', 'Происхождение и индивидуальные пояснения каждого параметра приведены в приложении.', '']
        if initiative['measurement']['population']=='all_cases':
            shares=[]
            for name,label in SCENARIO_LABELS.items():
                p=by_name[name]['parameters'].get('applicability')
                shares.append(label + ': ' + (_number(p['value']) + ' (' + ORIGINS[p['origin']] + ')' if p else 'неизвестно'))
            lines += ['', 'Доля подходящих случаев: ' + '; '.join(shares) + '.', '']
        if model == 'labor_time':
            factor = 'число подходящих случаев' if initiative['measurement']['population'] == 'eligible_cases' else 'обращения × применимость'
            lines += ['', 'Человеко-часы = ' + factor + ' × (минуты до − после) / 60. Проверка и исправления включены.', '']
        else:
            lines += ['', 'Сокращение длительности этапа = минуты до − после; на всю услугу оценка не распространяется.', '']
    else:
        keys=list(dict.fromkeys(k for s in cases for k,p in s['parameters'].items() if p['value'] is not None))
        lines += ['| Предпосылка и единица | Консервативный | Базовый | Оптимистичный |', '| --- | --- | --- | --- |']
        for key in keys:
            values=[];unit=''
            for name in SCENARIO_LABELS:
                p=by_name[name]['parameters'].get(key)
                if p is None: values.append('неизвестно');continue
                unit=UNIT_LABELS.get(p['unit'],p['unit'])
                values.append(_safe(_number(p['value']) + ('' if common_origin or p['value'] is None else '; '+ORIGINS[p['origin']])))
            lines += ['| '+_safe(PARAMETERS[key]+' ('+unit+')')+' | '+' | '.join(values)+' |']
        for name,label in SCENARIO_LABELS.items():
            notes=[x for x in note_sets[name] if x not in common]
            lines += [label+': '+_business(_first_sentence(' '.join(notes or note_sets[name])))]
        columns={'capacity_equivalent':'Эквивалент мощности','annual_opex':'Эксплуатация за год',
                 'first_year_net_capacity_equivalent':'Первый год после внедрения'}
        lines += ['', '| Сценарий | '+' | '.join(columns.values())+' |', '| --- | --- | --- | --- |']
        for name,label in SCENARIO_LABELS.items():
            results=by_name[name]['results']
            lines += ['| '+label+' | '+' | '.join(_number(results.get(k,{}).get('value'))+' руб.' for k in columns)+' |']
        lines += ['', 'Часы = годовые обращения × применимость × использование × (минуты до − после) / 60 × достижение эффекта; '
                  'эквивалент = часы × стоимость часа. Эксплуатация = постоянные расходы + обработанные случаи × расходы на случай; '
                  'первый год = эквивалент − эксплуатация − внедрение.', '']
    if any(v['value'] is not None and Decimal(v['value']) < 0 for s in cases for v in s['results'].values()):
        lines += ['Отрицательное значение означает увеличение затрат или длительности.', '']
    return lines


def _pilot_candidates(initiatives):
    # A short discussion agenda, not a ranking within the qualitative category.
    return [a for a in initiatives if a['prospect']['category'] == 'high'][:3]


def _source_proofs(initiative, registry):
    """Prefer direct measured-operation proofs, then cover other targeted operations."""
    measured = set(initiative['measurement']['source_action_ids'])
    candidates = [c for g in initiative['grounding'] for c in g['source_candidates']]
    candidates.sort(key=lambda c: c['source_action_id'] not in measured)
    selected, seen = [], set()
    for candidate in candidates:
        refs = [ref for ref in candidate['evidence_refs'] if ref in registry]
        if not refs: continue
        # No guessed legal clauses or semantic relevance classifier: these are the
        # original candidate's direct proofs, not unrelated contextual quotations.
        refs.sort(key=lambda ref: (ref not in initiative['evidence_refs'], len(registry[ref]['quote'])))
        proof = registry[refs[0]]
        identity = (proof['clause_number'], proof['quote'])
        if identity not in seen:
            selected.append(proof); seen.add(identity)
    return selected[:6], len(selected) > 6


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


def render_markdown(artifact):
    original = artifact['report']
    # Old, genuinely restored 2.1 artifacts can be read without changing their
    # bytes. New artifacts store the same numbers and agenda in the report itself.
    r = original if all(a.get('sequence_number') for a in original['initiatives']) else number_initiatives(original)
    initiatives = r['initiatives']; d = r['document']
    document = ' '.join(str(d.get(k) or '') for k in ('act_type', 'number', 'adoption_date')).strip()
    selected = _pilot_candidates(initiatives)
    selected_ids = {a['id'] for a in selected}
    presentation = r.get('presentation', {})
    lines = ['# AI-возможности и ожидаемый эффект', '', '## Резюме для руководителя', '',
             _safe(document or 'Нормативный акт') + ('. Редакция: ' + _safe(d['revision_date']) if d.get('revision_date') else ''), '']
    if d.get('title'):
        lines += [_business(d['title']), '']
    processes = list(dict.fromkeys(business_text(g['process_title']) for a in initiatives for g in a['grounding']
                                  if g['process_title'].strip().casefold() != 'требует проверки'))
    if processes:
        lines += ['Рассмотрены процессы: ' + _business('; '.join(processes)) + '.', '']
    if selected:
        lines += ['Первые кандидаты — предварительная повестка обсуждения и пилотов:', '']
        for a in selected:
            reason = _first_sentence(a.get('presentation', {}).get('ordering_reason') or a['prospect']['reason'])
            lines += ['- **' + _business(initiative_title(a)) + ':** ' + _business(reason)]
        lines += ['']
    elif initiatives:
        lines += ['Обоснованных кандидатов для первого ограниченного пилота пока нет. Сначала нужно выполнить условия:', '']
        for a in initiatives[:3]:
            lines += ['- **' + _business(initiative_title(a)) + ':** ' + _business(a['prospect']['main_uncertainty'])]
        lines += ['']
    else:
        lines += [_business(r['explanation']), '']
    ordering_note = ('Внутри категории — предварительная редакционная очередь; при отсутствии сопоставимых оснований сохранён исходный порядок.'
                     if presentation.get('origin') == 'editorial' else 'Внутри одинаковой категории сохранён исходный порядок.')
    lines += ['Группы следуют по перспективности. ' + ordering_note + ' Нумерация не означает доказанную окупаемость.', '',
              '## Сравнение AI-инициатив', '',
              '| № | Инициатива | Перспективность | Ожидаемый эффект | Главное условие |',
              '| --- | --- | --- | --- | --- |']
    short_categories = {'high': 'Высокая', 'medium': 'Средняя', 'low': 'Низкая', 'insufficient': 'Недостаточно оснований'}
    for a in initiatives:
        p = a['prospect']; editorial = a.get('presentation', {})
        title = editorial.get('short_title') or a['title']
        condition = editorial.get('main_condition_short') or _first_sentence(p['main_uncertainty'])
        lines += ['| ' + ' | '.join((str(a['sequence_number']), _business(title), short_categories[p['category']],
                                    _business(compact_effect(a)), _business(condition))) + ' |']
    lines += ['', '## Перспективные направления для первых пилотов', '']
    if not selected:
        lines += ['Начать с уточнения границ задачи, безопасного доступа к примерам и экспертного эталона; '
                  'не переходить к внедрению только на основании нормативной цитаты.', '']
    for a in selected:
        p = a['prospect']; m = a['measurement']; editorial = a.get('presentation', {})
        lines += ['### ' + _business(initiative_title(a)), '',
                  '**Проблема для проверки:** ' + _business(editorial.get('business_summary') or a['problem']), '',
                  '**Почему ИИ:** ' + _business(a['why_ai']) + ' **Альтернатива без GenAI:** ' + _business(a['non_ai_alternative']), '',
                  '**Пилот:** ' + _business(p['pilot']) + ' **Критерий:** ' + _business(m['success_criterion']), '',
                  '**Контроль:** ' + _business(a['human_role']) + ' ' + _business(a['risks'][0]), '',
                  '**Изменит оценку:** ' + _business(p['change_condition']), '']
        if editorial.get('ordering_reason'):
            lines += ['**Основание очередности:** ' + _business(editorial['ordering_reason']), '']
    lines += ['## Ожидаемый эффект и предпосылки оценки', '']
    for a in initiatives:
        p = a['prospect']; m = a['measurement']; e = a['economics']; editorial = a.get('presentation', {})
        lines += ['### ' + _business(initiative_title(a)), '',
                  '**Перспективность:** ' + _safe(p['label']) + '. ' + _business(_first_sentence(p['reason'])), '']
        if a['id'] not in selected_ids:
            lines += ['**Основание:** ' + _business(_first_sentence(a['why_ai'])) + ' **Без GenAI:** ' + _business(a['non_ai_alternative']), '',
                      '**Проверка:** ' + _business(m['method']) + ' **Критерий:** ' + _business(m['success_criterion']), '',
                      'Изменит оценку: ' + _business(p['change_condition']), '']
        lines += [_business(economic_summary(a)), '']
        numeric = _scenario_lines(a)
        if numeric:
            if m.get('partial_initiative'):
                lines += ['Оценивается только выбранный участок: ' + _business(m['work']) +
                          ' Другие операции не включены. ' + _business(m['boundary']) +
                          ' Случай: ' + _business(m['case_unit']), '']
            else:
                lines += ['Граница: ' + _business(m['boundary']) + ' Случай: ' + _business(m['case_unit']), '']
            lines += numeric
        else:
            if m.get('partial_initiative'):
                lines += ['Граница оценки: ' + _business(m['work']) + ' Остальные операции не включены.', '']
            if e['required_parameters']:
                lines += ['Для расчёта нужны: ' + ', '.join(_safe(PARAMETERS[k]) for k in e['required_parameters']) + '.', '']
        if m.get('secondary'):
            metric = m['secondary']
            lines += ['Дополнительно измерить: ' + _business(metric['name']) +
                      ' (' + _safe(UNIT_LABELS.get(metric['unit'], metric['unit'])) + ').', '']
    lines += ['## Ограничения', '',
              '- До пилота подтвердить доступ к данным, обезличивание и защищённый контур работы с документами.',
              '- НПА подтверждает операцию, а не её фактическую сложность, готовность данных или необходимость ИИ.',
              '- Сценарии — допущения, не показатели ведомства. Рабочее время с проверкой и исправлениями отличается от ожидания и срока услуги.',
              '- Пересекающиеся эффекты не суммируются без независимых замеров. Часы, деньги, длительность и качество не складываются.',
              '- Эквивалент мощности — не бюджетная экономия. Неизвестные затраты — не ноль. Решения и утверждение документов остаются уполномоченному человеку.']
    if r['normalization_status'] != 'completed':
        lines += ['- Для части операций нужно уточнить границы и участников до количественных выводов.']
    if any(a['prospect'].get('assessment_issues') for a in initiatives):
        lines += ['- Для отдельных гипотез ещё не определён допустимый измеримый пилот; они не включены в первые кандидаты.']
    lines += ['', '## Приложение с источниками', '',
              'Цитаты подтверждают операции, а не прогноз эффекта. Полные описания, цитаты, координаты и расчётные параметры сохранены в JSON.', '']
    for a in initiatives:
        lines += ['**' + _business(initiative_title(a)) + '**', '']
        proofs, more = _source_proofs(a, r['evidence'])
        for proof in proofs:
            lines += ['Пункт ' + _safe(proof['clause_number'] or 'не определён') +
                      (f"; страница PDF {proof['page_number']}" if proof['page_number'] else '') + ':', '']
            quote = proof['quote']
            if len(quote) > 300:
                quote = quote[:299].rstrip() + '…'
            fence = '`' * max(3, 1 + max((len(x) for x in re.findall(r'`+', quote)), default=0))
            lines += [fence + 'text', quote, fence, '']
        if more:
            lines += ['Дополнительные подтверждения целевых операций приведены в полном JSON.', '']
    if initiatives:
        lines += ['## Протоколы измерения и условия пилотов', '',
                  'Эти условия уточняют краткую управленческую часть; прогноз качества без наблюдений не задаётся.', '']
        for a in initiatives:
            m = a['measurement']; p = a['prospect']
            lines += ['### ' + _business(initiative_title(a)), '',
                      '**Измеряемая работа:** ' + _business(m['work']) + ' **Граница:** ' + _business(m['boundary']), '',
                      '**Один случай:** ' + _business(m['case_unit']), '',
                      '**Главная неопределённость:** ' + _business(p['main_uncertainty']), '']
            protocol = m.get('quality_protocol')
            if protocol:
                lines += ['**Метрика качества:** ' + _business(protocol['calculation']) +
                          ' **Эталон:** ' + _business(protocol['expert_reference']) +
                          ' **Недопустимые ошибки:** ' + _business(protocol['unacceptable_errors']), '']
    numeric_initiatives = [a for a in initiatives if _scenario_lines(a)]
    if numeric_initiatives:
        lines += ['## Дополнительные расчётные предпосылки', '',
                  'Каждая предпосылка относится только к указанной измеряемой работе. Сценарные числа — не фактические показатели ведомства.', '']
        for a in numeric_initiatives:
            lines += ['### ' + _business(initiative_title(a)), ''] + _assumption_lines(a)
    if r['rejected']:
        lines += ['## Отклонённые сценарии', '']
        lines += ['- **' + _business(x['title']) + ':** ' + _business(_first_sentence(x['reason'])) for x in r['rejected']]
    return '\n'.join(lines)
