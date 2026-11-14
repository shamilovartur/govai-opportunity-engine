import json
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.proto.Common_pb2 import FileURLs
from streamlit.runtime.uploaded_file_manager import UploadedFile, UploadedFileRec
from streamlit.testing.v1 import AppTest

from govai.normalization import normalize_analysis
from govai.analysis_io import verify_analysis
from govai.processes import ProcessError
from test_normalization import Normalizer, node, plan_for, simple_case

APP = Path(__file__).resolve().parents[1] / 'app.py'


def upload(name, data):
    return UploadedFile(UploadedFileRec(name, name, 'application/octet-stream', data), FileURLs())


def button(app, label):
    return next(b for b in app.button if b.label == label)


def setup_inputs(normalized=None):
    doc, raw, analysis = simple_case(('Секретарь регистрирует заявление.', 'Исполнитель направляет ответ.'))
    raw['generated_at'] = '2026-11-14T00:00:00+00:00'
    ids = list(analysis.actions)
    plan = plan_for(analysis, [node(analysis, [ids[0]], key='register', process='receipt', title='Регистрация'),
                              node(analysis, [ids[1]], key='reply', process='dispatch', title='Направление ответа')],
                    titles={'receipt': 'Приём обращения', 'dispatch': 'Выдача результата'})
    data = json.dumps(raw, ensure_ascii=False, indent=4).encode()  # preserve exact formatting on download
    analysis = verify_analysis(data, doc)
    def uploader(label, **kw):
        if label == 'Выберите документ': return upload('test.txt', doc['text'].encode())
        if label == 'Готовый processes.json': return upload('processes.json', data)
        if label == 'Нормализованный normalized_processes.json' and normalized:
            return upload('normalized_processes.json', normalized)
        return None
    return analysis, plan, data, uploader


class UIProvider(Normalizer):
    prepared_plan = None
    calls_total = 0
    def __init__(self, *args):
        super().__init__(self.prepared_plan)
    def normalize(self, payload, **kwargs):
        type(self).calls_total += 1
        return super().normalize(payload, **kwargs)
    def close(self):
        pass


def test_import_normalize_navigation_and_conditional_evidence_never_repeat_api(monkeypatch):
    _, plan, original, uploader = setup_inputs()
    UIProvider.prepared_plan, UIProvider.calls_total = plan, 0
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    with patch('streamlit.file_uploader', side_effect=uploader), \
         patch('govai.process_ui.extract_with_checkpoint', side_effect=AssertionError('No extraction')) as extract, \
         patch('govai.map_ui.OpenAINormalizer', UIProvider):
        app = AppTest.from_file(str(APP)).run()
        button(app, 'Импортировать результат').click().run()
        assert not app.exception and not app.error
        assert app.session_state['process_origin'] == 'imported'
        assert app.session_state['process_result_bytes'] == original
        assert UIProvider.calls_total == 0
        button(app, 'Построить карту процессов').click().run()
        assert not app.exception and not app.error
        assert UIProvider.calls_total == 1
        assert not app.text_area
        assert not any('Секретарь регистрирует заявление' in t.value for t in app.text)
        picker = next(s for s in app.selectbox if s.label == 'Выберите процесс')
        picker.set_value('dispatch').run()
        next(c for c in app.checkbox if c.label == 'Показать детали действия').check().run()
        assert any('Исполнитель направляет ответ' in t.value for t in app.text)
        assert not any('Секретарь регистрирует заявление' in t.value for t in app.text)
        next(c for c in app.checkbox if c.label == 'Показать подтверждающие цитаты').check().run()
        assert any('Пункт 2' in c.value for c in app.caption)
        for b in app.get('download_button'):
            assert b.proto.ignore_rerun  # real downloads are additionally exercised in Chromium
        app.run()
        assert UIProvider.calls_total == 1
        assert app.session_state['process_result_bytes'] == original
        extract.assert_not_called()


def test_restore_normalized_in_new_session_with_no_key_and_no_api(monkeypatch):
    a, plan, _, _ = setup_inputs()
    artifact = normalize_analysis(a, Normalizer(plan))
    _, _, _, uploader = setup_inputs(json.dumps(artifact).encode())
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    with patch('streamlit.file_uploader', side_effect=uploader), \
         patch('govai.map_ui.OpenAINormalizer', side_effect=AssertionError('No paid calls')) as normalize, \
         patch('govai.process_ui.extract_with_checkpoint', side_effect=AssertionError('No extraction')):
        app = AppTest.from_file(str(APP)).run()
        button(app, 'Импортировать результат').click().run()
        button(app, 'Восстановить карту').click().run()
        assert not app.exception and not app.error
        assert app.session_state['normalized_origins'][artifact['normalization_id']] == 'imported'
        assert any(s.label == 'Выберите процесс' for s in app.selectbox)
        app.run()
        normalize.assert_not_called()


def test_normalizer_error_preserves_original_and_does_not_request_extraction(monkeypatch):
    _, _, original, uploader = setup_inputs()
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    class Broken(UIProvider):
        def normalize(self, payload, **kw):
            raise ProcessError('Неполный ответ нормализатора.')
    with patch('streamlit.file_uploader', side_effect=uploader), patch('govai.map_ui.OpenAINormalizer', Broken), \
         patch('govai.process_ui.extract_with_checkpoint', side_effect=AssertionError('No extraction')):
        app = AppTest.from_file(str(APP)).run()
        button(app, 'Импортировать результат').click().run()
        button(app, 'Построить карту процессов').click().run()
        assert not app.exception
        assert 'Ошибка второго этапа' in app.error[0].value
        assert app.session_state['process_result_bytes'] == original
        assert any(b.label == 'Скачать процессы JSON' for b in app.get('download_button'))
        assert not any(b.label == 'Скачать normalized_processes.json' for b in app.get('download_button'))
        assert next(iter(app.session_state['normalization_failures'].values()))['requests'] == 1


def test_missing_key_for_normalization_is_not_a_request_to_repeat_extraction(monkeypatch):
    _, _, original, uploader = setup_inputs()
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with patch('streamlit.file_uploader', side_effect=uploader), \
         patch('streamlit.runtime.secrets.Secrets.to_dict', return_value={}), \
         patch('govai.map_ui.OpenAINormalizer') as factory:
        app = AppTest.from_file(str(APP)).run()
        button(app, 'Импортировать результат').click().run()
        button(app, 'Построить карту процессов').click().run()
        assert not app.exception
        assert 'второго этапа' in app.error[0].value and 'OPENAI_API_KEY' in app.error[0].value
        assert app.session_state['process_result_bytes'] == original
        factory.assert_not_called()
