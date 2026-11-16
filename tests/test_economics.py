from copy import deepcopy
from decimal import Decimal

import pytest

from govai.economics import (REQUIRED, Parameter, Scenario, UNITS, calculate_scenario, assess_economics,
                             illustrative_scenarios, unknown_parameters, portfolio_assessment,
                             EFFECT_UNITS, required_parameters, calculate_effect_scenario, assess_effect)
from govai.processes import ProcessError


def test_reference_base_calculation_is_exact_and_not_budget_saving():
    s = calculate_scenario(illustrative_scenarios()[1])
    expected = {'used_cases': '3600.00', 'released_hours': '270.00', 'capacity_equivalent': '270000.00',
                'annual_opex': '127200.00', 'annual_net_capacity_equivalent': '142800.00',
                'first_year_net_capacity_equivalent': '-157200.00'}
    assert {k:v['value'] for k,v in s['results'].items()} == expected
    assert s['status'] == 'illustrative' and s['budget_savings'] is None
    assert all(p['origin']=='illustrative' for p in s['parameters'].values())
    assert 'realization' in s['results']['released_hours']['formula']


def test_scenarios_change_explicit_premises_not_a_final_multiplier():
    cases = [calculate_scenario(s) for s in illustrative_scenarios()]
    assert [c['results']['released_hours']['value'] for c in cases] == ['48.00','270.00','864.00']
    for k in ['applicability','adoption','after_minutes','realization']:
        assert len({s['parameters'][k]['value'] for s in cases}) == 3


def test_unknown_cost_is_not_zero_and_does_not_erase_known_hours():
    s = illustrative_scenarios()[1].model_dump()
    s['parameters']['hour_cost'] = unknown_parameters()['hour_cost']
    result = calculate_scenario(s)
    assert result['results']['released_hours']['value'] == '270.00'
    assert result['results']['capacity_equivalent']['value'] is None
    assert result['results']['first_year_net_capacity_equivalent']['value'] is None
    assert result['missing_parameters'] == ['hour_cost']


def test_no_parameters_no_implicit_factual_defaults():
    e = assess_economics('time_capacity')
    assert e['status']=='needs_data'
    assert all(p['value'] is None for p in e['input_parameters'].values())
    assert e['scenarios'] == []
    assert set(e['required_parameters']) == set(REQUIRED)


@pytest.mark.parametrize('value', ['NaN','Infinity','-1','1e100000','0.00000000000000001'])
def test_invalid_finite_numeric_inputs(value):
    s=illustrative_scenarios()[0].model_dump(); s['parameters']['annual_cases']['value']=value
    with pytest.raises(ProcessError): calculate_scenario(s)


@pytest.mark.parametrize('mutation', ['share','unit','unknown_non_null','known_null','extra_parameter'])
def test_parameters_units_and_provenance_are_validated(mutation):
    s=illustrative_scenarios()[0].model_dump()
    if mutation=='share': s['parameters']['adoption']['value']='1.1'
    elif mutation=='unit': s['parameters']['before_minutes']['unit']='days'
    elif mutation=='unknown_non_null': s['parameters']['hour_cost']['origin']='unknown'
    elif mutation=='known_null': s['parameters']['hour_cost']['value']=None
    else: s['parameters']['fake']=s['parameters']['hour_cost']
    with pytest.raises(ProcessError): calculate_scenario(s)


def test_zero_is_allowed_only_as_explicit_value_and_negative_effect_is_preserved():
    s=illustrative_scenarios()[0].model_dump()
    s['parameters']['after_minutes']['value']='20'
    result=calculate_scenario(s)
    assert Decimal(result['results']['released_hours']['value']) < 0
    assert len(result['warnings'])==2
    s['parameters']['adoption']['value']='0'
    assert calculate_scenario(s)['results']['released_hours']['value']=='0.00'


def test_pure_calculation_does_not_mutate_inputs_and_requires_three_cases():
    s=illustrative_scenarios()[0].model_dump(); before=deepcopy(s)
    calculate_scenario(s); assert s==before
    with pytest.raises(ProcessError): assess_economics('time_capacity', scenarios=[s,s,s])


def test_spurious_financial_premises_do_not_unblock_reviewed_operation():
    e=assess_economics('time_capacity', blocked_reasons=['Спорная операция'], scenarios=illustrative_scenarios())
    assert not e['scenarios'] and e['status']=='needs_data'


def test_complementary_llm_label_does_not_allow_summing_shared_sources():
    rows=[{'id':'a','action_ids':['n1'],'source_action_ids':['s1'],'lineage_ids':['n1'],
           'economics':{'status':'conditional_estimate'}},
          {'id':'b','action_ids':['n2'],'source_action_ids':['s1'],'lineage_ids':['n2'],
           'economics':{'status':'conditional_estimate'}}]
    r=portfolio_assessment(rows,[{'left':'a','right':'b','relation':'complementary','reason':'LLM says independent'}])
    assert r['conflicts'][0]['shared_source_action_ids']==['s1']
    assert r['total_released_hours'] is None and len(r['excluded_from_total'])==2
    rows[1]['source_action_ids']=['s2']
    assert portfolio_assessment(rows,[])['total_capacity_equivalent'] is None


