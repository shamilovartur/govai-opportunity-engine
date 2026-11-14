import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from govai.analysis_io import verify_analysis
from govai.economics import REQUIRED
from govai.extraction import extract_document
from govai.normalization import normalize_analysis
from govai.opportunities import (OpportunityPlan, OpportunityError, OpportunitySettings, generate_opportunities,
    restore_opportunities, prepare_request, economic_restrictions, operational_action_ids, compact_input)
from govai.opportunity_report import render_markdown, rank_initiatives
from govai.processes import ProcessError, Candidates, extract_processes
from opportunity_helpers import map_case, simple_map, proposal, three_proposals, OpportunityMock, supplied_scenarios
from test_normalization import Normalizer, node, plan_for


def plan(*rows, rejected=None, relationships=None):
    return OpportunityPlan(proposals=list(rows), rejected=rejected or [], relationships=relationships or [],
                           explanation='Контролируемый тестовый ответ, не реальная генерация.')


def test_real_261_excerpts_three_grounded_initiatives_and_no_source_mutation():
    _, raw, a, n = map_case()
    original = json.dumps([raw,n], ensure_ascii=False)
    provider=OpportunityMock(three_proposals(n))
    result=generate_opportunities(n,a,provider)
    assert len(provider.calls)==1 and len(result['report']['initiatives'])==3
    assert json.dumps([raw,n],ensure_ascii=False)==original
    assert result['stage_status']=='completed' and result['report']['status']=='preliminary'
    for i in result['report']['initiatives']:
        assert i['economics']['status']=='needs_data'
        assert i['economics']['scenarios']==[]
        assert i['priority']['scores_0_to_5']['economic']==0
        assert set(i['source_action_ids']) <= a.actions.keys()
        for g in i['grounding']:
            for s in g['source_candidates']:
                for e in s['evidence_refs']:
                    assert result['report']['evidence'][e]==a.evidence[e]
    text=render_markdown(result)
    for heading in ['Резюме для руководителя','Рейтинг инициатив','Краткие business cases','Доказательная база','Экономическая методика']:
        assert heading in text
    assert '270.00' not in text and '270 000' not in text
    assert 'Требуются дополнительные данные' in text and '| Сценарий |' not in text
    assert 'Механизм эффекта' in text and 'Количественная часть рейтинга не оценена' in text
    assert result['report']['portfolio']['total_budget_savings'] is None
    assert result['metrics']['usage']['total_tokens']==150
    assert 'source_candidates' in provider.calls[0] and 'text' not in provider.calls[0]


def test_incomplete_map_preserves_warning_and_blocks_disputed_action_as_financial_basis():
    _,_,a,n=map_case(incomplete=True)
    disputed=next(x for x in n['map']['actions'] if x['id']=='review-action-b24087c749856e83')
    rows=three_proposals(n).proposals
    bad=proposal(n,[disputed['id']],key='pending',title='Помощь при приёме документов на спорном этапе')
    r=generate_opportunities(n,a,OpportunityMock(plan(*[x.model_dump() for x in rows],bad)))['report']
    assert r['normalization_status']=='incomplete'
    assert any('неполная' in w for w in r['warnings'])
    assert len(r['initiatives'])==4 and not r['rejected']
    candidate=next(i for i in r['initiatives'] if i['id']=='pending')
    assert candidate['economics']['status']=='needs_data' and not candidate['economics']['scenarios']
    assert any('отложена' in x for x in candidate['uncertainties'])
    assert all(x['reasons'] for x in r['portfolio']['excluded_from_total'])


@pytest.mark.parametrize('fault', ['action','source','evidence','foreign_evidence','relationship','duplicate_id'])
def test_bad_ids_and_invalid_plans_fail_before_any_report(fault):
    _,_,a,n=simple_map(('Специалист готовит проект ответа.','Специалист проверяет документ.'))
    p=proposal(n,['op-0']); rel=[]
    if fault=='action': p['action_ids']=['fake']
    elif fault=='source': n['map']['actions'][0]['source_action_ids']=['fake-source']
    elif fault=='evidence': p['evidence_refs']=['invented']
    elif fault=='foreign_evidence': p['evidence_refs']=n['map']['actions'][1]['evidence_refs']
    elif fault=='relationship': rel=[{'left':'pilot','right':'missing','relation':'complementary','reason':'wrong'}]
    rows=[p,p] if fault=='duplicate_id' else [p]
    with pytest.raises(ProcessError): generate_opportunities(n,a,OpportunityMock(plan(*rows,relationships=rel)))


