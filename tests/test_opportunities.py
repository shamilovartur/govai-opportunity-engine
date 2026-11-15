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
from govai.opportunity_report import render_markdown, assess_prospects
from govai.processes import ProcessError, Candidates, extract_processes
from opportunity_helpers import map_case, simple_map, agency_map, proposal, three_proposals, OpportunityMock, supplied_scenarios, prospect_assessment
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
        assert i['prospect']['preliminary'] and 'score' not in i['prospect']
        assert set(i['source_action_ids']) <= a.actions.keys()
        for g in i['grounding']:
            for s in g['source_candidates']:
                for e in s['evidence_refs']:
                    assert result['report']['evidence'][e]==a.evidence[e]
    text=render_markdown(result)
    for heading in ['Резюме для руководителя','Сравнение AI-инициатив','Перспективные направления для первых пилотов','Ожидаемый эффект и предпосылки оценки','Приложение с источниками']:
        assert heading in text
    assert '270.00' not in text and '270 000' not in text
    assert 'требуется измерение на пилоте' in text and '| Сценарий |' not in text
    assert '**Основание:**' in text and '**Критерий:**' in text and 'Для расчёта нужны:' in text
    assert '/100' not in text and 'балл' not in text.casefold() and 'Токены' not in text
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
def test_source_faults_are_quarantined_but_global_integrity_failures_stop_report(fault):
    _,_,a,n=simple_map(('Специалист готовит проект ответа.','Специалист проверяет документ.'))
    p=proposal(n,['op-0']); rel=[]
    if fault=='action': p['action_ids']=['fake']
    elif fault=='source': n['map']['actions'][0]['source_action_ids']=['fake-source']
    elif fault=='evidence': p['evidence_refs']=['invented']
    elif fault=='foreign_evidence': p['evidence_refs']=n['map']['actions'][1]['evidence_refs']
    elif fault=='relationship': rel=[{'left':'pilot','right':'missing','relation':'complementary','reason':'wrong'}]
    rows=[p,p] if fault=='duplicate_id' else [p]
    if fault in ('source','duplicate_id'):
        with pytest.raises(ProcessError): generate_opportunities(n,a,OpportunityMock(plan(*rows,relationships=rel)))
    elif fault=='relationship':
        report=generate_opportunities(n,a,OpportunityMock(plan(*rows,relationships=rel)))['report']
        assert report['initiatives'] and any('связи' in w for w in report['warnings'])
        assert report['portfolio']['total_released_hours'] is None
    else:
        report=generate_opportunities(n,a,OpportunityMock(plan(*rows,relationships=rel)))['report']
        assert not report['initiatives'] and report['rejected'][0]['origin']=='integrity_validator'


@pytest.mark.parametrize('change,reason', [({'task_type':'deterministic'},'обычной автоматизации'),
    ({'ai_necessity':'unnecessary'},'обычной автоматизации'),
    ({'human_approval_required':False},'человеком'),
    ({'novelty':'duplicates_existing'},'Уже предусмотренная')])
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


def test_restore_recomputes_grounding_finances_and_prospects_without_calls():
    _,_,a,n=map_case(classified=True); provider=OpportunityMock(three_proposals(n))
    saved=generate_opportunities(n,a,provider,scenario_inputs={'documents':supplied_scenarios()})
    restored=restore_opportunities(json.dumps(saved).encode(),n,a,'test-model')
    assert restored==saved and len(provider.calls)==1
    for field in ['economics','prospect','evidence']:
        bad=deepcopy(saved)
        if field=='evidence': next(iter(bad['report']['evidence'].values()))['quote']='Fake citation'
        elif field=='prospect': bad['report']['initiatives'][0]['prospect']['category']='invented'
        else:
            item=next(i for i in bad['report']['initiatives'] if i['id']=='documents')
            item['economics']['scenarios'][1]['results']['released_hours']['value']='1000000'
        with pytest.raises(ProcessError,match='изменён'): restore_opportunities(json.dumps(bad).encode(),n,a,'test-model')
    with pytest.raises(ProcessError,match='несовместим'): restore_opportunities(json.dumps(saved).encode(),n,a,'other-model')
    with pytest.raises(ProcessError,match='несовместим'):
        restore_opportunities(json.dumps(saved).encode(),n,a,'test-model',settings=OpportunitySettings(max_output_tokens=19000))


def test_user_premises_have_provenance_and_normative_deadline_is_not_work_time():
    _,_,a,n=map_case(classified=True); provider=OpportunityMock(three_proposals(n))
    r=generate_opportunities(n,a,provider,scenario_inputs={'documents':supplied_scenarios()})
    supplied=next(x for x in r['report']['initiatives'] if x['id']=='documents')
    assert supplied['economics']['status']=='conditional_estimate'
    assert supplied['economics']['scenarios'][1]['budget_savings'] is None
    assert restore_opportunities(json.dumps(r).encode(),n,a,'test-model')==r
    eid=next(e for e,v in a.evidence.items() if '10 календарных дней' in v['quote'])
    bad=supplied_scenarios()
    bad[0]['parameters']['before_minutes'].update(value='10',origin='document',evidence_refs=[eid],note='Подмена срока трудозатратами')
    bad_report=generate_opportunities(n,a,OpportunityMock(three_proposals(n)),scenario_inputs={'correction-help':bad})['report']
    assert len(bad_report['initiatives']) == 2
    assert all(i['id'] != 'correction-help' for i in bad_report['initiatives'])
    # The deadline is a contextual quote, not this measured operation's proof.
    assert any(r.get('id') == 'correction-help' and 'доказательство' in r['reason'] for r in bad_report['rejected'])


