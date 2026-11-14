"""Offline contract/grounding tests. These are NOT evidence of live LLM quality."""

from copy import deepcopy
from pathlib import Path

import pytest

from govai.extraction import extract_document
from govai.processes import Candidates, ChunkSettings, ProcessError, build_chunks, extract_processes


def document(text):
    return extract_document('test.txt', text.encode())


def candidate(fragment, *, anchor=None, description='Зарегистрировать обращение'):
    evidence = {'fragment_id': fragment['fragment_id'], 'quote': fragment['text'].strip()}
    return {'title': 'Рассмотрение обращения', 'anchor': anchor or evidence,
            'actions': [{'description': description, 'evidence': [evidence], 'actor': None,
                         'inputs': [], 'outputs': [], 'conditions': [], 'dependencies': []}]}


class MockProvider:
    name = 'mock'
    model = 'offline-fixture'

    def __init__(self, callback=None):
        self.callback = callback or (lambda p: [])
        self.calls = []

    def extract(self, payload):
        self.calls.append(payload)
        return Candidates.model_validate({'procedures': self.callback(payload),
                                         'explanation': 'Ответ mock-провайдера для проверки конвейера.'})


def test_chunk_coverage_order_large_document_and_semantic_boundaries():
    text = ''.join(f'Раздел {i}. Порядок\n\n{i}. Проверить обращение.\n'
                   f'{i}.1. Исполнитель регистрирует заявление.\n'
                   + 'Дополнительные условия выполнения действия. ' * 12 + '\n\n'
                   for i in range(1, 220))
    doc = document(text)
    settings = ChunkSettings(max_chunks=500)
    chunks = build_chunks(doc, settings)
    assert len(chunks) > 100
    assert len(text) > 113889
    assert ''.join(p['text'] for c in chunks for p in c.core) == text
    ranges = [(p['start'], p['end']) for c in chunks for p in c.core]
    assert ranges[0][0] == 0 and ranges[-1][1] == len(text)
    assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:]))
    assert all(len({p['section_id'] for p in c.core}) == 1 for c in chunks)
    provider = MockProvider()
    progress = []
    result = extract_processes(doc, provider, settings=settings,
                              progress=lambda n, t: progress.append((n, t)))
    assert len(provider.calls) == len(chunks)
    assert result['coverage']['core_characters'] == len(text)
    assert progress[0] == (0, len(chunks)) and progress[-1] == (len(chunks), len(chunks))
    assert result['procedures'] == [] and result['explanation']


def test_long_single_fragment_split_and_bidirectional_overlap():
    text = '1. ' + 'Исполнитель проверяет заявление и регистрирует результат. ' * 40
    chunks = build_chunks(document(text), ChunkSettings(core_chars=400, overlap_chars=90))
    assert len(chunks) > 3
    for c in chunks:
        assert sum(len(p['text']) for p in c.core) <= 400
    assert ''.join(p['text'] for c in chunks for p in c.core) == text
    assert len({p['fragment_id'] for c in chunks for p in c.core}) == 1
    assert any(p['end'] > chunks[0].core[-1]['end'] for p in chunks[0].context)
    assert any(p['start'] < chunks[1].core[0]['start'] for p in chunks[1].context)


def test_clause_kept_whole_and_parent_context_present():
    doc = document('1. Рассмотрение заявления.\n' + '1.1. ' + 'Проверка. ' * 50 + '\n'
                   '1.2. Исполнитель направляет ответ заявителю.\n')
    chunks = build_chunks(doc, ChunkSettings(core_chars=300))
    last = chunks[-1]
    assert any(p['clause_number'] == '1' for p in last.context)
    assert any('направляет ответ' in p['text'] for p in last.core)


def test_action_quote_crossing_chunk_boundary_is_validated_and_deduplicated():
    doc = document('1. ' + 'Сведения. ' * 18 + 'Исполнитель регистрирует обращение и отправляет ответ. '
                   + 'Дополнение. ' * 25)
    quote = 'Исполнитель регистрирует обращение и отправляет ответ.'
    def reply(p):
        if p['chunk_id'] not in ('chunk-1', 'chunk-2'):
            return []
        anchor = {'fragment_id': 'f-0', 'quote': '1.'}
        a = candidate(p['core'][0], anchor=anchor)
        a['actions'][0]['evidence'] = [{'fragment_id': 'f-0', 'quote': quote}]
        return [a]
    result = extract_processes(doc, MockProvider(reply), settings=ChunkSettings(core_chars=210, overlap_chars=100))
    assert not result['rejected_candidates']
    assert len(result['procedures'][0]['actions']) == 1
    assert result['procedures'][0]['actions'][0]['evidence'][0]['quote'] == quote


