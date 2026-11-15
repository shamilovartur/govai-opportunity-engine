import json
import re
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
from govai.opportunity_report import render_markdown, render_details, management_view, assess_prospects
from govai.processes import ProcessError, Candidates, extract_processes
from opportunity_helpers import map_case, simple_map, agency_map, proposal, three_proposals, OpportunityMock, supplied_scenarios, prospect_assessment
from test_normalization import Normalizer, node, plan_for


def _read_markdown(text):
    """Compare visible wording, independent of Markdown punctuation escaping."""
    return re.sub(r'\\([\\`*_{}\[\]()#+.!<>|])', r'\1', text)


def _docx_visible_text(document):
    # Include semantic blocks placed in table cells, including nested tables.
    return '\n'.join(''.join(paragraph.xpath('.//w:t/text()'))
                     for paragraph in document.element.xpath('.//w:p'))


def plan(*rows, rejected=None, relationships=None):
    return OpportunityPlan(proposals=list(rows), rejected=rejected or [], relationships=relationships or [],
                           explanation='Контролируемый тестовый ответ, не реальная генерация.')


def presented_plan(*rows, summary=None, rejected=None):
    """Explicit compact mock wording in the retained resource-plan contract."""
    from govai.opportunities import OperationalOpportunityPlan
    proposals=[]
    for row in rows:
        row=deepcopy(row)
        row['presentation']={
            'short_title':'Помощь в проверке проекта ответа',
            'expected_effect_short':'Помочь специалисту проверить каждый вывод по документам.',
            'main_condition_short':'Доступные обезличенные дела и контроль ответственного специалиста.',
            'business_summary':'ИИ сопоставляет материалы и предлагает проект. Специалист проверяет ссылки, исправляет ошибки и утверждает ответ.',
            'ordering_reason':'Можно сравнить подготовку проверенного проекта с шаблоном на одинаковых завершённых делах.'}
        row.setdefault('resources',{'scope':'Ограниченная проверка на обезличенных завершённых делах.',
            'tasks':[],'dependencies':['Нужно подтвердить доступ к обезличенным примерам.'],
            'operating_cost_basis':'Зависят от числа обращений к модели; фактическая нагрузка неизвестна.'})
        row.setdefault('proposed_flow',[])
        proposals.append(row)
    return OperationalOpportunityPlan(proposals=proposals,rejected=rejected or [],relationships=[],
        recommendation_title='Начать с проверки проекта ответа',
        explanation='Явно заданный тестовый план; он не подтверждает качество настоящей модели.',
        executive_summary=summary or 'Начать с проверки проекта ответа. Сравнить помощь ИИ с обычным шаблоном на обезличенных завершённых делах. Специалист проверяет каждый вывод; доступность материалов и фактическое время работы пока неизвестны.')


def ranked_plan(*rows, order=None, dependencies=None, summary=None, rejected=None):
    """Hand-authored comparative plan; no claim about real model quality."""
    from govai.opportunities import RankedOpportunityPlan
    data=presented_plan(*rows,summary=summary,rejected=rejected).model_dump()
    for row in data['proposals']:
        row['input_materials']=['Обезличенные материалы обращения и применимые положения документа.']
        row['staff_result']='Проект с проверяемыми ссылками и перечнем вопросов для проверки специалистом.'
    order=order if order is not None else [row['key'] for row in data['proposals']]
    data['priority_order']=[{'key':key,'depends_on':(dependencies or {}).get(key,[]),
        'compared_with':order[index+1:index+2],
        'reason':'Раньше следующей работы можно проверить ограниченный результат на одинаковых материалах без внешней интеграции.' if index+1<len(order) else 'Завершает предварительную очередь: сначала нужны результаты проверки предыдущих работ.',
        'change_condition':'Пересмотреть очередь, если доступ к проверяемым материалам или ограничения обработки изменятся.'}
        for index,key in enumerate(order)]
    return RankedOpportunityPlan.model_validate(data)


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
    for heading in ['Общая очередь ИИ-доработок','Карта процессов','Условия и основания']:
        assert heading in text
    assert '270.00' not in text and '270 000' not in text
    assert '| Сценарий |' not in text
    assert 'Возможное сокращение повторного ввода' in text
    details=render_details(result)
    assert all(proof['quote'] in details for proof in result['report']['evidence'].values())
    assert all(sid in details for row in result['report']['initiatives'] for sid in row['source_action_ids'])
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
    assert base['rank']==1 and base['priority']['status']=='comparison_missing'
    assert 'score' not in base['prospect']


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


def test_target_and_declared_context_evidence_are_accepted_without_context_becoming_economic_scope():
    from govai.opportunities import assemble_report
    # Controlled reproduction of the described 59-ФЗ failure, not the missing live archive.
    _,_,analysis,_=simple_map(('Обращение регистрируют в течение трёх дней.',
        'Обращение направляют в компетентный государственный орган.',
        'Гражданина уведомляют о переадресации обращения.'))
    sids=list(analysis.actions)
    draft=plan_for(analysis,[node(analysis,[sids[0]],key='a-registration'),
        node(analysis,[sids[1]],key='a-forwarding'),node(analysis,[sids[2]],key='a-notification')])
    normalized=normalize_analysis(analysis,Normalizer(draft))
    nodes={row['id']:row for row in normalized['map']['actions']}
    refs=[ref for row in nodes.values() for ref in row['evidence_refs']]
    p=proposal(normalized,['a-forwarding'],key='competence_routing',
        context_action_ids=['a-registration','a-notification'],evidence_refs=refs)
    report=assemble_report(normalized,analysis,plan(p))
    assert [row['id'] for row in report['initiatives']]==['competence_routing']
    item=report['initiatives'][0]
    assert item['source_action_ids']==nodes['a-forwarding']['source_action_ids']
    assert {row['action_id'] for row in item['context_grounding']}=={'a-registration','a-notification'}
    assert all(row['economic_basis'] is False for row in item['context_grounding'])
    assert set(refs)<=set(item['evidence_refs'])


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


@pytest.mark.parametrize('fixture,previous_digest,previous_bytes',[
    ('public_excerpt','536c155c33344cd6b9f1417fee563813fd9d5a681f73e0e2485c60f29effcdfc',21563),
    ('short_repetitions','d829e4d7a9beab48bb2a7dba7dfa713e858a7bf8360296a0648b8ad3f4438fdf',4821),
])
def test_lossless_interning_retains_the_previous_expanded_payload(fixture,previous_digest,previous_bytes):
    from govai.analysis_io import digest
    # Digests of the expanded payload before break-even interning: every source,
    # actor, condition, ID, quote and span must remain identical, not only counts.
    if fixture=='public_excerpt':_,_,analysis,normalized=map_case(incomplete=True)
    else:
        _,_,analysis,normalized=simple_map(('Специалист проверяет документы.',)*6,
            actors=('Специалист ведомства',)*6,participants=('agency',)*6)
    original=deepcopy(normalized)
    payload=compact_input(normalized,analysis)
    expanded={key:unpack_payload(value,payload) for key,value in payload.items() if key!='texts'}
    assert digest(expanded)==previous_digest
    assert len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())<previous_bytes
    assert normalized==original


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
    main,appendix=report,render_details(artifact)
    for forbidden in ('a98','action-123','review','incomplete','не объединено','Токены','/100','weighted_contributions'):
        assert forbidden not in main
    assert 'часть группировки ещё не подтверждена' in _read_markdown(main)
    assert 'действие' in _read_markdown(main)
    assert 'Не объединено с a98' in artifact['report']['initiatives'][0]['prospect']['reason']
    assert all(proof['quote'] in appendix for proof in artifact['report']['evidence'].values())
    assert 'Не объединено с a98' in appendix
    assert all(sid in appendix for item in artifact['report']['initiatives'] for sid in item['source_action_ids'])
    assert '…' not in main


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
    assert '133,33' in render_markdown(saved) and 'человеко-час' in render_markdown(saved)
    assert 'Иллюстративный сценарий' in render_markdown(saved)


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
    details=_read_markdown(render_details(result))
    assert 'Доля существенных доводов' in details and 'экспертная сверка' in details
    assert 'не пропущены существенные доводы' in details and '%' not in text
    assert p['expected_effect'] in _read_markdown(text)


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
    assert 'составление ответа не измеряется' in _read_markdown(render_details(r))
    main=_read_markdown(render_markdown(r))
    assert 'исполнитель измеряемой работы не установлен' in main
    assert 'нельзя приписывать это время сотрудникам ведомства' in main and '133,33' not in main
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
    assert not management_view(result)['pilots']
    assert 'Недостаточно оснований' in render_markdown(result)


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
    summary=text.split('## Общая очередь ИИ-доработок')[0]
    assert rows[0]['title'] in summary
    assert rows[1]['title'] not in summary
    cases=text.split('## Первые доработки: работа инструмента и проверка')[1].split('## Условия и основания')[0]
    assert rows[0]['measurement']['success_criterion'] in _read_markdown(cases) and 'специалист' in cases.casefold()
    assert 'не только фиксированный шаблон' in cases or 'фиксированная схема полей' in cases
    details=render_details(result)
    assert rows[0]['non_ai_alternative'] in _read_markdown(details) and rows[0]['human_role'] in _read_markdown(details)