@pytest.mark.parametrize('change,reason', [({'task_type':'deterministic'},'обычной автоматизации'),
    ({'ai_necessity':'unnecessary'},'обычной автоматизации'),
    ({'human_approval_required':False},'человеком'),
    ({'novelty':'duplicates_existing'},'Уже предусмотренная'),
    ({'ai_necessity':'uncertain'},'не обосновано')])
def test_rejected_scenarios_are_kept_with_reasons(change,reason):
    _,_,a,n=simple_map()
    r=generate_opportunities(n,a,OpportunityMock(plan(proposal(n,**change))))['report']
    assert not r['initiatives'] and reason in r['rejected'][0]['reason']


def test_existing_automation_cannot_be_new_savings_even_with_unknown_mode():
    _,_,a,n=simple_map(('Информационная система автоматически проверяет наличие обязательных полей.',))
    first=generate_opportunities(n,a,OpportunityMock(plan(proposal(n))))['report']
    assert not first['initiatives'] and 'автоматизация' in first['rejected'][0]['reason']
    p=proposal(n,novelty='incremental',incremental_value='Смысловое выявление противоречий сверх проверки наличия полей.')
    second=generate_opportunities(n,a,OpportunityMock(plan(p)))['report']['initiatives'][0]
    assert second['economics']['status']=='needs_data' and not second['economics']['scenarios']


def test_same_opportunity_different_title_not_duplicated_and_overlap_not_summed():
    _,_,a,n=simple_map()
    p=proposal(n,key='one')
    duplicate=proposal(n,key='two',title='Другое название того же помощника')
    separate=proposal(n,key='three',title='Сверка противоречий',task_type='semantic_review')
    rel=[{'left':'one','right':'three','relation':'complementary','reason':'Модель считает дополняющими.'}]
    r=generate_opportunities(n,a,OpportunityMock(plan(p,duplicate,separate,relationships=rel)))['report']
    assert len(r['initiatives'])==2 and 'дубль' in r['rejected'][0]['reason']
    assert r['portfolio']['conflicts'] and r['portfolio']['total_capacity_equivalent'] is None


def test_parent_overview_never_becomes_second_economic_opportunity():
    _,_,a,n=map_case()
    context=next(x for x in n['map']['actions'] if x['kind']=='context')
    report=generate_opportunities(n,a,OpportunityMock(plan(proposal(n,[context['id']]))))['report']
    assert not report['initiatives'] and report['rejected']


def test_another_npa_excerpt_without_special_production_rules():
    path=Path(__file__).parent/'fixtures/law_59_article8_excerpt.txt'
    doc=extract_document(path.name,path.read_bytes())
    class Source:
        name,model='mock','test-model'
        def extract(self,payload):
            f=next(f for f in payload['core'] if 'содержащее вопросы' in f['text'])
            e={'fragment_id':f['fragment_id'],'quote':f['text']}
            return Candidates.model_validate({'procedures':[{'title':'Направление обращения','anchor':e,'actions':[{
                'description':'Определить компетентный орган и направить обращение с уведомлением гражданина.',
                'actor':None,'inputs':[],'outputs':[],'conditions':[],'dependencies':[],'evidence':[e]}]}], 'explanation':'Mock excerpt'})
    raw=extract_processes(doc,Source()); a=verify_analysis(json.dumps(raw).encode(),doc)
    n=normalize_analysis(a,Normalizer(plan_for(a,[node(a,list(a.actions),key='route')])))
    p=proposal(n,['route'],task_type='routing',title='Поддержка маршрутизации обращения')
    result=generate_opportunities(n,a,OpportunityMock(plan(p)))
    assert result['report']['initiatives'][0]['title']=='Поддержка маршрутизации обращения'
    assert next(iter(result['report']['evidence'].values()))['clause_number']=='3'


def test_no_eligible_actions_requires_no_model_and_empty_model_answer_is_preserved():
    _,_,a,n=simple_map()
    none=OpportunityMock(plan(rejected=[{'title':'Фиксированный шаблон','action_ids':['op-0'],'reason':'Достаточно обычной автоматизации'}]))
    result=generate_opportunities(n,a,none)
    assert not result['report']['initiatives'] and len(none.calls)==1
    blank=normalize_analysis(a,Normalizer(plan_for(a,[node(a,list(a.actions),kind='requirement')])))
    result=generate_opportunities(blank,a,None)
    assert result['metrics']['requests']==0 and not result['report']['initiatives']


