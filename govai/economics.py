"""Pure, auditable effect calculations with explicit premises and no budget-saving claim."""

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_EVEN, localcontext
from itertools import combinations
from typing import Literal

from pydantic import Field, model_validator

from govai.processes import ProcessError, StrictModel

METHOD_VERSION = 'effect-2.1'
UNITS = {
    'annual_cases': 'cases/year', 'applicability': 'share', 'adoption': 'share',
    'before_minutes': 'minutes/case', 'after_minutes': 'minutes/case', 'realization': 'share',
    'hour_cost': 'RUB/hour', 'annual_fixed_opex': 'RUB/year',
    'variable_cost_per_case': 'RUB/case', 'implementation_cost': 'RUB',
}
REQUIRED = tuple(UNITS)
FORMULAS = {
    'used_cases': 'annual_cases * applicability * adoption',
    'released_hours': 'used_cases * (before_minutes - after_minutes) / 60 * realization',
    'capacity_equivalent': 'released_hours * hour_cost',
    'annual_opex': 'annual_fixed_opex + used_cases * variable_cost_per_case',
    'annual_net_capacity_equivalent': 'capacity_equivalent - annual_opex',
    'first_year_net_capacity_equivalent': 'annual_net_capacity_equivalent - implementation_cost',
}
OUTPUT_UNITS = {'used_cases': 'cases/year', 'released_hours': 'hours/year',
                'capacity_equivalent': 'RUB/year', 'annual_opex': 'RUB/year',
                'annual_net_capacity_equivalent': 'RUB/year',
                'first_year_net_capacity_equivalent': 'RUB/first_year'}
SCENARIOS = ('conservative', 'base', 'optimistic')
EFFECT_UNITS = {
    'case_count': 'cases', 'before_minutes': 'minutes/case', 'after_minutes': 'minutes/case',
    'applicability': 'share', 'before_elapsed_minutes': 'minutes/process',
    'after_elapsed_minutes': 'minutes/process',
}
RESOURCE_VERSION = 'resources-1.0'
PROTOTYPE_METHOD_VERSION = 'prototype-1.0'
PROTOTYPE_UNITS = {
    'case_count': 'cases', 'before_minutes': 'minutes/case', 'after_minutes': 'minutes/case',
    'person_days_min': 'person_days', 'person_days_max': 'person_days',
    'hours_per_day': 'hours/person_day', 'team_hour_rate': 'RUB/hour',
    'staff_hour_rate': 'RUB/hour', 'cash_realization': 'share',
    'variable_cost_per_case': 'RUB/case',
}


def _hours_for_cases(cases, saved_minutes):
    return cases * saved_minutes / Decimal(60)


class Parameter(StrictModel):
    value: str | None
    origin: Literal['document', 'user', 'illustrative', 'unknown']
    unit: str
    note: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[str]

    @model_validator(mode='after')
    def consistent(self):
        if not self.note.strip():
            raise ValueError('A parameter needs an explanation.')
        if (self.value is None) != (self.origin == 'unknown'):
            raise ValueError('Unknown is not zero; known values need provenance.')
        if self.origin == 'document' and not self.evidence_refs:
            raise ValueError('A document value needs evidence.')
        if self.value is not None:
            try:
                n = Decimal(self.value)
                if len(self.value) > 32 or not n.is_finite() or n < 0 or n > Decimal('1e15') or n.as_tuple().exponent < -12:
                    raise ValueError('Invalid numeric parameter.')
            except InvalidOperation as exc:
                raise ValueError('Invalid decimal.') from exc
        return self


class Scenario(StrictModel):
    name: Literal['conservative', 'base', 'optimistic']
    parameters: dict[str, Parameter]


def unknown_parameters():
    return {k: Parameter(value=None, origin='unknown', unit=u,
        note='Требуется измерение или подтверждённая оценка владельца процесса.', evidence_refs=[]).model_dump()
        for k, u in UNITS.items()}