def test_siblings_of_one_overview_and_declared_alternatives_are_conflicts():
    rows=[{'id':x,'action_ids':[x],'source_action_ids':[x],'lineage_ids':[x,'parent'],
           'economics':{'status':'illustrative'}} for x in ['a','b']]
    assert portfolio_assessment(rows,[])['conflicts'][0]['shared_lineage_ids']==['parent']
    rows[1]['lineage_ids']=['b']
    assert portfolio_assessment(rows,[{'left':'a','right':'b','relation':'alternative'}])['conflicts']


def test_even_single_conditional_estimate_explains_unavailable_portfolio_total():
    item = {'id': 'one', 'action_ids': ['a'], 'source_action_ids': ['s'], 'lineage_ids': ['a'],
            'economics': {'status': 'conditional_estimate'}}
    result = portfolio_assessment([item], [])
    assert result['total_capacity_equivalent'] is None
    assert result['excluded_from_total'][0]['initiative_id'] == 'one'
    assert result['excluded_from_total'][0]['reasons']


def effect_case(name='base', origin='illustrative', **values):
    return {'name': name, 'parameters': {key: {'value': value, 'unit': EFFECT_UNITS[key],
        'origin': origin if value is not None else 'unknown', 'evidence_refs': [],
        'note': ('Индивидуальное допущение: чтение двух документов, сопоставление и проверка специалистом.'
                 if value is not None else 'Предстоит измерить на ограниченном пилоте.')}
        for key, value in values.items()}}


def test_scoped_labor_hours_need_neither_salary_nor_annual_workload():
    source = effect_case(case_count='1000', before_minutes='20', after_minutes='12')
    original = deepcopy(source)
    result = calculate_effect_scenario('labor_time', source, metric_unit='hours')
    assert source == original
    assert result['results']['released_hours'] == {
        'value': '133.33', 'unit': 'hours', 'formula': 'case_count * saved_minutes / 60'}
    assert result['results']['saved_minutes']['value'] == '8.00'
    assert result['status'] == 'illustrative' and result['budget_savings'] is None
    assert set(result['parameters']) == {'before_minutes', 'after_minutes', 'case_count'}
    assert all(p['origin'] == 'illustrative' for p in result['parameters'].values())


def test_per_case_time_does_not_invent_volume_and_hours_require_explicit_count():
    source = effect_case(before_minutes='20', after_minutes='12')
    per_case = calculate_effect_scenario('labor_time', source)
    hours = calculate_effect_scenario('labor_time', source, metric_unit='hours')
    assert per_case['results']['saved_minutes']['value'] == '8.00'
    assert per_case['results']['released_hours']['value'] is None
    assert per_case['status'] == 'illustrative' and per_case['missing_parameters'] == []
    assert hours['status'] == 'needs_data' and hours['missing_parameters'] == ['case_count']
    assert required_parameters('labor_time') == ['before_minutes', 'after_minutes']
    assert required_parameters('labor_time', metric_unit='hours') == ['before_minutes', 'after_minutes', 'case_count']


def test_applicability_is_explicit_for_all_cases_and_never_repeated_for_eligible_cases():
    source = effect_case(case_count='1000', before_minutes='20', after_minutes='12', applicability='0.5')
    with pytest.raises(ProcessError): calculate_effect_scenario('labor_time', source)
    result = calculate_effect_scenario('labor_time', source, population='all_cases')
    assert result['results']['released_hours']['value'] == '66.67'
    assert result['results']['saved_minutes']['value'] == '4.00'
    source['parameters'].pop('applicability')
    unknown = calculate_effect_scenario('labor_time', source, population='all_cases')
    assert unknown['results']['released_hours']['value'] is None
    assert unknown['results']['saved_minutes']['value'] is None
    assert unknown['missing_parameters'] == ['applicability']


def test_scoped_negative_effect_and_unknown_after_time_are_not_hidden():
    source = effect_case(case_count='1000', before_minutes='12', after_minutes='20')
    result = calculate_effect_scenario('labor_time', source)
    assert result['results']['released_hours']['value'] == '-133.33'
    assert result['results']['saved_minutes']['value'] == '-8.00'
    assert 'отрицательный эффект' in result['warnings'][-1]
    source['parameters']['after_minutes'].update(value=None, origin='unknown')
    result = calculate_effect_scenario('labor_time', source)
    assert all(p['value'] is None for p in result['results'].values())


@pytest.mark.parametrize('mutation', ['unit', 'share', 'note', 'origin', 'infinity', 'money'])
def test_scoped_parameters_are_not_exempt_from_validation(mutation):
    source = effect_case(case_count='1000', before_minutes='20', after_minutes='12', applicability='0.5')
    if mutation == 'unit': source['parameters']['before_minutes']['unit'] = 'days'
    elif mutation == 'share': source['parameters']['applicability']['value'] = '1.2'
    elif mutation == 'note': source['parameters']['before_minutes']['note'] = ' '
    elif mutation == 'origin': source['parameters']['before_minutes']['origin'] = 'unknown'
    elif mutation == 'infinity': source['parameters']['before_minutes']['value'] = 'Infinity'
    else: source['parameters']['hour_cost'] = source['parameters']['before_minutes']
    with pytest.raises(ProcessError): calculate_effect_scenario('labor_time', source, population='all_cases')


