from pathlib import Path
from unittest.mock import patch

from streamlit.proto.Common_pb2 import FileURLs
from streamlit.runtime.uploaded_file_manager import UploadedFile, UploadedFileRec
from streamlit.testing.v1 import AppTest

from govai.processes import Candidates, ProcessError

APP = Path(__file__).resolve().parents[1] / 'app.py'


def upload(text):
    return UploadedFile(UploadedFileRec('test', 'document.txt', 'text/plain', text.encode()), FileURLs())


def button(app):
    return next(b for b in app.button if b.label == 'Извлечь процессы')


class Provider:
    name, model = 'mock', 'ui-test'

    def __init__(self, *args):
        pass

    def close(self):
        pass

    def extract(self, payload):
        fragment = payload['core'][0]
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
        assert 'OPENAI_API_KEY' in app.error[0].value
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
        assert any('Подтверждающие пункты' in e.label for e in app.expander)
        assert any(b.label == 'Скачать процессы JSON' for b in app.get('download_button'))
        assert any('пункт 1' in c.value for c in app.caption)
        app.run()
        assert app.session_state['process_result']['provider'] == 'mock'
    with patch('streamlit.file_uploader', return_value=upload('Другой документ.')):
        app.run()
        assert not app.exception
        assert 'process_result' not in app.session_state


def test_api_failure_clears_previous_result(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'ui-test')
    with patch('streamlit.file_uploader', return_value=upload('1. Исполнитель регистрирует обращение.\n')), \
         patch('govai.process_ui.OpenAIProvider', Provider):
        app = AppTest.from_file(str(APP)).run()
        button(app).click().run()
        assert 'process_result' in app.session_state
        with patch.object(Provider, 'extract', side_effect=ProcessError('Превышено время ожидания OpenAI.')):
            button(app).click().run()
        assert not app.exception
        assert 'Превышено время' in app.error[0].value
        assert 'process_result' not in app.session_state
