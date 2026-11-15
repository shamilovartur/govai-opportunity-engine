import pytest
from pathlib import Path
from unittest.mock import patch

from streamlit.proto.Common_pb2 import FileURLs
from streamlit.runtime.uploaded_file_manager import UploadedFile, UploadedFileRec
from streamlit.testing.v1 import AppTest

from govai.processes import Candidates, ProcessError
from govai.checkpoints import dump_checkpoint

APP = Path(__file__).resolve().parents[1] / 'app.py'


def upload(text):
    return UploadedFile(UploadedFileRec('test', 'document.txt', 'text/plain', text.encode()), FileURLs())

def button(app):
    return next(b for b in app.button if b.label in ('Извлечь процессы', 'Продолжить анализ'))


class Provider:
    name, model = 'mock', 'ui-test'
    prompt_version, prompt_sha256 = '1.0', 'ui-test-prompt'

    def __init__(self, *args):
        pass

    def close(self):
        pass

    def extract(self, payload):
        fragment = next(p for p in payload['core'] if p['clause_id'])
        evidence = {'fragment_id': fragment['fragment_id'], 'quote': fragment['text'].strip()}
        return Candidates.model_validate({'procedures': [{'title': 'Регистрация обращения',
            'anchor': evidence, 'actions': [{'description': 'Зарегистрировать обращение',
            'actor': None, 'inputs': [], 'outputs': [], 'conditions': [], 'dependencies': [],
            'evidence': [evidence]}]}], 'explanation': 'Mock UI test'})


