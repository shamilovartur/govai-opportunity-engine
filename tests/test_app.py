from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.proto.Common_pb2 import FileURLs
from streamlit.runtime.uploaded_file_manager import UploadedFile, UploadedFileRec
from streamlit.testing.v1 import AppTest

from govai.url_input import URLInputError, _html_document

APP = Path(__file__).resolve().parents[1] / "app.py"


def run_with_upload(filename, data):
    # AppTest does not expose file-uploader interaction. Supply only the upload;
    # the actual parser and all rendered app elements run unchanged.
    upload = UploadedFile(
        UploadedFileRec("test-file", filename, "application/octet-stream", data),
        FileURLs(),
    )
    with patch("streamlit.file_uploader", return_value=upload):
        return AppTest.from_file(str(APP)).run(timeout=15)


def test_initial_screen():
    app = AppTest.from_file(str(APP)).run(timeout=15)
    assert not app.exception
    assert app.title[0].value == "GovAI Opportunity Engine"
    assert app.info
    assert not app.text_area


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
    assert app.subheader[0].value == 'Структура документа'
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
