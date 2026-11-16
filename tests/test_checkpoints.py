"""Offline recovery tests: model responses are mocked; grounding/merge is real."""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json

import pytest

from govai.checkpoints import (
    AnalysisInterrupted, combine_checkpoints, compatible_checkpoint, dump_checkpoint,
    extract_with_checkpoint, load_checkpoint, new_checkpoint,
)
from govai.extraction import extract_document
from govai.processes import Candidates, ChunkSettings, ProcessError, extract_processes

SETTINGS = ChunkSettings(core_chars=250, overlap_chars=80, context_chars=300)


def source(text=None):
    text = text or ''.join(f'Раздел {n}. Работа с документами\n'
                          f'{n}. Секретарь регистрирует заявление и направляет ответ заявителю.\n\n'
                          for n in range(1, 5))
    return extract_document('test.txt', text.encode())


class Provider:
    name, model = 'mock', 'test-model'
    prompt_version, prompt_sha256 = '1.0', sha256(b'test instructions').hexdigest()

    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    def extract(self, payload):
        self.calls.append(payload['chunk_id'])
        if payload['chunk_id'] == self.fail_at:
            raise ProcessError('Временная ошибка API.')
        f = next(p for p in payload['core'] if 'Секретарь' in p['text'])
        evidence = {'fragment_id': f['fragment_id'], 'quote': f['text'].strip()}
        return Candidates.model_validate({'procedures': [{
            'title': 'Регистрация и ответ', 'anchor': evidence, 'actions': [{
                'description': 'Зарегистрировать заявление и направить ответ', 'actor': None,
                'inputs': [], 'outputs': [], 'conditions': [], 'dependencies': [],
                'evidence': [evidence]}]}], 'explanation': 'Mock-ответ для теста восстановления.'})


def interrupted():
    snapshots = []
    provider = Provider(fail_at='chunk-3')
    with pytest.raises(AnalysisInterrupted) as caught:
        extract_with_checkpoint(source(), provider, settings=SETTINGS, on_checkpoint=snapshots.append)
    return provider, snapshots, caught.value


def test_mid_block_failure_saves_each_success_and_reports_partial_progress():
    provider, snapshots, error = interrupted()
    assert provider.calls == ['chunk-1', 'chunk-2', 'chunk-3']
    assert [len(s['results']) for s in snapshots] == [0, 1, 2]
    assert error.completed_blocks == 2 and error.total_blocks == 4
    assert '2/4' in str(error) and 'не завершён' in str(error)
    assert error.checkpoint == snapshots[-1]
    assert set(error.checkpoint['results']) == {'chunk-1', 'chunk-2'}
    assert 'procedures' not in error.checkpoint  # no final result is returned or exported on failure


def test_resume_only_requests_missing_blocks_then_revalidates_and_merges():
    _, snapshots, error = interrupted()
    provider = Provider()
    progress = []
    result = extract_with_checkpoint(source(), provider, settings=SETTINGS, checkpoint=error.checkpoint,
        on_checkpoint=snapshots.append, progress=lambda n, total: progress.append((n, total)))
    assert provider.calls == ['chunk-3', 'chunk-4']
    assert progress == [(2, 4), (3, 4), (4, 4)]
    assert result['execution']['blocks_reused'] == 2
    assert result['execution']['blocks_requested'] == 2
    assert result['coverage']['chunks_completed'] == 4
    assert len(result['procedures']) == 4
    original = extract_processes(source(), Provider(), settings=SETTINGS)
    assert result['procedures'] == original['procedures']
    assert result['rejected_candidates'] == original['rejected_candidates']
    again = Provider()
    repeated = extract_with_checkpoint(source(), again, settings=SETTINGS, checkpoint=snapshots[-1])
    assert not again.calls
    assert repeated['execution']['blocks_reused'] == 4
    assert repeated['procedures'] == result['procedures']


def test_json_restore_after_loss_of_all_session_state(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'TEST_SECRET_MUST_NOT_APPEAR')
    _, snapshots, _ = interrupted()
    data = dump_checkpoint(snapshots[-1])
    assert b'TEST_SECRET_MUST_NOT_APPEAR' not in data and b'api_key' not in data.lower()
    assert 'Секретарь' in data.decode()
    del snapshots
    restored = load_checkpoint(data)
    provider = Provider()
    result = extract_with_checkpoint(source(), provider, settings=SETTINGS, checkpoint=restored)
    assert provider.calls == ['chunk-3', 'chunk-4']
    assert result['status'] == 'completed'


@pytest.mark.parametrize('changed', ['document', 'text', 'structure', 'provider', 'model',
                                    'prompt_version', 'prompt_sha256', 'settings', 'schema'])
def test_incompatible_checkpoint_rejected_before_any_api_request(changed, monkeypatch):
    _, _, error = interrupted()
    doc, provider, settings = source(), Provider(), SETTINGS
    if changed == 'document':
        doc['source']['sha256'] = sha256(b'other file with identical extracted text').hexdigest()
    elif changed == 'text':
        doc = source(source()['text'].replace('Секретарь', 'Специалист'))
    elif changed == 'structure':
        doc['fragments'][0]['attribution'] = 'unassigned'
    elif changed == 'settings':
        settings = replace(SETTINGS, overlap_chars=81)
    elif changed == 'schema':
        original = Candidates.model_json_schema()
        monkeypatch.setattr(Candidates, 'model_json_schema', lambda: dict(original, title='Changed'))
    else:
        setattr(provider, 'name' if changed == 'provider' else changed, 'changed')
    with pytest.raises(ProcessError, match='несовместим'):
        extract_with_checkpoint(doc, provider, settings=settings, checkpoint=error.checkpoint)
    assert not provider.calls