def illustrative_scenarios():
    """A reference workload, not an estimate about the agency or current IT systems."""
    shared = {'annual_cases': '10000', 'before_minutes': '15', 'hour_cost': '1000',
              'annual_fixed_opex': '120000', 'variable_cost_per_case': '2', 'implementation_cost': '300000'}
    variations = {
        'conservative': {'applicability': '0.4', 'adoption': '0.4', 'after_minutes': '12', 'realization': '0.6'},
        'base': {'applicability': '0.6', 'adoption': '0.6', 'after_minutes': '9', 'realization': '0.75'},
        'optimistic': {'applicability': '0.8', 'adoption': '0.8', 'after_minutes': '6', 'realization': '0.9'},
    }
    return [Scenario(name=name, parameters={k: Parameter(value=v, origin='illustrative', unit=UNITS[k],
        note='Условная нагрузка пилота; не фактический показатель ведомства. Заменить измерением.',
        evidence_refs=[]) for k, v in {**shared, **changes}.items()}) for name, changes in variations.items()]


def calculate_scenario(scenario: Scenario | dict) -> dict:
    """Decimal arithmetic; missing operands propagate to null only in dependent metrics."""
    try:
        s = Scenario.model_validate(scenario)
        if set(s.parameters) - set(REQUIRED):
            raise ValueError('Unknown parameter')
        params = {**unknown_parameters(), **{k: v.model_dump() for k, v in s.parameters.items()}}
        for k, p in params.items():
            if p['unit'] != UNITS[k]:
                raise ValueError('Wrong unit')
            if UNITS[k] == 'share' and p['value'] is not None and Decimal(p['value']) > 1:
                raise ValueError('Share exceeds one')
        values = {k: Decimal(p['value']) if p['value'] is not None else None for k, p in params.items()}
        def compute(keys, fn):
            return fn(*(values[k] for k in keys)) if all(values[k] is not None for k in keys) else None
        with localcontext() as ctx:
            ctx.prec = 60
            ctx.rounding = ROUND_HALF_EVEN
            values['used_cases'] = compute(['annual_cases', 'applicability', 'adoption'], lambda n,a,u: n*a*u)
            values['released_hours'] = compute(['used_cases', 'before_minutes', 'after_minutes', 'realization'],
                                               lambda n,b,a,r: n*(b-a)/60*r)
            values['capacity_equivalent'] = compute(['released_hours', 'hour_cost'], lambda h,c: h*c)
            values['annual_opex'] = compute(['annual_fixed_opex', 'used_cases', 'variable_cost_per_case'], lambda f,n,c: f+n*c)
            values['annual_net_capacity_equivalent'] = compute(['capacity_equivalent', 'annual_opex'], lambda e,c: e-c)
            values['first_year_net_capacity_equivalent'] = compute(['annual_net_capacity_equivalent', 'implementation_cost'], lambda e,c: e-c)
            def formatted(value):
                if value is None: return None
                rounded = value.quantize(Decimal('0.01'))
                return format(abs(rounded) if rounded == 0 else rounded, 'f')
            results = {k: {'value': formatted(values[k]),
                           'unit': OUTPUT_UNITS[k], 'formula': f} for k, f in FORMULAS.items()}
        missing = [k for k, p in params.items() if p['value'] is None]
        illustrative = any(p['origin'] == 'illustrative' for p in params.values())
        return {'name': s.name, 'status': 'needs_data' if missing else 'illustrative' if illustrative else 'conditional_estimate',
                'parameters': params, 'missing_parameters': missing, 'results': results,
                'budget_savings': None,
                'budget_savings_reason': 'Высвобождение мощности не доказывает сокращение расходов бюджета.',
                'warnings': ['Время после внедрения включает человеческую проверку; нормативный срок не является трудозатратами.']
                    + (['Трудозатраты увеличиваются при заданных предпосылках.'] if values['released_hours'] is not None and values['released_hours'] < 0 else [])}
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ProcessError('Некорректные параметры экономики: проверьте происхождение, единицы, доли и конечные числа.') from exc


def required_parameters(model, population='eligible_cases', *, metric_unit=None):
    """Service metadata is derived from the calculator, never required from an LLM."""
    if population not in ('eligible_cases', 'all_cases'):
        raise ProcessError('Неизвестна база количества случаев.')
    if model == 'time_capacity': return list(REQUIRED)
    if model == 'qualitative': return []
    if model == 'elapsed_time': return ['before_elapsed_minutes', 'after_elapsed_minutes']
    if model == 'labor_time':
        return ['before_minutes', 'after_minutes'] + (['applicability'] if population == 'all_cases' else []) + (
            ['case_count'] if metric_unit == 'hours' else [])
    raise ProcessError('Неизвестная модель эффекта.')


