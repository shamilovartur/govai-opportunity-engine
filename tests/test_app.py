from pathlib import Path
from hashlib import sha256
from io import BytesIO
import re
from zipfile import ZipFile
from unittest.mock import patch

import pytest
from streamlit.proto.Common_pb2 import FileURLs
from streamlit.runtime.uploaded_file_manager import UploadedFile, UploadedFileRec
from streamlit.testing.v1 import AppTest

from govai.url_input import URLInputError, _html_document

APP = Path(__file__).resolve().parents[1] / "app.py"


def technical(app, fragments=False):
    toggle = next((c for c in app.checkbox if c.label == 'Показать исходный текст и структуру'), None)
    if toggle:
        toggle.check().run(timeout=15)
    fragment_toggle = next((c for c in app.checkbox if c.label == 'Показать фрагменты и ссылки на пункты'), None)
    if fragments and fragment_toggle:
        fragment_toggle.check().run(timeout=15)
    return app


def run_with_upload(filename, data):
    # AppTest does not expose file-uploader interaction. Supply only the upload;
    # the actual parser and all rendered app elements run unchanged.
    upload = UploadedFile(
        UploadedFileRec("test-file", filename, "application/octet-stream", data),
        FileURLs(),
    )
    with patch("streamlit.file_uploader", return_value=upload):
        return technical(AppTest.from_file(str(APP)).run(timeout=15), fragments=True)


def test_initial_screen():
    app = AppTest.from_file(str(APP)).run(timeout=15)
    assert not app.exception
    assert app.title[0].value == 'Поиск вариантов для внедрения AI'
    assert any(item.value == 'Загрузите документ в удобном формате. Агент изучит рабочий процесс '
               'и предложит рекомендации по оптимизации' for item in app.markdown)
    assert app.radio[0].label == 'Способ загрузки'
    assert not app.subheader
    assert not app.expander
    assert [b.label for b in app.get('download_button')] == ['Скачать готовый отчёт (PDF, 6 страниц)', 'Скачать текст НПА (TXT)']
    assert all(b.proto.ignore_rerun for b in app.get('download_button'))
    assert not any('Скан без распознанного текста' in item.value for item in app.caption)
    uploader = app.get('file_uploader')[0].proto
    assert uploader.label == 'Выберите документ' and uploader.max_upload_size_mb == 10
    assert not uploader.help
    assert list(uploader.type) == ['.pdf', '.docx', '.txt']
    assert not app.text_area


def test_link_mode_uses_one_open_input_and_start_action():
    app = AppTest.from_file(str(APP)).run(timeout=15)
    app.radio[0].set_value('Загрузить по ссылке').run()
    assert app.text_input[0].label == 'Ссылка на документ'
    assert [b.label for b in app.button] == ['Начать']
    assert not app.expander and not app.get('file_uploader')
    assert not app.caption


def test_txt_upload_displays_text():
    app = run_with_upload("example.txt", "Тестовый документ".encode())
    assert not app.exception
    assert app.success
    assert app.session_state['document_read_cache']['document']['text'] == "Тестовый документ"


def test_pdf_upload_displays_text(pdf_bytes):
    app = run_with_upload("example.pdf", pdf_bytes)
    assert not app.exception
    assert "First page: procurement" in app.session_state['document_read_cache']['document']['text']
    assert "Second page: deadline" in app.session_state['document_read_cache']['document']['text']


def test_docx_upload_displays_text(docx_bytes):
    app = run_with_upload("example.docx", docx_bytes)
    assert not app.exception
    assert "Условия закупки" in app.session_state['document_read_cache']['document']['text']
    assert "30 ноября" in app.session_state['document_read_cache']['document']['text']


@pytest.mark.parametrize("filename,data", [("broken.pdf", b"broken"), ("empty.txt", b"")])
def test_invalid_upload_displays_error(filename, data):
    app = run_with_upload(filename, data)
    assert not app.exception
    assert app.error
    assert not app.text_area


def test_whitespace_document_displays_warning():
    app = run_with_upload("empty.txt", b" \n\t")
    assert not app.exception
    assert "не найден текст" in app.warning[0].value
    assert not app.text_area


def test_real_npa_structure_is_visible():
    sample = APP.parent / 'examples/rosimushchestvo_261/source.txt'
    app = run_with_upload('source.txt', sample.read_bytes())
    assert not app.exception
    assert any(h.value == 'Приказ № 261' for h in app.subheader)
    assert any('30.05.2023' in c.value for c in app.caption)
    assert not app.dataframe and not app.text_area
    assert any(c['marker'] == '2.6.' for c in app.session_state['document_read_cache']['document']['clauses'])