def test_unknown_data_and_unknown_money_do_not_automatically_make_a_pilot_low():
    _,_,a,n=simple_map()
    assessment=prospect_assessment(pilot_feasibility='bounded',
        pilot_reason='Изолированный пилот на материалах одного типа с проверкой экспертом; доступ нужно подтвердить.',
        main_uncertainty='Готовность данных неизвестна, предварительно согласовать безопасную выборку.')
    base=generate_opportunities(n,a,OpportunityMock(plan(proposal(n,prospect_assessment=assessment))))['report']['initiatives'][0]
    assert base['economics']['status']=='needs_data'
    assert base['prospect']['category']=='high'
    assert 'неизвестна' in base['prospect']['main_uncertainty']
    assert base['prospect']['change_condition'] and base['prospect']['pilot'] and base['prospect']['success_criterion']
    assert 'priority' not in base and 'rank' not in base and 'score' not in base['prospect']


@pytest.mark.parametrize('value_case,pilot_feasibility,expected', [
    ('concrete','bounded','high'), ('concrete','needs_preparation','medium'),
    ('weak','bounded','low'), ('concrete','not_justified','low'),
    ('unclear','bounded','insufficient'), ('concrete','unknown','insufficient')])
def test_qualitative_rubric_uses_explicit_mechanism_and_pilot_reasons(value_case,pilot_feasibility,expected):
    _,_,_,normalized=simple_map()
    assessment=prospect_assessment(value_case=value_case,pilot_feasibility=pilot_feasibility)
    result=assess_prospects([proposal(normalized,prospect_assessment=assessment)],incomplete_input=True)[0]['prospect']
    assert result['category']==expected
    assert assessment['value_reason'] in result['reason'] and assessment['pilot_reason'] in result['reason']
    assert all(result[field] for field in ('main_uncertainty','change_condition','pilot','success_criterion'))
    assert not {'score','rank','weights','scores_0_to_5','weighted_contributions'} & result.keys()


def test_qualitative_groups_have_no_quota_or_artificial_places():
    _,_,_,normalized=simple_map()
    rows=[{'id':key,**proposal(normalized,prospect_assessment=prospect_assessment(pilot_feasibility='bounded'))} for key in ('z','b','a','d')]
    result=assess_prospects(rows,incomplete_input=True)
    assert [x['id'] for x in result]==['z','b','a','d']
    assert all(x['prospect']['category']=='high' for x in result)
    assert all('rank' not in x for x in result)
    for row in rows: row['prospect_assessment']['value_case']='unclear'
    assert all(x['prospect']['category']=='insufficient' for x in assess_prospects(rows))


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
        report=generate_opportunities(n,a,OpportunityMock(three_proposals(n)),scenario_inputs={'correction-help':bad})['report']
        assert len(report['initiatives'])==2
        assert report['rejected'][0]['id']=='correction-help' and report['rejected'][0]['origin']=='integrity_validator'


def test_financial_costs_and_incomplete_input_do_not_replace_the_pilot_rubric():
    _,_,a,n=agency_map()
    p=proposal(n,prospect_assessment=prospect_assessment(pilot_feasibility='bounded',
        pilot_reason='Только экспертная проверка помощи на ограниченном корпусе, без внедрения в рабочую систему.'))
    values=supplied_scenarios()
    for s in values:
        s['parameters']['annual_cases']['value']='100000'
        s['parameters']['implementation_cost']['value']='100000'
    initiative=generate_opportunities(n,a,OpportunityMock(plan(p)),scenario_inputs={'pilot':values})['report']['initiatives'][0]
    assert initiative['prospect']['category']=='high' and initiative['prospect']['preliminary']
    assert assess_prospects([initiative],incomplete_input=True)[0]['prospect']['category']=='high'
    for s in values: s['parameters']['implementation_cost']['value']='1000000000'
    costly=generate_opportunities(n,a,OpportunityMock(plan(p)),scenario_inputs={'pilot':values})['report']['initiatives'][0]
    assert costly['prospect']==initiative['prospect']
    assert float(costly['economics']['scenarios'][1]['results']['first_year_net_capacity_equivalent']['value'])<0


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
    assert item['prospect']['preliminary'] and 'score' not in item['prospect']


def test_mixed_clean_and_disputed_scope_is_not_calculated_using_only_clean_actions():
    _,_,a,n=map_case(incomplete=True)
    reviewed='review-action-b24087c749856e83'
    p=proposal(n,['receipt',reviewed])
    r=generate_opportunities(n,a,OpportunityMock(plan(p)),scenario_inputs={'pilot':supplied_scenarios()})
    item=r['report']['initiatives'][0]
    assert item['action_ids']==['receipt',reviewed]
    assert item['economics']['status']=='needs_data' and not item['economics']['scenarios']
    assert any('выбранного участка' in text for text in item['economics']['reasons'])
    assert not item['measurement']['partial_initiative']


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
    _,_,a,n=simple_map(('Специалист ведомства сопоставляет сведения и готовит проект ответа.',
                        'Система автоматически проверяет заполнение обязательного поля.'),
                      actors=('Специалист ведомства', 'Система'), participants=('agency', 'system'))
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
    _,_,a,n=map_case(classified=True); scenarios=supplied_scenarios()
    scenarios[1]['parameters']['hour_cost'].update(value=None,origin='unknown',note='Стоимость неизвестна')
    r=generate_opportunities(n,a,OpportunityMock(three_proposals(n)),scenario_inputs={'documents':scenarios})
    rows={i['id']:i for i in r['report']['initiatives']}
    base=rows['documents']['economics']['scenarios'][1]
    assert base['results']['released_hours']['value']=='270.00'
    assert base['results']['capacity_equivalent']['value'] is None
    assert not rows['grounded-reply']['economics']['scenarios']
    assert not rows['correction-help']['economics']['scenarios']
    assert rows['documents']['prospect']['preliminary'] and 'score' not in rows['documents']['prospect']
    assert r['report']['portfolio']['total_released_hours'] is None


