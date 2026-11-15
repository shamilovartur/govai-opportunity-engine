import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from streamlit.proto.Common_pb2 import FileURLs
from streamlit.runtime.uploaded_file_manager import UploadedFile, UploadedFileRec
from streamlit.testing.v1 import AppTest

from govai.normalization import normalize_analysis
from govai.analysis_io import verify_analysis
from govai.processes import ProcessError
from govai.normalization import markdown_report
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
         patch('govai.map_ui.OpenAINormalizer', UIProvider), \
         patch('govai.map_ui.markdown_report', wraps=markdown_report) as export_markdown, \
         patch('govai.map_ui.json', SimpleNamespace(dumps=Mock(wraps=json.dumps))) as export_json:
        app = AppTest.from_file(str(APP)).run()
        button(app, 'Импортировать результат').click().run()
        assert not app.exception and not app.error
        assert app.session_state['process_origin'] == 'imported'
        assert app.session_state['process_result_bytes'] == original
        assert UIProvider.calls_total == 0
        button(app, 'Продолжить анализ').click().run()
        assert not app.exception and not app.error
        assert UIProvider.calls_total == 1
        assert not app.text_area
        assert not any('Секретарь регистрирует заявление' in t.value for t in app.text)
        assert any(x.value == 'Процессы готовы.' for x in app.success)
        assert not any(s.label == 'Раздел карты' for s in app.selectbox)
        next(c for c in app.checkbox if c.label == 'Показать карту и доказательства').check().run()
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
        assert export_markdown.call_count == export_json.dumps.call_count == 1
        downloads = next(iter(app.session_state['normalized_downloads'].values()))
        assert isinstance(downloads['json'], bytes) and isinstance(downloads['markdown'], bytes)
        assert json.loads(downloads['json']) == downloads['artifact']
        assert downloads['markdown'].decode() == markdown_report(downloads['artifact'],
            app.session_state['verified_analysis'].raw['metadata'])


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
        assert any(s.value == 'Процессы готовы.' for s in app.success)
        assert not any(s.label == 'Выберите процесс' for s in app.selectbox)
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
        button(app, 'Продолжить анализ').click().run()
        assert not app.exception
        assert 'Анализ не завершён' in app.error[0].value
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
        button(app, 'Продолжить анализ').click().run()
        assert not app.exception
        assert 'Анализ не завершён' in app.error[0].value and 'Владельцу приложения' in app.error[0].value
        assert 'OPENAI_API_KEY' not in app.error[0].value
        assert app.session_state['process_result_bytes'] == original
        factory.assert_not_called()


class AutomaticNormalizer:
    """A deterministic mock response for UI sequencing, never a live model."""
    name='mock'
    calls_total=0
    def __init__(self, key, model): self.model=model
    def close(self): pass
    def normalize(self, payload, **kwargs):
        from types import SimpleNamespace
        type(self).calls_total += 1
        a=SimpleNamespace(actions={x['id']:x for x in payload['actions']})
        return plan_for(a,[node(a,[sid],key=f'op-{i}',title=x['description'])
                           for i,(sid,x) in enumerate(a.actions.items())],titles={'p1':'Работа с обращением'}),None


def test_partial_map_is_preliminary_and_names_affected_operation_without_api(monkeypatch):
    analysis, plan, _, _ = setup_inputs()
    partial = plan.model_copy(update={'actions': plan.actions[:1]})
    artifact = normalize_analysis(analysis, Normalizer(partial))
    assert artifact['map']['status'] == 'incomplete'
    _, _, _, uploader = setup_inputs(json.dumps(artifact).encode())
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    with patch('streamlit.file_uploader', side_effect=uploader), \
         patch('govai.map_ui.OpenAINormalizer', side_effect=AssertionError('No model call')):
        app = AppTest.from_file(str(APP)).run()
        button(app, 'Импортировать результат').click().run()
        button(app, 'Восстановить карту').click().run()
    assert not app.exception
    assert any(v.value == 'Предварительный список процессов готов.' for v in app.info)
    assert not any(v.value == 'Процессы готовы.' for v in app.success)
    assert any('Исполнитель направляет ответ.' in v.value for v in app.warning)
    assert any('до количественной оценки эффекта' in v.value for v in app.warning)


