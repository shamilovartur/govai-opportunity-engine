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
    return next(b for b in app.button if b.label in ('Проанализировать документ', 'Продолжить анализ'))

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
        assert not any(b.label in ('Проанализировать документ', 'Продолжить анализ') for b in app.button)
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

@pytest.fixture(autouse=True)
def mock_remaining_stages():
    from test_map_ui import AutomaticNormalizer
    from opportunity_helpers import OpportunityMock, proposal
    from test_opportunities import plan
    from govai.opportunities import generate_opportunities
    from govai.opportunity_ui import opportunity_state
    import streamlit as st
    def finish(normalized,analysis):
        ids=[a['id'] for a in normalized['map']['actions'] if a['kind']=='operation']
        result=generate_opportunities(normalized,analysis,OpportunityMock(plan(proposal(normalized,ids[:1])) if ids else plan()))
        key,_,_,reports=opportunity_state(normalized);reports[key]=result
        return result
    with patch('govai.map_ui.OpenAINormalizer',AutomaticNormalizer), patch('govai.process_ui.prepare_opportunity_report',side_effect=finish):
        yield


def test_single_button_sequences_stages_and_document_change_clears_report(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL','ui-test')
    current=[upload('1. Исполнитель регистрирует обращение.')]
    calls=[]
    class Counted(Provider):
        def extract(self,payload):
            calls.append(payload['chunk_id']);return super().extract(payload)
    with patch('streamlit.file_uploader',side_effect=lambda *a,**kw:current[0]),patch('govai.process_ui.OpenAIProvider',Counted):
        app=AppTest.from_file(str(APP)).run()
        assert [b.label for b in app.button]==['Проанализировать документ']
        button(app).click().run()
        assert not app.exception and not app.error
        assert app.session_state['process_checkpoints'] and app.session_state['normalized_maps'] and app.session_state['opportunity_reports']
        assert len(calls)==1 and not app.button
        assert len(app.get('download_button'))==2
        assert not app.json and not app.dataframe and not app.text_area
        app.run();assert len(calls)==1
        current[0]=upload('1. Другой документ с другой операцией.')
        app.run()
        assert not app.get('download_button')
        assert 'process_result' not in app.session_state
        assert [b.label for b in app.button]==['Проанализировать документ']
        assert len(calls)==1


def test_completed_checkpoint_can_resume_internally_without_extraction(monkeypatch):
    from govai.extraction import extract_document
    from govai.checkpoints import extract_with_checkpoint,load_checkpoint
    document=extract_document('test.txt','1. Исполнитель регистрирует обращение.'.encode())
    snapshots=[]
    first=extract_with_checkpoint(document,Provider(),on_checkpoint=snapshots.append)
    restored=load_checkpoint(dump_checkpoint(snapshots[-1]))
    with patch.object(Provider,'extract',side_effect=AssertionError('No repeat request')):
        second=extract_with_checkpoint(document,Provider(),checkpoint=restored)
    assert second['execution']['blocks_reused']==1
    assert first['procedures']==second['procedures']


def test_public_screen_has_no_diagnostics_or_secret(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','never-display-this-test-key')
    monkeypatch.setenv('OPENAI_MODEL','ui-test')
    with patch('streamlit.file_uploader',return_value=upload('1. Исполнитель регистрирует обращение.')):
        app=AppTest.from_file(str(APP)).run()
    text=' '.join(str(e.value) for group in (app.markdown,app.caption,app.info) for e in group)
    assert all(t not in text for t in ('never-display-this-test-key','ui-test','Версия приложения','Ветка:'))
    assert not any(e.label=='Технические данные' for e in app.expander)
    assert all(w.proto.label=='Выберите документ' for w in app.get('file_uploader'))