def test_explicit_individual_illustrative_assumptions_are_preserved_without_spreading():
    from govai.economics import illustrative_scenarios
    _,_,a,n=map_case(classified=True)
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
    report=generate_opportunities(n,a,OpportunityMock(plan(p)))['report']
    assert not report['initiatives'] and report['rejected'][0]['origin']=='integrity_validator'


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
    checked=generate_opportunities(n,a,None,model='test-model',provider_name='openai',saved_plan=bad)['report']
    assert not checked['initiatives'] and 'доказательства' in checked['rejected'][0]['reason']
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


def test_management_report_translates_internal_diagnostics_and_keeps_sources_short():
    from test_normalization import simple_case
    long='Специалист сопоставляет доводы обращения с документами и готовит мотивированный проект ответа. '*8
    _,_,a=simple_case((long,))
    n=normalize_analysis(a,Normalizer(plan_for(a,[node(a,list(a.actions),key='op',title='Подготовить проект ответа')])))
    assessment=prospect_assessment(value_reason='Не объединено с a98; review требует проверки.',
        main_uncertainty='incomplete: действие action-123 связано с a98.')
    row=proposal(n,prospect_assessment=assessment,uncertainties=['Не объединено с a98; review.'])
    artifact=generate_opportunities(n,a,OpportunityMock(plan(row)))
    report=render_markdown(artifact)
    main,appendix=report.split('## Приложение с источниками',1)
    for forbidden in ('a98','action-123','review','incomplete','не объединено','Токены','/100','weighted_contributions'):
        assert forbidden not in main
    assert 'Границы операции и распределение ответственности требуют уточнения' in main
    assert 'Не объединено с a98' in artifact['report']['initiatives'][0]['prospect']['reason']
    quoted=appendix.split('```text\n',1)[1].split('\n```',1)[0]
    assert len(quoted)<=300 and quoted.endswith('…')
    assert any(proof['quote'].startswith(quoted[:-1]) for proof in artifact['report']['evidence'].values())
    assert 'action-' not in appendix


def test_llm_time_assumptions_are_individual_hypotheses_not_agency_facts():
    from opportunity_helpers import labor_assumptions
    _,_,a,n=agency_map()
    row=proposal(n,economic_model='labor_time',required_parameters=[])
    row['measurement']['time_scenarios']=labor_assumptions()
    original=deepcopy(row)
    other=proposal(n,key='quality',title='Проверка полноты мотивировки',task_type='semantic_review',economic_model='qualitative')
    saved=generate_opportunities(n,a,OpportunityMock(plan(row,other)))
    items={i['id']:i for i in saved['report']['initiatives']}
    e=items['pilot']['economics']
    assert e['status']=='illustrative' and e['scenarios'][1]['results']['saved_minutes']['value']=='8.00'
    assert e['scenarios'][1]['results']['released_hours']['value'] is None
    assert set(items['pilot']['required_parameters'])=={'before_minutes','after_minutes'}
    assert all(v['value'] is None for v in e['input_parameters'].values())
    assert all(p['origin']=='illustrative' for s in e['scenarios'] for p in s['parameters'].values() if p['value'] is not None)
    assert items['quality']['economics']['scenarios']==[]
    assert saved['report']['portfolio']['conflicts'] and saved['report']['portfolio']['total_released_hours'] is None
    assert row==original
    assert restore_opportunities(json.dumps(saved).encode(),n,a,'test-model')==saved


def test_explicit_sample_hours_need_neither_salary_nor_annual_workload():
    from opportunity_helpers import labor_assumptions
    _,_,a,n=agency_map()
    p=proposal(n,economic_model='labor_time',required_parameters=[])
    p['measurement']['primary']['unit']='hours'
    inputs=[]
    for case in labor_assumptions():
        inputs.append({'name':case['name'],'parameters':{
            'before_minutes':case['before_minutes'],'after_minutes':case['after_minutes'],
            'case_count':{'value':'1000','origin':'illustrative','unit':'cases',
                          'note':'Условная выборка подходящих случаев для учебного расчёта, не годовой объём ведомства.','evidence_refs':[]}}})
    saved=generate_opportunities(n,a,OpportunityMock(plan(p)),scenario_inputs={'pilot':inputs})
    item=saved['report']['initiatives'][0]
    base=item['economics']['scenarios'][1]
    assert base['results']['released_hours']['value']=='133.33'
    assert set(item['required_parameters'])=={'before_minutes','after_minutes','case_count'}
    assert 'hour_cost' not in base['parameters'] and 'annual_cases' not in base['parameters']
    assert 'applicability' not in base['parameters'] and base['budget_savings'] is None
    assert '133,33 человеко-часа' in render_markdown(saved)
    assert 'Сценарное допущение' in render_markdown(saved)