def test_elapsed_time_is_a_distinct_metric_with_no_labor_or_money_claim():
    result = calculate_effect_scenario('elapsed_time', effect_case(
        before_elapsed_minutes='1440', after_elapsed_minutes='1320'))
    assert result['results'] == {'saved_elapsed_minutes': {'value': '120.00', 'unit': 'minutes/process',
                                  'formula': 'before_elapsed_minutes - after_elapsed_minutes'}}
    assert 'не рабочие минуты' in result['warnings'][0]
    assert result['budget_savings'] is None
    with pytest.raises(ProcessError): calculate_effect_scenario('elapsed_time', effect_case(before_minutes='20'))


def test_quality_without_observations_has_a_measurement_plan_and_no_fake_improvement():
    result = assess_effect('qualitative')
    assert result['status'] == 'needs_data' and not result['scenarios']
    assert result['required_parameters'] == [] and result['input_parameters'] == {}
    assert 'метрике качества' in result['reasons'][0]
    with pytest.raises(ProcessError):
        assess_effect('qualitative', scenarios=[effect_case(before_minutes='20', after_minutes='12')])


def test_individual_time_scenarios_keep_assumptions_separate_from_observed_facts():
    cases = [effect_case(name, case_count='1000', before_minutes='20', after_minutes=after)
             for name, after in [('conservative', '16'), ('base', '12'), ('optimistic', '10')]]
    result = assess_effect('labor_time', metric_unit='hours', scenarios=cases)
    assert result['status'] == 'illustrative'
    assert [s['results']['released_hours']['value'] for s in result['scenarios']] == ['66.67', '133.33', '166.67']
    assert all(p['value'] is None for p in result['input_parameters'].values())
    assert all(p['origin'] == 'illustrative' for s in result['scenarios'] for p in s['parameters'].values())
    assert 'не являются измеренными' in result['reasons'][0]
    missing = assess_effect('labor_time', metric_unit='hours')
    assert missing['status'] == 'needs_data' and not missing['scenarios']
    assert set(missing['required_parameters']) == {'before_minutes', 'after_minutes', 'case_count'}


def test_three_scenario_names_without_distinct_premises_are_rejected():
    cases = [effect_case(name, before_minutes='20', after_minutes='12')
             for name in ('conservative', 'base', 'optimistic')]
    with pytest.raises(ProcessError, match='числовыми предпосылками'):
        assess_effect('labor_time', scenarios=cases)
    with pytest.raises(ProcessError, match='три различных сценария'):
        assess_effect('labor_time', scenarios=cases[:2])


def test_scoped_values_do_not_override_measurement_restrictions():
    cases = [effect_case(name, before_minutes='20', after_minutes=after)
             for name, after in [('conservative', '16'), ('base', '12'), ('optimistic', '10')]]
    result = assess_effect('labor_time', scenarios=cases, blocked_reasons=['Границы работы не определены.'])
    assert result['status'] == 'needs_data' and not result['scenarios']
    assert result['reasons'] == ['Границы работы не определены.']


def test_scenario_names_or_numeric_formatting_do_not_create_distinct_premises():
    from govai.economics import assess_effect
    cases=[]
    for name,value in zip(('conservative','base','optimistic'),('20','20.0','20.00')):
        cases.append({'name':name,'parameters':{
            'before_minutes':{'value':value,'origin':'illustrative','unit':'minutes/case','note':'Один и тот же условный замер.','evidence_refs':[]},
            'after_minutes':{'value':'12','origin':'illustrative','unit':'minutes/case','note':'Один и тот же условный замер.','evidence_refs':[]}}})
    with pytest.raises(ProcessError,match='различаться'):
        assess_effect('labor_time',scenarios=cases)


def test_new_effect_stage_validates_annual_scenarios_even_if_financial_scope_is_blocked():
    from govai.economics import assess_effect
    cases=[s.model_dump() for s in illustrative_scenarios()]
    cases[0]['parameters']['hour_cost']['unit']='minutes/case'
    with pytest.raises(ProcessError):
        assess_effect('time_capacity',scenarios=cases,blocked_reasons=['Не установлены границы работы.'])
    cases=[s.model_dump() for s in illustrative_scenarios()]
    for i in (1,2): cases[i]['parameters']=deepcopy(cases[0]['parameters'])
    with pytest.raises(ProcessError,match='различаться'):
        assess_effect('time_capacity',scenarios=cases)


def _participant_case(categories=('agency', 'organization', 'applicant', 'unknown', 'system')):
    """Explicitly sourced actors; labels are a test plan, not inferred production rules."""
    from test_normalization import simple_case, node, plan_for, Normalizer
    from govai.normalization import normalize_analysis
    lines = ('Специалист ведомства сопоставляет сведения в заявлении.',
             'Специалист МФЦ сопоставляет сведения в заявлении.',
             'Заявитель проверяет содержание своего заявления.',
             'Сведения сопоставляются перед подготовкой проекта.',
             'Информационная система автоматически проверяет заполнение поля.')
    actors = ('Специалист ведомства', 'Специалист МФЦ', 'Заявитель', None, 'Информационная система')
    _, _, analysis = simple_case(lines, actors=actors)
    nodes = []
    for i, (sid, source) in enumerate(analysis.actions.items()):
        nodes.append(node(analysis, [sid], key=f'actor-{i}', process=f'process-{i}',
                          participant=categories[i], participant_refs=source['actor']['refs'] if source['actor'] else []))
    normalized = normalize_analysis(analysis, Normalizer(plan_for(analysis, nodes)))
    return analysis, normalized


