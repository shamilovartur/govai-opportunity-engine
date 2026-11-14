"""Pure, auditable capacity calculations. No LLM numbers and no budget-saving claim."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from itertools import combinations
from typing import Literal

from pydantic import Field, model_validator

from govai.processes import ProcessError, StrictModel

METHOD_VERSION = 'capacity-1.0'
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


class Parameter(StrictModel):
    value: str | None
    origin: Literal['document', 'user', 'illustrative', 'unknown']
    unit: str
    note: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[str]

    @model_validator(mode='after')
    def consistent(self):
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


def assess_economics(model, *, blocked_reasons=(), scenarios=None, allow_illustrative=True):
    """No hidden factual defaults. Illustration is an explicit, separately marked case."""
    missing = list(REQUIRED) if model == 'time_capacity' else []
    if blocked_reasons or model == 'qualitative':
        return {'model': model, 'status': 'needs_data', 'scenarios': [], 'input_parameters': unknown_parameters(),
                'required_parameters': missing, 'reasons': list(blocked_reasons) or [
                    'Количественный механизм не обоснован; нужны данные о трудозатратах и качестве.']}
    supplied = scenarios is not None
    if scenarios is None:
        scenarios = illustrative_scenarios() if allow_illustrative else [Scenario(name=n, parameters={}) for n in SCENARIOS]
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