def test_changed_chunk_payload_digest_is_rejected(monkeypatch):
    _, _, error = interrupted()
    from govai.processes import Chunk
    original = Chunk.payload
    def changed(self, metadata):
        return dict(original(self, metadata), extra='new context')
    monkeypatch.setattr(Chunk, 'payload', changed)
    provider = Provider()
    with pytest.raises(ProcessError, match='несовместим'):
        extract_with_checkpoint(source(), provider, settings=SETTINGS, checkpoint=error.checkpoint)
    assert not provider.calls


def test_restored_false_quotes_are_rejected_by_existing_grounding():
    _, _, error = interrupted()
    checkpoint = deepcopy(error.checkpoint)
    action = checkpoint['results']['chunk-1']['procedures'][0]['actions'][0]
    action['evidence'][0]['quote'] = 'Несуществующая подтверждающая цитата.'
    provider = Provider()
    result = extract_with_checkpoint(source(), provider, settings=SETTINGS,
                                     checkpoint=load_checkpoint(dump_checkpoint(checkpoint)))
    assert provider.calls == ['chunk-3', 'chunk-4']
    assert result['status'] == 'completed_with_rejections'
    assert result['rejected_candidates'][0]['reason'] == 'quote_not_in_sent_fragment'
    assert len(result['procedures']) == 3


@pytest.mark.parametrize('mutation', ['unknown_block', 'missing_field', 'extra_secret',
                                     'empty_unexplained', 'wrong_id', 'wrong_version'])
def test_corrupt_checkpoint_not_used(mutation):
    _, _, error = interrupted()
    checkpoint = error.checkpoint
    if mutation == 'unknown_block':
        checkpoint['results']['chunk-999'] = checkpoint['results']['chunk-1']
    elif mutation == 'missing_field':
        del checkpoint['results']['chunk-1']['explanation']
    elif mutation == 'extra_secret':
        checkpoint['api_key'] = 'untrusted-uploaded-value'
    elif mutation == 'empty_unexplained':
        checkpoint['results']['chunk-1'] = {'procedures': [], 'explanation': ''}
    elif mutation == 'wrong_id':
        checkpoint['analysis_id'] = 'fake'
    else:
        checkpoint['schema_version'] = '99'
    provider = Provider()
    with pytest.raises(ProcessError) as caught:
        extract_with_checkpoint(source(), provider, settings=SETTINGS, checkpoint=checkpoint)
    assert 'untrusted-uploaded-value' not in str(caught.value)
    assert not provider.calls


@pytest.mark.parametrize('data', [b'{', b'null', b'[]', b'\xff', b'{"a": 1, "a": 2}'])
def test_invalid_json_is_safe(data):
    with pytest.raises(ProcessError):
        load_checkpoint(data)


def test_size_limit_and_no_secret_fields(monkeypatch):
    checkpoint = new_checkpoint(source(), Provider(), SETTINGS)
    data = dump_checkpoint(checkpoint)
    monkeypatch.setattr('govai.checkpoints.MAX_CHECKPOINT_BYTES', 50)
    with pytest.raises(ProcessError, match='64 МБ'):
        load_checkpoint(data)
    with pytest.raises(ProcessError, match='64 МБ'):
        dump_checkpoint(checkpoint)


def test_import_older_snapshot_keeps_newer_blocks_and_rejects_conflicts():
    _, snapshots, _ = interrupted()
    merged = combine_checkpoints(snapshots[-1], snapshots[-2])
    assert len(merged['results']) == 2
    snapshots[-2]['results']['chunk-1']['explanation'] = 'Другой ответ'
    with pytest.raises(ProcessError, match='другой ответ'):
        combine_checkpoints(merged, snapshots[-2])
    assert len(merged['results']) == 2


def test_failed_or_invalid_response_is_never_cached():
    snapshots = []
    class Invalid(Provider):
        def extract(self, payload):
            return {'procedures': [], 'explanation': ''}
    with pytest.raises(AnalysisInterrupted) as caught:
        extract_with_checkpoint(source(), Invalid(), settings=SETTINGS, on_checkpoint=snapshots.append)
    assert not caught.value.checkpoint['results']
    assert len(snapshots) == 1


def test_persistence_error_stops_requests_and_retains_in_memory_response():
    provider = Provider()
    def write(snapshot):
        if snapshot['results']:
            raise OSError('SENSITIVE_PATH')
    with pytest.raises(AnalysisInterrupted) as caught:
        extract_with_checkpoint(source(), provider, settings=SETTINGS, on_checkpoint=write)
    assert provider.calls == ['chunk-1']
    assert len(caught.value.checkpoint['results']) == 1
    assert 'SENSITIVE_PATH' not in str(caught.value)