def test_quality_without_observations_keeps_metric_method_and_pilot_criterion():
    _,_,a,n=simple_map(('Специалист рассматривает доводы жалобы и составляет мотивированный ответ.',))
    p=proposal(n,economic_model='qualitative')
    p['measurement']['primary']['name']='Доля существенных доводов, рассмотренных с корректным подтверждением'
    p['measurement']['method']='Слепая экспертная сверка с заранее размеченными доводами завершённых дел одной процедуры.'
    p['measurement']['success_criterion']='На контрольной выборке не пропущены существенные доводы и нет неподтверждённых выводов.'
    result=generate_opportunities(n,a,OpportunityMock(plan(p)))
    row=result['report']['initiatives'][0]
    assert row['economics']['status']=='needs_data' and row['economics']['scenarios']==[]
    assert row['economics']['input_parameters']=={} and row['required_parameters']==[]
    text=render_markdown(result)
    assert 'Доля существенных доводов' in text and 'экспертная сверка' in text
    assert 'не пропущены существенные доводы' in text and '%' not in text


@pytest.mark.parametrize('fault',['unknown_source','foreign_source','repeated_source','wrong_unit','claimed_user_measurement','claimed_document_measurement'])
def test_measurement_scope_and_model_numeric_provenance_do_not_bypass_validation(fault):
    from opportunity_helpers import labor_assumptions
    _,_,a,n=simple_map(('Специалист сопоставляет документы.', 'Специалист готовит мотивированный ответ.'))
    p=proposal(n,['op-0'],economic_model='labor_time'); m=p['measurement']
    m['time_scenarios']=labor_assumptions()
    if fault=='unknown_source': m['source_action_ids']=['invented-source']
    elif fault=='foreign_source': m['source_action_ids']=n['map']['actions'][1]['source_action_ids']
    elif fault=='repeated_source': m['source_action_ids']*=2
    elif fault=='wrong_unit': m['primary']['unit']='RUB/year'
    else:
        value=m['time_scenarios'][0]['before_minutes']
        value.update(origin='document' if fault=='claimed_document_measurement' else 'user',
                     evidence_refs=p['evidence_refs'] if fault=='claimed_document_measurement' else [])
    report=generate_opportunities(n,a,OpportunityMock(plan(p)))['report']
    if fault in ('unknown_source','foreign_source','repeated_source'):
        assert not report['initiatives'] and report['rejected'][0]['origin']=='integrity_validator'
    else:
        assert report['initiatives'] and not report['rejected']
        assert report['initiatives'][0]['economics']['status']=='needs_data'
        assert not report['initiatives'][0]['economics']['scenarios']


def test_explicit_part_of_disputed_initiative_keeps_measurement_but_unknown_actor_blocks_hours():
    from opportunity_helpers import labor_assumptions
    _,_,a,_=simple_map(('Специалист сопоставляет документы.', 'Специалист составляет проект ответа.'))
    n=normalize_analysis(a,Normalizer(plan_for(a,[])))
    aids=[x['id'] for x in n['map']['actions']]
    p=proposal(n,aids,economic_model='labor_time')
    measured=n['map']['actions'][0]['source_action_ids']
    p['measurement'].update(source_action_ids=measured,work='Только сопоставление документов.',
        boundary='От начала чтения до таблицы расхождений; составление ответа не измеряется.',time_scenarios=labor_assumptions())
    r=generate_opportunities(n,a,OpportunityMock(plan(p)))
    row=r['report']['initiatives'][0]
    assert r['report']['normalization_status']=='incomplete' and r['report']['status']=='preliminary'
    assert row['measurement']['partial_initiative'] and row['measurement']['source_action_ids']==measured
    assert row['measurement']['source_limitations'] and row['uncertainties']
    assert row['economics']['status']=='needs_data' and row['economics']['scenarios']==[]
    assert any('участника' in text for text in row['economics']['reasons'])
    assert row['confidence']['requires_review'] and r['report']['portfolio']['total_budget_savings'] is None
    assert 'составление ответа не измеряется' in render_markdown(r)
    money=proposal(n,aids)
    monetary=generate_opportunities(n,a,OpportunityMock(plan(money)),scenario_inputs={'pilot':supplied_scenarios()})
    assert monetary['report']['initiatives'][0]['economics']['scenarios']==[]


@pytest.mark.parametrize('fault',['human_check_missing','duplicate_cases','partial_scenarios'])
def test_unjustified_time_forecast_keeps_hypothesis_and_measurement_plan(fault):
    from opportunity_helpers import labor_assumptions
    _,_,a,n=simple_map(); p=proposal(n,economic_model='labor_time')
    p['measurement']['time_scenarios']=labor_assumptions()
    if fault=='human_check_missing': p['measurement']['human_review_included']=False
    elif fault=='duplicate_cases':
        for s in p['measurement']['time_scenarios']: s['after_minutes']['value']='12'
    else: p['measurement']['time_scenarios'].pop()
    r=generate_opportunities(n,a,OpportunityMock(plan(p)))
    row=r['report']['initiatives'][0]
    assert not r['report']['rejected'] and row['economics']['status']=='needs_data'
    assert row['economics']['scenarios']==[] and row['economics']['reasons']
    assert row['measurement']['method'] and row['measurement']['success_criterion']