def test_unrecognized_structure_preserves_text_in_ui():
    app = run_with_upload('ordinary.txt', 'Обычный текст без нумерации.'.encode())
    assert not app.exception
    assert not app.text_area
    assert 'numbered_clauses_not_recognized' in app.session_state['document_read_cache']['document']['warnings']
    assert app.session_state['document_read_cache']['document']['text'] == 'Обычный текст без нумерации.'


def url_result():
    html = (APP.parent / 'tests/fixtures/npa-page.html').read_bytes()
    doc = _html_document(html, 'https://public.example.org/doc', 'text/html')
    doc['source'].update(type='url_html', original_url='https://public.example.org/doc',
                         final_url='https://public.example.org/doc',
                         retrieved_at='2026-11-14T00:00:00+00:00')
    return doc


def test_url_mode_loads_html_and_does_not_refetch_on_rerun():
    app = AppTest.from_file(str(APP)).run(timeout=15)
    app.radio[0].set_value('Загрузить по ссылке').run()
    app.text_input[0].set_value('https://public.example.org/doc').run()
    with patch('govai.url_input.extract_url_document', return_value=url_result()) as fetch:
        app.button[0].click().run()
        assert not app.exception
        technical(app)
        assert 'Заявитель направляет заявление' in app.session_state['url_document']['text']
        assert 'Полнота НПА автоматически не подтверждена' in app.warning[0].value
        assert not app.get('download_button')
        app.run()
        fetch.assert_called_once_with('https://public.example.org/doc')


def test_url_change_and_failed_retry_clear_old_document():
    app = AppTest.from_file(str(APP)).run(timeout=15)
    app.radio[0].set_value('Загрузить по ссылке').run()
    app.text_input[0].set_value('https://public.example.org/doc').run()
    with patch('govai.url_input.extract_url_document', return_value=url_result()):
        app.button[0].click().run()
    technical(app)
    assert 'url_document' in app.session_state
    app.text_input[0].set_value('https://public.example.org/other').run()
    assert not app.text_area
    with patch('govai.url_input.extract_url_document', side_effect=URLInputError('HTTP 403: доступ запрещён')):
        app.button[0].click().run()
    assert not app.exception
    assert '403' in app.error[0].value
    assert not app.text_area
    assert [b.label for b in app.get('download_button')] == ['Скачать готовый отчёт (PDF, 6 страниц)', 'Скачать текст НПА (TXT)']


def test_url_mode_blocks_local_address_without_network():
    app = AppTest.from_file(str(APP)).run(timeout=15)
    app.radio[0].set_value('Загрузить по ссылке').run()
    app.text_input[0].set_value('https://127.0.0.1/private').run()
    app.button[0].click().run()
    assert not app.exception
    assert 'Локальные и внутренние адреса запрещены' in app.error[0].value
    assert not app.text_area


def test_file_reading_is_cached_per_file_content_and_encoding():
    from govai.extraction import extract_document
    value = UploadedFile(UploadedFileRec('source', 'source.txt', 'text/plain',
        'Документ с русским текстом.'.encode()), FileURLs())
    with patch('streamlit.file_uploader', return_value=value), \
         patch('govai.extraction.extract_document', wraps=extract_document) as read:
        app = AppTest.from_file(str(APP)).run(timeout=15)
        assert read.call_count == 1
        app.run()
        technical(app)
        assert read.call_count == 1
        assert not app.expander and not app.selectbox
    changed = UploadedFile(UploadedFileRec('source', 'source.txt', 'text/plain',
        'Другой текст того же файла.'.encode()), FileURLs())
    with patch('streamlit.file_uploader', return_value=changed), \
         patch('govai.extraction.extract_document', wraps=extract_document) as read:
        app.run()
        assert read.call_count == 1


def test_corrupt_file_is_not_reparsed_on_navigation():
    from govai.extraction import extract_document
    value = UploadedFile(UploadedFileRec('broken', 'broken.pdf', 'application/pdf', b'broken'), FileURLs())
    with patch('streamlit.file_uploader', return_value=value), \
         patch('govai.extraction.extract_document', wraps=extract_document) as read:
        app = AppTest.from_file(str(APP)).run(timeout=15)
        app.run()
        assert read.call_count == 1
        assert 'Попробуйте другую копию PDF' in app.error[0].value
        assert 'process_result' not in app.session_state