def test_identically_named_actions_at_different_locations_are_preserved():
    doc = document('1. Прием документов.\n1.1. Секретарь регистрирует заявление.\n'
                   '1.2. Секретарь регистрирует решение.\n')
    def reply(p):
        anchor = {'fragment_id': p['core'][0]['fragment_id'], 'quote': p['core'][0]['text'].strip()}
        return [candidate(f, anchor=anchor, description='Зарегистрировать документ')
                for f in p['core'] if 'регистрирует' in f['text']]
    result = extract_processes(doc, MockProvider(reply))
    assert len(result['procedures']) == 1
    assert len(result['procedures'][0]['actions']) == 2
    assert len({a['id'] for a in result['procedures'][0]['actions']}) == 2


def test_limit_and_document_integrity_fail_before_any_api_call():
    doc = document('1. ' + 'Слово ' * 1000)
    provider = MockProvider()
    with pytest.raises(ProcessError, match='лимит'):
        extract_processes(doc, provider, settings=ChunkSettings(core_chars=200, max_chunks=1))
    assert not provider.calls
    doc['fragments'][0]['text'] += 'дописано'
    with pytest.raises(ProcessError, match='целостность'):
        extract_processes(doc, provider)
    assert not provider.calls


def test_exact_grounding_distinct_actions_dedup_and_untrusted_number_absent():
    doc = document('1. Исполнитель регистрирует обращение и направляет ответ заявителю.\n')
    def reply(p):
        a = candidate(p['core'][0])
        a['actions'].append(deepcopy(a['actions'][0]))
        b = deepcopy(a['actions'][0]); b['description'] = 'Направить ответ заявителю'
        a['actions'].append(b)
        a['actions'][0]['actor'] = {'text': 'Исполнитель', 'evidence': [
            {'fragment_id': p['core'][0]['fragment_id'], 'quote': 'Исполнитель'}]}
        a['actions'][1]['actor'] = deepcopy(a['actions'][0]['actor'])
        return [a, deepcopy(a)]
    result = extract_processes(doc, MockProvider(reply))
    assert len(result['procedures']) == 1
    actions = result['procedures'][0]['actions']
    assert len(actions) == 2
    for a in actions:
        assert a['requires_review']
        for e in a['evidence']:
            assert e['clause_number'] == '1' and e['page_number'] is None
            for m in e['matches']:
                assert doc['text'][m['start']:m['end']] == e['quote']
    assert actions[0]['actor']['text'] == 'Исполнитель'


@pytest.mark.parametrize('bad', [
    {'fragment_id': 'f-invented', 'quote': 'Исполнитель регистрирует обращение.'},
    {'fragment_id': 'f-0', 'quote': 'Исполнитель отправляет документ на Луну.'},
    {'fragment_id': 'f-0', 'quote': ' '},
    {'fragment_id': 'f-0', 'quote': '1.'},
])
def test_false_sources_are_rejected(bad):
    doc = document('1. Исполнитель регистрирует обращение.\n')
    def reply(p):
        a = candidate(p['core'][0]); a['actions'][0]['evidence'] = [bad]
        return [a]
    result = extract_processes(doc, MockProvider(reply))
    assert result['procedures'] == []
    assert result['rejected_candidates'] and result['status'] == 'completed_with_rejections'
    assert 'не означает отсутствия' in result['explanation']


def test_unknown_procedure_anchor_and_unsupported_actor_removed():
    doc = document('1. Исполнитель регистрирует обращение.\n')
    def reply(p):
        a = candidate(p['core'][0])
        a['actions'][0]['actor'] = {'text': 'Министр', 'evidence': [
            {'fragment_id': 'f-0', 'quote': 'Министр'}]}
        bad = deepcopy(a); bad['anchor']['fragment_id'] = 'f-fake'
        return [a, bad]
    result = extract_processes(doc, MockProvider(reply))
    action = result['procedures'][0]['actions'][0]
    assert action['actor'] is None and action['review_reasons'] == ['unsupported_actor_removed']
    assert len(result['rejected_candidates']) == 2