def test_negative_labor_scenario_is_not_hidden_or_converted_to_budget_savings():
    from opportunity_helpers import labor_assumptions
    _,_,a,n=agency_map(); p=proposal(n,economic_model='labor_time')
    p['measurement']['time_scenarios']=labor_assumptions()
    for s in p['measurement']['time_scenarios']: s['before_minutes']['value']='8'
    r=generate_opportunities(n,a,OpportunityMock(plan(p)))
    base=r['report']['initiatives'][0]['economics']['scenarios'][1]
    assert base['results']['saved_minutes']['value']=='-4.00' and base['budget_savings'] is None
    assert base['warnings'] and '-4,00' in render_markdown(r)


def test_reference_kind_is_explicitly_rejected_instead_of_migrated():
    # A minimal synthetic format marker, not the uploaded reference or a fabricated restored artifact.
    _,_,a,n=simple_map()
    marker={'kind':'govai_reconstructed_reference_not_application_artifact','recovery_status':'partial'}
    with pytest.raises(ProcessError,match='справочная реконструкция'):
        restore_opportunities(json.dumps(marker).encode(),n,a,'test-model')


def test_money_on_already_eligible_base_keeps_hypothesis_without_repeated_applicability():
    _,_,a,n=simple_map(); p=proposal(n)
    p['measurement']['population']='eligible_cases'
    r=generate_opportunities(n,a,OpportunityMock(plan(p)),scenario_inputs={'pilot':supplied_scenarios()})
    row=r['report']['initiatives'][0]
    assert not r['report']['rejected'] and row['economics']['status']=='needs_data'
    assert row['economics']['scenarios']==[]
    assert any('подходящие случаи нельзя учитывать повторно' in x for x in row['economics']['reasons'])


@pytest.mark.parametrize('fault', ['two_scenarios','duplicate_scenarios','unit','blank_note','nan','foreign_evidence'])
def test_nine_proposals_survive_local_optional_fault_without_loosening_sources(fault):
    from opportunity_helpers import labor_assumptions
    _,_,a,n=simple_map(tuple(f'Специалист сопоставляет материалы дела {i} и готовит проект.' for i in range(9)))
    rows=[proposal(n,[f'op-{i}'],key=f'pilot-{i}',title=f'Проверка материалов вида {i}',economic_model='labor_time') for i in range(9)]
    rows[0]['measurement']['time_scenarios']=labor_assumptions()
    cases=rows[0]['measurement']['time_scenarios']
    if fault=='two_scenarios': cases.pop()
    elif fault=='duplicate_scenarios':
        for s in cases: s['after_minutes']['value']='12'
    elif fault=='unit': cases[0]['after_minutes']['unit']='RUB/year'
    elif fault=='blank_note': cases[0]['after_minutes']['note']=''
    elif fault=='nan': cases[0]['after_minutes']['value']='NaN'
    else: cases[-1]['after_minutes']['evidence_refs']=rows[1]['evidence_refs']
    provider=OpportunityMock(plan(*rows)); pending=[]
    saved=generate_opportunities(n,a,provider,on_plan=pending.append)
    assert len(provider.calls)==1 and pending and len(saved['plan']['proposals'])==9
    report=saved['report']
    assert len(report['initiatives'])==(8 if fault=='foreign_evidence' else 9)
    assert len(report['rejected'])==(1 if fault=='foreign_evidence' else 0)
    if fault=='foreign_evidence':
        assert report['rejected'][0]['id']=='pilot-0' and report['rejected'][0]['origin']=='integrity_validator'
    else:
        first=next(i for i in report['initiatives'] if i['id']=='pilot-0')
        assert first['economics']['status']=='needs_data' and not first['economics']['scenarios']
    assert restore_opportunities(json.dumps(saved).encode(),n,a,'test-model')==saved


def test_plan_backup_is_not_report_revalidates_and_rebuilds_without_api(monkeypatch):
    import govai.opportunities as module
    _,_,a,n=simple_map(); provider=OpportunityMock(plan(proposal(n))); pending=[]
    assemble=module.assemble_report
    monkeypatch.setattr(module,'assemble_report',lambda *args,**kw: (_ for _ in ()).throw(RuntimeError('local failure')))
    with pytest.raises(OpportunityError): generate_opportunities(n,a,provider,on_plan=pending.append)
    data=module.export_opportunity_plan(pending[0],n,a,'test-model')
    assert json.loads(data)['kind']=='govai_opportunity_plan' and 'report' not in json.loads(data)
    checked=module.restore_opportunity_plan(data,n,a,'test-model')
    with pytest.raises(ProcessError): restore_opportunities(data,n,a,'test-model')
    bad=json.loads(data); bad['plan']['proposals'][0]['evidence_refs']=['fake']
    with pytest.raises(ProcessError): module.restore_opportunity_plan(json.dumps(bad).encode(),n,a,'test-model')
    with pytest.raises(ProcessError): module.restore_opportunity_plan(data,n,a,'another-model')
    monkeypatch.setattr(module,'assemble_report',assemble)
    result=generate_opportunities(n,a,None,model='test-model',provider_name='openai',saved_plan=checked)
    assert result['report']['initiatives'] and len(provider.calls)==1


