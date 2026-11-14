from io import BytesIO

import pytest
from docx import Document
from pypdf import PdfWriter

from govai.extraction import MAX_FILE_SIZE, ExtractionError, extract_text


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_utf8_txt_preserves_cyrillic_and_lines(encoding):
    text = "Государственные закупки\nСрок: 30 ноября\n"
    assert extract_text("document.TXT", text.encode(encoding)) == text


@pytest.mark.parametrize("encoding", ["utf-16", "cp1251"])
def test_explicit_txt_encoding(encoding):
    text = "Условия участия"
    assert extract_text("document.txt", text.encode(encoding), encoding=encoding) == text


def test_wrong_txt_encoding_has_actionable_error():
    with pytest.raises(ExtractionError, match="кодировку"):
        extract_text("document.txt", "Закупки".encode("cp1251"))


def test_unsupported_encoding():
    with pytest.raises(ExtractionError, match="кодировка"):
        extract_text("document.txt", b"text", encoding="unknown")


def test_binary_file_renamed_to_txt():
    with pytest.raises(ExtractionError, match="бинарные"):
        extract_text("document.txt", b"\x00\x01binary")


def test_pdf_extracts_all_pages_in_order(pdf_bytes):
    assert extract_text("document.PDF", pdf_bytes) == (
        "First page: procurement\n\nSecond page: deadline"
    )


def test_docx_preserves_paragraph_and_table_order(docx_bytes):
    assert extract_text("document.DOCX", docx_bytes) == (
        "Условия закупки\nСрок подачи\t30 ноября\nКонтакты заказчика"
    )


def test_docx_nested_table_and_merged_cells():
    document = Document()
    table = document.add_table(rows=1, cols=2)
    cell = table.cell(0, 0).merge(table.cell(0, 1))
    cell.text = "Общие условия"
    cell.add_table(rows=1, cols=1).cell(0, 0).text = "Вложенное условие"
    buffer = BytesIO()
    document.save(buffer)
    text = extract_text("nested.docx", buffer.getvalue())
    assert text.count("Общие условия") == 1
    assert text.count("Вложенное условие") == 1
    assert text.index("Общие условия") < text.index("Вложенное условие")


@pytest.mark.parametrize("extension", ["pdf", "docx", "txt"])
def test_zero_byte_files(extension):
    with pytest.raises(ExtractionError, match="Файл пуст"):
        extract_text(f"document.{extension}", b"")


@pytest.mark.parametrize("filename", ["document.csv", "document.doc", "document"])
def test_unsupported_formats(filename):
    with pytest.raises(ExtractionError, match="Неподдерживаемый формат"):
        extract_text(filename, b"text")


@pytest.mark.parametrize("extension", ["pdf", "docx"])
def test_corrupt_documents(extension):
    with pytest.raises(ExtractionError, match=f"Не удалось прочитать {extension.upper()}"):
        extract_text(f"broken.{extension}", b"not a document")


def test_file_size_limit():
    with pytest.raises(ExtractionError, match="10 МБ"):
        extract_text("large.txt", b"a" * (MAX_FILE_SIZE + 1))


def test_encrypted_pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("test-password", algorithm="RC4-128")
    buffer = BytesIO()
    writer.write(buffer)
    with pytest.raises(ExtractionError, match="защищён паролем"):
        extract_text("protected.pdf", buffer.getvalue())


def test_pdf_without_text_layer():
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = BytesIO()
    writer.write(buffer)
    assert extract_text("blank.pdf", buffer.getvalue()) == ""


def test_empty_docx():
    buffer = BytesIO()
    Document().save(buffer)
    assert extract_text("empty.docx", buffer.getvalue()) == ""
