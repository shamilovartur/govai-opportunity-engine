from copy import deepcopy
from decimal import Decimal

import pytest

from govai.economics import (Parameter, Scenario, UNITS, calculate_scenario, assess_economics,
                             illustrative_scenarios, unknown_parameters, portfolio_assessment)
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
    e = assess_economics('time_capacity', allow_illustrative=False)
    assert e['status']=='needs_data'
    assert all(p['value'] is None for p in e['input_parameters'].values())
    assert all(v['value'] is None for s in e['scenarios'] for v in s['results'].values())


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
