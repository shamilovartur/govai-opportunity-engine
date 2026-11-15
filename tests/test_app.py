from pathlib import Path
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
        if fragments:
            next(c for c in app.checkbox if c.label == 'Показать фрагменты и ссылки на пункты').check().run(timeout=15)
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
    assert app.title[0].value == 'Поиск вариантов для внедрения ИИ'
    assert any(item.value == 'Загрузите документ в удобном формате. Агент изучит рабочий процесс '
               'и предложит рекомендации по оптимизации' for item in app.markdown)
    assert app.radio[0].label == 'Способ загрузки'
    assert not app.subheader and not app.expander
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
    assert app.text_area[0].value == "Тестовый документ"


def test_pdf_upload_displays_text(pdf_bytes):
    app = run_with_upload("example.pdf", pdf_bytes)
    assert not app.exception
    assert "First page: procurement" in app.text_area[0].value
    assert "Second page: deadline" in app.text_area[0].value


def test_docx_upload_displays_text(docx_bytes):
    app = run_with_upload("example.docx", docx_bytes)
    assert not app.exception
    assert "Условия закупки" in app.text_area[0].value
    assert "30 ноября" in app.text_area[0].value


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
    assert any(h.value == 'Структура документа' for h in app.subheader)
    rendered = '\n'.join(item.value for item in app.markdown)
    assert 'Номер: 261' in rendered
    assert 'Редакция: 2023-05-30' in rendered
    assert '2.6.' in app.dataframe[0].value['Пункт'].values


def test_unrecognized_structure_preserves_text_in_ui():
    app = run_with_upload('ordinary.txt', 'Обычный текст без нумерации.'.encode())
    assert not app.exception
    assert 'Нумерованные пункты не распознаны' in app.info[0].value
    assert app.text_area[0].value == 'Обычный текст без нумерации.'


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
        assert 'Заявитель направляет заявление' in app.text_area[0].value
        assert 'Полнота НПА автоматически не подтверждена' in app.warning[0].value
        assert len(app.get('download_button')) == 1
        app.run()
        fetch.assert_called_once_with('https://public.example.org/doc')


def test_url_change_and_failed_retry_clear_old_document():
    app = AppTest.from_file(str(APP)).run(timeout=15)
    app.radio[0].set_value('Загрузить по ссылке').run()
    app.text_input[0].set_value('https://public.example.org/doc').run()
    with patch('govai.url_input.extract_url_document', return_value=url_result()):
        app.button[0].click().run()
    technical(app)
    assert app.text_area
    app.text_input[0].set_value('https://public.example.org/other').run()
    assert not app.text_area
    with patch('govai.url_input.extract_url_document', side_effect=URLInputError('HTTP 403: доступ запрещён')):
        app.button[0].click().run()
    assert not app.exception
    assert '403' in app.error[0].value
    assert not app.text_area
    assert not app.get('download_button')


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
        settings = next(e for e in app.expander if e.label == 'Дополнительные настройки TXT')
        assert not settings.proto.expanded
        next(s for s in app.selectbox if s.label == 'Кодировка TXT').set_value('Windows-1251').run()
        assert read.call_count == 2
        app.run()
        assert read.call_count == 2
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
    full_title = next(e for e in real.expander if e.label == 'Полное название документа')
    assert not full_title.proto.expanded
    assert 'без проведения торгов' in '\n'.join(x.value for x in full_title.markdown)
    assert '…' not in '\n'.join(x.value for x in full_title.markdown)


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
