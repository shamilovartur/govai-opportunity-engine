"""Hand-authored candidate fixtures check wiring, never actual LLM recall."""

from pathlib import Path

from govai.extraction import extract_document
from govai.processes import Candidates, build_chunks, extract_processes

SOURCE = Path(__file__).resolve().parents[1] / 'examples/rosimushchestvo_261/source.txt'
# This oracle is deliberately confined to tests. Each pair identifies a real action
# passage; it is not an exhaustive gold annotation of every action in the act.
CASES = {
    '3.2': ('Прием и регистрация документов', [
        ('3.2', 'Заявительные документы принимаются', 'Принять заявительные документы'),
        ('3.2', 'осуществляет их проверку', 'Проверить основания для отказа в приеме')]),
    '3.3': ('Рассмотрение документов', [
        ('3.3', 'Исполнитель в течение 2 рабочих дней', 'Проверить полноту документов'),
        ('3.3', 'исполнитель запрашивает такие документы', 'Запросить недостающие документы')]),
    '3.4': ('Межведомственный запрос документов', [
        ('3.4.1', 'осуществляет подготовку и направление запроса', 'Подготовить запрос документов'),
        ('3.4.1', 'осуществляет подготовку и направление запроса', 'Направить запрос документов')]),
    '3.5': ('Решение о предоставлении услуги или направлении документов', [
        ('3.5.1', 'осуществляет подготовку поручения', 'Подготовить поручение территориальному органу'),
        ('3.5.2', 'осуществляет подготовку письма', 'Подготовить письмо с комплектом документов')]),
    '3.6': ('Решение о предоставлении земельного участка', [
        ('3.6.1', 'обеспечивает подготовку и согласование', 'Подготовить распорядительный акт'),
        ('3.6.2', 'проект распорядительного акта дорабатывается', 'Доработать проект при замечаниях')]),
    '3.7': ('Выдача документов заявителю', [
        ('3.7.1', 'выдаются Заявителю', 'Выдать или направить решение на бумаге'),
        ('3.7.3', 'направляет Заявителю принятое решение', 'Направить электронное решение')]),
    '3.8': ('Исправление опечаток и ошибок', [
        ('3.8.1', 'Решение об исправлении', 'Принять решение об исправлении ошибок'),
        ('3.8.2', 'исправление допущенных', 'Исправить ошибки или направить ответ об их отсутствии')]),
}


def test_261_seven_procedures_fourteen_actions_mock_contract():
    doc = extract_document(SOURCE.name, SOURCE.read_bytes())
    assert doc['metadata']['revision_date']['value'] == '2023-05-30'
    anchors = {}
    for number in CASES:
        clause = next(c for c in doc['clauses'] if c['number'] == number)
        f = next(f for f in doc['fragments'] if f['id'] == clause['fragment_ids'][0])
        anchors[number] = {'fragment_id': f['id'], 'quote': f['text'].strip()}

    class FixtureProvider:
        name, model = 'mock', 'hand-authored-261-candidates'

        def extract(self, payload):
            procedures = []
            for number, (title, cases) in CASES.items():
                actions = []
                for clause, needle, description in cases:
                    for p in payload['core']:
                        if p['clause_number'] == clause and needle in p['text']:
                            actions.append({'description': description, 'actor': None,
                                'inputs': [], 'outputs': [], 'conditions': [], 'dependencies': [],
                                'evidence': [{'fragment_id': p['fragment_id'], 'quote': p['text'].strip()}]})
                if actions:
                    procedures.append({'title': title, 'anchor': anchors[number], 'actions': actions})
            return Candidates.model_validate({'procedures': procedures,
                'explanation': 'Ручная тестовая разметка кандидатов; реальный LLM не вызывался.'})

    result = extract_processes(doc, FixtureProvider())
    assert result['provider'] == 'mock'
    assert not result['rejected_candidates']
    assert len(result['procedures']) == 7
    assert [p['anchor_evidence'][0]['clause_number'] for p in result['procedures']] == list(CASES)
    assert all(len(p['actions']) == 2 for p in result['procedures'])
    assert sum(len(p['actions']) for p in result['procedures']) == 14
    assert result['coverage']['core_characters'] == len(doc['text']) == 113889
    assert result['coverage']['chunks_completed'] == len(build_chunks(doc))
    for p in result['procedures']:
        for a in p['actions']:
            for e in a['evidence']:
                assert any(doc['text'][m['start']:m['end']] == e['quote'] for m in e['matches'])