@pytest.mark.parametrize('fault', ['same_notes','same_rationale','no_work','no_human_work','zero_review_time'])
def test_time_numbers_require_work_explanations_not_just_nonempty_notes(fault):
    from opportunity_helpers import agency_map, labor_assumptions
    _,_,a,n=agency_map(); p=proposal(n,economic_model='labor_time')
    p['measurement']['time_scenarios']=labor_assumptions()
    cases=p['measurement']['time_scenarios']
    if fault=='same_notes':
        for s in cases: s['before_minutes']['note']=s['after_minutes']['note']='Иллюстративная оценка для проверки на пилоте.'
    elif fault=='same_rationale':
        for s in cases: s['rationale']='Иллюстративная оценка для проверки на пилоте.'
    elif fault=='no_work': p['measurement']['work']='Работа'
    elif fault=='no_human_work': p['human_role']='Неизвестно'
    else: cases[0]['after_minutes']['value']='0'
    result=generate_opportunities(n,a,OpportunityMock(plan(p)))
    row=result['report']['initiatives'][0]
    assert row['economics']['status']=='needs_data' and not row['economics']['scenarios']
    assert row['economics']['reasons'] and row['measurement']['method']
    assert not result['report']['rejected']


@pytest.mark.parametrize('fault', ['mechanism','alternative','human','risk','pilot','measurement','quality_protocol'])
def test_inconsistent_high_prospect_preserves_hypothesis_but_not_high_category(fault):
    _,_,a,n=simple_map()
    p=proposal(n,prospect_assessment=prospect_assessment(pilot_feasibility='bounded'))
    if fault=='mechanism': p['why_ai']=' '
    elif fault=='alternative': p['non_ai_alternative']=p['why_ai']
    elif fault=='human': p['human_role']=' '
    elif fault=='risk': p['risks']=[' ']
    elif fault=='pilot': p['prospect_assessment']['pilot']=' '
    elif fault=='measurement': p['measurement']['method']=' '
    else:
        from opportunity_helpers import measurement
        p['measurement']=measurement(n,p['action_ids'],'qualitative',quality_protocol=None)
    result=generate_opportunities(n,a,OpportunityMock(plan(p)))
    assert len(result['report']['initiatives'])==1 and not result['report']['rejected']
    accepted=result['report']['initiatives'][0]
    assert accepted['prospect']['category']=='insufficient' and accepted['prospect']['assessment_issues']
    assert accepted['prospect_assessment']['pilot_feasibility']=='bounded'
    assert 'Обоснованных кандидатов для первого ограниченного пилота пока нет' in render_markdown(result)


def test_management_text_keeps_substantive_risk_around_internal_labels():
    from govai.opportunity_report import business_text
    source='Не объединено с a98: приём в МФЦ и проверка ведомством имеют разных исполнителей. review: без доступа к оригиналам нельзя проверить правомерность исправления.'
    actual=business_text(source)
    assert 'a98' not in actual and 'review' not in actual
    assert 'приём в МФЦ и проверка ведомством имеют разных исполнителей' in actual
    assert 'без доступа к оригиналам нельзя проверить правомерность исправления' in actual
    assert 'границы' in actual.casefold()


def test_summary_names_actual_first_pilot_candidates_and_explains_non_ai_alternative():
    _,_,a,n=map_case()
    rows=three_proposals(n).model_dump()['proposals']
    rows[0]['prospect_assessment']=prospect_assessment(pilot_feasibility='bounded')
    result=generate_opportunities(n,a,OpportunityMock(plan(*rows)))
    text=render_markdown(result)
    summary=text.split('## Сравнение AI-инициатив')[0]
    assert rows[0]['title'] in summary and 'повестка обсуждения' in summary
    assert rows[1]['title'] not in summary
    cases=text.split('## Перспективные направления для первых пилотов')[1].split('## Ожидаемый эффект')[0]
    assert '**Почему ИИ:**' in cases and '**Альтернатива без GenAI:**' in cases
    assert '**Контроль:**' in cases and '**Критерий:**' in cases
    assert 'не только фиксированный шаблон' in cases or 'фиксированная схема полей' in cases


def test_source_appendix_prefers_explicit_measured_scope_and_keeps_alternative_outcomes():
    _,_,a,n=map_case()
    p=proposal(n,['correction','answer'],task_type='semantic_review')
    p['measurement']['source_action_ids']=n['map']['actions'][1]['source_action_ids']
    saved=generate_opportunities(n,a,OpportunityMock(plan(p)))
    appendix=render_markdown(saved).split('## Приложение с источниками')[1]
    assert 'исправление допущенных' in appendix and 'об отсутствии опечаток' in appendix
    assert 'action-' not in appendix and 'evidence-' not in appendix


def test_hours_metric_without_actual_sample_keeps_minutes_and_marked_standard_comparison():
    from opportunity_helpers import agency_map, labor_assumptions
    _,_,a,n=agency_map(); p=proposal(n,economic_model='labor_time')
    p['measurement']['primary']['unit']='hours'
    p['measurement']['time_scenarios']=labor_assumptions()
    result=generate_opportunities(n,a,OpportunityMock(plan(p)))
    row=result['report']['initiatives'][0]
    base=next(s for s in row['economics']['scenarios'] if s['name']=='base')
    assert base['results']['released_hours']['value'] is None
    assert base['results']['standardized_hours']['value']=='133.33'
    from govai.opportunity_report import economic_summary
    summary=economic_summary(row)
    assert '8,00 человеко-минут на подходящий случай' in summary
    assert '133,33 человеко-часа на условные 1 000 подходящих случаев' in summary
    assert 'не годовая нагрузка' in summary and row['economics']['status']=='needs_data'