def test_source_appendix_prefers_explicit_measured_scope_and_keeps_alternative_outcomes():
    _,_,a,n=map_case()
    p=proposal(n,['correction','answer'],task_type='semantic_review')
    p['measurement']['source_action_ids']=n['map']['actions'][1]['source_action_ids']
    saved=generate_opportunities(n,a,OpportunityMock(plan(p)))
    appendix=render_details(saved)
    assert 'исправление допущенных' in appendix and 'об отсутствии опечаток' in appendix
    assert all(sid in appendix for sid in saved['report']['initiatives'][0]['source_action_ids'])
    assert 'action-' not in render_markdown(saved)


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


def test_docx_pdf_and_bundle_use_same_saved_report_with_complete_details_and_json():
    from io import BytesIO
    from zipfile import ZipFile
    from docx import Document
    from docx.oxml.ns import qn
    from pypdf import PdfReader
    from govai.report_export import render_docx, render_pdf, render_bundle
    _,_,a,n=map_case(); provider=OpportunityMock(three_proposals(n))
    artifact=generate_opportunities(n,a,provider)
    before=deepcopy(artifact)
    markdown=render_markdown(artifact).encode('utf-8')
    details=render_details(artifact).encode('utf-8')
    data=render_docx(artifact); pdf=render_pdf(artifact)
    document=Document(BytesIO(data))
    text=_docx_visible_text(document)
    assert management_view(artifact)['conclusion']['heading'] in text and 'Условия и основания' in text
    assert 'Приложение с источниками' not in text
    for row in management_view(artifact)['initiatives']:
        assert f"№ {row['number']}. {row['title']}" in text
        assert row['benefit'] in text and row['condition'] in text
    assert document.sections[0].page_width.pt==450
    assert document.sections[0].page_height.pt==800
    assert document.styles['Normal'].font.size.pt>=11
    assert document.styles['Normal'].font.name=='IBM Plex Sans'
    assert all(not rel.is_external for rel in document.part.rels.values())
    pages=PdfReader(BytesIO(pdf)).pages
    pdf_text=' '.join(' '.join(page.extract_text() for page in pages).split())
    assert len(pages)>=1 and management_view(artifact)['conclusion']['heading'] in pdf_text
    assert all(float(page.mediabox.width)==450 and float(page.mediabox.height)==800 for page in pages)
    assert 'Сверка материалов при исправлении ошибок' in pdf_text
    assert all(item['title'] in pdf_text for item in artifact['report']['initiatives'])
    assert 'исправление допущенных' in details.decode() and 'об отсутствии опечаток' in details.decode()
    assert all(proof['quote'] in details.decode() for proof in artifact['report']['evidence'].values())
    with ZipFile(BytesIO(render_bundle(artifact,docx_data=data,pdf_data=pdf,markdown_data=markdown,
                                      details_data=details))) as archive:
        assert set(archive.namelist())=={'opportunity-report.docx','opportunity-report.pdf','opportunity-report.md',
                                        'opportunities.json','opportunity-details.txt','README.txt'}
        assert archive.testzip() is None
        assert archive.read('opportunity-report.docx')==data
        assert archive.read('opportunity-report.pdf')==pdf
        assert archive.read('opportunity-report.md')==markdown
        assert archive.read('opportunity-details.txt')==details
        saved=archive.read('opportunities.json')
        assert json.loads(saved)==artifact
        assert restore_opportunities(saved,n,a,'test-model')==artifact
        assert 'предварительн' in archive.read('README.txt').decode('utf-8').casefold()
        assert '30.05.2023' in archive.read('README.txt').decode('utf-8')
    assert len(provider.calls)==1 and artifact==before


def test_literal_source_markup_stays_in_details_and_bundle_mismatch_is_rejected():
    from io import BytesIO
    from docx import Document
    from govai.report_export import render_docx, render_bundle
    _,_,a,n=simple_map()
    saved=generate_opportunities(n,a,OpportunityMock(plan(proposal(n))))
    # Presentation robustness only; source validators remain tested separately.
    long_title='Проверка сложных документов и мотивировки ответа с сопоставлением связанных материалов'
    saved['report']['initiatives'][0]['title']=long_title
    literal='Текст с `кавычками`, **звёздочками**, символом | и ссылкой [пример](https://invalid.example).'
    for registry in (saved['report']['evidence'],saved['report']['process_map']['evidence']):
        for proof in registry.values(): proof['quote']=literal
    document=Document(BytesIO(render_docx(saved)))
    assert literal in render_details(saved)
    assert not document.element.xpath('.//w:hyperlink')
    visible=_docx_visible_text(document)
    assert long_title in visible
    assert '…' not in visible
    with pytest.raises(ValueError,match='complete JSON|does not describe'):
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


def _legacy_opportunity_artifact(normalized, analysis, proposals):
    """A synthetic artifact in the actual pre-numbering 2.1 format."""
    from govai.opportunities import assemble_report, canonical_plan, compatibility
    from govai.analysis_io import digest
    parsed=canonical_plan(plan(*proposals))
    identity=compatibility(normalized,'test-model',schema_version='2.1')
    return {'kind':'govai_opportunities','schema_version':'2.1','report_id':digest(identity),
        'compatibility':identity,'generated_at':'2026-11-15T12:00:00+00:00','stage_status':'completed',
        'plan':parsed.model_dump(),'scenario_inputs':{},'report':assemble_report(normalized,analysis,parsed,context_evidence=False),
        'metrics':{'elapsed_seconds':10.5,'requests':1,'usage':{'input_tokens':100,'output_tokens':50,'total_tokens':150}}}


def historical_opportunity_artifact(normalized, analysis, parsed, version='2.3'):
    """Explicit synthetic old contract, never relabel a current provider response."""
    from govai.opportunities import canonical_plan, compatibility, _numbered_report
    from govai.analysis_io import digest
    parsed=canonical_plan(parsed)
    identity=compatibility(normalized,'test-model',schema_version=version)
    return {'kind':'govai_opportunities','schema_version':version,'report_id':digest(identity),
        'compatibility':identity,'generated_at':'2026-11-15T12:00:00+00:00','stage_status':'completed',
        'plan':parsed.model_dump(),'scenario_inputs':{},
        'report':_numbered_report(normalized,analysis,parsed,{},OpportunitySettings(),artifact_version=version),
        'metrics':{'elapsed_seconds':10.5,'requests':1,'usage':{'input_tokens':100,'output_tokens':50,'total_tokens':150}},
        'editorial_inputs':{},'derivation':None}


def _editorial_inputs(initiatives, order):
    return {'order':order,'ordering_basis':'Редакционная очередь ограниченных пилотов; не балльный рейтинг.',
        'initiatives':[{'id':row['id'],'short_title':row['title'],
            'expected_effect_short':'Меньше рабочих минут на проверенный результат.',
            'main_condition_short':'Подготовленные примеры и экспертная проверка.',
            'business_summary':'Измерить определённую операцию с проверкой каждого результата человеком.',
            'ordering_reason':'Ограниченный измеряемый участок позволяет провести проверку до расширения пилота.'}
            for row in initiatives]}


def _service_map_input(normalized):
    """Explicit analyst wording over verified fixture quotes; never a model result."""
    return {'version':'service-map-1','origin':'analyst_source_review',
        'scope':'Краткий обзор подтверждённых операций; не полный перечень всех действий.',
        'stages':[{'stage':'Проверить материалы и подготовить проект',
            'participant':'Специалист ведомства', 'result':'Проверенные материалы и проект ответа.',
            'evidence_refs':list(normalized['map']['evidence'])}]}


def test_editorial_service_map_restores_without_api_or_changes_to_economic_scope(monkeypatch):
    from govai.opportunities import rebuild_opportunities
    _,_,analysis,normalized=simple_map(
        ('Специалист ведомства проверяет материалы дела и готовит проект ответа.',
         'Специалист ведомства регистрирует результат рассмотрения.'),
        actors=('Специалист ведомства',)*2,participants=('agency',)*2)
    original=generate_opportunities(normalized,analysis,OpportunityMock(presented_plan(proposal(normalized,['op-0']))))
    encoded=json.dumps(original).encode(); before=deepcopy([original,normalized])
    def forbidden(*args,**kwargs):raise AssertionError('Local review must not call a provider')
    monkeypatch.setattr(OpportunityMock,'opportunities',forbidden)
    service=_service_map_input(normalized)
    derived=rebuild_opportunities(encoded,normalized,analysis,'test-model',
        editorial_inputs={'service_map':service},reason='Добавлен проверенный обзор услуги с сохранёнными цитатами; расчёты и план не изменены.')
    assert derived['metrics']['requests']==0 and derived['plan']==original['plan']
    assert derived['report']['initiatives']==original['report']['initiatives']
    assert derived['report']['evidence']==original['report']['evidence']
    assert derived['report']['presentation']['executive_summary']==original['report']['presentation']['executive_summary']
    checked=derived['report']['presentation']['service_map']
    assert checked['origin']=='analyst_source_review'
    assert checked['evidence']==normalized['map']['evidence']
    assert set(checked['evidence'])-set(derived['report']['evidence'])  # context does not enlarge a measurement
    assert restore_opportunities(json.dumps(derived).encode(),normalized,analysis,'test-model')==derived
    assert restore_opportunities(encoded,normalized,analysis,'test-model')==original
    assert 'service_map' not in original['report']['presentation'] and [original,normalized]==before
    for field in ('editorial_inputs','report'):
        damaged=deepcopy(derived)
        target=(damaged[field] if field=='editorial_inputs' else damaged[field]['presentation'])
        target['service_map']['stages'][0]['evidence_refs']=['unknown']
        with pytest.raises(ProcessError):restore_opportunities(json.dumps(damaged).encode(),normalized,analysis,'test-model')