def assess_economics(model, *, blocked_reasons=(), scenarios=None, allow_illustrative=False):
    """No hidden factual defaults. Illustration is an explicit, separately marked case."""
    missing = required_parameters(model)
    if blocked_reasons or model == 'qualitative' or (scenarios is None and not allow_illustrative):
        return {'model': model, 'status': 'needs_data', 'scenarios': [], 'input_parameters': unknown_parameters(),
                'required_parameters': missing, 'reasons': list(blocked_reasons) or [
                    'Индивидуальные параметры не предоставлены; количественная оценка не выполнена.' if model == 'time_capacity'
                    else 'Количественный механизм не обоснован; нужны данные о трудозатратах и качестве.']}
    supplied = scenarios is not None
    if scenarios is None:
        scenarios = illustrative_scenarios()
    names = [Scenario.model_validate(s).name for s in scenarios]
    if len(names) != 3 or set(names) != set(SCENARIOS):
        raise ProcessError('Нужны три различных сценария: консервативный, базовый и оптимистичный.')
    results = [calculate_scenario(s) for s in scenarios]
    base = next(s for s in results if s['name'] == 'base')
    actual = {k: p if p['origin'] in ('document', 'user') else unknown_parameters()[k]
              for k, p in base['parameters'].items()}
    return {'model': model, 'status': 'needs_data' if any(s['status'] == 'needs_data' for s in results)
            else 'illustrative' if any(s['status'] == 'illustrative' for s in results) else 'conditional_estimate',
            'scenarios': results, 'input_parameters': actual,
            'required_parameters': [k for k, p in actual.items() if p['value'] is None],
            'reasons': [] if supplied else ['Все числа сценария иллюстративные; фактические показатели неизвестны.',
                'Наличие измеряемого человеческого этапа — допущение сценария, а не вывод о ручной работе из НПА.']}


def _effect_units(model, population):
    names = required_parameters(model, population)
    if model == 'labor_time': names = [*names, 'case_count']
    return {name: EFFECT_UNITS[name] for name in names}


def _unknown_effect_parameters(model, population):
    return {name: Parameter(value=None, origin='unknown', unit=unit,
        note='Нужны замеры или явно обоснованные индивидуальные предпосылки.', evidence_refs=[]).model_dump()
        for name, unit in _effect_units(model, population).items()}


