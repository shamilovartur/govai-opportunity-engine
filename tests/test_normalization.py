"""Hand-authored grouping plans exercise rules, not the quality of a live LLM."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from govai.analysis_io import digest, verify_analysis
from govai.extraction import extract_document
from govai.normalization import (
    NormalizationError, NormalizationPlan, NormalizationSettings, compatibility, materialize,
    normalize_analysis, prepare_request, restore_normalized, markdown_report,
)
from govai.processes import Candidates, ProcessError, extract_processes

ROOT = Path(__file__).resolve().parents[1]
ACCEPT = ['action-9a73e5889b74bd7d', 'action-be9e6c550d931b36']
FIX = ['action-055433483d0430cd', 'action-d5674a64bdc768c4']
NO_ERROR = ['action-8058d42dbbe7be2c', 'action-e16c7c8956e7b0c8']


def raw_template(document, procedures):
    return {'schema_version': '1.0', 'status': 'completed', 'provider': 'mock', 'model': 'test-model',
            'prompt_version': '1.0', 'generated_at': '2026-11-14T00:00:00+00:00',
            'source': document['source'], 'metadata': document['metadata'],
            'text_sha256': document['source']['text_sha256'], 'procedures': procedures,
            'explanation': 'Test fixture, not a new LLM call.', 'rejected_candidates': [],
            'coverage': {'chunks_total': 1, 'chunks_completed': 1,
                         'core_characters': len(document['text']), 'source_characters': len(document['text'])},
            'limitations': ['quote_match_does_not_prove_entailment']}


def real_case():
    p = ROOT / 'examples/rosimushchestvo_261/source.txt'
    doc = extract_document(p.name, p.read_bytes())
    fragments = {f['id']: f for f in doc['fragments']}
    clauses = {c['id']: c for c in doc['clauses']}
    def expand(obj):
        if isinstance(obj, list):
            return [expand(v) for v in obj]
        if isinstance(obj, dict):
            if 'fragment_id' in obj:
                f = fragments[obj['fragment_id']]
                span = obj['matches'][0]
                return dict(obj, quote=doc['text'][span['start']:span['end']],
                    clause_id=f['clause_id'], clause_number=clauses.get(f['clause_id'], {}).get('number'),
                    section_id=f['section_id'], page_number=f['page_number'], attribution=f['attribution'])
            return {k: expand(v) for k, v in obj.items()}
        return obj
    fixture = json.loads((ROOT / 'tests/fixtures/normalization-261-cases.json').read_text())
    raw = raw_template(doc, expand(fixture['procedures']))
    return doc, raw, verify_analysis(json.dumps(raw, ensure_ascii=False).encode(), doc)


def simple_case(lines=('Секретарь регистрирует заявление.',), actors=None):
    doc = extract_document('test.txt', ''.join(f'{i+1}. {t}\n' for i, t in enumerate(lines)).encode())
    class SourceProvider:
        name, model = 'mock', 'test-model'
        def extract(self, payload):
            procs = []
            for i, f in enumerate(p for p in payload['core'] if p['text'].strip()):
                e = {'fragment_id': f['fragment_id'], 'quote': f['text'].strip()}
                actor = {'text': actors[i], 'evidence': [e]} if actors and actors[i] else None
                procs.append({'title': 'Процедура', 'anchor': e, 'actions': [{
                    'description': lines[i], 'actor': actor, 'inputs': [], 'outputs': [], 'conditions': [],
                    'dependencies': [], 'evidence': [e]}]})
            return Candidates.model_validate({'procedures': procs, 'explanation': 'Fixture'})
    raw = extract_processes(doc, SourceProvider())
    return doc, raw, verify_analysis(json.dumps(raw, ensure_ascii=False).encode(), doc)


def node(analysis, sids, key='a1', process='p1', **changes):
    value = {'key': key, 'process_key': process, 'title': 'Действие', 'source_action_ids': sids,
             'kind': 'operation', 'parent_key': None,
             'evidence_refs': list(dict.fromkeys(e for sid in sids for e in analysis.actions[sid]['refs'])),
             'participant': 'unknown', 'participant_refs': [], 'mode': 'unknown', 'mode_refs': [],
             'merge_reason': 'Одна операция, объект, исполнитель, условия, этап и канал.' if len(sids) > 1 else None,
             'split_reason': None, 'review_reason': None}
    value.update(changes)
    return value


def plan_for(analysis, nodes, titles=None):
    titles = titles or {}
    procs = [{'key': p, 'title': titles.get(p, p), 'summary': 'Содержание процедуры по НПА.',
              'lane': 'service', 'evidence_refs': list(dict.fromkeys(e for n in nodes if n['process_key'] == p
                                                                    for e in n['evidence_refs']))}
             for p in dict.fromkeys(n['process_key'] for n in nodes)]
    return NormalizationPlan.model_validate({'processes': procs, 'actions': nodes, 'explanation': 'Ручной тестовый план.'})


def test_actual_duplicate_acceptance_merges_details_without_mutating_source():
    _, raw, a = real_case()
    before = json.dumps(raw, ensure_ascii=False)
    plan = plan_for(a, [node(a, ACCEPT, title='Принять документы')])
    result = materialize(a, plan)
    merged = next(n for n in result['actions'] if n['source_action_ids'] == ACCEPT)
    assert merged['kind'] == 'operation'
    assert len(merged['retained_evidence_refs']) > len(merged['evidence_refs'])
    assert all(result['source_actions'][sid]['dependencies'] for sid in ACCEPT)
    assert len(result['accounting']) == len(a.actions) and not result['unaccounted_source_action_ids']
    assert result['status'] == 'incomplete'  # omitted other IDs remain explicitly in review
    assert json.dumps(raw, ensure_ascii=False) == before
    assert a.original_bytes == before.encode()


def test_actual_error_correction_alternatives_join_process_but_remain_two_operations():
    _, _, a = real_case()
    result = materialize(a, plan_for(a, [node(a, FIX, key='fix', title='Исправить ошибки'),
                                        node(a, NO_ERROR, key='answer', title='Ответить об отсутствии ошибки')]))
    active = [n for n in result['actions'] if n['process_id'] == 'p1']
    assert len(active) == 2
    assert active[0]['source_action_ids'] == FIX and active[1]['source_action_ids'] == NO_ERROR
    assert any('10 календарных дней' in v['text'] for sid in FIX for v in result['source_actions'][sid]['conditions'])
    assert any('Альтернативный' in v['text'] for sid in NO_ERROR for v in result['source_actions'][sid]['conditions'])
    unsafe = materialize(a, plan_for(a, [node(a, FIX + NO_ERROR)]))
    assert set(FIX + NO_ERROR) <= set(unsafe['deferred_source_action_ids'])
    assert 'альтернативный' in unsafe['issues'][0]['reason']


def test_actual_list_in_one_clause_does_not_force_procedure_merge_or_count_overview_twice():
    _, _, a = real_case()
    sids = ['action-64fde2c3870b8209', 'action-805a660a3075ba2d', 'action-84734bde53f798a8',
            'action-a73a4a6ea8cabb2e', 'action-8067248f8460ec26']
    nodes = [node(a, [sid], key=f'n{i}', process=f'p{i}', kind='overview') for i, sid in enumerate(sids)]
    nodes.append(node(a, FIX, key='fix', process='p4', parent_key='n4'))
    result = materialize(a, plan_for(a, nodes))
    active = [p for p in result['processes'] if p['lane'] != 'review']
    assert len(active) == 5
    assert sum(n['kind'] == 'operation' for n in result['actions']) == 1
    assert next(n for n in result['actions'] if n['id'] == 'fix')['parent_id'] == 'n4'


def test_actual_agency_and_mfc_actors_are_not_automatically_merged():
    _, _, a = real_case()
    sids = [ACCEPT[0], 'action-b24087c749856e83']
    result = materialize(a, plan_for(a, [node(a, sids)]))
    assert not any(len(n['source_action_ids']) > 1 for n in result['actions'])
    assert set(sids) <= set(result['deferred_source_action_ids'])
    assert 'исполнители' in result['issues'][0]['reason']


@pytest.mark.parametrize('lines', [
    ('Направить решение на бумажном носителе.', 'Направить решение в электронной форме.'),
    ('Направить ответ в течение 3 дней.', 'Направить ответ в течение 10 дней.'),
])
def test_different_channels_and_deadlines_block_unsafe_merge(lines):
    _, _, a = simple_case(lines)
    result = materialize(a, plan_for(a, [node(a, list(a.actions))]))
    assert len(result['deferred_source_action_ids']) == 2


def test_same_name_different_operations_and_same_quote_split_are_preserved():
    _, _, a = simple_case(('Подготовить и направить запрос.',))
    sid = next(iter(a.actions))
    nodes = [node(a, [sid], key='prepare', title='Подготовить запрос', split_reason='Подготовка и направление — две операции.'),
             node(a, [sid], key='send', title='Направить запрос', split_reason='Та же составная исходная формулировка.')]
    result = materialize(a, plan_for(a, nodes))
    assert len(result['actions']) == 2 and result['accounting'][0]['disposition'] == 'split'
    assert result['actions'][0]['evidence_refs'] == result['actions'][1]['evidence_refs']
    for n in nodes:
        n['title'] = 'Обработка запроса'
    assert len(materialize(a, plan_for(a, nodes))['actions']) == 2


def test_unknown_ids_rejected_and_uncovered_ids_kept_in_review():
    _, _, a = simple_case()
    n = node(a, list(a.actions)); n['source_action_ids'] = ['invented']
    with pytest.raises(ProcessError, match='source_action_id'):
        materialize(a, plan_for(a, [n]))
    n = node(a, list(a.actions)); n['evidence_refs'] = ['e-invented']
    with pytest.raises(ProcessError, match='доказательств'):
        materialize(a, plan_for(a, [n]))
    result = materialize(a, NormalizationPlan(processes=[], actions=[], explanation='Incomplete'))
    assert result['status'] == 'incomplete'
    assert result['missing_from_plan'] == list(a.actions)
    assert result['unaccounted_source_action_ids'] == []
    assert result['actions'][0]['kind'] == 'review'


@pytest.mark.parametrize('mode', ['automation_explicit', 'manual_explicit', 'mixed'])
def test_absence_of_automation_or_executor_is_not_manual_work_or_an_invented_actor(mode):
    _, _, a = simple_case(('Документы принимаются на бумаге.',))
    n = node(a, list(a.actions), mode=mode, participant='agency')
    n['mode_refs'] = n['participant_refs'] = n['evidence_refs']
    result = materialize(a, plan_for(a, [n]))['actions'][0]
    assert result['mode'] == result['participant'] == 'unknown'
    assert result['review_reasons']


def test_explicit_automation_is_separate_from_actor_and_actual_implementation():
    _, _, a = real_case()
    sid = 'action-1b680ebbcc459be0'
    n = node(a, [sid], mode='automation_explicit'); n['mode_refs'] = n['evidence_refs']
    action = materialize(a, plan_for(a, [n]))['actions'][0]
    assert action['mode'] == 'automation_explicit' and action['participant'] == 'unknown'


def test_negated_automation_does_not_become_an_automation_claim():
    _, _, a = simple_case(('Действие не выполняется автоматически.',))
    n = node(a, list(a.actions), mode='automation_explicit'); n['mode_refs'] = n['evidence_refs']
    assert materialize(a, plan_for(a, [n]))['actions'][0]['mode'] == 'unknown'


def test_uncertain_merge_is_deferred_and_explicit_exclusion_keeps_source():
    _, _, a = simple_case(('Рассмотреть заявление.', 'Проверить заявление.'))
    result = materialize(a, plan_for(a, [node(a, list(a.actions), review_reason='Неясно, совпадает ли операция.')]))
    assert len(result['deferred_source_action_ids']) == 2
    nodes = [node(a, [sid], key=f'n{i}', kind='excluded', review_reason='Это нормативное требование, решение требует проверки.')
             for i, sid in enumerate(a.actions)]
    result = materialize(a, plan_for(a, nodes))
    assert len(result['accounting']) == 2
    assert all(row['disposition'] == 'excluded' for row in result['accounting'])
    assert all(n['review_reasons'] for n in result['actions'])


def test_condition_conflict_and_unexplained_split_never_silently_merge_or_drop():
    doc, raw, _ = simple_case(('При наличии документов принять заявление.', 'При отсутствии документов вернуть заявление.'))
    for p in raw['procedures']:
        a = p['actions'][0]
        a['conditions'] = [{'text': a['description'], 'evidence': deepcopy(a['evidence'])}]
    analysis = verify_analysis(json.dumps(raw).encode(), doc)
    m = materialize(analysis, plan_for(analysis, [node(analysis, list(analysis.actions))]))
    assert len(m['deferred_source_action_ids']) == 2
    sid = next(iter(analysis.actions))
    m = materialize(analysis, plan_for(analysis, [node(analysis, [sid], key='a1'), node(analysis, [sid], key='a2')]))
    assert sid in m['deferred_source_action_ids']
    assert len(m['accounting']) == len(analysis.actions)


def test_parent_operation_becomes_overview_and_applicant_context_is_preserved():
    _, _, a = simple_case(('Заявитель направляет обращение.', 'Секретарь принимает обращение.'), actors=['Заявитель', 'Секретарь'])
    sids = list(a.actions)
    nodes = [node(a, [sids[0]], key='parent', kind='operation', participant='applicant',
                  participant_refs=a.actions[sids[0]]['actor']['refs']),
             node(a, [sids[1]], key='child', parent_key='parent')]
    m = materialize(a, plan_for(a, nodes))
    assert next(n for n in m['actions'] if n['id']=='parent')['kind'] == 'overview'
    assert sum(n['kind']=='operation' for n in m['actions']) == 1
    assert m['actions'][0]['participant'] == 'applicant'


def test_result_prompt_and_settings_invalidate_saved_normalization():
    doc, raw, a = simple_case()
    artifact = normalize_analysis(a, Normalizer(plan_for(a, [node(a, list(a.actions))])))
    with pytest.raises(ProcessError, match='несовместим'):
        restore_normalized(json.dumps(artifact).encode(), a, 'test-model', 'mock', replace(NormalizationSettings(), max_input_bytes=399999))
    altered = deepcopy(artifact); altered['compatibility']['prompt_version'] = 'old'
    with pytest.raises(ProcessError, match='несовместим'):
        restore_normalized(json.dumps(altered).encode(), a, 'test-model', 'mock')
    raw['explanation'] = 'Changed original result'
    changed = verify_analysis(json.dumps(raw).encode(), doc)
    with pytest.raises(ProcessError, match='несовместим'):
        restore_normalized(json.dumps(artifact).encode(), changed, 'test-model', 'mock')


class Normalizer:
    name, model = 'mock', 'test-model'
    def __init__(self, plan):
        self.plan = plan
        self.calls = 0
    def normalize(self, payload, **kwargs):
        self.calls += 1
        return self.plan, {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30}


def test_restore_rechecks_plan_and_evidence_without_generation_and_keeps_diagnostics():
    doc, raw, a = simple_case()
    raw['rejected_candidates'] = [{'scope': 'action', 'reason': 'quote_not_in_sent_fragment'}]
    a = verify_analysis(json.dumps(raw).encode(), doc)
    provider = Normalizer(plan_for(a, [node(a, list(a.actions))]))
    artifact = normalize_analysis(a, provider)
    restored = restore_normalized(json.dumps(artifact).encode(), a, provider.model, provider.name)
    assert restored == artifact and provider.calls == 1
    assert artifact['map']['source_diagnostics']['rejected_candidates'] == raw['rejected_candidates']
    assert artifact['metrics']['requests'] == 1 and artifact['metrics']['elapsed_seconds'] >= 0
    assert artifact['metrics']['usage']['total_tokens'] == 30
    assert 'Исходные ID' in markdown_report(artifact, doc['metadata'])
    forged = deepcopy(artifact); forged['map']['evidence'][next(iter(a.evidence))]['quote'] = 'forged'
    with pytest.raises(ProcessError, match='повреждена'):
        restore_normalized(json.dumps(forged).encode(), a, provider.model, provider.name)
    with pytest.raises(ProcessError, match='несовместим'):
        restore_normalized(json.dumps(artifact).encode(), a, 'another-model', provider.name)


def test_preflight_limits_do_not_call_provider_or_truncate_data():
    _, _, a = simple_case()
    provider = Normalizer(plan_for(a, [node(a, list(a.actions))]))
    with pytest.raises(NormalizationError, match='лимит') as caught:
        normalize_analysis(a, provider, settings=NormalizationSettings(max_input_bytes=10))
    assert provider.calls == 0 and caught.value.metrics['requests'] == 0
    payload, sizes = prepare_request(a)
    assert len(payload['actions']) == len(a.actions)
    assert len(payload['evidence']) == len(a.evidence)
    assert sizes['input_bytes'] > 0


def test_other_document_and_no_candidates_require_no_request():
    doc, _, _ = simple_case(('Настоящий текст содержит только определение термина.',))
    raw = raw_template(doc, [])
    a = verify_analysis(json.dumps(raw).encode(), doc)
    artifact = normalize_analysis(a, None)
    assert artifact['metrics']['requests'] == 0
    assert artifact['map']['status'] == 'completed' and not artifact['map']['actions']


@pytest.mark.parametrize('fault', ['error', 'bad_schema'])
def test_failed_normalization_retains_original_and_reports_stage_metrics(fault):
    _, _, a = simple_case()
    original = a.original_bytes
    class Broken(Normalizer):
        def normalize(self, payload, **kw):
            if fault == 'error':
                raise ProcessError('Отказ или неполный ответ модели')
            return {}, None
    with pytest.raises(NormalizationError) as caught:
        normalize_analysis(a, Broken(None))
    assert caught.value.metrics['requests'] == 1
    assert a.original_bytes == original


@pytest.mark.parametrize('mutation', ['source', 'quote', 'coordinates', 'clause', 'duplicate', 'fake_validation'])
def test_import_checks_real_evidence_and_ids_instead_of_validation_flags(mutation):
    doc, raw, _ = simple_case()
    a = raw['procedures'][0]['actions'][0]
    if mutation == 'source': raw['source']['sha256'] = 'wrong'
    elif mutation == 'quote': a['evidence'][0]['quote'] = 'Not in document'
    elif mutation == 'coordinates': a['evidence'][0]['matches'][0]['start'] += 1
    elif mutation == 'clause': a['evidence'][0]['clause_number'] = '99'
    elif mutation == 'duplicate': raw['procedures'][0]['actions'].append(deepcopy(a))
    else:
        a['validation'] = 'trusted'; a['requires_review'] = False
        a['evidence'][0]['fragment_id'] = 'fake'
    with pytest.raises(ProcessError):
        verify_analysis(json.dumps(raw).encode(), doc)


@pytest.mark.parametrize('case', ['ancestor', 'cycle', 'other_process'])
def test_invalid_parent_links_are_clear_errors_not_crashes(case):
    _, _, a = simple_case(('Первое действие.', 'Второе действие.'))
    one, two = list(a.actions)
    nodes = [node(a, [one], key='child', parent_key='parent'),
             node(a, [two], key='parent')]
    if case == 'ancestor': nodes[1]['parent_key'] = 'unknown'
    elif case == 'cycle': nodes[1]['parent_key'] = 'child'
    else: nodes[1]['process_key'] = 'another-process'
    with pytest.raises(ProcessError):
        materialize(a, plan_for(a, nodes))


@pytest.mark.parametrize('bad', [b'{"kind":"govai_process_checkpoint"}',
                               b'{"kind":"govai_normalized_processes"}',
                               b'{"procedures":[],"procedures":[]}', b'{"x":NaN}'])
def test_import_rejects_other_formats_and_ambiguous_json(bad):
    doc, _, _ = simple_case()
    with pytest.raises(ProcessError):
        verify_analysis(bad, doc)


def test_another_real_npa_excerpt_uses_same_normalizer_without_special_rules():
    path = ROOT / 'tests/fixtures/law_59_article8_excerpt.txt'
    doc = extract_document(path.name, path.read_bytes())
    class ExcerptProvider:
        name, model = 'mock', 'test-model'
        def extract(self, payload):
            fragment = next(f for f in payload['core'] if 'обязательной регистрации' in f['text'])
            proof = {'fragment_id': fragment['fragment_id'], 'quote': fragment['text']}
            return Candidates.model_validate({'procedures': [{'title': 'Регистрация обращения',
                'anchor': proof, 'actions': [{'description': 'Зарегистрировать письменное обращение',
                    'actor': None, 'inputs': [], 'outputs': [], 'conditions': [
                        {'text': 'В течение трёх дней с момента поступления', 'evidence': [proof]}],
                    'dependencies': [], 'evidence': [proof]}]}], 'explanation': 'Mock of a real excerpt.'})
    raw = extract_processes(doc, ExcerptProvider())
    analysis = verify_analysis(json.dumps(raw, ensure_ascii=False).encode(), doc)
    artifact = normalize_analysis(analysis, Normalizer(plan_for(analysis, [
        node(analysis, list(analysis.actions), title='Зарегистрировать обращение')],
        titles={'p1': 'Регистрация обращения'})))
    assert artifact['map']['status'] == 'completed'
    action = artifact['map']['actions'][0]
    assert action['mode'] == action['participant'] == 'unknown'
    assert 'трёх дней' in artifact['map']['source_actions'][action['source_action_ids'][0]]['conditions'][0]['text']