@pytest.mark.parametrize('fault',['unknown','duplicate','unproven_quote','empty','version','origin','corrupt_evidence'])
def test_editorial_service_map_rejects_invalid_or_foreign_evidence(fault):
    from govai.opportunities import rebuild_opportunities
    original,normalized,analysis,_=_management_fixture(1)
    service=_service_map_input(normalized)
    if fault=='unknown':service['stages'][0]['evidence_refs']=['unrelated-source']
    elif fault=='duplicate':service['stages'][0]['evidence_refs']*=2
    elif fault=='unproven_quote':service['stages'][0]['quote']='Произвольное чужое утверждение.'
    elif fault=='empty':service['stages']=[]
    elif fault=='version':service['version']='service-map-99'
    elif fault=='origin':service['origin']='model'
    else:
        normalized=deepcopy(normalized)
        normalized['map']['evidence'][service['stages'][0]['evidence_refs'][0]]['quote']='Неподтверждённый текст.'
    with pytest.raises(ProcessError):
        rebuild_opportunities(json.dumps(original).encode(),normalized,analysis,'test-model',
            editorial_inputs={'service_map':service},reason='Проверка отказа при недостоверном источнике или повреждённом формате карты услуги.')


def _individual_completeness_scenarios():
    """Explicit test premises, supplied locally, never attributed to a model."""
    return [{'name':name,'parameters':{
        'before_minutes':{'value':'20','origin':'illustrative','unit':'minutes/case','evidence_refs':[],
            'note':'Иллюстративные рабочие минуты на чтение и проверку полноты одного комплекта без ИИ.'},
        'after_minutes':{'value':after,'origin':'illustrative','unit':'minutes/case','evidence_refs':[],
            'note':'Иллюстративные минуты на проверку таблицы ИИ и исправление её ошибок специалистом.'},
        'case_count':{'value':'1000','origin':'illustrative','unit':'cases','evidence_refs':[],
            'note':'Условная база сравнения: подходящие комплекты, не реальная годовая нагрузка.'}}}
        for name,after in [('conservative','16'),('base','12'),('optimistic','10')]]


def test_legacy_report_derives_numbered_individual_effect_and_restores_without_api(monkeypatch):
    from govai.opportunities import rebuild_opportunities
    from hashlib import sha256
    _,_,analysis,normalized=simple_map(
        ('Исполнитель ведомства проверяет полноту документов.', 'Исполнитель ведомства готовит проект ответа.'),
        actors=('Исполнитель ведомства','Исполнитель ведомства'),participants=('agency','agency'))
    proposals=[proposal(normalized,['op-0'],key='checking',title='Проверка документов',economic_model='labor_time'),
               proposal(normalized,['op-1'],key='drafting',title='Проект ответа',economic_model='labor_time')]
    original=_legacy_opportunity_artifact(normalized,analysis,proposals)
    original_bytes=json.dumps(original,ensure_ascii=False).encode()
    before=deepcopy(original)
    assert restore_opportunities(original_bytes,normalized,analysis,'test-model')==original
    def forbidden(*args,**kwargs): raise AssertionError('No provider calls during reassembly or restoration')
    monkeypatch.setattr(OpportunityMock,'opportunities',forbidden)
    editorial=_editorial_inputs(original['report']['initiatives'],['checking','drafting'])
    inputs={'checking':_individual_completeness_scenarios()}
    result=rebuild_opportunities(original_bytes,normalized,analysis,'test-model',scenario_inputs=inputs,
        editorial_inputs=editorial,reason='После анализа добавлены согласованные иллюстративные предпосылки только для проверки полноты.')
    assert original==before and original_bytes==json.dumps(original,ensure_ascii=False).encode()
    assert result['schema_version']=='2.5' and result['plan']==original['plan']
    assert result['derivation']['parent_artifact_sha256']==sha256(original_bytes).hexdigest()
    assert result['derivation']['parent_report_id']==original['report_id']
    assert result['derivation']['source_generation_metrics']==original['metrics']
    assert result['metrics']['requests']==0 and result['metrics']['usage'] is None
    assert result['metrics']['elapsed_seconds']>=0 and result['scenario_inputs']==inputs
    rows=result['report']['initiatives']
    assert [row['sequence_number'] for row in rows]==[1,2]
    assert [row['id'] for row in rows]==['checking','drafting']
    assert all('score' not in row['prospect'] for row in rows)
    assert [row['rank'] for row in rows]==[1,2]
    cases=rows[0]['economics']['scenarios']
    assert [case['results']['released_hours']['value'] for case in cases]==['66.67','133.33','166.67']
    assert all(value['origin']=='illustrative' for case in cases for value in case['parameters'].values())
    assert not rows[1]['economics']['scenarios']
    assert all(case['budget_savings'] is None for case in cases)
    assert result['report']['portfolio']['total_budget_savings'] is None
    for row in rows:
        prior=next(x for x in before['report']['initiatives'] if x['id']==row['id'])
        assert row['grounding']==prior['grounding'] and row['measurement']==prior['measurement']
    assert restore_opportunities(json.dumps(result).encode(),normalized,analysis,'test-model')==result


@pytest.mark.parametrize('fault', ['number','effect','source','editorial','derivation','version'])
def test_numbered_report_restore_rejects_tampering(fault):
    from govai.opportunities import rebuild_opportunities
    _,_,analysis,normalized=agency_map()
    source=_legacy_opportunity_artifact(normalized,analysis,[proposal(normalized,economic_model='labor_time')])
    derived=rebuild_opportunities(json.dumps(source).encode(),normalized,analysis,'test-model',
        scenario_inputs={'pilot':_individual_completeness_scenarios()},reason='Локальная иллюстрация на явно указанной условной выборке.')
    row=derived['report']['initiatives'][0]
    if fault=='number': row['sequence_number']=2
    elif fault=='effect': row['economics']['scenarios'][0]['results']['released_hours']['value']='999'
    elif fault=='source': derived['plan']['proposals'][0]['action_ids']=['invented']
    elif fault=='editorial': derived['editorial_inputs']={'order':['invented']}
    elif fault=='derivation': derived['derivation']['parent_artifact_sha256']='0'*64
    else: derived['schema_version']='2.1'
    with pytest.raises(ProcessError): restore_opportunities(json.dumps(derived).encode(),normalized,analysis,'test-model')


def test_local_reassembly_rejects_foreign_sources_unknown_ids_and_incompatible_document():
    from govai.opportunities import rebuild_opportunities
    _,_,analysis,normalized=simple_map(
        ('Исполнитель ведомства проверяет документы.', 'Исполнитель ведомства готовит проект ответа.'),
        actors=('Исполнитель ведомства','Исполнитель ведомства'),participants=('agency','agency'))
    source=_legacy_opportunity_artifact(normalized,analysis,[proposal(normalized,['op-0'],economic_model='labor_time')])
    data=json.dumps(source).encode(); reason='Согласованные предпосылки для ограниченной проверки на пилоте.'
    supplied=_individual_completeness_scenarios()
    supplied[0]['parameters']['before_minutes']['evidence_refs']=normalized['map']['actions'][1]['evidence_refs']
    with pytest.raises(ProcessError,match='чужое|неизвестное'):
        rebuild_opportunities(data,normalized,analysis,'test-model',scenario_inputs={'pilot':supplied},reason=reason)
    with pytest.raises(ProcessError,match='неизвестную'):
        rebuild_opportunities(data,normalized,analysis,'test-model',scenario_inputs={'invented':[]},reason=reason)
    editorial=_editorial_inputs(source['report']['initiatives'],['invented'])
    with pytest.raises(ProcessError):
        rebuild_opportunities(data,normalized,analysis,'test-model',editorial_inputs=editorial,reason=reason)
    _,_,other_analysis,other_map=simple_map(('Другое ведомство регистрирует документы по другой процедуре.',))
    with pytest.raises(ProcessError):
        rebuild_opportunities(data,other_map,other_analysis,'test-model',reason=reason)


def test_original_21_pending_plan_still_rebuilds_without_provider():
    from govai.opportunities import restore_opportunity_plan
    _,_,analysis,normalized=agency_map()
    original=_legacy_opportunity_artifact(normalized,analysis,[proposal(normalized,economic_model='labor_time')])
    pending={k:deepcopy(original[k]) for k in ('schema_version','compatibility','plan','scenario_inputs','metrics','generated_at')}
    pending['kind']='govai_opportunity_plan'
    checked=restore_opportunity_plan(json.dumps(pending).encode(),normalized,analysis,'test-model')
    assert checked==pending
    result=generate_opportunities(normalized,analysis,None,model='test-model',provider_name='openai',saved_plan=checked)
    assert result['schema_version']=='2.5' and result['plan']==original['plan']
    assert result['report']['initiatives'][0]['sequence_number']==1
    assert result['metrics']['requests']==original['metrics']['requests']