def calculate_effect_scenario(model, scenario: Scenario | dict, *, population='eligible_cases', metric_unit=None):
    """Calculate one scoped effect. Cases are a stated sample, never an invented annual workload."""
    if model == 'time_capacity': return calculate_scenario(scenario)
    required = required_parameters(model, population, metric_unit=metric_unit)
    if model == 'qualitative':
        raise ProcessError('Для показателя качества без наблюдений нужен план измерения, а не числовой прогноз.')
    try:
        s = Scenario.model_validate(scenario)
        units = _effect_units(model, population)
        if set(s.parameters) - set(units):
            raise ValueError('Unknown parameter or repeated applicability for eligible cases.')
        params = {**_unknown_effect_parameters(model, population),
                  **{name: value.model_dump() for name, value in s.parameters.items()}}
        for name, p in params.items():
            if p['unit'] != units[name]: raise ValueError('Wrong unit')
            if units[name] == 'share' and p['value'] is not None and Decimal(p['value']) > 1:
                raise ValueError('Share exceeds one')
        values = {name: Decimal(p['value']) if p['value'] is not None else None for name, p in params.items()}
        def compute(names, calculation):
            return calculation(*(values[name] for name in names)) if all(values[name] is not None for name in names) else None
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 60, ROUND_HALF_EVEN
            if model == 'labor_time':
                values['eligible_saved_minutes'] = compute(['before_minutes', 'after_minutes'], lambda before, after: before-after)
                minutes = ['eligible_saved_minutes'] + (['applicability'] if population == 'all_cases' else [])
                values['saved_minutes'] = compute(minutes, lambda difference, share=Decimal(1): difference*share)
                values['released_hours'] = compute(['case_count', 'saved_minutes'], _hours_for_cases)
                # A unit of comparison, never an observed workload or a substitute
                # for the unknown case_count. Applicability is already in saved_minutes.
                values['standardized_basis'] = Decimal(1000)
                values['standardized_hours'] = compute(['standardized_basis', 'saved_minutes'], _hours_for_cases)
                formulas = {'eligible_saved_minutes': ('minutes/eligible_case', 'before_minutes - after_minutes'),
                    'saved_minutes': ('minutes/case', '(before_minutes - after_minutes)' +
                    (' * applicability' if population == 'all_cases' else '')),
                    'released_hours': ('hours', 'case_count * saved_minutes / 60'),
                    'standardized_hours': ('hours/1000_' + population, '1000 * saved_minutes / 60')}
                warning = 'Минуты после включают проверку и исправление результата специалистом; нормативный срок не является трудозатратами.'
                if population == 'all_cases':
                    warning += ' Показатель на один случай взвешен по явно указанной доле применимости.'
            else:
                values['saved_elapsed_minutes'] = compute(['before_elapsed_minutes', 'after_elapsed_minutes'], lambda before, after: before-after)
                formulas = {'saved_elapsed_minutes': ('minutes/process', 'before_elapsed_minutes - after_elapsed_minutes')}
                warning = 'Время прохождения относится только к указанным границам процесса; это не рабочие минуты сотрудника и не срок всей услуги.'
            results = {}
            for name, (unit, formula) in formulas.items():
                value = values[name]
                rounded = value.quantize(Decimal('0.01')) if value is not None else None
                results[name] = {'value': None if rounded is None else format(abs(rounded) if rounded == 0 else rounded, 'f'),
                                 'unit': unit, 'formula': formula}
            if model == 'labor_time':
                results['standardized_hours']['basis'] = {
                    'value': '1000', 'unit': 'cases', 'origin': 'illustrative', 'population': population,
                    'note': 'Условная единица сравнения, не фактическая нагрузка и не годовой объём.'}
        missing = [name for name in required if params[name]['value'] is None]
        illustrative = any(p['origin'] == 'illustrative' for p in params.values())
        return {'name': s.name, 'status': 'needs_data' if missing else 'illustrative' if illustrative else 'conditional_estimate',
            'population': population, 'parameters': params, 'missing_parameters': missing, 'results': results,
            'budget_savings': None, 'budget_savings_reason': 'Измерение времени не доказывает сокращение расходов бюджета.',
            'warnings': [warning] + (['При заданных предпосылках время увеличивается; отрицательный эффект сохранён.']
                if any(values[name] is not None and values[name] < 0 for name in formulas) else [])}
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ProcessError('Некорректные параметры эффекта: проверьте единицы, происхождение, долю применимости и конечные числа.') from exc


def assess_effect(model, *, population='eligible_cases', metric_unit=None, blocked_reasons=(), scenarios=None):
    """Assess a single primary metric; no default workload, salary, cost or quality improvement."""
    required = required_parameters(model, population, metric_unit=metric_unit)
    if model == 'qualitative' and scenarios:
        raise ProcessError('Для показателя качества без наблюдений нужен план измерения, а не числовой прогноз.')
    unknown = unknown_parameters() if model == 'time_capacity' else _unknown_effect_parameters(model, population)
    results, parsed = [], None
    if scenarios is not None and model != 'qualitative':
        try: parsed = [Scenario.model_validate(s) for s in scenarios]
        except (ValueError, TypeError) as exc:
            raise ProcessError('Некорректные параметры сценариев эффекта.') from exc
        if len(parsed) != 3 or {s.name for s in parsed} != set(SCENARIOS):
            raise ProcessError('Нужны три различных сценария: консервативный, базовый и оптимистичный.')
        premises = {tuple(sorted((name, Decimal(p.value) if p.value is not None else None)
                                 for name, p in s.parameters.items())) for s in parsed}
        if any(p.value is not None for s in parsed for p in s.parameters.values()) and len(premises) != 3:
            raise ProcessError('Сценарии должны различаться явными числовыми предпосылками, а не только названиями.')
        if model != 'time_capacity' or blocked_reasons:
            results = [calculate_effect_scenario(model, s, population=population, metric_unit=metric_unit) for s in parsed]
    if model == 'time_capacity':
        return assess_economics(model, blocked_reasons=blocked_reasons, scenarios=parsed)
    if blocked_reasons or not results:
        return {'model': model, 'status': 'needs_data', 'scenarios': [], 'input_parameters': unknown,
            'required_parameters': required, 'reasons': list(blocked_reasons) or [
                'Нужны наблюдения и проверка по указанной метрике качества; числовое улучшение не прогнозируется.' if model == 'qualitative'
                else 'Индивидуальные предпосылки не предоставлены; сохранены метрика и план измерения.']}
    base = next(s for s in results if s['name'] == 'base')
    actual = {name: p if p['origin'] in ('document', 'user') else unknown[name] for name, p in base['parameters'].items()}
    return {'model': model, 'status': 'needs_data' if any(s['status'] == 'needs_data' for s in results)
            else 'illustrative' if any(s['status'] == 'illustrative' for s in results) else 'conditional_estimate',
            'scenarios': results, 'input_parameters': actual,
            'required_parameters': [name for name in required if actual[name]['value'] is None],
            'reasons': ['Сценарные допущения не являются измеренными показателями ведомства.']
                if any(s['status'] == 'illustrative' for s in results) else []}


