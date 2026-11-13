from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.proto.Common_pb2 import FileURLs
from streamlit.runtime.uploaded_file_manager import UploadedFile, UploadedFileRec
from streamlit.testing.v1 import AppTest

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