def test_inputs_outputs_conditions_dependencies_have_individual_evidence():
    doc = document('1. После проверки секретарь принимает заявление и выдает расписку, если документы полны.\n')
    def reply(p):
        a = candidate(p['core'][0])
        for field, text in [('inputs', 'заявление'), ('outputs', 'расписку'),
                            ('conditions', 'если документы полны'), ('dependencies', 'После проверки')]:
            a['actions'][0][field] = [{'text': text, 'evidence': [
                {'fragment_id': 'f-0', 'quote': text}]}]
        a['actions'][0]['outputs'].append({'text': 'Лицензия', 'evidence': [
            {'fragment_id': 'f-0', 'quote': 'Лицензия'}]})
        return [a]
    result = extract_processes(doc, MockProvider(reply))
    a = result['procedures'][0]['actions'][0]
    for field in ('inputs', 'outputs', 'conditions', 'dependencies'):
        assert len(a[field]) == 1
        assert a[field][0]['evidence'][0]['clause_number'] == '1'
    assert 'unsupported_outputs_removed' in a['review_reasons']


def test_citation_to_valid_but_unsent_fragment_rejected():
    doc = document('Раздел 1. Начало\n1. Регистрируют обращение.\n'
                   + 'Раздел 2. Середина\n2. ' + 'Общие сведения. ' * 80
                   + '\nРаздел 3. Конец\n3. Направляют ответ заявителю.\n')
    last = doc['fragments'][-1]
    def reply(p):
        if p['chunk_id'] != 'chunk-1':
            return []
        a = candidate(p['core'][0]); a['actions'][0]['evidence'] = [
            {'fragment_id': last['id'], 'quote': last['text'].strip()}]
        return [a]
    result = extract_processes(doc, MockProvider(reply),
                               settings=ChunkSettings(core_chars=300, overlap_chars=0, context_chars=0))
    assert result['procedures'] == []
    assert result['rejected_candidates'][0]['reason'] == 'quote_not_in_sent_fragment'


def test_unknown_structure_keeps_null_clause_and_no_processes_explained():
    doc = document('Секретарь принимает обращение и регистрирует его в журнале.\n')
    result = extract_processes(doc, MockProvider(lambda p: [candidate(p['core'][0])]))
    assert result['procedures'][0]['actions'][0]['evidence'][0]['clause_number'] is None
    no_processes = document('Термины.\nЗаявление — форма обращения гражданина.\nПеречень: паспорт, копия.\n')
    result = extract_processes(no_processes, MockProvider())
    assert not result['procedures'] and result['coverage']['chunks_completed'] > 0
    assert result['coverage']['chunks'][0]['explanation']


def test_mid_document_failure_does_not_return_partial_success():
    class Broken(MockProvider):
        def extract(self, payload):
            if self.calls:
                raise ProcessError('Неполный ответ LLM')
            return super().extract(payload)
    with pytest.raises(ProcessError, match='Завершённый результат не сформирован'):
        extract_processes(document('1. ' + 'Текст. ' * 200), Broken(),
                          settings=ChunkSettings(core_chars=300))


@pytest.mark.parametrize('response', [{}, {'procedures': [], 'explanation': ''}])
def test_malformed_or_empty_unexplained_response_fails(response):
    class Invalid(MockProvider):
        def extract(self, payload):
            return response
    with pytest.raises(ProcessError):
        extract_processes(document('Текст без структуры.'), Invalid())


def test_pdf_evidence_preserves_physical_pages():
    path = Path(__file__).parent / 'fixtures/cyrillic-pages.pdf'
    doc = extract_document(path.name, path.read_bytes())
    def reply(p):
        return [candidate(f) for f in p['core'] if f['page_number'] == 2 and len(f['text'].strip()) > 20]
    result = extract_processes(doc, MockProvider(reply))
    assert result['procedures']
    assert all(e['page_number'] == 2 for p in result['procedures'] for a in p['actions'] for e in a['evidence'])


def test_another_real_npa_excerpt_without_algorithm_changes():
    path = Path(__file__).parent / 'fixtures/law_59_article8_excerpt.txt'
    doc = extract_document(path.name, path.read_bytes())
    def reply(p):
        return [candidate(f, description='Зарегистрировать письменное обращение')
                for f in p['core'] if f['clause_number'] == '2' and 'обязательной регистрации' in f['text']]
    result = extract_processes(doc, MockProvider(reply))
    assert result['procedures'][0]['actions'][0]['evidence'][0]['clause_number'] == '2'
    assert result['provider'] == 'mock'