def test_numbering_preserves_categories_and_stable_ties_without_scores():
    from govai.opportunity_report import number_initiatives
    rows=[{'id':key,'title':f'Содержательный пилот {key}','prospect':{'category':category}}
          for key,category in [('medium','medium'),('first','high'),('second','high'),('unknown','insufficient'),('low','low')]]
    original={'initiatives':rows,'evidence':{'proof':{'quote':'Подтверждённая операция.'}}}
    before=deepcopy(original)
    stable=number_initiatives(original)
    assert [row['id'] for row in stable['initiatives']]==['first','second','medium','low','unknown']
    assert [row['sequence_number'] for row in stable['initiatives']]==[1,2,3,4,5]
    assert stable['presentation']['origin']=='stable' and 'исходный порядок' in stable['presentation']['ordering_basis']
    order=['second','first','medium','low','unknown']
    editorial=_editorial_inputs(rows,order)
    revised=number_initiatives(original,editorial)
    assert [row['id'] for row in revised['initiatives']]==order
    assert {row['id']:row['prospect'] for row in revised['initiatives']}=={row['id']:row['prospect'] for row in rows}
    assert revised['presentation']['origin']=='editorial' and original==before
    assert revised['evidence']==before['evidence']
    assert 'score' not in json.dumps(revised) and 'rank' not in json.dumps(revised)
    editorial['order']=['medium','second','first','low','unknown']
    with pytest.raises(ProcessError,match='категорий'): number_initiatives(original,editorial)


def test_local_scenario_wrong_unit_preserves_hypothesis_without_numeric_effect():
    from govai.opportunities import rebuild_opportunities
    _,_,analysis,normalized=agency_map()
    original=_legacy_opportunity_artifact(normalized,analysis,[proposal(normalized,economic_model='labor_time')])
    inputs=_individual_completeness_scenarios()
    inputs[0]['parameters']['before_minutes']['unit']='RUB/hour'
    result=rebuild_opportunities(json.dumps(original).encode(),normalized,analysis,'test-model',
        scenario_inputs={'pilot':inputs},reason='Явные условные параметры отдельного измеряемого участка; проверка единиц обязательна.')
    row=result['report']['initiatives'][0]
    assert row['economics']['status']=='needs_data' and not row['economics']['scenarios']
    assert row['grounding'] and not result['report']['rejected']
    assert result['metrics']['requests']==0
    assert restore_opportunities(json.dumps(result).encode(),normalized,analysis,'test-model')==result


def test_management_effect_and_assumptions_preserve_scope_and_all_distinct_values():
    from govai.opportunities import rebuild_opportunities
    _,_,analysis,normalized=simple_map(
        ('Исполнитель ведомства проверяет комплектность документов.', 'Исполнитель ведомства готовит проект запроса.'),
        actors=('Исполнитель ведомства','Исполнитель ведомства'),participants=('agency','agency'))
    candidate=proposal(normalized,['op-0','op-1'],economic_model='labor_time')
    candidate['measurement'].update(
        source_action_ids=normalized['map']['actions'][0]['source_action_ids'],
        work='Только сверка комплектности документов исполнителем ведомства.',
        boundary='Чтение комплекта, проверка выводов ИИ и исправление ошибок; подготовка и отправка запроса не входят.')
    original=_legacy_opportunity_artifact(normalized,analysis,[candidate])
    inputs={'pilot':_individual_completeness_scenarios()}
    editorial=_editorial_inputs(original['report']['initiatives'],['pilot'])
    editorial['initiatives'][0]['expected_effect_short']='Только сверка комплектности.'
    saved=rebuild_opportunities(json.dumps(original).encode(),normalized,analysis,'test-model',
        scenario_inputs=inputs,editorial_inputs=editorial,
        reason='После анализа добавлена иллюстрация для сверки комплектности, без переноса на другие операции.')
    before=deepcopy(saved)
    row=saved['report']['initiatives'][0]
    assert row['measurement']['partial_initiative']
    overview=management_view(saved)
    assert overview['initiatives'][0]['benefit']=='Только сверка комплектности.'
    effect=overview['effects'][0]
    assert row['measurement']['work'] in effect['boundary']
    assert 'подготовка и отправка запроса не входят' in effect['boundary']
    assert '133,33' in ' '.join(effect['rows'][1])
    markdown=render_markdown(saved)
    appendix=_read_markdown(render_details(saved))
    assert 'Общие предпосылки для всех сценариев' not in markdown
    assert 'Общие предпосылки для всех сценариев' in appendix
    before_note=inputs['pilot'][0]['parameters']['before_minutes']['note'].rstrip('.')
    count_note=inputs['pilot'][0]['parameters']['case_count']['note'].rstrip('.')
    after_note=inputs['pilot'][0]['parameters']['after_minutes']['note'].rstrip('.')
    assert appendix.count(before_note)==1 and appendix.count(count_note)==1
    assert appendix.count(after_note)==3
    assert '20,00 человеко-минут на случай; Сценарное допущение' in appendix
    assert '1 000,00 случаев; Сценарное допущение' in appendix
    for value in ('16,00','12,00','10,00'):
        assert value+' человеко-минут на случай; Сценарное допущение' in appendix
    assert saved==before and saved['scenario_inputs']==inputs
    assert len(row['economics']['scenarios'])==3


def _management_fixture(count, *, long_names=False, scenario=False):
    """Synthetic layout fixture; it does not demonstrate model quality on another act."""
    total=max(1,count)
    lines=tuple(f'Специалист ведомства проверяет материалы дела вида {i+1} и готовит проект ответа.' for i in range(total))
    _,_,analysis,normalized=simple_map(lines,actors=('Специалист ведомства',)*total,participants=('agency',)*total)
    rows=[]
    for index in range(count):
        title=(f'Проверка материалов дела {index+1}' if not long_names else
               f'Проверка материалов дела {index+1} и сопоставление существенных доводов с документами перед подготовкой мотивированного ответа заявителю')
        rows.append(proposal(normalized,[f'op-{index}'],key=f'case-{index+1}',title=title,
            economic_model='labor_time' if index==0 and scenario else 'qualitative',
            prospect_assessment=prospect_assessment(pilot_feasibility='bounded' if index<3 else 'needs_preparation')))
    provider=OpportunityMock(plan(*rows))
    supplied={'case-1':_individual_completeness_scenarios()} if count and scenario else None
    artifact=generate_opportunities(normalized,analysis,provider,scenario_inputs=supplied)
    assert len(artifact['report']['initiatives'])==count
    return artifact,normalized,analysis,provider


@pytest.mark.parametrize('count,long_names,scenario',[(0,False,False),(1,True,False),(5,False,False),(8,False,True),(12,False,True),(20,True,False)])
def test_management_exports_adapt_without_editorial_data_or_dropping_initiatives(count,long_names,scenario):
    from io import BytesIO
    from docx import Document
    from pypdf import PdfReader
    from govai.report_export import render_pdf,render_docx
    artifact,normalized,analysis,provider=_management_fixture(count,long_names=long_names,scenario=scenario)
    before=deepcopy(artifact)
    view=management_view(artifact)
    assert not artifact['editorial_inputs']
    assert len(view['initiatives'])==count and len(view['pilots'])<=3
    assert [row['number'] for row in view['initiatives']]==list(range(1,count+1))
    assert [row['id'] for row in view['initiatives']]==[row['id'] for row in artifact['report']['initiatives']]
    assert [row['category'] for row in view['initiatives']]==[row['prospect']['category'] for row in artifact['report']['initiatives']]
    markdown=render_markdown(artifact)
    detailed=render_details(artifact)
    pdf=PdfReader(BytesIO(render_pdf(artifact)))
    text=' '.join(' '.join(page.extract_text() or '' for page in pdf.pages).split())
    assert len(pdf.pages)>=1
    assert all(float(page.mediabox.width)==450 and float(page.mediabox.height)==800 for page in pdf.pages)
    assert all(len((page.extract_text() or '').strip())>80 for page in pdf.pages)
    assert view['conclusion']['heading'] in text and 'Условия и основания' in text
    assert '\ufffd' not in text and '…' not in markdown
    document=Document(BytesIO(render_docx(artifact)))
    assert document.styles['Normal'].font.size.pt>=11
    for row in view['initiatives']:
        assert row['title'] in markdown and row['title'] in text
        assert row['category_label'] in text
        assert row['condition'] in _read_markdown(markdown)
        assert row['id'] in detailed
    if count:
        catalogues=[table for table in document.tables if table.rows[0].cells[0].text=='Инициатива']
        for table in catalogues:
            assert all(run.font.size.pt>=10.5 for row in table.rows for cell in row.cells for paragraph in cell.paragraphs
                       for run in paragraph.runs if run.font.size is not None)
        # Tables are grouped by priority; long entries may use full-width blocks.
        body=_docx_visible_text(document)
        for item in view['initiatives']:
            assert f"№ {item['number']}. {item['title']}" in body
            assert item['category_label'] in body
            assert item['benefit'] in body and item['condition'] in body
    else:
        assert not view['pilots'] and not view['effects']
        assert 'Обоснованных вариантов применения ИИ не найдено' in markdown
        assert 'Контролируемый тестовый ответ' not in markdown  # pre-validation model explanation is not a recommendation
        assert 'Эффект и границы расчёта' not in markdown and 'Эффект и границы расчёта' not in text
    for proof in artifact['report']['evidence'].values():
        assert proof['quote'] in detailed
    if scenario:
        assert all(value in markdown and value in text for value in ('66,67','133,33','166,67'))
        assert 'Иллюстративный сценарий' in markdown
        assert 'не годовая' in markdown.casefold() or 'не годовой' in markdown.casefold()
    else:
        assert '66,67' not in markdown and '133,33' not in markdown and '166,67' not in markdown
    assert artifact==before and len(provider.calls)==1
    assert restore_opportunities(json.dumps(artifact).encode(),normalized,analysis,'test-model')==artifact