def test_docx_and_bundle_use_same_saved_report_with_cyrillic_and_complete_json():
    from io import BytesIO
    from zipfile import ZipFile
    from docx import Document
    from docx.oxml.ns import qn
    from govai.report_export import render_docx, render_bundle
    _,_,a,n=map_case(); provider=OpportunityMock(three_proposals(n))
    artifact=generate_opportunities(n,a,provider)
    before=deepcopy(artifact)
    markdown=render_markdown(artifact).encode('utf-8')
    data=render_docx(artifact)
    document=Document(BytesIO(data))
    text='\n'.join(p.text for p in document.paragraphs)
    assert 'Резюме для руководителя' in text and 'Приложение с источниками' in text
    assert 'Сверка материалов при исправлении ошибок' in text
    assert 'исправление допущенных' in text and 'об отсутствии опечаток' in text
    assert '\\.' not in text and '##' not in text
    assert document.tables and len(document.tables[0].columns)==4
    assert document.tables[0].rows[0]._tr.trPr.find(qn('w:tblHeader')) is not None
    assert document.sections[0].page_width.cm==pytest.approx(21,abs=.01)
    assert document.sections[0].page_height.cm==pytest.approx(29.7,abs=.01)
    assert not document.element.xpath('.//w:br[@w:type="page"]')
    assert all(not rel.is_external for rel in document.part.rels.values())
    with ZipFile(BytesIO(render_bundle(artifact,docx_data=data,markdown_data=markdown))) as archive:
        assert set(archive.namelist())=={'opportunity-report.docx','opportunity-report.md','opportunities.json','README.txt'}
        assert archive.testzip() is None
        assert archive.read('opportunity-report.docx')==data
        assert archive.read('opportunity-report.md')==markdown
        saved=archive.read('opportunities.json')
        assert json.loads(saved)==artifact
        assert restore_opportunities(saved,n,a,'test-model')==artifact
        assert 'предварительные' in archive.read('README.txt').decode('utf-8')
        assert '2023-05-30' in archive.read('README.txt').decode('utf-8')
    assert len(provider.calls)==1 and artifact==before


def test_docx_literal_source_markup_long_names_and_bundle_mismatch_are_safe():
    from io import BytesIO
    from docx import Document
    from govai.report_export import render_docx, render_bundle
    _,_,a,n=simple_map()
    saved=generate_opportunities(n,a,OpportunityMock(plan(proposal(n))))
    # Presentation robustness only; source validators remain tested separately.
    saved['report']['initiatives'][0]['title']='Очень длинное название содержательной проверки ' * 8
    for proof in saved['report']['evidence'].values():
        proof['quote']='Текст с `кавычками`, **звёздочками**, символом | и ссылкой [пример](https://invalid.example).'
    document=Document(BytesIO(render_docx(saved)))
    text='\n'.join(p.text for p in document.paragraphs)
    assert '**звёздочками**' in text and '[пример](https://invalid.example)' in text
    assert not document.element.xpath('.//w:hyperlink')
    assert 'Очень длинное название' in document.tables[0].cell(1,0).text
    with pytest.raises(ValueError,match='does not describe'):
        render_bundle(saved,json_data=b'{}')


def test_untrusted_rejection_metadata_does_not_erase_grounded_proposal():
    _,_,a,n=simple_map()
    ready=plan(proposal(n),rejected=[{'title':'Непроверенный сценарий','action_ids':['fake'],'reason':'Текст модели'}])
    saved=generate_opportunities(n,a,OpportunityMock(ready))
    assert len(saved['report']['initiatives'])==1
    rejected=saved['report']['rejected'][0]
    assert rejected['origin']=='integrity_validator' and rejected['action_ids']==[]
    assert rejected['source_action_ids']==[] and 'не подтверждены' in rejected['reason']
    assert saved['plan']['rejected'][0]['action_ids']==['fake']
    assert restore_opportunities(json.dumps(saved).encode(),n,a,'test-model')==saved


@pytest.mark.parametrize('mode,meaning',[('automation_explicit','автоматизация прямо предусмотрена НПА'),
    ('manual_explicit','ручное действие прямо предусмотрено НПА'),('mixed','смешанный способ'),('unknown','не определён')])
def test_business_translation_preserves_automation_evidence_and_substantive_risk(mode,meaning):
    from govai.opportunity_report import business_text
    value=business_text(f'Нужно разграничить проверку содержания, включая записи с mode={mode}; юридическое решение остаётся человеку.')
    assert meaning in value and 'Нужно разграничить проверку содержания' in value
    assert 'юридическое решение остаётся человеку' in value and 'mode=' not in value


@pytest.mark.parametrize('metric',['primary','secondary'])
def test_explicit_numbers_do_not_override_incompatible_measurement_units(metric):
    from opportunity_helpers import agency_map,labor_assumptions
    _,_,a,n=agency_map(); p=proposal(n,economic_model='labor_time')
    if metric=='secondary': p['measurement'][metric]={'category':'labor','name':'Дополнительное рабочее время','unit':'RUB/year'}
    else: p['measurement'][metric]['unit']='RUB/year'
    supplied=[{'name':s['name'],'parameters':{'before_minutes':s['before_minutes'],'after_minutes':s['after_minutes']}} for s in labor_assumptions()]
    row=generate_opportunities(n,a,OpportunityMock(plan(p)),scenario_inputs={'pilot':supplied})['report']['initiatives'][0]
    assert row['economics']['status']=='needs_data' and not row['economics']['scenarios']
    assert row['measurement']['validation_issues'] and row['prospect']['category']=='insufficient'


