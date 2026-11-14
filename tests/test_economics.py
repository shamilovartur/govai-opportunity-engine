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