def test_saved_executive_summary_is_rendered_from_one_common_presentation():
    from io import BytesIO
    from docx import Document
    from pypdf import PdfReader
    from govai.opportunities import rebuild_opportunities
    from govai.report_export import render_pdf,render_docx
    original,n,a,_=_management_fixture(1)
    editorial=_editorial_inputs(original['report']['initiatives'],['case-1'])
    summary='Начать с проверки документов одного вида: специалист увидит пропуски и проверит вывод по первоисточнику. Сначала сравнить помощь ИИ с действующим чек-листом.'
    editorial['executive_summary']=summary
    saved=rebuild_opportunities(json.dumps(original).encode(),n,a,'test-model',editorial_inputs=editorial,
        reason='Явная редакционная проработка сохранённого результата для управленческого обсуждения, без нового запроса модели.')
    view=management_view(saved)
    assert view['requisites']=='Реквизиты не определены'
    dated=deepcopy(saved)
    dated['report']['document'].update(act_type='Приказ',number='261',
        adoption_date='2020-09-11',revision_date='2023-05-30')
    assert management_view(dated)['requisites']=='Приказ № 261 от 11.09.2020 · редакция 30.05.2023'
    sentences=[sentence.rstrip('.') for sentence in re.split(r'(?<=\.)\s+',summary)]
    assert all(sentence in json.dumps(view,ensure_ascii=False) for sentence in sentences)
    assert all(sentence in _read_markdown(render_markdown(saved)) for sentence in sentences)
    word=Document(BytesIO(render_docx(saved)))
    assert all(sentence in _docx_visible_text(word) for sentence in sentences)
    pdf=PdfReader(BytesIO(render_pdf(saved)))
    assert all(sentence in ' '.join(' '.join(page.extract_text() for page in pdf.pages).split()) for sentence in sentences)
    assert saved['plan']==original['plan'] and saved['metrics']['requests']==0
    assert restore_opportunities(json.dumps(saved).encode(),n,a,'test-model')==saved


def test_management_effect_keeps_each_scenarios_own_comparison_basis():
    from govai.opportunities import rebuild_opportunities
    original,normalized,analysis,_=_management_fixture(1,scenario=True)
    inputs=deepcopy(original['scenario_inputs'])
    for scenario,count in zip(inputs['case-1'],('500','1000','1500')):
        scenario['parameters']['case_count']['value']=count
    saved=rebuild_opportunities(json.dumps(original).encode(),normalized,analysis,'test-model',
        scenario_inputs=inputs,reason='Разные явно иллюстративные базы сравнения должны оставаться видимыми рядом с индивидуальными результатами.')
    effect=management_view(saved)['effects'][0]
    assert effect['headers'][-1]=='Эффект и база сравнения'
    for row,count in zip(effect['rows'],('500','1 000','1 500')):
        assert 'на '+count+' подходящих случаев' in row[-1]
    assert restore_opportunities(json.dumps(saved).encode(),normalized,analysis,'test-model')==saved


def test_current_plan_preserves_full_analysis_and_shared_compact_wording_without_editorial_input():
    from govai.opportunities import OperationalOpportunityPlan
    _,raw,analysis,normalized=simple_map()
    source_snapshot=deepcopy([raw,normalized])
    p=presented_plan(proposal(normalized)); before=p.model_dump()
    provider=OpportunityMock(p)
    result=generate_opportunities(normalized,analysis,provider)
    assert result['schema_version']=='2.5' and result['compatibility']['prompt_version']=='opportunities-2.3'
    assert result['editorial_inputs']=={} and result['derivation'] is None
    item=result['report']['initiatives'][0]
    assert item['presentation']['origin']=='model'
    for key,value in before['proposals'][0]['presentation'].items():assert item['presentation'][key]==value
    for key in ('problem','ai_role','human_role','why_ai','non_ai_alternative','conditions','risks','action_ids'):
        assert item[key]==before['proposals'][0][key]
    assert result['report']['presentation']['executive_summary']==p.executive_summary
    assert result['report']['initiatives'][0]['priority']['origin']=='historical_unranked'
    assert item['sequence_number']==1 and 'score' not in item['prospect']
    assert item['economics']['scenarios']==[]
    assert restore_opportunities(json.dumps(result).encode(),normalized,analysis,'test-model')==result
    assert [raw,normalized]==source_snapshot and p.model_dump()==before
    assert len(provider.calls)==1
    assert isinstance(OperationalOpportunityPlan.model_validate(result['plan']),OperationalOpportunityPlan)


def test_current_summary_cannot_recommend_an_initiative_rejected_by_source_validation():
    _,_,analysis,normalized=simple_map(('Специалист проверяет документы.','Специалист готовит проект ответа.'))
    good=proposal(normalized,['op-0'],key='valid',title='Проверка материалов')
    bad=proposal(normalized,['op-1'],key='invalid',title='Подготовка проекта',evidence_refs=['invented'])
    p=presented_plan(good,bad,summary='Начать с недостоверной рекомендации, которую валидатор должен исключить из управленческого резюме.')
    result=generate_opportunities(normalized,analysis,OpportunityMock(p))
    assert [row['id'] for row in result['report']['initiatives']]==['valid']
    assert result['report']['rejected'][0]['origin']=='integrity_validator'
    assert 'executive_summary' not in result['report']['presentation']
    assert p.executive_summary not in render_markdown(result)
    assert restore_opportunities(json.dumps(result).encode(),normalized,analysis,'test-model')==result


def test_current_summary_does_not_overrule_python_reassessment_of_pilot():
    _,_,analysis,normalized=simple_map()
    p=presented_plan(proposal(normalized,ai_necessity='uncertain'),
        summary='Начать этот пилот: предложенная моделью рекомендация ещё не прошла проверку добавочной ценности.')
    result=generate_opportunities(normalized,analysis,OpportunityMock(p))
    row=result['report']['initiatives'][0]
    assert row['prospect']['category']=='insufficient' and row['prospect']['assessment_issues']
    assert row['presentation']['short_title']==p.proposals[0].presentation.short_title
    assert 'executive_summary' not in result['report']['presentation']
    assert p.executive_summary not in render_markdown(result)
    assert restore_opportunities(json.dumps(result).encode(),normalized,analysis,'test-model')==result


def test_current_structured_plan_survives_assembly_failure_and_rebuilds_without_api(monkeypatch):
    import govai.opportunities as module
    _,_,analysis,normalized=simple_map()
    p=presented_plan(proposal(normalized)); provider=OpportunityMock(p); saved=[]
    original=module.assemble_report
    def fail(*args,**kwargs):raise ProcessError('Локальная ошибка оформления; план сохранён.')
    monkeypatch.setattr(module,'assemble_report',fail)
    with pytest.raises(OpportunityError):generate_opportunities(normalized,analysis,provider,on_plan=saved.append)
    assert len(provider.calls)==1 and len(saved)==1
    assert saved[0]['schema_version']=='2.3' and saved[0]['compatibility']['schema_version']=='2.5'
    backup=module.export_opportunity_plan(saved[0],normalized,analysis,'test-model')
    checked=module.restore_opportunity_plan(backup,normalized,analysis,'test-model')
    monkeypatch.setattr(module,'assemble_report',original)
    result=generate_opportunities(normalized,analysis,None,model='test-model',provider_name='openai',saved_plan=checked)
    assert len(provider.calls)==1 and result['schema_version']=='2.5'
    assert result['plan']['executive_summary']==p.executive_summary
    assert restore_opportunities(json.dumps(result).encode(),normalized,analysis,'test-model')==result


@pytest.mark.parametrize('fault',['version','prompt','schema','presentation','document','source_validation','plan_contract'])
def test_current_and_legacy_contracts_reject_relabelling_or_tampering(fault):
    _,_,analysis,normalized=simple_map()
    result=generate_opportunities(normalized,analysis,OpportunityMock(presented_plan(proposal(normalized))))
    bad=deepcopy(result)
    if fault=='version':bad['schema_version']='2.2'
    elif fault=='prompt':bad['compatibility']['prompt_version']='opportunities-2.1'
    elif fault=='schema':bad['compatibility']['plan_schema_sha256']='0'*64
    elif fault=='presentation':bad['report']['initiatives'][0]['presentation']['expected_effect_short']='Доказанная экономия миллионов рублей.'
    elif fault=='source_validation':bad['compatibility']['source_validation_version']='unchecked'
    elif fault=='plan_contract':bad['compatibility']['plan_contract_version']='2.1'
    else:bad['compatibility']['document_sha256']='0'*64
    with pytest.raises(ProcessError):restore_opportunities(json.dumps(bad).encode(),normalized,analysis,'test-model')