def test_measured_applicant_time_keeps_its_own_participant_and_cannot_become_agency_money():
    from govai.opportunities import measurement_participant, economic_restrictions
    _, normalized = _participant_case(); m = normalized['map']
    aid = 'actor-2'; source_ids = next(a['source_action_ids'] for a in m['actions'] if a['id'] == aid)
    participant = measurement_participant(m, source_ids)
    assert participant['category'] == 'applicant' and 'заявителя' in participant['label']
    assert participant['actors'] == ['Заявитель'] and not participant['limitations']
    assert aid not in economic_restrictions(m, measured_sources=source_ids, model='labor_time')
    assert any('заявителя' in reason for reason in economic_restrictions(
        m, measured_sources=source_ids, model='time_capacity')[aid])


def test_agency_and_mfc_are_distinct_and_mixed_labor_requires_source_boundary():
    from govai.opportunities import measurement_participant, economic_restrictions
    _, normalized = _participant_case(); m = normalized['map']
    nodes = {a['id']: a for a in m['actions']}
    agency, mfc = nodes['actor-0']['source_action_ids'], nodes['actor-1']['source_action_ids']
    assert measurement_participant(m, agency)['category'] == 'agency'
    assert measurement_participant(m, mfc)['category'] == 'organization'
    assert 'МФЦ' in measurement_participant(m, mfc)['label']
    assert measurement_participant(m, agency + mfc)['category'] == 'mixed'
    blocked = economic_restrictions(m, measured_sources=agency + mfc, model='labor_time')
    assert set(blocked) == {'actor-0', 'actor-1'}
    assert all(any('разграничить' in reason for reason in reasons) for reasons in blocked.values())
    assert not economic_restrictions(m, measured_sources=agency, model='labor_time')
    assert not economic_restrictions(m, measured_sources=mfc, model='labor_time')


def test_unknown_actor_or_unclassified_review_is_not_invented_as_agency_work():
    from govai.opportunities import measurement_participant, economic_restrictions
    _, normalized = _participant_case(); m = normalized['map']
    nodes = {a['id']: a for a in m['actions']}
    missing = nodes['actor-3']['source_action_ids']
    assert measurement_participant(m, missing)['category'] == 'unknown'
    assert 'actor-3' in economic_restrictions(m, measured_sources=missing, model='labor_time')
    # Even an explicit actor title does not establish an absent participant classification.
    nodes['actor-0'].update(kind='review', participant='unknown', participant_refs=[])
    review = nodes['actor-0']['source_action_ids']
    assert measurement_participant(m, review)['actors'] == ['Специалист ведомства']
    assert measurement_participant(m, review)['category'] == 'unknown'
    assert 'actor-0' in economic_restrictions(m, measured_sources=review, model='labor_time')
    # It still remains available for qualitative hypotheses and elapsed-time measurement.
    from govai.opportunities import operational_action_ids
    assert 'actor-0' in operational_action_ids(m)
    assert 'actor-0' not in economic_restrictions(m, measured_sources=review, model='qualitative')


def test_system_operation_is_not_employee_time_and_invalid_actor_source_is_rejected():
    from govai.opportunities import measurement_participant, economic_restrictions
    _, normalized = _participant_case(); m = normalized['map']
    source_ids = next(a['source_action_ids'] for a in m['actions'] if a['id'] == 'actor-4')
    assert measurement_participant(m, source_ids)['category'] == 'system'
    limits = economic_restrictions(m, measured_sources=source_ids, model='labor_time')['actor-4']
    assert any('человеческого рабочего времени' in text for text in limits)
    assert any('автоматизацию' in text for text in limits)
    with pytest.raises(ProcessError): measurement_participant(m, ['nonexistent'])
    with pytest.raises(ProcessError): measurement_participant(m, source_ids * 2)


def test_standardized_thousand_case_illustration_is_not_an_actual_workload():
    source = effect_case(before_minutes='20', after_minutes='12')
    original = deepcopy(source)
    result = calculate_effect_scenario('labor_time', source)
    sample = result['results']['standardized_hours']
    assert sample['value'] == '133.33'
    assert sample['unit'] == 'hours/1000_eligible_cases'
    assert sample['formula'] == '1000 * saved_minutes / 60'
    assert sample['basis'] == {'value': '1000', 'unit': 'cases', 'origin': 'illustrative',
        'population': 'eligible_cases',
        'note': 'Условная единица сравнения, не фактическая нагрузка и не годовой объём.'}
    assert result['results']['released_hours']['value'] is None
    assert result['parameters']['case_count']['value'] is None
    assert not {'annual_cases', 'hour_cost', 'applicability'} & result['parameters'].keys()
    assert source == original
    # An explicitly supplied sample remains separate from the standardized unit.
    source['parameters']['case_count'] = effect_case(case_count='200')['parameters']['case_count']
    result = calculate_effect_scenario('labor_time', source)
    assert result['results']['released_hours']['value'] == '26.67'
    assert result['results']['standardized_hours']['value'] == '133.33'