def test_document_requisites_unknown_and_full_title_not_truncated():
    app = run_with_upload('ordinary.txt', 'Простой текст без реквизитов.'.encode())
    assert any(h.value == 'ordinary.txt' for h in app.subheader)
    assert not any('не определено №' in h.value for h in app.subheader)
    assert any('Редакция: не определена' in c.value for c in app.caption)
    source = APP.parent/'examples/rosimushchestvo_261/source.txt'
    real = run_with_upload('source.txt', source.read_bytes())
    assert not any(e.label == 'Полное название документа' for e in real.expander)
    assert 'без проведения торгов' in real.session_state['document_read_cache']['document']['metadata']['title']['value']



@pytest.mark.parametrize('message,expected', [
    ('Превышено время ожидания OpenAI. Повторите попытку позже.', 'не ответил за отведённое время'),
    ('OpenAI вернул пустой ответ или JSON, не соответствующий схеме.', 'неполный или некорректный ответ'),
    ('OpenAI вернул незавершённый ответ (возможен лимит вывода).', 'не выдан за готовый результат'),
    ('OpenAI отклонил запрос. Проверьте поддержку Structured Outputs и размер контекста модели.', 'Владельцу приложения'),
    ('Карта превышает лимит входа AI-возможностей. Запрос не отправлен, данные не усечены.', 'Запрос не отправлен, данные не сокращены'),
])
def test_analysis_service_errors_explain_impact_without_internal_instructions(message, expected):
    from govai.document_ui import analysis_error_message
    visible = analysis_error_message(message)
    assert expected in visible
    assert all(token not in visible for token in ('JSON', 'Structured Outputs', 'secrets.toml'))
    integrity = 'Подтверждающая цитата не найдена в указанном фрагменте. Источник отклонён.'
    assert analysis_error_message(integrity) == integrity


def _file(name, data):
    return UploadedFile(UploadedFileRec(name, name, 'application/octet-stream', data), FileURLs())


def _reference_source():
    return (APP.parent / 'examples/rosimushchestvo_261/source.txt').read_bytes()


def _reference_card_numbers(app):
    return [int(match[1]) for item in app.markdown
            if (match := re.match(r'<div class="govai-card-heading"><span>(\d+)</span>', item.value))]


@pytest.mark.parametrize('filename', ['source.txt', 'renamed-full-regulation.txt'])
def test_full_approved_source_shows_seven_cards_and_exact_pdf_without_provider(monkeypatch, filename):
    import streamlit as st
    from govai.reference import PDF_SHA256, restore_reference_result
    from govai.extraction import extract_document
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    data = _reference_source()
    with patch('streamlit.file_uploader', return_value=_file(filename, data)), \
         patch('govai.llm.OpenAIProvider.__init__', side_effect=AssertionError('No provider for approved source')) as provider, \
         patch('govai.process_ui.extract_with_checkpoint', side_effect=AssertionError('No extraction')) as extract, \
         patch('govai.map_ui.normalize_analysis', side_effect=AssertionError('No normalization')) as normalize, \
         patch('govai.opportunity_ui.generate_opportunities', side_effect=AssertionError('No opportunity generation')) as generate, \
         patch('streamlit.download_button', wraps=st.download_button) as downloads:
        app = AppTest.from_file(str(APP)).run(timeout=15)
        assert not app.exception and not app.error
        assert _reference_card_numbers(app) == list(range(1, 8))
        assert any('Проверенный разбор · редакция 30.05.2023' in s.value for s in app.caption)
        assert not any(b.label in ('Извлечь процессы', 'Сформировать отчёт') for b in app.button)
        assert 'process_result' not in app.session_state
        label = 'Скачать полный отчёт (PDF, 6 страниц)'
        prepared = {call.kwargs['label']: call.kwargs['data'] for call in downloads.call_args_list if 'label' in call.kwargs}
        assert sha256(prepared[label]).hexdigest() == PDF_SHA256
        assert len(app.get('download_button')) == 2
        assert all(b.proto.ignore_rerun for b in app.get('download_button'))
        assert all(not b.proto.HasField('deferred_file_id') for b in app.get('download_button'))
        cached = next(iter(app.session_state['opportunity_exports'].values()))
        assert not app.text_area and not app.dataframe
        assert not any(e.label == 'Технические данные' for e in app.expander)
        app.run(); app.run()
        assert next(iter(app.session_state['opportunity_exports'].values())) is cached
        assert _reference_card_numbers(app) == list(range(1, 8))
        for mock in (provider, extract, normalize, generate):
            mock.assert_not_called()