def labor_change(scenario):
    """A presentation metric derived from a verified labor scenario, without changing its contract.

    The percentage concerns one eligible case. It must not claim the same change
    for every case when applicability is unknown, nor create an annual workload.
    Keeping it outside the stored effect result preserves historical restores.
    """
    try:
        if scenario.get('population') not in ('eligible_cases', 'all_cases'):
            raise ValueError('Not a scoped labor scenario')
        if 'eligible_saved_minutes' not in scenario['results']:
            raise ValueError('Not a labor calculation')
        parameters = {name: Parameter.model_validate(scenario['parameters'][name])
                      for name in ('before_minutes', 'after_minutes')}
        if any(p.unit != 'minutes/case' for p in parameters.values()):
            raise ValueError('Wrong unit')
        before = parameters['before_minutes'].value
        after = parameters['after_minutes'].value
        origins = list(dict.fromkeys(p.origin for p in parameters.values()))
        value = None
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 60, ROUND_HALF_EVEN
            if before is not None and after is not None and Decimal(before) != 0:
                percent = ((Decimal(before)-Decimal(after))/Decimal(before)*100).quantize(Decimal('0.01'))
                value = format(abs(percent) if percent == 0 else percent, 'f')
        return {'before_minutes': before, 'after_minutes': after,
            'saved_minutes': scenario['results']['eligible_saved_minutes']['value'],
            'reduction_percent': value, 'percent_unit': '%',
            'percent_formula': '(before_minutes - after_minutes) / before_minutes * 100',
            'population': 'eligible_cases', 'origins': origins,
            'status': 'needs_data' if value is None else 'illustrative' if 'illustrative' in origins else 'conditional_estimate',
            'reason': ('Нельзя определить относительное изменение при неизвестном или нулевом времени до.'
                       if value is None else 'Изменение рабочего времени на один подходящий случай; не доля всех обращений и не бюджетная экономия.')}
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as exc:
        raise ProcessError('Не удалось проверить исходные минуты для относительного изменения времени.') from exc