def test_standardized_all_case_illustration_needs_applicability_and_never_defaults_to_full_coverage():
    source = effect_case(before_minutes='20', after_minutes='12')
    unknown = calculate_effect_scenario('labor_time', source, population='all_cases')
    assert unknown['results']['standardized_hours']['value'] is None
    assert unknown['results']['saved_minutes']['value'] is None
    assert unknown['missing_parameters'] == ['applicability']
    source['parameters']['applicability'] = effect_case(applicability='0.25')['parameters']['applicability']
    result = calculate_effect_scenario('labor_time', source, population='all_cases')
    assert result['results']['saved_minutes']['value'] == '2.00'
    sample = result['results']['standardized_hours']
    assert sample['value'] == '33.33' and sample['unit'] == 'hours/1000_all_cases'
    assert sample['basis']['population'] == 'all_cases'
    assert result['results']['released_hours']['value'] is None
    # Applying the same share to an already eligible population remains invalid.
    with pytest.raises(ProcessError): calculate_effect_scenario('labor_time', source)


def test_standardized_illustration_preserves_negative_effect_and_is_separate_from_observed_values():
    result = calculate_effect_scenario('labor_time', effect_case(origin='user', before_minutes='8', after_minutes='12'))
    assert result['status'] == 'conditional_estimate'
    assert result['results']['standardized_hours']['value'] == '-66.67'
    assert result['results']['standardized_hours']['basis']['origin'] == 'illustrative'
    assert all(p['origin'] == 'user' for k, p in result['parameters'].items() if k != 'case_count')
    assert result['budget_savings'] is None and 'отрицательный эффект' in result['warnings'][-1]
    elapsed = calculate_effect_scenario('elapsed_time', effect_case(before_elapsed_minutes='20', after_elapsed_minutes='12'))
    assert 'standardized_hours' not in elapsed['results']


def test_eligible_minutes_remain_known_without_all_case_applicability_but_do_not_imply_coverage():
    source = effect_case(before_minutes='20', after_minutes='12')
    result = calculate_effect_scenario('labor_time', source, population='all_cases')
    assert result['results']['eligible_saved_minutes'] == {
        'value': '8.00', 'unit': 'minutes/eligible_case', 'formula': 'before_minutes - after_minutes'}
    assert result['status'] == 'needs_data' and result['missing_parameters'] == ['applicability']
    assert result['results']['saved_minutes']['value'] is None
    assert result['results']['standardized_hours']['value'] is None
    assert result['results']['released_hours']['value'] is None
    source['parameters']['applicability'] = effect_case(applicability='0.25')['parameters']['applicability']
    result = calculate_effect_scenario('labor_time', source, population='all_cases')
    assert result['results']['eligible_saved_minutes']['value'] == '8.00'
    assert result['results']['saved_minutes']['value'] == '2.00'
    assert result['results']['standardized_hours']['value'] == '33.33'


def test_relative_labor_change_is_decimal_scoped_and_keeps_scenario_provenance():
    from govai.economics import labor_change
    scenario = calculate_effect_scenario('labor_time', effect_case(
        before_minutes='20', after_minutes='12', case_count='1000'))
    original = deepcopy(scenario)
    result = labor_change(scenario)
    assert result['before_minutes'] == '20' and result['after_minutes'] == '12'
    assert result['saved_minutes'] == '8.00' and result['reduction_percent'] == '40.00'
    assert result['percent_unit'] == '%' and result['population'] == 'eligible_cases'
    assert result['origins'] == ['illustrative'] and result['status'] == 'illustrative'
    assert result['percent_formula'] == '(before_minutes - after_minutes) / before_minutes * 100'
    assert scenario == original
    assert scenario['results']['released_hours']['value'] == '133.33'
    assert 'reduction_percent' not in scenario['results']  # historical artifact calculations are unchanged


def test_relative_time_change_is_not_a_claim_about_all_cases_or_a_money_effect():
    from govai.economics import labor_change
    scenario = calculate_effect_scenario('labor_time', effect_case(before_minutes='20', after_minutes='12'),
                                         population='all_cases')
    assert scenario['results']['saved_minutes']['value'] is None
    result = labor_change(scenario)
    assert result['reduction_percent'] == '40.00' and result['population'] == 'eligible_cases'
    assert 'не доля всех обращений' in result['reason']
    assert 'released_hours' not in result and 'budget_savings' not in result


@pytest.mark.parametrize('before,after,expected', [('12', '20', '-66.67'), ('0', '12', None),
                                                 ('20', None, None), ('0.003', '0.002', '33.33')])
def test_relative_change_keeps_negative_effect_zero_denominator_and_precision(before, after, expected):
    from govai.economics import labor_change
    scenario = calculate_effect_scenario('labor_time', effect_case(
        origin='user', before_minutes=before, after_minutes=after))
    value = labor_change(scenario)
    assert value['reduction_percent'] == expected
    assert value['status'] == ('needs_data' if expected is None else 'conditional_estimate')
    assert 'illustrative' not in value['origins']