def test_missing_key_visible_and_never_calls_provider(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with patch('streamlit.file_uploader', return_value=upload('1. Зарегистрировать обращение.\n')), \
         patch('streamlit.runtime.secrets.Secrets.to_dict', return_value={}), \
         patch('govai.process_ui.OpenAIProvider') as factory:
        app = AppTest.from_file(str(APP)).run()
        button(app).click().run()
        assert not app.exception
        assert 'Владельцу приложения' in app.error[0].value
        assert 'OPENAI_API_KEY' not in app.error[0].value and 'secrets.toml' not in app.error[0].value
        assert 'OPENAI_API_KEY' in app.session_state['process_error']
        factory.assert_not_called()
        assert 'process_result' not in app.session_state


def test_hierarchy_evidence_export_rerun_and_changed_document(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'ui-test')
    with patch('streamlit.file_uploader', return_value=upload('1. Исполнитель регистрирует обращение.\n')), \
         patch('govai.process_ui.OpenAIProvider', Provider):
        app = AppTest.from_file(str(APP)).run()
        button(app).click().run()
        assert not app.exception and not app.error
        assert len(app.session_state['process_result']['procedures']) == 1
        assert 'verified_analysis' in app.session_state
        timing = next(iter(app.session_state['process_run_metrics'].values()))
        assert timing['extraction_api_wait_seconds'] is None  # mock has no transport instrumentation
        assert timing['extraction_local_seconds'] is None
        assert any(x.value == 'Процессы готовы.' for x in app.success)
        assert not any('Зарегистрировать обращение' == t.value for t in app.text)
        assert any(b.label == 'Скачать процессы JSON' for b in app.get('download_button'))
        assert any(e['clause_number'] == '1' for e in app.session_state['verified_analysis'].evidence.values())
        app.run()
        assert app.session_state['process_result']['provider'] == 'mock'
    with patch('streamlit.file_uploader', return_value=upload('Другой документ.')):
        app.run()
        assert not app.exception
        assert 'process_result' not in app.session_state


def test_completed_checkpoint_skips_api_on_repeated_analysis(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'ui-test')
    with patch('streamlit.file_uploader', return_value=upload('1. Исполнитель регистрирует обращение.\n')), \
         patch('govai.process_ui.OpenAIProvider', Provider):
        app = AppTest.from_file(str(APP)).run()
        button(app).click().run()
        checkpoint=next(iter(app.session_state['process_checkpoints'].values()))
        data=dump_checkpoint(checkpoint)
    restored=UploadedFile(UploadedFileRec('checkpoint','saved.json','application/json',data),FileURLs())
    with patch('streamlit.file_uploader',side_effect=lambda label,**kw: restored if label=='Checkpoint JSON' else upload('1. Исполнитель регистрирует обращение.\n')), \
         patch('govai.process_ui.OpenAIProvider',Provider), \
         patch.object(Provider,'extract',side_effect=AssertionError('Repeated API call')) as extract:
        second=AppTest.from_file(str(APP)).run()
        next(b for b in second.button if b.label=='Восстановить checkpoint').click().run()
        button(second).click().run()
        extract.assert_not_called()
        assert not second.exception and not second.error
        assert second.session_state['process_result']['execution']['blocks_reused']==1
        timing = next(iter(second.session_state['process_run_metrics'].values()))
        assert timing['extraction_api_wait_seconds'] == 0
        assert timing['extraction_local_seconds'] == timing['extraction_seconds']


def test_partial_error_download_and_restore_in_fresh_session(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'ui-test')
    text = ''.join(f'Раздел {i}. Регистрация\n{i}. Исполнитель регистрирует обращение.\n\n'
                   for i in range(1, 4))
    class InterruptOnce(Provider):
        calls = []
        fail = True

        def extract(self, payload):
            self.calls.append(payload['chunk_id'])
            if payload['chunk_id'] == 'chunk-2' and InterruptOnce.fail:
                InterruptOnce.fail = False
                raise ProcessError('Временная ошибка OpenAI.')
            return super().extract(payload)

    document_upload = upload(text)
    with patch('streamlit.file_uploader', side_effect=lambda label, **kw:
               None if label == 'Checkpoint JSON' else document_upload), \
         patch('govai.process_ui.OpenAIProvider', InterruptOnce):
        first = AppTest.from_file(str(APP)).run()
        button(first).click().run()
        assert not first.exception
        assert 'Успешно обработано блоков: 1/3' in first.error[0].value
        assert 'process_result' not in first.session_state
        assert not any(b.label == 'Скачать процессы JSON' for b in first.get('download_button'))
        assert any(b.label == 'Скачать checkpoint JSON' for b in first.get('download_button'))
        checkpoint = next(iter(first.session_state['process_checkpoints'].values()))
        data = dump_checkpoint(checkpoint)

    restored_upload = UploadedFile(UploadedFileRec('checkpoint', 'saved.checkpoint.json',
                                  'application/json', data), FileURLs())
    with patch('streamlit.file_uploader', side_effect=lambda label, **kw:
               restored_upload if label == 'Checkpoint JSON' else document_upload), \
         patch('govai.process_ui.OpenAIProvider', InterruptOnce):
        second = AppTest.from_file(str(APP)).run()  # new session, no shared Streamlit state
        assert not second.session_state['process_checkpoints']
        next(b for b in second.button if b.label == 'Восстановить checkpoint').click().run()
        assert not second.exception and not second.error
        assert InterruptOnce.calls == ['chunk-1', 'chunk-2']
        assert 'process_result' not in second.session_state
        button(second).click().run()
        assert not second.exception and not second.error
        assert InterruptOnce.calls == ['chunk-1', 'chunk-2', 'chunk-2', 'chunk-3']
        result = second.session_state['process_result']
        assert result['coverage']['chunks_completed'] == 3
        assert result['execution']['blocks_reused'] == 1
        assert any(b.label == 'Скачать процессы JSON' for b in second.get('download_button'))
        second.run()  # uploaded older file is not automatically imported again
        assert len(next(iter(second.session_state['process_checkpoints'].values()))['results']) == 3
        next(b for b in second.button if b.label == 'Восстановить checkpoint').click().run()
        assert not second.exception
        assert len(next(iter(second.session_state['process_checkpoints'].values()))['results']) == 3
        second.run()
        assert not any(b.label in ('Извлечь процессы','Продолжить анализ') for b in second.button)
        assert InterruptOnce.calls == ['chunk-1', 'chunk-2', 'chunk-2', 'chunk-3']


def test_incompatible_import_rejected_without_api_or_state_replacement(monkeypatch):
    from govai.checkpoints import new_checkpoint
    from govai.extraction import extract_document
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'ui-test')
    wrong = new_checkpoint(extract_document('different.txt', 'Другой документ.'.encode()), Provider())
    uploaded = UploadedFile(UploadedFileRec('wrong', 'wrong.json', 'application/json',
                            dump_checkpoint(wrong)), FileURLs())
    with patch('streamlit.file_uploader', side_effect=lambda label, **kw:
               uploaded if label == 'Checkpoint JSON' else upload('1. Исполнитель регистрирует обращение.\n')), \
         patch('govai.process_ui.OpenAIProvider', Provider), \
         patch.object(Provider, 'extract', side_effect=AssertionError('Must not call API')) as api:
        app = AppTest.from_file(str(APP)).run()
        next(b for b in app.button if b.label == 'Восстановить checkpoint').click().run()
        assert not app.exception
        assert 'несовместим' in app.error[0].value
        assert not app.session_state['process_checkpoints']
        api.assert_not_called()