def test_current_plan_local_editorial_rebuild_preserves_model_origin_and_old_source():
    from govai.opportunities import rebuild_opportunities
    _,_,analysis,normalized=simple_map()
    result=generate_opportunities(normalized,analysis,OpportunityMock(presented_plan(proposal(normalized))))
    encoded=json.dumps(result).encode(); before=deepcopy(result)
    editorial=_editorial_inputs(result['report']['initiatives'],['pilot'])
    editorial['executive_summary']='После содержательной проверки предлагается ограниченный пилот на обезличенных делах; решения остаются специалисту.'
    derived=rebuild_opportunities(encoded,normalized,analysis,'test-model',editorial_inputs=editorial,
        reason='Явная редакционная проработка сохранённого результата; новые факты и расчёты не добавлялись.')
    assert derived['schema_version']=='2.5' and derived['plan']==result['plan']
    assert derived['report']['presentation']['origin']=='editorial'
    assert derived['report']['presentation']['executive_summary']==editorial['executive_summary']
    assert derived['derivation']['parent_schema_version']=='2.5'
    assert derived['metrics']['requests']==0 and result==before
    assert restore_opportunities(json.dumps(derived).encode(),normalized,analysis,'test-model')==derived


def test_known_legacy_plan_schema_hash_is_unchanged():
    from govai.analysis_io import digest
    from govai.opportunities import compatibility, OpportunityGenerationPlan
    _,_,analysis,normalized=simple_map()
    assert digest(OpportunityPlan.model_json_schema())=='e4c72854556d1ab31ee9aa35177b0d407f6f0cff1f1e8c2ad5d7e1438b782625'
    for version in ('2.1','2.2'):
        identity=compatibility(normalized,'test-model',schema_version=version)
        assert identity['prompt_version']=='opportunities-2.1'
        assert identity['prompt_sha256']=='59c2df4c7e033976e2b953d929a5cffc01d14ac8c04f8bcadcdd84b011c72f06'
    assert digest(OpportunityGenerationPlan.model_json_schema())=='3a811f4fef00f02bb476763596238ed8b88617a9818aa0aa035585145ba1038f'
    identity=compatibility(normalized,'test-model',schema_version='2.3')
    assert identity['prompt_version']=='opportunities-2.2'
    assert identity['prompt_sha256']=='95f154caf2937fa8a5f9856a3077de6cdc4066f115f237e491e11d5b2c66fe6a'


def test_current_zero_initiatives_remains_a_substantive_result_without_numbers():
    _,_,analysis,normalized=simple_map()
    p=presented_plan(summary='Для указанного шага достаточно фиксированного шаблона и проверки обязательных полей. Дополнительная польза ИИ не обоснована.')
    result=generate_opportunities(normalized,analysis,OpportunityMock(p))
    assert result['schema_version']=='2.5' and result['report']['initiatives']==[]
    assert result['report']['presentation']['executive_summary']==p.executive_summary
    assert restore_opportunities(json.dumps(result).encode(),normalized,analysis,'test-model')==result


def _routing_context_fixture():
    _,_,analysis,normalized=simple_map(('Специалист регистрирует обращение.',
        'Специалист направляет обращение в компетентный орган.',
        'Специалист уведомляет гражданина о переадресации.',
        'Специалист проверяет другую заявку.'))
    nodes={row['id']:row for row in normalized['map']['actions']}
    refs=[ref for key in ('op-0','op-1','op-2') for ref in nodes[key]['evidence_refs']]
    p=proposal(normalized,['op-1'],key='competence_routing',context_action_ids=['op-0','op-2'],evidence_refs=refs)
    return analysis,normalized,nodes,p


@pytest.mark.parametrize('fault',['missing_target','foreign_evidence','foreign_context','unproved_second_target','context_measurement'])
def test_declared_context_does_not_relax_target_or_financial_source_validation(fault):
    analysis,normalized,nodes,p=_routing_context_fixture()
    if fault=='missing_target':p['evidence_refs']=nodes['op-0']['evidence_refs']+nodes['op-2']['evidence_refs']
    elif fault=='foreign_evidence':p['evidence_refs']+=nodes['op-3']['evidence_refs']
    elif fault=='foreign_context':p['context_action_ids']+=['invented']
    elif fault=='unproved_second_target':p['action_ids']+=['op-3']
    else:p['measurement']['source_action_ids']=nodes['op-0']['source_action_ids']
    result=generate_opportunities(normalized,analysis,OpportunityMock(presented_plan(p)))
    assert result['report']['initiatives']==[]
    assert result['report']['rejected'][0]['origin']=='integrity_validator'
    assert 'executive_summary' not in result['report']['presentation']
    assert restore_opportunities(json.dumps(result).encode(),normalized,analysis,'test-model')==result


def test_prior_target_only_result_restores_then_reassembles_context_plan_locally_without_relabelling(monkeypatch):
    from govai.opportunities import OpportunityGenerationPlan, rebuild_opportunities, restore_opportunity_plan
    from govai.analysis_io import digest
    analysis,normalized,nodes,p=_routing_context_fixture()
    current=presented_plan(p).model_dump()
    current.pop('recommendation_title')
    for row in current['proposals']:
        row.pop('resources');row.pop('proposed_flow')
    original=historical_opportunity_artifact(normalized,analysis,OpportunityGenerationPlan.model_validate(current))
    encoded=json.dumps(original,ensure_ascii=False).encode();before=deepcopy(original)
    assert not original['report']['initiatives'] and original['report']['rejected'][0]['origin']=='integrity_validator'
    assert restore_opportunities(encoded,normalized,analysis,'test-model')==original
    def forbidden(*args,**kwargs):raise AssertionError('No paid request for local source-validation repair')
    monkeypatch.setattr(OpportunityMock,'opportunities',forbidden)
    derived=rebuild_opportunities(encoded,normalized,analysis,'test-model',
        reason='Локальная повторная проверка собственных доказательств операции и явно указанного контекста без нового запроса.')
    assert original==before and derived['schema_version']=='2.5'
    assert derived['plan']==original['plan'] and derived['derivation']['source_plan_sha256']==digest(original['plan'])
    assert derived['compatibility']['plan_contract_version']=='2.2'
    assert derived['compatibility']['prompt_sha256']==original['compatibility']['prompt_sha256']
    assert derived['compatibility']['source_validation_version']=='target-context-1'
    assert derived['metrics']['requests']==0 and derived['metrics']['usage'] is None
    assert [row['id'] for row in derived['report']['initiatives']]==['competence_routing']
    assert derived['report']['initiatives'][0]['resources']['effort']['min'] is None
    assert restore_opportunities(json.dumps(derived).encode(),normalized,analysis,'test-model')==derived
    # Importing the original remains a historical read, not an implicit migration.
    assert restore_opportunities(encoded,normalized,analysis,'test-model')==before
    pending={key:deepcopy(original[key]) for key in ('compatibility','plan','scenario_inputs','metrics','generated_at')}
    pending.update(kind='govai_opportunity_plan',schema_version='2.2')
    checked=restore_opportunity_plan(json.dumps(pending).encode(),normalized,analysis,'test-model')
    retried=generate_opportunities(normalized,analysis,None,model='test-model',provider_name='openai',saved_plan=checked)
    assert [row['id'] for row in retried['report']['initiatives']]==['competence_routing']
    assert retried['plan']==original['plan'] and retried['schema_version']=='2.5'
    assert retried['metrics']['requests']==original['metrics']['requests']  # historical request, no additional call
    assert restore_opportunities(json.dumps(retried).encode(),normalized,analysis,'test-model')==retried


def test_current_resources_and_flow_are_optional_assumptions_not_source_or_calendar_claims():
    _,_,analysis,normalized=agency_map()
    resource={'scope':'Ограниченный прототип без подключения к системе ведомства.',
        'tasks':[{'work':'Подготовить защищённую загрузку и ответы по документам.',
            'role':'Разработчик','lower_days':'3','upper_days':'5','unit':'person_days','origin':'illustrative',
            'basis':'Сценарный диапазон для одного формата входных данных без внешней интеграции.'}],
        'dependencies':['Обезличенные примеры и эксперт для проверки доступны после отдельного согласования.'],
        'operating_cost_basis':'Зависит от объёма документов и использования модели; объём и цена не заданы.'}
    p=proposal(normalized,resources=resource,proposed_flow=['Документы поступают','ИИ предлагает проект','Специалист проверяет'])
    artifact=generate_opportunities(normalized,analysis,OpportunityMock(presented_plan(p)))
    row=artifact['report']['initiatives'][0]
    assert artifact['schema_version']=='2.5' and artifact['compatibility']['plan_contract_version']=='2.3'
    assert row['resources']['status']=='illustrative'
    assert row['resources']['effort']['min']=='3.00' and row['resources']['effort']['max']=='5.00'
    assert row['resources']['calendar_days'] is None and row['resources']['implementation_cost']['value'] is None
    assert row['proposed_flow']==p['proposed_flow']
    assert artifact['plan']['proposals'][0]['resources']==resource
    assert restore_opportunities(json.dumps(artifact).encode(),normalized,analysis,'test-model')==artifact
    invalid=deepcopy(resource);invalid['tasks'][0]['lower_days']='not-a-number'
    unsafe_summary='Готовая оценка требует 999 человеко-дней; этот вывод не должен пережить отклонение исходных чисел.'
    broken=generate_opportunities(normalized,analysis,OpportunityMock(presented_plan(
        proposal(normalized,resources=invalid),summary=unsafe_summary)))
    assert len(broken['report']['initiatives'])==1
    assert broken['report']['initiatives'][0]['resources']['effort']['min'] is None
    assert broken['plan']['proposals'][0]['resources']['tasks'][0]['lower_days']=='not-a-number'
    assert 'executive_summary' not in broken['report']['presentation']
    assert unsafe_summary not in render_markdown(broken)
    assert restore_opportunities(json.dumps(broken).encode(),normalized,analysis,'test-model')==broken