def test_preflight_limits_stop_without_truncation_or_calls():
    _,_,a,n=map_case(); provider=OpportunityMock(three_proposals(n))
    with pytest.raises(OpportunityError,match='лимит') as failure:
        generate_opportunities(n,a,provider,settings=OpportunitySettings(max_input_bytes=100))
    assert not provider.calls and failure.value.metrics['requests']==0
    data,size=prepare_request(n,a)
    assert len(data['actions'])==len(n['map']['actions'])
    assert set(data['economic_restrictions']) <= {x['id'] for x in data['actions']}
    assert size['max_output_tokens']==20000


def test_restore_recomputes_grounding_finances_and_ranking_without_calls():
    _,_,a,n=map_case(); provider=OpportunityMock(three_proposals(n))
    saved=generate_opportunities(n,a,provider,scenario_inputs={'documents':supplied_scenarios()})
    restored=restore_opportunities(json.dumps(saved).encode(),n,a,'test-model')
    assert restored==saved and len(provider.calls)==1
    for field in ['economics','priority','evidence']:
        bad=deepcopy(saved)
        if field=='evidence': next(iter(bad['report']['evidence'].values()))['quote']='Fake citation'
        elif field=='priority': bad['report']['initiatives'][0]['priority']['score']=100
        else:
            item=next(i for i in bad['report']['initiatives'] if i['id']=='documents')
            item['economics']['scenarios'][1]['results']['released_hours']['value']='1000000'
        with pytest.raises(ProcessError,match='изменён'): restore_opportunities(json.dumps(bad).encode(),n,a,'test-model')
    with pytest.raises(ProcessError,match='несовместим'): restore_opportunities(json.dumps(saved).encode(),n,a,'other-model')
    with pytest.raises(ProcessError,match='несовместим'):
        restore_opportunities(json.dumps(saved).encode(),n,a,'test-model',settings=OpportunitySettings(illustrative_scenarios=True))


def test_user_premises_have_provenance_and_normative_deadline_is_not_work_time():
    _,_,a,n=map_case(); provider=OpportunityMock(three_proposals(n))
    r=generate_opportunities(n,a,provider,scenario_inputs={'correction-help':supplied_scenarios()})
    supplied=next(x for x in r['report']['initiatives'] if x['id']=='correction-help')
    assert supplied['economics']['status']=='conditional_estimate'
    assert supplied['economics']['scenarios'][1]['budget_savings'] is None
    assert restore_opportunities(json.dumps(r).encode(),n,a,'test-model')==r
    eid=next(e for e,v in a.evidence.items() if '10 календарных дней' in v['quote'])
    bad=supplied_scenarios()
    bad[0]['parameters']['before_minutes'].update(value='10',origin='document',evidence_refs=[eid],note='Подмена срока трудозатратами')
    with pytest.raises(ProcessError): generate_opportunities(n,a,OpportunityMock(three_proposals(n)),scenario_inputs={'correction-help':bad})


def test_unknown_priority_never_increases_score_and_illustrations_do_not_buy_rank():
    _,_,a,n=simple_map()
    base=generate_opportunities(n,a,OpportunityMock(plan(proposal(n))))['report']['initiatives'][0]
    assert base['priority']['scores_0_to_5']['economic']==base['priority']['scores_0_to_5']['reliability']==0
    unknown=deepcopy(base)
    for key in ['integration_complexity','data_readiness','delivery_effort','risk']: unknown['priority_assessment'][key]='unknown'
    unknown=rank_initiatives([unknown])[0]
    assert unknown['priority']['score']==0 and unknown['priority']['category']=='deferred'
    assert unknown['priority']['score']<=base['priority']['score']


def test_failure_preserves_map_and_safe_error_metrics():
    _,_,a,n=simple_map(); snapshot=deepcopy(n)
    class Broken(OpportunityMock):
        def opportunities(self,*args,**kwargs): raise ProcessError('Неполный ответ API.')
    with pytest.raises(OpportunityError) as caught: generate_opportunities(n,a,Broken(None))
    assert caught.value.metrics['requests']==1 and caught.value.metrics['elapsed_seconds']>=0
    assert n==snapshot