@pytest.fixture(autouse=True)
def mock_normalization_stage():
    from test_map_ui import AutomaticNormalizer
    AutomaticNormalizer.calls_total=0
    with patch('govai.map_ui.OpenAINormalizer',AutomaticNormalizer):
        yield


def test_checkpoint_download_is_registered_before_api_and_reads_latest_snapshot(monkeypatch):
    import json
    import streamlit as st
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'ui-test')
    snapshots = []; downloads = []
    original_download = st.download_button
    def download(label, data, **kwargs):
        if label == 'Скачать checkpoint JSON': downloads.append(data)
        return original_download(label, data, **kwargs)
    class InspectProvider(Provider):
        def extract(self, payload):
            assert downloads, 'Checkpoint download must exist while the analysis is still running'
            snapshots.append(json.loads(downloads[0]())['results'])
            return super().extract(payload)
    text = 'Раздел 1. Приём\n1. Исполнитель регистрирует обращение.\n\nРаздел 2. Ответ\n2. Исполнитель готовит ответ.'
    with patch('streamlit.file_uploader', return_value=upload(text)), \
         patch('streamlit.download_button', side_effect=download), \
         patch('govai.process_ui.OpenAIProvider', InspectProvider):
        app = AppTest.from_file(str(APP)).run()
        button(app).click().run()
        assert not app.exception and not app.error
        assert [len(snapshot) for snapshot in snapshots] == [0, 1]
        assert len(json.loads(downloads[0]())['results']) == 2


def test_running_analysis_rerender_has_no_second_launch(monkeypatch):
    import time
    from types import SimpleNamespace
    from govai.checkpoints import new_checkpoint
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'ui-test')
    with patch('streamlit.file_uploader', return_value=upload('1. Исполнитель регистрирует обращение.\n')), \
         patch('govai.process_ui.OpenAIProvider', Provider), \
         patch.object(Provider, 'extract', side_effect=AssertionError('No duplicate call')) as api:
        app = AppTest.from_file(str(APP)).run()
        document = app.session_state['document_read_cache']['document']
        descriptor = SimpleNamespace(name=Provider.name, model='ui-test',
            prompt_version=Provider.prompt_version, prompt_sha256=Provider.prompt_sha256)
        key = new_checkpoint(document, descriptor)['analysis_id']
        app.session_state['process_in_flight'][key] = {'owner': 'first', 'started': time.perf_counter()-7,
            'stage': 'Извлекаем процессы из документа', 'completed_blocks': 0, 'total_blocks': 1}
        app.run()
        assert not app.exception
        assert any('Повторный запрос не отправляется' in v.value for v in app.info)
        assert not any(b.label in ('Извлечь процессы', 'Продолжить анализ') for b in app.button)
        api.assert_not_called()


