from io import BytesIO

import pytest
from docx import Document
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


@pytest.fixture
def pdf_bytes():
    """A real two-page PDF with known text, generated without external services."""
    writer = PdfWriter()
    for text in ("First page: procurement", "Second page: deadline"):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {
                        NameObject("/F1"): DictionaryObject(
                            {
                                NameObject("/Type"): NameObject("/Font"),
                                NameObject("/Subtype"): NameObject("/Type1"),
                                NameObject("/BaseFont"): NameObject("/Helvetica"),
                            }
                        )
                    }
                )
            }
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = stream
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture
def docx_bytes():
    document = Document()
    document.add_paragraph("Условия закупки")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Срок подачи"
    table.cell(0, 1).text = "30 ноября"
    document.add_paragraph("Контакты заказчика")
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()