def assess_resources(resources):
    """Sum an explicit engineering work breakdown; never invent staff, rates or a schedule.

    These are optional model assumptions, not financial facts from the NPA. Invalid
    or incomplete ranges leave the hypothesis usable while suppressing the total.
    The original submitted values remain in the artifact's plan; returned task
    numbers are only those that passed these local checks.
    """
    source = resources if isinstance(resources, dict) else {}
    scope = source.get('scope') if isinstance(source.get('scope'), str) else ''
    dependencies = source.get('dependencies', [])
    cost_basis = source.get('operating_cost_basis') if isinstance(source.get('operating_cost_basis'), str) else ''
    issues, tasks, roles = [], [], []
    if not scope.strip():
        issues.append('Не определён ограниченный объём реализации; общий диапазон трудоёмкости не рассчитан.')
    if (not isinstance(dependencies, list) or any(not isinstance(s, str) or not s.strip() for s in dependencies)):
        dependencies = []
        issues.append('Зависимости реализации требуют уточнения.')
    supplied = source.get('tasks', [])
    if not isinstance(supplied, list) or not supplied:
        supplied = []
        issues.append('Нет разбивки реализации на работы и роли; нужны отдельные оценки этих работ.')
    seen = set()
    for index, task in enumerate(supplied, 1):
        item = task if isinstance(task, dict) else {}
        work = item.get('work') if isinstance(item.get('work'), str) else ''
        role = item.get('role') if isinstance(item.get('role'), str) else ''
        basis = item.get('basis') if isinstance(item.get('basis'), str) else ''
        reasons = []
        lower, upper = item.get('lower_days'), item.get('upper_days')
        origin = item.get('origin')
        if not all(text.strip() for text in (work, role, basis)):
            reasons.append('Нужно назвать работу, необходимую роль и основание диапазона.')
        signature = (' '.join(work.casefold().split()), ' '.join(role.casefold().split()))
        if signature in seen:
            reasons.append('Одинаковая работа одной роли повторяется; её нельзя учитывать дважды.')
        seen.add(signature)
        if item.get('unit') != 'person_days':
            reasons.append('Трудоёмкость реализации должна быть указана в человеко-днях, не в календарных днях.')
        if origin not in ('illustrative', 'unknown'):
            reasons.append('Оценка модели не подтверждает фактические ресурсы; допустимы только сценарные или неизвестные значения.')
        elif origin == 'unknown':
            if lower is not None or upper is not None:
                reasons.append('Неизвестный диапазон нельзя заменять числом.')
        else:
            try:
                # Reuse the existing bounded Decimal/provenance validation.
                numbers = [Parameter(value=v, origin='illustrative', unit='person_days',
                                     note=basis or 'Не указано основание.', evidence_refs=[]) for v in (lower, upper)]
                if Decimal(numbers[0].value) > Decimal(numbers[1].value):
                    raise ValueError('Reversed range')
            except (ValueError, TypeError, InvalidOperation):
                reasons.append('Нужен конечный неотрицательный диапазон от меньшего значения к большему.')
        if reasons or origin == 'unknown':
            lower = upper = None
        tasks.append({'work': work, 'role': role, 'lower_days': lower, 'upper_days': upper,
            'unit': 'person_days', 'origin': 'illustrative' if not reasons and origin == 'illustrative' else 'unknown',
            'basis': basis, 'status': 'invalid' if reasons else 'needs_data' if origin == 'unknown' else 'illustrative',
            'issues': reasons})
        if role.strip() and role not in roles:
            roles.append(role)
        issues.extend(f'Работа {index}: {reason}' for reason in reasons)
    minimum = maximum = None
    complete = bool(tasks) and not issues and all(task['status'] == 'illustrative' for task in tasks)
    if complete:
        with localcontext() as ctx:
            ctx.prec, ctx.rounding = 60, ROUND_HALF_EVEN
            minimum = format(sum(Decimal(t['lower_days']) for t in tasks).quantize(Decimal('0.01')), 'f')
            maximum = format(sum(Decimal(t['upper_days']) for t in tasks).quantize(Decimal('0.01')), 'f')
    elif tasks and not issues:
        issues.append('Оценены не все работы. Общий диапазон неизвестен; известная часть не выдана за полную стоимость реализации.')
    return {'method_version': RESOURCE_VERSION, 'status': 'illustrative' if complete else 'needs_data',
        'scope': scope, 'tasks': tasks, 'roles': roles, 'dependencies': dependencies,
        'effort': {'min': minimum, 'max': maximum, 'unit': 'person_days',
                   'origin': 'illustrative' if complete else 'unknown',
                   'formula': 'sum(task.lower_days) .. sum(task.upper_days)'},
        'operating_costs': {'value': None, 'origin': 'unknown', 'basis': cost_basis or
            'Нужны объём использования, тарифы и объём сопровождения; регулярные расходы не оценены.'},
        'implementation_cost': {'value': None, 'unit': 'RUB', 'origin': 'unknown'},
        'calendar_days': None, 'issues': issues,
        'limitations': ['Каждый диапазон — суммарная трудоёмкость работы её роли или группы ролей; повторно умножать его на число участников нельзя.',
                        'Человеко-дни — трудоёмкость, не календарный срок и не подтверждение наличия сотрудников.',
                        'Диапазон — инженерное сценарное допущение для указанного объёма; до решения о запуске нужна проверка разбивки.',
                        'Без ставок и тарифов стоимость внедрения и использования неизвестна, а не равна нулю.']}