def test_export_contains_only_used_proofs_not_original_upload_or_credentials():
    _,_,a,n=map_case()
    r=generate_opportunities(n,a,OpportunityMock(three_proposals(n)))
    serialized=json.dumps(r,ensure_ascii=False)
    assert 'original_bytes' not in serialized and 'source_candidates": {' not in serialized
    assert 'api_key' not in serialized and 'Authorization' not in serialized
    assert len(r['report']['evidence']) < len(a.evidence)
    assert a.raw['source']['filename'] not in serialized


def test_legal_deadline_number_cannot_become_hour_cost_or_volume():
    _,_,a,n=map_case()
    eid=next(e for e,v in a.evidence.items() if '10 календарных дней' in v['quote'])
    for field in ['hour_cost','annual_cases']:
        bad=supplied_scenarios()
        bad[0]['parameters'][field].update(value='10',origin='document',evidence_refs=[eid],note='Неверная интерпретация числа')
        with pytest.raises(ProcessError):
            generate_opportunities(n,a,OpportunityMock(three_proposals(n)),scenario_inputs={'correction-help':bad})


def test_capital_costs_and_incomplete_input_prevent_quick_pilot_label():
    _,_,a,n=simple_map()
    p=proposal(n,priority={'integration_complexity':'low','data_readiness':'ready','delivery_effort':'small',
                          'risk':'low','priority_basis':'Условный положительный пример для проверки шкалы.'})
    values=supplied_scenarios()
    for s in values:
        s['parameters']['annual_cases']['value']='100000'
        s['parameters']['implementation_cost']['value']='100000'
    initiative=generate_opportunities(n,a,OpportunityMock(plan(p)),scenario_inputs={'pilot':values})['report']['initiatives'][0]
    assert initiative['priority']['category']=='pilot_candidate' and initiative['priority']['preliminary']
    assert initiative['priority']['scores_0_to_5']['reliability']==3
    assert rank_initiatives([initiative],incomplete_input=True)[0]['priority']['category']!='pilot_candidate'
    for s in values: s['parameters']['implementation_cost']['value']='1000000000'
    costly=generate_opportunities(n,a,OpportunityMock(plan(p)),scenario_inputs={'pilot':values})['report']['initiatives'][0]
    assert costly['priority']['category']!='pilot_candidate'


def test_context_keeps_a_source_chain_but_does_not_increase_economic_scope():
    _,_,a,n=map_case()
    context=next(x for x in n['map']['actions'] if x['kind']=='context')
    p=proposal(n,['receipt'],context_action_ids=[context['id']])
    row=generate_opportunities(n,a,OpportunityMock(plan(p)))['report']['initiatives'][0]
    assert row['context_grounding'][0]['economic_basis'] is False
    assert row['context_grounding'][0]['source_candidates']
    assert not set(context['source_action_ids']) & set(row['source_action_ids'])
    assert row['economics']['scenarios']==[]
    assert row['economics']['status']=='needs_data'


def unpack_payload(value, payload):
    if isinstance(value, dict):
        if set(value)=={'text_ref'}: return payload['texts'][value['text_ref']]
        return {k:unpack_payload(v,payload) for k,v in value.items()}
    if isinstance(value,list): return [unpack_payload(v,payload) for v in value]
    return value


def test_all_verified_records_and_conditions_remain_visible_losslessly():
    _,_,a,n=map_case(incomplete=True)
    payload,_=prepare_request(n,a); expanded=unpack_payload(payload,payload)
    assert {x['id'] for x in expanded['actions']}=={x['id'] for x in n['map']['actions']}
    assert expanded['processes']==[{k:p[k] for k in ('id','title','summary','lane')} for p in n['map']['processes']]
    for sid,source in a.actions.items():
        entry=expanded['source_candidates'][sid]
        for field in ('description','refs','actor','inputs','outputs','conditions','dependencies'):
            assert entry.get(field,[])==source[field]
    for eid,ref in payload['evidence'].items():
        start,end=ref['span']
        assert payload['quotes'][ref['quote_ref']][start:end]==a.evidence[eid]['quote']
    assert set(payload['evidence'])=={e for x in n['map']['actions'] for e in x['retained_evidence_refs']}
    assert all(x['kind'] and 'process_id' in x for x in expanded['actions'])
    assert 'source_diagnostics' not in expanded