def test_local_individual_resource_inputs_preserve_old_plan_and_require_provenance():
    from govai.opportunities import rebuild_opportunities
    from govai.analysis_io import digest
    _,_,analysis,normalized=simple_map(('Специалист проверяет документы.','Специалист готовит ответ.'))
    original=_legacy_opportunity_artifact(normalized,analysis,[
        proposal(normalized,['op-0'],key='checking',title='Проверка документов'),
        proposal(normalized,['op-1'],key='drafting',title='Подготовка ответа')])
    encoded=json.dumps(original,ensure_ascii=False).encode();before=deepcopy(original)
    resources={'checking':{'scope':'Условный прототип проверки комплекта без интеграции с ведомственной системой.',
        'tasks':[{'work':'Настроить сопоставление перечня требований с документами.',
            'role':'Разработчик','lower_days':'3','upper_days':'5','unit':'person_days','origin':'illustrative',
            'basis':'Инженерное предположение для одного вида документов; сложность обработки влияет на диапазон.'}],
        'dependencies':['Должны быть согласованы защищённый доступ и обезличенные примеры.'],
        'operating_cost_basis':'Зависит от числа комплектов и расходов API; значения неизвестны.'}}
    reason='После анализа добавлены явные индивидуальные инженерные предположения для обсуждения ограниченного прототипа.'
    derived=rebuild_opportunities(encoded,normalized,analysis,'test-model',resource_inputs=resources,reason=reason)
    assert original==before and derived['plan']==original['plan']
    assert derived['resource_inputs']==resources and derived['compatibility']['resource_inputs_sha256']==digest(resources)
    assert derived['compatibility']['prompt_sha256']==original['compatibility']['prompt_sha256']
    rows={row['id']:row for row in derived['report']['initiatives']}
    assert rows['checking']['resources']['input_origin']=='local_engineering'
    assert rows['checking']['resources']['effort']['min']=='3.00'
    assert rows['drafting']['resources']['input_origin']=='not_provided'
    assert rows['drafting']['resources']['effort']['min'] is None
    assert derived['metrics']['requests']==0 and derived['derivation']['parent_schema_version']=='2.1'
    assert restore_opportunities(json.dumps(derived).encode(),normalized,analysis,'test-model')==derived
    for field in ('resource_inputs','derivation'):
        bad=deepcopy(derived)
        if field=='resource_inputs':bad[field]['checking']['tasks'][0]['lower_days']='999'
        else:bad[field]=None
        with pytest.raises(ProcessError):restore_opportunities(json.dumps(bad).encode(),normalized,analysis,'test-model')
    with pytest.raises(ProcessError,match='принятые инициативы'):
        rebuild_opportunities(encoded,normalized,analysis,'test-model',
            resource_inputs={'invented':resources['checking']},reason=reason)
    with pytest.raises(ProcessError,match='словарём'):
        rebuild_opportunities(encoded,normalized,analysis,'test-model',resource_inputs=['checking'],reason=reason)
    old_with_injected_resources=deepcopy(original);old_with_injected_resources['resource_inputs']=resources
    with pytest.raises(ProcessError):restore_opportunities(json.dumps(old_with_injected_resources).encode(),normalized,analysis,'test-model')


@pytest.mark.parametrize('fault',['missing_scope','unknown_task'])
def test_model_summary_cannot_publish_incomplete_resource_total(fault):
    _,_,analysis,normalized=agency_map()
    estimate={'scope':'Ограниченный прототип обработки одного вида документов.',
        'tasks':[{'work':'Подготовить проверяемый прототип сопоставления.',
            'role':'Разработчик','lower_days':'3','upper_days':'5','unit':'person_days','origin':'illustrative',
            'basis':'Условная оценка без внешней интеграции, на подготовленном наборе документов.'}],
        'dependencies':['Доступ к разрешённым примерам нужно согласовать.'],
        'operating_cost_basis':'Фактический объём и стоимость вызовов неизвестны.'}
    if fault=='missing_scope':estimate['scope']=''
    else:
        estimate['tasks'].append({'work':'Проверить ответы на экспертных примерах.',
            'role':'Предметный специалист','lower_days':None,'upper_days':None,'unit':'person_days','origin':'unknown',
            'basis':'Объём экспертной разметки неизвестен.'})
    summary='Начать проверку документов: вся реализация потребует 3–5 человеко-дней. Это утверждение ещё не прошло проверку полноты расчёта.'
    result=generate_opportunities(normalized,analysis,OpportunityMock(presented_plan(
        proposal(normalized,resources=estimate),summary=summary)))
    row=result['report']['initiatives'][0]
    assert row['resources']['status']=='needs_data' and row['resources']['effort']['min'] is None
    assert row['resources']['tasks'][0]['status']=='illustrative'
    assert 'executive_summary' not in result['report']['presentation']
    assert summary not in render_markdown(result)
    assert '3–5' not in management_view(result)['conclusion']['text']
    assert result['plan']['executive_summary']==summary
    assert restore_opportunities(json.dumps(result).encode(),normalized,analysis,'test-model')==result


def _comparative_case():
    _,_,analysis,normalized=simple_map(('Специалист проверяет заявление.',
        'Специалист сопоставляет документы.', 'Специалист готовит проект ответа.'))
    rows=[proposal(normalized,[f'op-{index}'],key=key,title=title,
        prospect_assessment=prospect_assessment(pilot_feasibility=feasibility))
        for index,(key,title,feasibility) in enumerate([
            ('draft','Подготовка проекта','bounded'),
            ('check','Проверка материалов','bounded'),
            ('corpus','Проверка нормативного корпуса','needs_preparation')])]
    return analysis,normalized,rows


def test_comparative_rank_uses_validated_order_and_dependencies_not_categories_or_generation_order():
    analysis,normalized,rows=_comparative_case()
    ready=ranked_plan(*rows,order=['corpus','check','draft'],dependencies={'check':['corpus'],'draft':['check']})
    source=deepcopy([ready.model_dump(),normalized])
    provider=OpportunityMock(ready)
    result=generate_opportunities(normalized,analysis,provider)
    items=result['report']['initiatives']
    assert [row['id'] for row in items]==['corpus','check','draft']
    assert [row['rank'] for row in items]==[1,2,3]
    assert [row['sequence_number'] for row in items]==[1,2,3]
    assert [row['prospect']['category'] for row in items]==['medium','high','high']
    assert items[1]['priority']['dependencies']==['corpus']
    assert items[1]['input_materials']==ready.proposals[1].input_materials
    assert items[1]['staff_result']==ready.proposals[1].staff_result
    assert result['compatibility']['plan_contract_version']=='2.4'
    assert result['compatibility']['prompt_version']=='opportunities-2.4'
    assert result['report']['presentation']['ordering_origin']=='model_comparison_checked'
    assert all(row['priority']['reason'] and row['priority']['change_condition'] for row in items)
    assert all('score' not in row['priority'] and 'weight' not in row['priority'] for row in items)
    assert restore_opportunities(json.dumps(result).encode(),normalized,analysis,'test-model')==result
    assert [ready.model_dump(),normalized]==source and len(provider.calls)==1


@pytest.mark.parametrize('fault',['missing','duplicate','unknown','unknown_dependency','unknown_comparison',
    'self_dependency','cycle','late_dependency','no_comparison','blank_reason'])
def test_comparative_order_rejects_incomplete_or_damaged_links_and_preserves_received_plan(fault):
    from govai.opportunities import RankedOpportunityPlan
    analysis,normalized,rows=_comparative_case()
    raw=ranked_plan(*rows).model_dump()
    order=raw['priority_order']
    if fault=='missing':order.pop()
    elif fault=='duplicate':order[1]['key']=order[0]['key']
    elif fault=='unknown':order[0]['key']='invented'
    elif fault=='unknown_dependency':order[0]['depends_on']=['invented']
    elif fault=='unknown_comparison':order[0]['compared_with'].append('invented')
    elif fault=='self_dependency':order[0]['depends_on']=[order[0]['key']]
    elif fault=='cycle':order[0]['depends_on']=[order[1]['key']];order[1]['depends_on']=[order[0]['key']]
    elif fault=='late_dependency':order[0]['depends_on']=[order[2]['key']]
    elif fault=='blank_reason':order[0]['reason']=' '*25
    else:order[0]['compared_with']=[]
    ready=RankedOpportunityPlan.model_validate(raw)
    provider=OpportunityMock(ready);received=[]
    with pytest.raises(OpportunityError):
        generate_opportunities(normalized,analysis,provider,on_plan=received.append)
    assert len(provider.calls)==1 and len(received)==1
    assert received[0]['plan']['priority_order']==raw['priority_order']
    assert received[0]['kind']=='govai_opportunity_plan'  # not a completed report