def test_document_change_during_extraction_preserves_response_without_wrong_attachment(monkeypatch):
    from contextlib import nullcontext
    from unittest.mock import Mock
    import streamlit as st
    from govai.extraction import extract_document
    from govai.process_ui import _run_analysis
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'ui-test')
    doc = extract_document('first.txt', '1. Исполнитель регистрирует обращение.'.encode())
    state = {'process_document': ('new-document',)}
    result = {'procedures': [], 'document_sha256': 'saved-old-document'}
    with patch.object(st, 'session_state', state), patch.object(st, 'secrets', {}), \
         patch('govai.process_ui.configuration', return_value=('unit-test-placeholder', 'ui-test')), \
         patch('govai.process_ui.OpenAIProvider', Provider), \
         patch('govai.process_ui.extract_with_checkpoint', return_value=result), \
         patch('govai.process_ui.normalize_processes', side_effect=AssertionError('Document changed')) as normalize, \
         patch.object(st, 'spinner', return_value=nullcontext()), patch.object(st, 'progress', return_value=Mock()):
        # No real model request: this checks the response-saving boundary after an existing request.
        with patch.object(st, 'secrets', Mock(to_dict=lambda: {})):
            _run_analysis(doc, {}, 'run', None, lambda x: None,
                identity=('old-document',), run_key='old-compatible-run', run={})
    assert state['completed_process_runs']['old-compatible-run'] == result
    assert 'process_result' not in state
    normalize.assert_not_called()


def test_diagnostics_show_version_and_config_state_without_secret(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'never-display-this-test-key')
    monkeypatch.setenv('OPENAI_MODEL', 'ui-test')
    with patch('streamlit.file_uploader', return_value=upload('1. Исполнитель регистрирует обращение.\n')), \
         patch('govai.process_ui.OpenAIProvider') as provider:
        app = AppTest.from_file(str(APP)).run()
        tech = next(e for e in app.expander if e.label == 'Технические данные')
        assert not tech.proto.expanded
        captions = '\n'.join(c.value for c in tech.caption)
        assert 'Версия приложения:' in captions and 'Ветка:' in captions
        assert 'Настройки' in captions or 'настройки анализа: заполнены' in captions
        assert 'ui-test' in captions
        assert 'never-display-this-test-key' not in captions
        provider.assert_not_called()


def test_request_wait_includes_failed_call_once_and_excludes_checkpoint_reuse(monkeypatch):
    from itertools import count
    from types import SimpleNamespace
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'ui-test')
    text = ''.join(f'Раздел {i}. Регистрация\n{i}. Исполнитель регистрирует обращение.\n\n' for i in range(1,4))
    class TimedProvider(Provider):
        calls = []
        fail = True
        def extract(self, payload):
            self.calls.append(payload['chunk_id'])
            self.last_call_elapsed_seconds = 0.05
            if payload['chunk_id'] == 'chunk-2' and type(self).fail:
                type(self).fail = False
                raise ProcessError('Временная ошибка сервиса.')
            return super().extract(payload)
    clock = count(1)
    with patch('streamlit.file_uploader', return_value=upload(text)), \
         patch('govai.process_ui.OpenAIProvider', TimedProvider), \
         patch('govai.process_ui.time', SimpleNamespace(perf_counter=lambda: next(clock))):
        app = AppTest.from_file(str(APP)).run()
        button(app).click().run()
        assert not app.exception and app.error
        first = next(iter(app.session_state['process_run_metrics'].values())).copy()
        assert first['extraction_api_wait_seconds'] == pytest.approx(0.1)
        assert first['extraction_local_seconds'] == pytest.approx(first['extraction_seconds']-0.1)
        assert 'normalization_seconds' not in first
        button(app).click().run()
        assert not app.exception and not app.error
        resumed = next(iter(app.session_state['process_run_metrics'].values())).copy()
        assert resumed['extraction_api_wait_seconds'] == pytest.approx(0.1)
        assert resumed['extraction_local_seconds'] == pytest.approx(resumed['extraction_seconds']-0.1)
        assert resumed['normalization_api_wait_seconds'] is None
        assert resumed['normalization_local_seconds'] is None
        assert TimedProvider.calls == ['chunk-1','chunk-2','chunk-2','chunk-3']
        app.run()
        assert next(iter(app.session_state['process_run_metrics'].values())) == resumed
        assert len(TimedProvider.calls) == 4