def test_all_operations_can_be_review_only_and_still_generate_qualitative_hypothesis():
    _,_,a,_=simple_map(('Специалист сопоставляет содержание документов и готовит проект ответа.',))
    n=normalize_analysis(a,Normalizer(plan_for(a,[])))
    aid=n['map']['actions'][0]['id']; original=deepcopy(n)
    assert set(economic_restrictions(n['map']))==operational_action_ids(n['map'])=={aid}
    provider=OpportunityMock(plan(proposal(n,[aid])))
    result=generate_opportunities(n,a,provider,scenario_inputs={'pilot':supplied_scenarios()})
    item=result['report']['initiatives'][0]
    assert len(provider.calls)==1 and n==original and result['report']['normalization_status']=='incomplete'
    assert item['action_ids']==[aid] and item['grounding'][0]['source_candidates']
    assert item['economics']['status']=='needs_data' and item['economics']['scenarios']==[]
    assert any('Группировка' in text for text in item['uncertainties'])
    assert item['priority']['scores_0_to_5']['economic']==0


def test_mixed_clean_and_disputed_scope_is_not_calculated_using_only_clean_actions():
    _,_,a,n=map_case(incomplete=True)
    reviewed='review-action-b24087c749856e83'
    p=proposal(n,['receipt',reviewed])
    r=generate_opportunities(n,a,OpportunityMock(plan(p)),scenario_inputs={'pilot':supplied_scenarios()})
    item=r['report']['initiatives'][0]
    assert item['action_ids']==['receipt',reviewed]
    assert item['economics']['status']=='needs_data' and not item['economics']['scenarios']
    assert any('всей инициативы' in text for text in item['economics']['reasons'])


@pytest.mark.parametrize('kind',['overview','requirement','context','excluded'])
def test_contextual_records_visible_but_cannot_supply_independent_operational_basis(kind):
    _,_,a,_=simple_map(('Специалист анализирует материалы.', 'Услуга предоставляется в установленном порядке.'))
    ids=list(a.actions)
    nodes=[node(a,[ids[0]],key='operation'),node(a,[ids[1]],key='context',kind=kind,
           review_reason='Не самостоятельная операция.' if kind=='excluded' else None)]
    n=normalize_analysis(a,Normalizer(plan_for(a,nodes)))
    payload,_=prepare_request(n,a)
    assert any(x['id']=='context' and x['kind']==kind for x in payload['actions'])
    rows=[proposal(n,['context'],key='bad',title='Общее требование'),
          proposal(n,['operation'],key='good',context_action_ids=['context'])]
    r=generate_opportunities(n,a,OpportunityMock(plan(*rows)))['report']
    assert [i['id'] for i in r['initiatives']]==['good']
    assert 'операционного основания' in r['rejected'][0]['reason']
    assert r['initiatives'][0]['context_grounding'][0]['economic_basis'] is False


def test_real_automatic_quote_survives_review_with_nonautomatic_candidate_title():
    from test_normalization import real_case
    doc,raw,a=real_case()  # Small public excerpt fixture, not the uploaded user JSON.
    sid='action-aceadc34c5efaaab'
    for group in raw['procedures']:
        for row in group['actions']:
            if row['id']==sid: row['description']='Проверить электронное заявление и основания отказа.'
    a=verify_analysis(json.dumps(raw).encode(),doc)
    n=normalize_analysis(a,Normalizer(plan_for(a,[])))
    aid='review-'+sid; assert next(x for x in n['map']['actions'] if x['id']==aid)['mode']=='unknown'
    payload,_=prepare_request(n,a)
    assert any('автоматическ' in text for text in payload['quotes'].values())
    first=generate_opportunities(n,a,OpportunityMock(plan(proposal(n,[aid]))))['report']
    assert not first['initiatives'] and 'автоматизация' in first['rejected'][0]['reason']
    extra=proposal(n,[aid],novelty='incremental',incremental_value='Семантические противоречия между документами сверх проверки полей.')
    second=generate_opportunities(n,a,OpportunityMock(plan(extra)))['report']['initiatives'][0]
    assert second['economics']['scenarios']==[] and second['economics']['status']=='needs_data'
    assert any('автоматизац' in reason for reason in second['economics']['reasons'])