def test_relative_change_rejects_elapsed_or_corrupted_labor_units():
    from govai.economics import labor_change
    elapsed = calculate_effect_scenario('elapsed_time', effect_case(before_elapsed_minutes='20', after_elapsed_minutes='12'))
    with pytest.raises(ProcessError): labor_change(elapsed)
    labor = calculate_effect_scenario('labor_time', effect_case(before_minutes='20', after_minutes='12'))
    labor['parameters']['before_minutes']['unit'] = 'calendar_days'
    with pytest.raises(ProcessError): labor_change(labor)


def resource_case():
    return {
        'scope': 'Отдельный прототип сверки текстовых документов без интеграции с ведомственной системой.',
        'tasks': [
            {'work': 'Подготовить набор обезличенных документов и перечень проверяемых сведений.',
             'role': 'Предметный специалист', 'lower_days': '1.5', 'upper_days': '3',
             'unit': 'person_days', 'origin': 'illustrative',
             'basis': 'Предполагается готовый перечень документов; требуется проверить обезличенные примеры.'},
            {'work': 'Настроить сверку документов и подготовить интерфейс проверки найденных расхождений.',
             'role': 'Разработчик', 'lower_days': '3.25', 'upper_days': '6.5',
             'unit': 'person_days', 'origin': 'illustrative',
             'basis': 'Прототип принимает текстовые файлы; подключение действующей системы в оценку не входит.'},
        ],
        'dependencies': ['До начала подтвердить разрешение на обработку обезличенных документов.'],
        'operating_cost_basis': 'Расходы зависят от количества обрабатываемых документов, тарифов модели и объёма поддержки.',
    }


def test_resource_work_breakdown_sums_person_days_without_inventing_calendar_budget_or_staff():
    from govai.economics import assess_resources, RESOURCE_VERSION
    submitted = resource_case(); original = deepcopy(submitted)
    result = assess_resources(submitted)
    assert submitted == original
    assert result['method_version'] == RESOURCE_VERSION
    assert result['status'] == 'illustrative'
    assert result['effort'] == {'min': '4.75', 'max': '9.50', 'unit': 'person_days',
                               'origin': 'illustrative', 'formula': 'sum(task.lower_days) .. sum(task.upper_days)'}
    assert result['roles'] == ['Предметный специалист', 'Разработчик']
    assert result['dependencies'] == submitted['dependencies'] and result['scope'] == submitted['scope']
    assert result['implementation_cost'] == {'value': None, 'unit': 'RUB', 'origin': 'unknown'}
    assert result['calendar_days'] is None and result['operating_costs']['value'] is None
    assert result['operating_costs']['origin'] == 'unknown' and result['operating_costs']['basis']
    assert all(t['origin'] == 'illustrative' and t['status'] == 'illustrative' for t in result['tasks'])
    assert not result['issues']
    assert any('не календарный срок' in text for text in result['limitations'])


def test_unknown_work_range_does_not_turn_known_subset_into_full_implementation_estimate():
    from govai.economics import assess_resources
    submitted = resource_case()
    submitted['tasks'][1].update(lower_days=None, upper_days=None, origin='unknown')
    result = assess_resources(submitted)
    assert result['effort']['min'] is None and result['effort']['max'] is None
    assert result['status'] == 'needs_data'
    assert result['tasks'][0]['lower_days'] == '1.5'  # the known assumption is retained
    assert result['tasks'][1]['lower_days'] is None
    assert any('не выдана за полную' in text for text in result['issues'])
    assert result['implementation_cost']['value'] is None


@pytest.mark.parametrize('fault', ['negative', 'infinite', 'reversed', 'unit', 'unknown_number',
                                  'false_factual', 'missing_basis', 'missing_role', 'missing_scope'])
def test_bad_optional_resource_assumptions_suppress_total_instead_of_losing_hypothesis(fault):
    from govai.economics import assess_resources
    submitted = resource_case(); task = submitted['tasks'][0]
    if fault == 'negative': task['lower_days'] = '-1'
    elif fault == 'infinite': task['upper_days'] = 'Infinity'
    elif fault == 'reversed': task['lower_days'] = '100'
    elif fault == 'unit': task['unit'] = 'calendar_days'
    elif fault == 'unknown_number': task['origin'] = 'unknown'
    elif fault == 'false_factual': task['origin'] = 'document'
    elif fault == 'missing_basis': task['basis'] = ' '
    elif fault == 'missing_role': task['role'] = ''
    else: submitted['scope'] = ''
    before = deepcopy(submitted)
    result = assess_resources(submitted)
    assert result['status'] == 'needs_data' and result['issues']
    assert result['effort']['min'] is None and result['effort']['max'] is None
    assert result['implementation_cost']['value'] is None and result['operating_costs']['value'] is None
    assert submitted == before
    assert len(result['tasks']) == 2
    if fault != 'missing_scope':
        assert result['tasks'][0]['lower_days'] is None and result['tasks'][0]['status'] == 'invalid'


