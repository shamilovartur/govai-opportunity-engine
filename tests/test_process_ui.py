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
    return next(b for b in app.button if b.label == 'Извлечь процессы')


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


def test_completed_checkpoint_skips_api_on_repeated_analysis(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'ui-test')
    with patch('streamlit.file_uploader', return_value=upload('1. Исполнитель регистрирует обращение.\n')), \
         patch('govai.process_ui.OpenAIProvider', Provider):
        app = AppTest.from_file(str(APP)).run()
        button(app).click().run()
        assert 'process_result' in app.session_state
        with patch.object(Provider, 'extract', side_effect=AssertionError('Repeated API call')) as extract:
            button(app).click().run()
            extract.assert_not_called()
        assert not app.exception
        assert not app.error
        assert app.session_state['process_result']['execution']['blocks_reused'] == 1


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
        button(second).click().run()
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