def test_unrelated_automation_context_does_not_veto_other_operation():
    _,_,a,n=simple_map(('Специалист сопоставляет сведения и готовит проект ответа.',
                        'Система автоматически проверяет заполнение обязательного поля.'))
    p=proposal(n,['op-0'],context_action_ids=['op-1'])
    r=generate_opportunities(n,a,OpportunityMock(plan(p)),scenario_inputs={'pilot':supplied_scenarios()})['report']
    assert not r['rejected'] and r['initiatives'][0]['economics']['status']=='conditional_estimate'
    assert r['initiatives'][0]['economics']['scenarios'][1]['results']['released_hours']['value']=='270.00'
    assert 'op-1' not in r['initiatives'][0]['action_ids']


def test_lane_review_preserves_operation_but_blocks_money_and_retains_review_reasons():
    _,_,a,_=simple_map()
    row=node(a,list(a.actions),key='op',review_reason='Спорен исполнитель, нужно уточнить границы.')
    draft=plan_for(a,[row]); draft.processes[0].lane='review'
    n=normalize_analysis(a,Normalizer(draft)); payload,_=prepare_request(n,a)
    assert payload['actions'][0]['id']=='op' and payload['processes'][0]['lane']=='review'
    r=generate_opportunities(n,a,OpportunityMock(plan(proposal(n,['op']))),scenario_inputs={'pilot':supplied_scenarios()})
    item=r['report']['initiatives'][0]
    assert not item['economics']['scenarios']
    assert 'Спорен исполнитель, нужно уточнить границы.' in item['uncertainties']
    assert r['report']['status']=='preliminary'


def test_old_opportunity_report_requires_only_report_regeneration():
    _,_,a,n=map_case(); r=generate_opportunities(n,a,OpportunityMock(three_proposals(n)))
    old=deepcopy(r); old['schema_version']='1.0'
    with pytest.raises(ProcessError,match='Повторите только формирование отчёта'):
        restore_opportunities(json.dumps(old).encode(),n,a,'test-model')
    assert restore_opportunities(json.dumps(r).encode(),n,a,'test-model')==r


def test_explicit_scenarios_remain_individual_and_partial_unknowns_not_zero():
    _,_,a,n=map_case(); scenarios=supplied_scenarios()
    scenarios[1]['parameters']['hour_cost'].update(value=None,origin='unknown',note='Стоимость неизвестна')
    r=generate_opportunities(n,a,OpportunityMock(three_proposals(n)),scenario_inputs={'documents':scenarios})
    rows={i['id']:i for i in r['report']['initiatives']}
    base=rows['documents']['economics']['scenarios'][1]
    assert base['results']['released_hours']['value']=='270.00'
    assert base['results']['capacity_equivalent']['value'] is None
    assert not rows['grounded-reply']['economics']['scenarios']
    assert not rows['correction-help']['economics']['scenarios']
    assert rows['documents']['priority']['scores_0_to_5']['economic']==0
    assert r['report']['portfolio']['total_released_hours'] is None


def test_explicit_individual_illustrative_assumptions_are_preserved_without_spreading():
    from govai.economics import illustrative_scenarios
    _,_,a,n=map_case()
    r=generate_opportunities(n,a,OpportunityMock(three_proposals(n)),scenario_inputs={'documents':illustrative_scenarios()})
    rows={i['id']:i for i in r['report']['initiatives']}
    assert rows['documents']['economics']['status']=='illustrative'
    assert rows['documents']['economics']['scenarios'][1]['results']['released_hours']['value']=='270.00'
    assert all(p['origin']=='illustrative' for s in rows['documents']['economics']['scenarios'] for p in s['parameters'].values())
    assert all(not rows[k]['economics']['scenarios'] for k in ('grounded-reply','correction-help'))


@pytest.mark.parametrize('fault',['unknown','foreign'])
def test_qualitative_review_hypothesis_cannot_bypass_evidence_checks(fault):
    _,_,a,_=simple_map(('Специалист анализирует содержание заявления.', 'Специалист готовит ответ.'))
    n=normalize_analysis(a,Normalizer(plan_for(a,[])))
    aids=[x['id'] for x in n['map']['actions']]
    p=proposal(n,[aids[0]],economic_model='qualitative')
    p['evidence_refs']=['invented'] if fault=='unknown' else next(x for x in n['map']['actions'] if x['id']==aids[1])['evidence_refs']
    with pytest.raises(ProcessError): generate_opportunities(n,a,OpportunityMock(plan(p)))