@pytest.mark.parametrize('variation', ['foreign_same_name', 'truncated', 'changed_rule', 'changed_edition'])
def test_similar_or_same_named_document_uses_ordinary_path_without_reference(variation):
    text = _reference_source().decode()
    if variation == 'foreign_same_name':
        text = 'Приказ Росимущества от 11.09.2020 №261. Редакция от 30.05.2023.\n1. Другой текст.'
    elif variation == 'truncated':
        text = text[:len(text)//2]
    elif variation == 'changed_rule':
        text = text.replace('без проведения торгов', 'по результатам проведения торгов', 1)
    else:
        text = text.replace('30.05.2023', '30.05.2024', 1)
    with patch('streamlit.file_uploader', return_value=_file('source.txt', text.encode())), \
         patch('govai.llm.OpenAIProvider.__init__', side_effect=AssertionError('No paid call on upload')) as provider:
        app = AppTest.from_file(str(APP)).run(timeout=15)
        assert not app.exception
        assert 'reference_result' not in app.session_state
        assert any(b.label == 'Проанализировать документ' for b in app.button)
        assert not _reference_card_numbers(app)
        assert not any(b.label.startswith('Скачать полный отчёт') for b in app.get('download_button'))
        provider.assert_not_called()


@pytest.mark.parametrize('next_input', ['other', 'broken', 'removed'])
def test_reference_then_changed_failed_or_removed_upload_hides_previous_result(next_input):
    current = [_file('source.txt', _reference_source())]
    with patch('streamlit.file_uploader', side_effect=lambda label, **kw: current[0] if label == 'Выберите документ' else None), \
         patch('govai.llm.OpenAIProvider.__init__', side_effect=AssertionError('No API on navigation')) as provider:
        app = AppTest.from_file(str(APP)).run(timeout=15)
        assert _reference_card_numbers(app) == list(range(1, 8))
        old_id = app.session_state['reference_result']['artifact_id']
        current[0] = (_file('source.txt', '1. Другой орган рассматривает другой вопрос.'.encode()) if next_input == 'other'
                      else _file('broken.pdf', b'broken') if next_input == 'broken' else None)
        app.run(timeout=15)
        assert not app.exception
        assert 'reference_result' not in app.session_state
        assert not _reference_card_numbers(app)
        assert not any('Проверенный разбор' in s.value for s in app.caption)
        assert not any(b.label.startswith('Скачать полный отчёт') or b.label == 'Скачать все материалы ZIP'
                       for b in app.get('download_button'))
        # Recovery bytes may remain source-bound in the cache, but must not render.
        assert 'public-reference-' + old_id in app.session_state['opportunity_exports']
        if next_input == 'broken':
            assert app.error and 'Не удалось прочитать PDF' in app.error[0].value
        if next_input == 'other':
            assert any(b.label == 'Проанализировать документ' for b in app.button)
        app.run(timeout=15)
        assert not _reference_card_numbers(app)
        provider.assert_not_called()


def test_non_utf8_txt_allows_explicit_encoding_only_when_needed():
    value=_file('legacy.txt','Русский текст в старой кодировке.'.encode('cp1251'))
    with patch('streamlit.file_uploader',return_value=value):
        app=AppTest.from_file(str(APP)).run()
        assert app.error
        app.selectbox[0].set_value('Windows-1251').run()
        assert not app.error and not app.exception
        assert app.session_state['document_read_cache']['document']['text']=='Русский текст в старой кодировке.'
        app.run(); assert not app.error


@pytest.mark.parametrize('mode',['Загрузить файл','Загрузить по ссылке'])
def test_start_example_is_exact_v4_without_input_or_provider(mode):
    import streamlit as st
    from govai.reference import PDF_SHA256
    with patch('govai.llm.OpenAIProvider.__init__',side_effect=AssertionError('No API')) as provider, \
         patch('streamlit.download_button',wraps=st.download_button) as downloads:
        app=AppTest.from_file(str(APP)).run()
        app.radio[0].set_value(mode).run()
        prepared={c.args[0]:c.args[1] for c in downloads.call_args_list if c.args}
        assert sha256(prepared['Скачать готовый отчёт (PDF, 6 страниц)']).hexdigest()==PDF_SHA256
        assert prepared['Скачать текст НПА (TXT)']==_reference_source()
        assert not app.expander and not app.json
        provider.assert_not_called()