@pytest.mark.parametrize('failed', [False, True])
def test_normalization_request_wait_and_local_time_are_observed_without_changing_saved_map(monkeypatch, failed):
    from itertools import count
    from types import SimpleNamespace
    _, plan, original, uploader = setup_inputs()
    class Timed(UIProvider):
        prepared_plan = plan
        def normalize(self, payload, **kw):
            self.last_call_elapsed_seconds = 0.25
            if failed:
                raise ProcessError('Не получен полный ответ модели.')
            return super().normalize(payload, **kw)
    clock = count(1)
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    with patch('streamlit.file_uploader', side_effect=uploader), \
         patch('govai.map_ui.OpenAINormalizer', Timed), \
         patch('govai.map_ui.time', SimpleNamespace(perf_counter=lambda: next(clock))):
        app = AppTest.from_file(str(APP)).run()
        button(app, 'Импортировать результат').click().run()
        assert 'process_run_metrics' not in app.session_state
        button(app, 'Продолжить анализ').click().run()
        assert not app.exception
        timing = next(iter(app.session_state['process_run_metrics'].values()))
        assert 'extraction_seconds' not in timing
        assert timing['normalization_api_wait_seconds'] == 0.25
        assert timing['normalization_local_seconds'] == timing['normalization_seconds']-0.25
        assert app.session_state['process_result_bytes'] == original
        if failed:
            assert app.error
        else:
            saved = next(iter(app.session_state['normalized_maps'].values()))
            assert 'api_wait_seconds' not in saved['metrics']  # presentation/session timing does not rewrite artifacts


def test_request_timing_never_invents_unknown_local_processing():
    from govai.map_ui import request_timing
    for missing in (None, float('nan'), float('inf'), -1.0, 3.0, True):
        assert request_timing(2.0, missing) == {'api_wait_seconds': None, 'local_seconds': None}
    assert request_timing(2.0, 1.5) == {'api_wait_seconds': 1.5, 'local_seconds': 0.5}


@pytest.mark.parametrize('failed', [False, True])
def test_explicit_normalization_retry_blocks_reentry_and_stale_click_after_success_or_failure(monkeypatch, failed):
    from govai.map_ui import normalize_processes
    analysis, plan, _, _ = setup_inputs()
    artifact = normalize_analysis(analysis, Normalizer(plan))
    _, _, _, uploader = setup_inputs(json.dumps(artifact).encode())
    class Reentry(UIProvider):
        prepared_plan = plan
        calls_total = 0
        def normalize(self, payload, **kw):
            type(self).calls_total += 1
            # A concurrent explicit click must not create a second provider/call.
            with pytest.raises(ProcessError, match='уже выполняются'):
                normalize_processes(analysis)
            if failed:
                raise ProcessError('Неполный ответ нормализатора.')
            return Normalizer.normalize(self, payload, **kw)
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    with patch('streamlit.file_uploader', side_effect=uploader), \
         patch('govai.map_ui.OpenAINormalizer', Reentry), \
         patch('govai.process_ui.extract_with_checkpoint', side_effect=AssertionError('No extraction')):
        app = AppTest.from_file(str(APP)).run()
        button(app, 'Импортировать результат').click().run()
        button(app, 'Восстановить карту').click().run()
        stale = button(app, 'Повторить только нормализацию')
        old_key = stale.key
        stale.click().run()
        assert not app.exception
        assert Reentry.calls_total == 1
        assert not app.session_state['normalization_in_flight']
        # The old result remains available even after a failed explicit refresh.
        assert app.session_state['normalized_maps']
        app.run()
        assert button(app, 'Повторить только нормализацию').key != old_key
        stale.click().run()
        assert not app.exception and Reentry.calls_total == 1