def test_fields_removed_by_extraction_are_not_reintroduced_as_context():
    from test_processes import document,candidate,MockProvider
    doc=document('1. Исполнитель проверяет документы и готовит проект ответа.')
    def answer(payload):
        c=candidate(payload['core'][0])
        c['actions'][0]['actor']={'text':'ВЫДУМАННЫЙ_ИСПОЛНИТЕЛЬ','evidence':[
            {'fragment_id':payload['core'][0]['fragment_id'],'quote':'НЕСУЩЕСТВУЮЩАЯ_ЦИТАТА'}]}
        return [c]
    raw=extract_processes(doc,MockProvider(answer)); a=verify_analysis(json.dumps(raw).encode(),doc)
    assert a.raw['rejected_candidates'] and not next(iter(a.actions.values()))['actor']
    n=normalize_analysis(a,Normalizer(plan_for(a,[])))
    payload,_=prepare_request(n,a); serialized=json.dumps(payload,ensure_ascii=False)
    assert 'ВЫДУМАННЫЙ_ИСПОЛНИТЕЛЬ' not in serialized and 'НЕСУЩЕСТВУЮЩАЯ_ЦИТАТА' not in serialized
    assert len(payload['source_candidates'])==1 and payload['quotes']


@pytest.mark.parametrize('parameters', [[], ['annual_cases'], ['before_minutes', 'after_minutes']])
def test_missing_service_parameter_names_are_completed_without_inventing_values(parameters):
    _,_,a,n=simple_map(); row=proposal(n,required_parameters=parameters)
    original=deepcopy(row)
    provider=OpportunityMock(plan(row)); saved=generate_opportunities(n,a,provider)
    assert row==original and len(provider.calls)==1
    assert saved['plan']['proposals'][0]['required_parameters']==list(REQUIRED)
    item=saved['report']['initiatives'][0]
    assert item['required_parameters']==item['economics']['required_parameters']==list(REQUIRED)
    assert item['economics']['status']=='needs_data' and item['economics']['scenarios']==[]
    assert all(p['value'] is None for p in item['economics']['input_parameters'].values())
    assert restore_opportunities(json.dumps(saved).encode(),n,a,'test-model')==saved


def test_received_plan_survives_assembly_error_and_rebuilds_locally(monkeypatch):
    import govai.opportunities as module
    _,_,a,n=simple_map(); provider=OpportunityMock(plan(proposal(n,required_parameters=[])))
    session={}; assemble=module.assemble_report
    def broken(*args,**kwargs): raise RuntimeError('private internal detail')
    monkeypatch.setattr(module,'assemble_report',broken)
    with pytest.raises(OpportunityError) as error:
        generate_opportunities(n,a,provider,on_plan=lambda p:session.update(plan=p))
    assert 'private' not in str(error.value) and 'report' not in session['plan']
    assert session['plan']['plan']['proposals'][0]['required_parameters']==list(REQUIRED)
    assert session['plan']['metrics']['requests']==1
    monkeypatch.setattr(module,'assemble_report',assemble)
    result=generate_opportunities(n,a,None,model='test-model',provider_name='openai',saved_plan=session['plan'])
    assert result['report']['initiatives'] and len(provider.calls)==1
    assert result['metrics']['requests']==1  # original paid attempt, not a second request
    assert result['metrics']['usage']==session['plan']['metrics']['usage']
    bad=deepcopy(session['plan']); bad['plan']['proposals'][0]['evidence_refs']=['invented']
    with pytest.raises(ProcessError,match='доказательства'):
        generate_opportunities(n,a,None,model='test-model',provider_name='openai',saved_plan=bad)
    bad_map=deepcopy(n); next(iter(bad_map['map']['evidence'].values()))['quote']='подмена'
    with pytest.raises(ProcessError):
        generate_opportunities(bad_map,a,None,model='test-model',provider_name='openai',saved_plan=session['plan'])
    _,_,other_a,other_n=simple_map(('Другой орган рассматривает другой документ.',))
    with pytest.raises(ProcessError,match='не соответствует'):
        generate_opportunities(other_n,other_a,None,model='test-model',provider_name='openai',saved_plan=session['plan'])
    with pytest.raises(ProcessError,match='не соответствует'):
        generate_opportunities(n,a,None,model='test-model',provider_name='openai',saved_plan=session['plan'],
                               settings=OpportunitySettings(max_input_bytes=390000))
    assert len(provider.calls)==1
