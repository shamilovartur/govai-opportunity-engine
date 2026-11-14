"""A short qualitative rubric and a management report over verified Python results."""

import re
from decimal import Decimal

PROSPECT_VERSION = 'prospect-1.0'
PROSPECT_RULES = {
    'insufficient': 'Механизм добавочной ценности неясен или возможность ограниченного пилота неизвестна.',
    'low': 'Добавочная ценность слаба либо ограниченный пилот пока не обоснован.',
    'medium': 'Механизм конкретен, но перед пилотом нужно устранить существенную неопределённость.',
    'high': 'Механизм конкретен; предложен ограниченный измеримый пилот с контролем рисков.',
    'unknown_data': 'Неизвестная готовность данных отражается в неопределённости, а не автоматически в низкой категории.',
    'scope': 'Перспективность дальнейшей проработки не означает доказанную окупаемость или готовность к внедрению.',
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
        assessed.append({**initiative, 'prospect': {
            'category': category, 'label': CATEGORIES[category],
            'reason': ' '.join((a['value_reason'].strip(), a['pilot_reason'].strip())),
            'main_uncertainty': a['main_uncertainty'], 'change_condition': a['change_condition'],
            'pilot': a['pilot'], 'success_criterion': a['success_criterion'], 'preliminary': True,
        }})
    # These are named groups, not scores or ordinal places within a group.
    return [a for category in CATEGORIES for a in assessed if a['prospect']['category'] == category]


def _safe(value):
    # Escape untrusted text; never embed active HTML or model-generated Markdown links.
    return re.sub(r'([\\`*_{}\[\]()#+.!<>|])', r'\\\1', ' '.join(str(value).split()))


def business_text(value):
    """Keep internal reconciliation diagnostics in JSON, translate their significance."""
    text = ' '.join(str(value).split())
    text = re.sub(r',?\s*включая записи с mode=[A-Za-z_]+', '', text)
    if re.search(r'\b(?:review|requires_review|review_reasons|incomplete|action[_-][\w-]+|context_action_ids|source_action_ids|a\d+)\b|mode=|не объединен[оаы]?|не объединён[оаы]?', text, re.I):
        return 'Границы операции и распределение ответственности требуют уточнения.'
    return text


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
                return f"{_number(hours)} человеко-часа на {_number(count)} {population}"
        elif minutes is not None:
            return f"{_number(minutes)} человеко-минут на " + ('подходящий случай' if measurement['population'] == 'eligible_cases' else 'обращение')
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
    base = next((s for s in e['scenarios'] if s['name'] == 'base'), None)
    if base:
        result = _result_summary(initiative, base)
        if result:
            assumed = any(p['origin'] == 'illustrative' for p in base['parameters'].values())
            return scope + ('Сценарная оценка: ' if assumed else 'Условная оценка: ') + result
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


def render_markdown(artifact):
    r = artifact['report']; initiatives = r['initiatives']; d = r['document']
    document = ' '.join(str(d.get(k) or '') for k in ('act_type', 'number', 'adoption_date')).strip()
    lines = ['# AI-возможности и ожидаемый эффект', '', '## Резюме для руководителя', '',
             _safe(document or 'Нормативный акт') + ('. Редакция: ' + _safe(d['revision_date']) if d.get('revision_date') else ''), '']
    processes = list(dict.fromkeys(business_text(g['process_title']) for a in initiatives for g in a['grounding']))
    if processes:
        lines += ['Рассмотрены процессы: ' + _brief('; '.join(processes), 16) + '.', '']
    lines += ['Перспективность означает обоснованность дальнейшей проработки, а не окупаемость или готовность к внедрению. '
              'Источники сверены с НПА; эффект проверяется пилотом.', '']
    if not initiatives:
        lines += [_business(r['explanation']), '']
    lines += ['## Сравнение AI-инициатив', '',
              '| Инициатива | Что улучшает | Перспективность | Ожидаемый эффект | Что нужно проверить |',
              '| --- | --- | --- | --- | --- |']
    for a in initiatives:
        p = a['prospect']; m = a['measurement']
        base = next((s for s in a['economics']['scenarios'] if s['name'] == 'base'), None)
        effect = _result_summary(a, base) if base else None
        effect = ('Сценарно: ' + effect) if effect and any(v['origin'] == 'illustrative' for v in base['parameters'].values()) else effect
        if not effect:
            effect = _metric_name(a) + ' — измерить'
        if m.get('partial_initiative'):
            effect = 'Выбранный участок: ' + effect
        lines += ['| ' + ' | '.join((_business(a['title']), _business(_first_sentence(a['ai_role'])), _safe(p['label']),
                    _business(effect), _business(p['main_uncertainty']))) + ' |']
    high = [a for a in initiatives if a['prospect']['category'] == 'high']
    lines += ['', '## Перспективные направления для первых пилотов', '']
    if high:
        lines += ['Сначала проверить инициативы высокой перспективности: их ограниченные пилоты и критерии описаны ниже.', '']
    else:
        lines += ['До пилотов устранить обозначенные неопределённости. Низкая перспективность и недостаток оснований — разные выводы.', '']
    lines += ['## Ожидаемый эффект и предпосылки оценки', '']
    for a in initiatives:
        p = a['prospect']; m = a['measurement']; e = a['economics']
        lines += ['### ' + _business(a['title']), '',
                  '**Основание:** ' + _business(p['reason']) + ' **Пилот:** ' + _business(m['method']) +
                  ' **Критерий:** ' + _business(m['success_criterion']), '']
        # Technical uncertainty is not copied as business prose; each scope retains its explicit boundary.
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
        lines += ['Изменит оценку: ' + _business(p['change_condition']), '']
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
    lines += ['', 'Полные описания и условия — в JSON; сокращения отмечены многоточием.', '']
    if r['rejected']:
        lines += ['## Отклонённые сценарии', '']
        lines += ['- **' + _brief(x['title'], 8) + ':** ' + _brief(_first_sentence(x['reason']), 10) for x in r['rejected']]
    lines += ['', '## Приложение с источниками', '',
              'Цитаты подтверждают операции, а не прогноз эффекта. Полные цитаты, координаты и расчётные параметры — в opportunities.json.', '']
    for a in initiatives:
        lines += ['**' + _business(a['title']) + '**', '']
        eid = a['grounding'][0]['source_candidates'][0]['evidence_refs'][0]
        proof = r['evidence'][eid]
        lines += ['Пункт ' + _safe(proof['clause_number'] or 'не определён') +
                  (f"; страница PDF {proof['page_number']}" if proof['page_number'] else '') + ':', '']
        quote = proof['quote']
        if len(quote) > 300:
            quote = quote[:299].rstrip() + '…'
        fence = '`' * max(3, 1 + max((len(x) for x in re.findall(r'`+', quote)), default=0))
        lines += [fence + 'text', quote, fence, '']
    return '\n'.join(lines)