def calculate_prototype_economics(parameters):
    """Calculate an individual prototype scenario from explicit, unit-bearing premises.

    No agency workload, salary, rate or cash conversion is supplied by default.
    A caller must establish the operation, actor, measurement boundaries and any
    document evidence. This arithmetic never proves that saved capacity reduces
    spending, nor that costs of industrial deployment have been included.
    """
    try:
        if not isinstance(parameters, dict) or set(parameters) - PROTOTYPE_UNITS.keys():
            raise ValueError('Unknown prototype parameter')
        parsed = {name: Parameter.model_validate(value) for name, value in parameters.items()}
        params = {name: (parsed[name] if name in parsed else Parameter(
            value=None, origin='unknown', unit=unit,
            note='Нужен замер или отдельно обоснованная предпосылка для этой операции.', evidence_refs=[]))
            for name, unit in PROTOTYPE_UNITS.items()}
        for name, p in params.items():
            if p.unit != PROTOTYPE_UNITS[name]:
                raise ValueError('Wrong prototype unit')
        values = {name: Decimal(p.value) if p.value is not None else None for name, p in params.items()}
        cash = values['cash_realization']
        if cash is not None and not Decimal(0) <= cash <= Decimal(1):
            raise ValueError('Cash realization must lie in [0,1]')
        hours = values['hours_per_day']
        if hours is not None and not Decimal(0) < hours <= Decimal(24):
            raise ValueError('A person-day must contain positive hours, at most 24')
        lower, upper = values['person_days_min'], values['person_days_max']
        if lower is not None and upper is not None and lower > upper:
            raise ValueError('Reversed resource range')

        # Derived values use unrounded Decimal operands. Presentation rounding
        # must never become an operand of a second monetary calculation.
        formulas = {
            'saved_minutes': ('minutes/case', ('before_minutes', 'after_minutes'),
                'before_minutes - after_minutes', lambda b, a: b-a),
            'saved_hours': ('hours', ('case_count', 'before_minutes', 'after_minutes'),
                'case_count * (before_minutes - after_minutes) / 60', lambda n, b, a: _hours_for_cases(n, b-a)),
            'prototype_cost_min': ('RUB', ('person_days_min', 'hours_per_day', 'team_hour_rate'),
                'person_days_min * hours_per_day * team_hour_rate', lambda d, h, r: d*h*r),
            'prototype_cost_max': ('RUB', ('person_days_max', 'hours_per_day', 'team_hour_rate'),
                'person_days_max * hours_per_day * team_hour_rate', lambda d, h, r: d*h*r),
            'capacity_equivalent_per_case': ('RUB/case', ('before_minutes', 'after_minutes', 'staff_hour_rate'),
                '(before_minutes - after_minutes) / 60 * staff_hour_rate', lambda b, a, r: _hours_for_cases(Decimal(1), b-a)*r),
            'net_value_per_case': ('RUB/case', ('before_minutes', 'after_minutes', 'staff_hour_rate', 'cash_realization', 'variable_cost_per_case'),
                '(before_minutes - after_minutes) / 60 * staff_hour_rate * cash_realization - variable_cost_per_case',
                lambda b, a, r, cash, cost: _hours_for_cases(Decimal(1), b-a)*r*cash-cost),
            'net_value_for_cases': ('RUB', ('case_count', 'before_minutes', 'after_minutes', 'staff_hour_rate', 'cash_realization', 'variable_cost_per_case'),
                'case_count * ((before_minutes - after_minutes) / 60 * staff_hour_rate * cash_realization - variable_cost_per_case)',
                lambda n, b, a, r, cash, cost: n*(_hours_for_cases(Decimal(1), b-a)*r*cash-cost)),
        }
        results = {}
        with localcontext() as context:
            context.prec, context.rounding = 120, ROUND_HALF_EVEN

            def result(value, unit, names, formula):
                origins = list(dict.fromkeys(params[name].origin for name in names))
                rounded = value.quantize(Decimal('0.01')) if value is not None else None
                return {'value': None if rounded is None else format(abs(rounded) if rounded == 0 else rounded, 'f'),
                    'exact_value': None if value is None else format(value, 'f'), 'unit': unit, 'formula': formula,
                    'input_parameters': list(names), 'parameter_origins': origins,
                    'status': 'needs_data' if value is None else 'illustrative' if 'illustrative' in origins else 'conditional_estimate'}

            for name, (unit, names, formula, fn) in formulas.items():
                value = fn(*(values[n] for n in names)) if all(values[n] is not None for n in names) else None
                values[name] = value
                results[name] = result(value, unit, names, formula)

            net = values['net_value_per_case']
            for bound in ('min', 'max'):
                cost_name = 'prototype_cost_' + bound
                names = (*formulas[cost_name][1], *formulas['net_value_per_case'][1])
                cost = values[cost_name]
                threshold = cost/net if cost is not None and net is not None and net > 0 else None
                entry = result(threshold, 'cases', names, cost_name + ' / net_value_per_case; net_value_per_case > 0')
                entry['minimum_whole_cases'] = (str(threshold.to_integral_value(rounding=ROUND_CEILING))
                                                 if threshold is not None else None)
                entry['reason'] = ('Неизвестны затраты прототипа или чистый эффект одного случая.'
                    if cost is None or net is None else
                    'Чистый эффект одного случая неположителен; положительного порога покрытия затрат нет.'
                    if net <= 0 else
                    'Условный порог для отдельного прототипа при заданной реализации денежного эффекта; не прогноз окупаемости внедрения.')
                results['threshold_cases_' + bound] = entry

        missing = [name for name, p in params.items() if p.value is None]
        illustrative = any(p.origin == 'illustrative' for p in params.values())
        warnings = [
            'Время после включает проверку и исправления человеком; нормативный срок не является рабочим временем.',
            'Число случаев — указанная база расчёта, не автоматически годовой поток; применимость повторно не умножается.',
            'Высвобождённые часы и их денежный эквивалент не доказывают сокращение расходов. cash_realization — отдельная предпосылка.',
            'Затраты относятся только к указанному отдельному прототипу; неизвестные интеграции, сопровождение и промышленное внедрение не приняты за ноль.',
            'Эффекты и общие работы нескольких инициатив не суммируются без проверки пересечений.',
        ]
        if values['saved_minutes'] is not None and values['saved_minutes'] < 0:
            warnings.append('Рабочее время увеличивается; отрицательный эффект сохранён.')
        if net is not None and net <= 0:
            warnings.append('Чистый денежный эффект неположителен; порог покрытия затрат не рассчитан.')
        return {'method_version': PROTOTYPE_METHOD_VERSION,
            'status': 'needs_data' if missing else 'illustrative' if illustrative else 'conditional_estimate',
            'parameters': {name: value.model_dump() for name, value in params.items()},
            'missing_parameters': missing, 'results': results, 'budget_savings': None,
            'budget_savings_reason': 'Условная денежная реализация времени не доказывает фактическое сокращение расходов.',
            'rounding': {'display': '0.01, ROUND_HALF_EVEN', 'whole_case_threshold': 'ROUND_CEILING of unrounded threshold'},
            'warnings': warnings}
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ProcessError('Некорректные параметры прототипа: проверьте единицы, происхождение, диапазон ресурсов и долю денежной реализации.') from exc