@pytest.mark.parametrize('field',['work','case_unit','boundary','method','success_criterion','human_role'])
def test_external_numbers_require_complete_measurement_scope_and_human_work(field):
    from opportunity_helpers import agency_map,labor_assumptions
    _,_,a,n=agency_map(); p=proposal(n,economic_model='labor_time')
    if field=='human_role': p[field]=' '
    else: p['measurement'][field]=' '
    supplied=[{'name':s['name'],'parameters':{'before_minutes':s['before_minutes'],'after_minutes':s['after_minutes']}} for s in labor_assumptions()]
    row=generate_opportunities(n,a,OpportunityMock(plan(p)),scenario_inputs={'pilot':supplied})['report']['initiatives'][0]
    assert row['economics']['status']=='needs_data' and not row['economics']['scenarios']
    assert row['measurement']['validation_issues']


@pytest.mark.parametrize('change',[{'ai_necessity':'uncertain'},{'novelty':'unknown'}])
def test_uncertain_added_value_preserves_hypothesis_but_not_high_prospect_or_numbers(change):
    from opportunity_helpers import agency_map,labor_assumptions
    _,_,a,n=agency_map(); p=proposal(n,economic_model='labor_time',**change)
    p['measurement']['time_scenarios']=labor_assumptions()
    p['prospect_assessment']['pilot_feasibility']='bounded'
    result=generate_opportunities(n,a,OpportunityMock(plan(p)))['report']
    assert len(result['initiatives'])==1 and not result['rejected']
    row=result['initiatives'][0]
    assert row['prospect']['category']=='insufficient' and row['prospect']['assessment_issues']
    assert row['economics']['status']=='needs_data' and not row['economics']['scenarios']
    assert row['grounding'] and row['measurement']['method']


def test_explicit_zero_after_does_not_claim_completed_human_review():
    from opportunity_helpers import agency_map,labor_assumptions
    _,_,a,n=agency_map(); p=proposal(n,economic_model='labor_time')
    supplied=[{'name':s['name'],'parameters':{'before_minutes':s['before_minutes'],'after_minutes':s['after_minutes']}} for s in labor_assumptions()]
    supplied[0]['parameters']['after_minutes']['value']='0'
    row=generate_opportunities(n,a,OpportunityMock(plan(p)),scenario_inputs={'pilot':supplied})['report']['initiatives'][0]
    assert not row['economics']['scenarios'] and any('Нулевое' in x for x in row['economics']['reasons'])


def test_concise_case_unit_is_not_confused_with_missing_measurement_definition():
    from opportunity_helpers import agency_map,labor_assumptions
    _,_,a,n=agency_map(); p=proposal(n,economic_model='labor_time')
    p['measurement'].update(case_unit='Одно обращение',time_scenarios=labor_assumptions())
    row=generate_opportunities(n,a,OpportunityMock(plan(p)))['report']['initiatives'][0]
    assert row['economics']['scenarios'][1]['results']['saved_minutes']['value']=='8.00'
    assert not row['measurement']['validation_issues']


@pytest.mark.parametrize('category,model', [
    ('money','time_capacity'), ('quality','qualitative'), ('elapsed_time','elapsed_time'),
])
def test_time_scenarios_for_other_metrics_keep_hypothesis_and_other_initiatives(category,model):
    from opportunity_helpers import labor_assumptions
    _,_,analysis,normalized=simple_map(
        ('Специалист ведомства проверяет содержание документов.',
         'Специалист ведомства готовит проект ответа.'),
        actors=('Специалист ведомства','Специалист ведомства'), participants=('agency','agency'))
    unsupported=proposal(normalized,['op-0'],key='other-metric',title='Проверка содержания',economic_model=model)
    unsupported['measurement']['time_scenarios']=labor_assumptions()
    supported=proposal(normalized,['op-1'],key='labor',title='Подготовка проекта',economic_model='labor_time')
    supported['measurement']['time_scenarios']=labor_assumptions()
    original=deepcopy(unsupported)
    provider=OpportunityMock(plan(unsupported,supported)); pending=[]
    artifact=generate_opportunities(normalized,analysis,provider,on_plan=pending.append)
    report=artifact['report']; rows={row['id']:row for row in report['initiatives']}
    assert set(rows)=={'other-metric','labor'} and not report['rejected']
    affected=rows['other-metric']
    assert affected['measurement']['primary']['category']==category
    assert affected['economics']['status']=='needs_data' and affected['economics']['scenarios']==[]
    assert any('минуты не соответствуют основной метрике' in reason for reason in affected['economics']['reasons'])
    assert affected['measurement']['method'] and affected['grounding'] and affected['evidence_refs']
    assert rows['labor']['economics']['scenarios'][1]['results']['saved_minutes']['value']=='8.00'
    assert unsupported==original and artifact['plan']['proposals'][0]['measurement']['time_scenarios']==original['measurement']['time_scenarios']
    assert restore_opportunities(json.dumps(artifact).encode(),normalized,analysis,'test-model')==artifact
    retried=generate_opportunities(normalized,analysis,None,model='test-model',provider_name='openai',saved_plan=pending[0])
    assert retried['report']==report and len(provider.calls)==1