def test_repeated_same_work_and_role_are_not_counted_twice():
    from govai.economics import assess_resources
    submitted = resource_case()
    repeated = deepcopy(submitted['tasks'][0]); repeated['work'] = repeated['work'].upper()
    submitted['tasks'].append(repeated)
    result = assess_resources(submitted)
    assert result['effort']['min'] is None and result['effort']['max'] is None
    assert any('учитывать дважды' in text for text in result['issues'])
    # Two distinct tasks of the same role are legitimate work, not a duplicate.
    submitted = resource_case(); submitted['tasks'][1]['role'] = submitted['tasks'][0]['role']
    result = assess_resources(submitted)
    assert result['effort']['min'] == '4.75' and len(result['roles']) == 1


@pytest.mark.parametrize('submitted', [None, {}, {'scope': 'Проверка ограниченного прототипа', 'tasks': []}])
def test_missing_resource_estimates_are_unknown_not_zero(submitted):
    from govai.economics import assess_resources
    result = assess_resources(submitted)
    assert result['status'] == 'needs_data'
    assert result['effort']['min'] is None and result['effort']['max'] is None
    assert result['calendar_days'] is None and result['implementation_cost']['value'] is None
    assert result['operating_costs']['value'] is None and result['operating_costs']['basis']


def test_resource_task_range_is_total_for_its_role_group_not_per_person():
    from govai.economics import assess_resources
    submitted = resource_case()
    submitted['tasks'][0]['role'] = 'Два предметных эксперта и методист'
    result = assess_resources(submitted)
    assert result['effort']['min'] == '4.75' and result['effort']['max'] == '9.50'
    assert result['tasks'][0]['lower_days'] == '1.5'
    assert any('умножать его на число участников нельзя' in text for text in result['limitations'])


def prototype_parameters(**overrides):
    from govai.economics import PROTOTYPE_UNITS
    values = {'case_count': '1000', 'before_minutes': '20', 'after_minutes': '12',
              'person_days_min': '6', 'person_days_max': '11', 'hours_per_day': '8',
              'team_hour_rate': '2500', 'staff_hour_rate': '1000',
              'cash_realization': '1', 'variable_cost_per_case': '5'}
    values.update(overrides)
    return {name: {'value': value, 'origin': 'illustrative' if value is not None else 'unknown',
                   'unit': PROTOTYPE_UNITS[name],
                   'note': 'Явный условный пример одной операции, с проверкой результата сотрудником; не замер ведомства.',
                   'evidence_refs': []} for name, value in values.items()}


@pytest.mark.parametrize('before,after,days,hours,cost,net,total,threshold,whole', [
    ('20', '12', ('6', '11'), '133.33', ('120000.00', '220000.00'), '128.33', '128333.33', ('935.06', '1714.29'), ('936', '1715')),
    ('40', '30', ('8', '14'), '166.67', ('160000.00', '280000.00'), '161.67', '161666.67', ('989.69', '1731.96'), ('990', '1732')),
    ('6', '4', ('7', '12'), '33.33', ('140000.00', '240000.00'), '28.33', '28333.33', ('4941.18', '8470.59'), ('4942', '8471')),
])
def test_approved_reference_prototype_arithmetic_is_recomputed_not_copied(before, after, days, hours, cost, net, total, threshold, whole):
    from govai.economics import calculate_prototype_economics
    params = prototype_parameters(before_minutes=before, after_minutes=after,
                                  person_days_min=days[0], person_days_max=days[1])
    original = deepcopy(params)
    result = calculate_prototype_economics(params); values = result['results']
    assert params == original
    assert result['status'] == 'illustrative' and result['budget_savings'] is None
    assert result['missing_parameters'] == []
    assert values['saved_hours']['value'] == hours
    assert values['net_value_per_case']['value'] == net
    assert values['net_value_for_cases']['value'] == total
    assert tuple(values['prototype_cost_' + b]['value'] for b in ('min', 'max')) == cost
    assert tuple(values['threshold_cases_' + b]['value'] for b in ('min', 'max')) == threshold
    assert tuple(values['threshold_cases_' + b]['minimum_whole_cases'] for b in ('min', 'max')) == whole
    assert all(p['origin'] == 'illustrative' for p in result['parameters'].values())
    assert all(v['parameter_origins'] == ['illustrative'] for v in values.values())
    assert all(v['formula'] and v['unit'] and v['input_parameters'] for v in values.values())
    assert 'не доказывает фактическое сокращение расходов' in result['budget_savings_reason']


def test_prototype_unknown_inputs_never_become_defaults_or_zero():
    from govai.economics import calculate_prototype_economics, PROTOTYPE_UNITS
    empty = calculate_prototype_economics({})
    assert empty['status'] == 'needs_data'
    assert set(empty['missing_parameters']) == set(PROTOTYPE_UNITS)
    assert all(p['value'] is None and p['origin'] == 'unknown' for p in empty['parameters'].values())
    assert all(value['value'] is None for value in empty['results'].values())
    params = prototype_parameters(staff_hour_rate=None, hours_per_day=None, case_count=None)
    result = calculate_prototype_economics(params)['results']
    assert result['saved_minutes']['value'] == '8.00'
    assert result['saved_hours']['value'] is None
    assert result['net_value_per_case']['value'] is None
    assert result['prototype_cost_min']['value'] is None
    assert result['threshold_cases_min']['value'] is None
    assert result['threshold_cases_min']['minimum_whole_cases'] is None


