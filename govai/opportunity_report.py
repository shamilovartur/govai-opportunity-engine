"""A short qualitative rubric and a management report over verified Python results."""

import re
from decimal import Decimal

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


def _brief(value, words=16):
    text = business_text(value)
    parts = text.split()
    return _safe(text if len(parts) <= words else ' '.join(parts[:words]).rstrip('.,;:') + '…')


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
        lines += ['| Сценарий | До / после (' + unit + ') | Эффект | Отличающееся допущение |', '| --- | --- | --- | --- |']
        for name, label in SCENARIO_LABELS.items():
            s = by_name[name]
            values=[]
            for key in (before,after):
                param=s['parameters'][key]
                values.append(_number(param['value']) + ('' if common_origin or param['value'] is None else '; ' + ORIGINS[param['origin']]))
            differing=[x for x in note_sets[name] if x not in common]
            note=' '.join(differing or note_sets[name])
            lines += ['| ' + label + ' | ' + _safe(' / '.join(values)) + ' | ' +
                      _safe(_result_summary(initiative,s) or 'Требуются дополнительные данные') + ' | ' + _brief(note,20) + ' |']
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
            lines += [label+': '+_brief(' '.join(notes or note_sets[name]),24)]
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


def render_markdown(artifact):
    r = artifact['report']; initiatives = r['initiatives']; d = r['document']
    document = ' '.join(str(d.get(k) or '') for k in ('act_type', 'number', 'adoption_date')).strip()
    selected = _pilot_candidates(initiatives)
    selected_ids = {a['id'] for a in selected}
    lines = ['# AI-возможности и ожидаемый эффект', '', '## Резюме для руководителя', '',
             _safe(document or 'Нормативный акт') + ('. Редакция: ' + _safe(d['revision_date']) if d.get('revision_date') else ''), '']
    if d.get('title'): lines += [_business(d['title']), '']
    processes = list(dict.fromkeys(business_text(g['process_title']) for a in initiatives for g in a['grounding']))
    if processes:
        lines += ['Рассмотрены процессы: ' + _brief('; '.join(processes), 22) + '.', '']
    if selected:
        lines += ['Для первой проверки предложены следующие ограниченные пилоты. Это повестка обсуждения, а не места в рейтинге:', '']
        for a in selected:
            lines += ['- **' + _business(a['title']) + ':** ' + _business(a['prospect']['reason'])]
        lines += ['']
    elif initiatives:
        lines += ['Обоснованных кандидатов для первого ограниченного пилота пока нет. Сначала нужно выполнить условия:', '']
        for a in initiatives[:3]:
            lines += ['- **' + _business(a['title']) + ':** ' + _business(a['prospect']['main_uncertainty'])]
        lines += ['']
    else:
        lines += [_business(r['explanation']), '']
    lines += ['Перспективность — основание для дальнейшей проверки, не доказательство окупаемости. НПА подтверждает операцию, '
              'а не её фактическую сложность, готовность данных или необходимость ИИ.', '',
              '## Сравнение AI-инициатив', '',
              '| Инициатива и решение | Перспективность: основание | Метрика и ожидаемый эффект | Условие пилота |',
              '| --- | --- | --- | --- |']
    for a in initiatives:
        p = a['prospect']
        lines += ['| ' + ' | '.join((_business(a['title']) + ': ' + _brief(_first_sentence(a['ai_role']), 24),
                    _safe(p['label']) + '. ' + _brief(p['reason'], 24),
                    _business(economic_summary(a)), _business(p['main_uncertainty']))) + ' |']
    lines += ['', '## Перспективные направления для первых пилотов', '']
    if not selected:
        lines += ['Начать с уточнения границ задачи, безопасного доступа к примерам и экспертного эталона; '
                  'не переходить к внедрению только на основании нормативной цитаты.', '']
    for a in selected:
        p = a['prospect']; m = a['measurement']
        lines += ['### ' + _business(a['title']), '',
            '**Проблема для проверки:** ' + _business(a['problem']), '',
            '**Почему ИИ:** ' + _business(a['why_ai']) + ' **Альтернатива без GenAI:** ' + _business(a['non_ai_alternative']), '',
            '**Пилот:** ' + _business(p['pilot']) + ' **Критерий:** ' + _business(m['success_criterion']), '',
            '**Контроль:** ' + _business(a['human_role']) + ' ' + _business(a['risks'][0]), '',
            '**Изменит оценку:** ' + _business(p['change_condition']), '']
    lines += ['## Ожидаемый эффект и предпосылки оценки', '']
    for a in initiatives:
        p = a['prospect']; m = a['measurement']; e = a['economics']
        lines += ['### ' + _business(a['title']), '']
        if a['id'] not in selected_ids:
            lines += ['**Основание:** ' + _business(a['why_ai']) + ' **Без GenAI:** ' + _business(a['non_ai_alternative']), '',
                      '**Проверка:** ' + _business(m['method']) + ' **Критерий:** ' + _business(m['success_criterion']), '',
                      'Изменит оценку: ' + _business(p['change_condition']), '']
        lines += [economic_summary(a), '']
        if m.get('partial_initiative'):
            lines += ['Оценивается только выбранный участок: ' + _business(m['work']) +
                      ' Другие операции не включены. ' + _business(m['boundary']) +
                      ' Случай: ' + _business(m['case_unit']), '']
        numeric = _scenario_lines(a)
        if numeric:
            if not m.get('partial_initiative'):
                lines += ['Граница: ' + _business(m['boundary']) + ' Случай: ' + _business(m['case_unit']), '']
            lines += numeric
        elif e['required_parameters']:
            lines += ['Для расчёта нужны: ' + ', '.join(_safe(PARAMETERS[k]) for k in e['required_parameters']) + '.', '']
        protocol = m.get('quality_protocol')
        if protocol:
            lines += ['**Метрика качества:** ' + _business(protocol['calculation']) +
                      ' **Эталон:** ' + _business(protocol['expert_reference']) +
                      ' **Недопустимые ошибки:** ' + _business(protocol['unacceptable_errors']), '']
        if m.get('secondary'):
            metric = m['secondary']
            lines += ['Дополнительно измерить: ' + _business(metric['name']) +
                      ' (' + _safe(UNIT_LABELS.get(metric['unit'], metric['unit'])) + ').', '']
    lines += ['## Ограничения', '',
              '- До пилота подтвердить доступ к данным, обезличивание и защищённый контур работы с документами.',
              '- Сценарии — допущения, не показатели ведомства. Рабочее время с проверкой и исправлениями отличается от ожидания и срока услуги.',
              '- Пересекающиеся эффекты не суммируются без независимых замеров. Часы, деньги, длительность и качество не складываются.',
              '- Эквивалент мощности — не бюджетная экономия. Неизвестные затраты — не ноль. Решения и утверждение документов остаются уполномоченному человеку.']
    if r['normalization_status'] != 'completed':
        lines += ['- Для части операций нужно уточнить границы и участников до количественных выводов.']
    if any(a['prospect'].get('assessment_issues') for a in initiatives):
        lines += ['- Для отдельных гипотез ещё не определён допустимый измеримый пилот; они не включены в первые кандидаты.']
    lines += ['', 'Полные описания и условия — в JSON; сокращения отмечены многоточием.', '']
    if r['rejected']:
        lines += ['## Отклонённые сценарии', '']
        lines += ['- **' + _brief(x['title'], 10) + ':** ' + _business(_first_sentence(x['reason'])) for x in r['rejected']]
    lines += ['', '## Приложение с источниками', '',
              'Цитаты подтверждают операции, а не прогноз эффекта. Полные цитаты, координаты и расчётные параметры — в opportunities.json.', '']
    for a in initiatives:
        lines += ['**' + _business(a['title']) + '**', '']
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
    return '\n'.join(lines)