def test_comparative_queue_keeps_valid_hypotheses_when_a_dependency_is_quarantined():
    analysis,normalized,rows=_comparative_case()
    rows[2]['evidence_refs']=['invented']
    ready=ranked_plan(*rows,order=['corpus','check','draft'],dependencies={'check':['corpus']})
    result=generate_opportunities(normalized,analysis,OpportunityMock(ready))
    accepted=result['report']['initiatives']
    assert [row['id'] for row in accepted]==['check','draft']
    assert [row['rank'] for row in accepted]==[1,2]
    assert accepted[0]['priority']['status']=='blocked_dependency'
    assert accepted[0]['priority']['queue']=='reserve'
    assert accepted[0]['priority']['unavailable_dependencies']==['corpus']
    assert accepted[0]['uncertainties']
    assert result['report']['rejected'][0]['origin']=='integrity_validator'
    assert result['report']['priority_status']=='requires_review'
    assert restore_opportunities(json.dumps(result).encode(),normalized,analysis,'test-model')==result


def test_current_process_map_keeps_all_verified_operations_and_non_ai_reasons():
    _,_,analysis,normalized=map_case(incomplete=True)
    p=proposal(normalized,['receipt'],key='receipt-help')
    rejected=[{'title':'Формальная проверка реквизитов','action_ids':['correction'],
        'reason':'Для фиксированного перечня реквизитов достаточно обычной проверки правил.'}]
    result=generate_opportunities(normalized,analysis,OpportunityMock(ranked_plan(p,rejected=rejected)))
    process_map=result['report']['process_map']
    assert process_map['status']=='incomplete' and process_map['origin']=='validated_normalization'
    assert [g['id'] for g in process_map['groups']]==[g['id'] for g in normalized['map']['processes']]
    nodes={row['id']:row for group in process_map['groups'] for row in group['actions']}
    assert set(nodes)=={row['id'] for row in normalized['map']['actions']}
    for original in normalized['map']['actions']:
        actual=nodes[original['id']]
        assert actual['source_action_ids']==original['source_action_ids']
        assert actual['evidence_refs']==original['retained_evidence_refs']
        assert actual['participant']==original['participant']
        if all(not normalized['map']['source_actions'][sid]['actor'] for sid in original['source_action_ids']):
            assert actual['actors']==[]
    assert any(group['non_ai_reasons']==[rejected[0]['reason']] for group in process_map['groups'])
    assert any(group['initiative_ids']==['receipt-help'] for group in process_map['groups'])
    assert any(row['kind']=='review' and row['review_reasons'] for row in nodes.values())
    assert all(proof==analysis.evidence[eid] for eid,proof in process_map['evidence'].items())
    assert restore_opportunities(json.dumps(result).encode(),normalized,analysis,'test-model')==result


def test_zero_accepted_current_plan_retains_process_map_without_invented_ranks_or_ai_quota():
    _,_,analysis,normalized=simple_map(('Специалист выполняет проверку обязательных полей.',))
    ready=ranked_plan(rejected=[{'title':'Проверка обязательных полей','action_ids':['op-0'],
        'reason':'Достаточно фиксированных правил; добавочная польза модели не обоснована.'}])
    result=generate_opportunities(normalized,analysis,OpportunityMock(ready))
    assert result['report']['initiatives']==[] and result['plan']['priority_order']==[]
    group=result['report']['process_map']['groups'][0]
    assert group['ai_status']=='rejected_scenarios' and group['initiative_ids']==[]
    assert 'фиксированных правил' in group['non_ai_reasons'][0]
    assert restore_opportunities(json.dumps(result).encode(),normalized,analysis,'test-model')==result


def test_resource_contract_24_remains_strictly_readable_without_new_rank_fields():
    from govai.opportunities import OperationalOpportunityPlan, compatibility
    from govai.analysis_io import digest
    _,_,analysis,normalized=simple_map()
    old=historical_opportunity_artifact(normalized,analysis,presented_plan(proposal(normalized)),version='2.4')
    old['resource_inputs']={}
    encoded=json.dumps(old,ensure_ascii=False).encode()
    assert digest(OperationalOpportunityPlan.model_json_schema())=='c75a22691a955fe2959e6b06aa418787f8f1ddabcc7dd2f8f97bf3fc9417a81f'
    identity=compatibility(normalized,'test-model',schema_version='2.4')
    assert identity['prompt_version']=='opportunities-2.3'
    assert identity['prompt_sha256']=='b162a2c123762d2b7a7e48b86c93c2f65caadf67a6bd80f2419435dcb96e97f6'
    assert 'rank' not in old['report']['initiatives'][0] and 'process_map' not in old['report']
    assert restore_opportunities(encoded,normalized,analysis,'test-model')==old
    tampered=deepcopy(old);tampered['schema_version']='2.5'
    with pytest.raises(ProcessError):restore_opportunities(json.dumps(tampered).encode(),normalized,analysis,'test-model')


def test_current_ranked_plan_survives_local_assembly_failure_and_revalidates_without_api(monkeypatch):
    import govai.opportunities as module
    analysis,normalized,rows=_comparative_case()
    ready=ranked_plan(*rows,order=['check','corpus','draft'],dependencies={'draft':['corpus']})
    provider=OpportunityMock(ready);received=[]
    real_assembler=module.assemble_report
    def fail(*args,**kwargs):raise ProcessError('Локальная сборка временно недоступна.')
    monkeypatch.setattr(module,'assemble_report',fail)
    with pytest.raises(OpportunityError):generate_opportunities(normalized,analysis,provider,on_plan=received.append)
    backup=module.export_opportunity_plan(received[0],normalized,analysis,'test-model')
    assert received[0]['schema_version']=='2.4' and received[0]['compatibility']['schema_version']=='2.5'
    monkeypatch.setattr(module,'assemble_report',real_assembler)
    def no_request(*args,**kwargs):raise AssertionError('Retry must not call the provider')
    monkeypatch.setattr(provider,'opportunities',no_request)
    checked=module.restore_opportunity_plan(backup,normalized,analysis,'test-model')
    restored=generate_opportunities(normalized,analysis,None,model='test-model',provider_name='openai',saved_plan=checked)
    assert [row['id'] for row in restored['report']['initiatives']]==['check','corpus','draft']
    assert len(provider.calls)==1 and restored['plan']==received[0]['plan']
    corrupted=json.loads(backup);corrupted['plan']['proposals'][0]['evidence_refs']=['foreign-proof']
    with pytest.raises(ProcessError):module.restore_opportunity_plan(json.dumps(corrupted).encode(),normalized,analysis,'test-model')
    _,_,other_analysis,other_normalized=simple_map(('Совсем другой документ и другая операция.',))
    with pytest.raises(ProcessError):module.restore_opportunity_plan(backup,other_normalized,other_analysis,'test-model')


def test_editorial_rebuild_cannot_place_a_required_dependency_after_its_consumer():
    from govai.opportunities import rebuild_opportunities
    analysis,normalized,rows=_comparative_case()
    # All categories are equal: this test isolates dependency order from the
    # historical category ordering supported for old editorial artifacts.
    for row in rows:row['prospect_assessment']=prospect_assessment(pilot_feasibility='bounded')
    ready=ranked_plan(*rows,order=['corpus','check','draft'],dependencies={'draft':['corpus']})
    original=generate_opportunities(normalized,analysis,OpportunityMock(ready))
    editorial=_editorial_inputs(original['report']['initiatives'],['draft','check','corpus'])
    snapshot=deepcopy(original)
    with pytest.raises(ProcessError,match='зависимость'):
        rebuild_opportunities(json.dumps(original).encode(),normalized,analysis,'test-model',
            editorial_inputs=editorial,reason='Редакционная попытка перенести зависимую работу раньше обязательной подготовки.')
    assert original==snapshot


def test_invalid_comparison_remains_portable_without_becoming_a_report_or_triggering_api():
    import govai.opportunities as module
    analysis,normalized,rows=_comparative_case()
    raw=ranked_plan(*rows).model_dump()
    raw['priority_order'][0]['compared_with']=[]
    ready=module.RankedOpportunityPlan.model_validate(raw)
    provider=OpportunityMock(ready);received=[]
    with pytest.raises(OpportunityError,match='сравнительное основание'):
        generate_opportunities(normalized,analysis,provider,on_plan=received.append)
    encoded=module.export_opportunity_plan(received[0],normalized,analysis,'test-model')
    checked=module.restore_opportunity_plan(encoded,normalized,analysis,'test-model')
    assert checked['plan']==received[0]['plan'] and checked['kind']=='govai_opportunity_plan'
    assert 'report' not in checked
    with pytest.raises(OpportunityError,match='сравнительное основание'):
        generate_opportunities(normalized,analysis,None,model='test-model',provider_name='openai',saved_plan=checked)
    assert len(provider.calls)==1
    bad=deepcopy(checked);bad['plan']['proposals'][0]['evidence_refs']=['foreign-evidence']
    with pytest.raises(ProcessError):module.restore_opportunity_plan(json.dumps(bad).encode(),normalized,analysis,'test-model')


def test_historical_reassembly_does_not_claim_comparative_evidence():
    _,_,analysis,normalized=simple_map()
    saved=generate_opportunities(normalized,analysis,OpportunityMock(presented_plan(proposal(normalized))))
    assert saved['report']['initiatives'][0]['priority']['origin']=='historical_unranked'
    note=management_view(saved)['ordering_note']
    assert 'сопоставимых данных' in note and 'она сопоставляет' not in note