def test_prototype_known_part_of_resource_range_does_not_invent_unknown_maximum():
    from govai.economics import calculate_prototype_economics
    result = calculate_prototype_economics(prototype_parameters(person_days_max=None))
    assert result['status'] == 'needs_data'
    assert result['missing_parameters'] == ['person_days_max']
    assert result['results']['prototype_cost_min']['value'] == '120000.00'
    assert result['results']['prototype_cost_max']['value'] is None
    assert result['results']['threshold_cases_max']['value'] is None


@pytest.mark.parametrize('change', [
    {'cash_realization': '1.01'}, {'cash_realization': '-0.1'},
    {'hours_per_day': '0'}, {'hours_per_day': '25'},
    {'person_days_min': '12', 'person_days_max': '11'},
    {'before_minutes': '-1'}, {'team_hour_rate': 'Infinity'},
    {'staff_hour_rate': 'NaN'}, {'case_count': '1e16'},
])
def test_prototype_invalid_numbers_ranges_and_cash_shares_are_rejected(change):
    from govai.economics import calculate_prototype_economics
    with pytest.raises(ProcessError, match='параметры прототипа'):
        calculate_prototype_economics(prototype_parameters(**change))


@pytest.mark.parametrize('mutation', ['unit', 'origin', 'blank_note', 'unknown_parameter', 'document_without_evidence'])
def test_prototype_units_and_numeric_origins_remain_mandatory(mutation):
    from govai.economics import calculate_prototype_economics
    values = prototype_parameters()
    if mutation == 'unit': values['person_days_min']['unit'] = 'calendar_days'
    elif mutation == 'origin': values['case_count']['origin'] = 'unknown'
    elif mutation == 'blank_note': values['after_minutes']['note'] = ' '
    elif mutation == 'unknown_parameter': values['guaranteed_savings'] = values['staff_hour_rate']
    else: values['staff_hour_rate']['origin'] = 'document'
    with pytest.raises(ProcessError): calculate_prototype_economics(values)


@pytest.mark.parametrize('changes,net', [
    ({'cash_realization': '0'}, '-5.00'),
    ({'before_minutes': '12', 'after_minutes': '20'}, '-138.33'),
    ({'before_minutes': '12', 'after_minutes': '12', 'variable_cost_per_case': '0'}, '0.00'),
])
def test_nonpositive_net_effect_preserves_losses_and_has_no_positive_threshold(changes, net):
    from govai.economics import calculate_prototype_economics
    result = calculate_prototype_economics(prototype_parameters(**changes))
    assert result['results']['net_value_per_case']['value'] == net
    for bound in ('min', 'max'):
        threshold = result['results']['threshold_cases_' + bound]
        assert threshold['value'] is None and threshold['minimum_whole_cases'] is None
        assert 'неположителен' in threshold['reason']
    assert result['budget_savings'] is None
    if changes.get('after_minutes') == '20':
        assert result['results']['saved_hours']['value'] == '-133.33'
        assert any('отрицательный эффект' in warning for warning in result['warnings'])


def test_cash_realization_changes_threshold_without_changing_released_hours():
    from govai.economics import calculate_prototype_economics
    result = calculate_prototype_economics(prototype_parameters(cash_realization='0.5'))
    assert result['results']['saved_hours']['value'] == '133.33'
    assert result['results']['capacity_equivalent_per_case']['value'] == '133.33'
    assert result['results']['net_value_per_case']['value'] == '61.67'
    assert result['results']['threshold_cases_min']['value'] == '1945.95'
    assert result['results']['threshold_cases_max']['value'] == '3567.57'
    assert result['results']['threshold_cases_min']['minimum_whole_cases'] == '1946'
    assert result['results']['threshold_cases_max']['minimum_whole_cases'] == '3568'


def test_prototype_rounding_is_not_used_as_a_calculation_operand():
    from govai.economics import calculate_prototype_economics
    result = calculate_prototype_economics(prototype_parameters())['results']
    # 128.33 * 1000 would incorrectly produce 128330, rather than the exact calculation rounded once.
    assert result['net_value_for_cases']['value'] == '128333.33'
    assert result['net_value_for_cases']['value'] != str(Decimal(result['net_value_per_case']['value']) * 1000)
    assert Decimal(result['threshold_cases_min']['exact_value']) > Decimal(result['threshold_cases_min']['value'])


def test_tiny_positive_effect_uses_unrounded_threshold_and_explicit_whole_cases():
    from govai.economics import calculate_prototype_economics
    result = calculate_prototype_economics(prototype_parameters(
        before_minutes='1.000000000001', after_minutes='1', variable_cost_per_case='0',
        staff_hour_rate='1', person_days_min='1', person_days_max='1', team_hour_rate='1', hours_per_day='1'))['results']
    assert result['net_value_per_case']['value'] == '0.00'
    assert Decimal(result['net_value_per_case']['exact_value']) > 0
    assert result['threshold_cases_min']['minimum_whole_cases'] == '60000000000000'


def test_prototype_empty_or_non_mapping_input_is_not_silently_coerced():
    from govai.economics import calculate_prototype_economics
    with pytest.raises(ProcessError): calculate_prototype_economics(None)
    with pytest.raises(ProcessError): calculate_prototype_economics([])