def portfolio_assessment(initiatives, relationships, *, incomplete_input=False):
    """Disjoint IDs are necessary, not sufficient, evidence of independent time savings."""
    conflicts = []
    for left, right in combinations(initiatives, 2):
        shared_actions = sorted(set(left['action_ids']) & set(right['action_ids']))
        shared_sources = sorted(set(left['source_action_ids']) & set(right['source_action_ids']))
        shared_ancestors = sorted(set(left['lineage_ids']) & set(right['lineage_ids']))
        declared = [r for r in relationships if {r['left'], r['right']} == {left['id'], right['id']}]
        if shared_actions or shared_sources or shared_ancestors or any(r['relation'] == 'alternative' for r in declared):
            conflicts.append({'initiative_ids': [left['id'], right['id']], 'shared_action_ids': shared_actions,
                'shared_source_action_ids': shared_sources, 'shared_lineage_ids': shared_ancestors,
                'declared_relations': declared, 'reason': 'Возможен повторный учёт одного действия/этапа; заявление LLM о дополнении не подтверждает независимость.'})
    exclusions = []
    for i in initiatives:
        reasons = ['Совокупный расчёт в этой версии не выполняется; доступна индивидуальная оценка.']
        if len(initiatives) > 1:
            reasons.append('Независимость измеренных базовых трудозатрат инициатив не подтверждена.')
        if incomplete_input:
            reasons.append('Входная нормализация неполная или содержит замечания.')
        if i['economics']['status'] != 'conditional_estimate':
            reasons.append('Параметры неизвестны, иллюстративны либо расчёт заблокирован.')
        if any(i['id'] in c['initiative_ids'] for c in conflicts):
            reasons.append('Обнаружено пересечение эффектов.')
        exclusions.append({'initiative_id': i['id'], 'reasons': reasons})
    # Multi-initiative totals are deliberately unavailable without externally verified measurement boundaries.
    return {'status': 'not_aggregated', 'total_released_hours': None, 'total_capacity_equivalent': None,
            'total_budget_savings': None, 'conflicts': conflicts, 'excluded_from_total': exclusions,
            'reason': 'Показаны индивидуальные оценки. Совокупный эффект без проверки независимости не рассчитывается.'}
