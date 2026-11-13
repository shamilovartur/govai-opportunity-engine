"""Extract document text in memory, independently of the Streamlit interface."""

from io import BytesIO
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

MAX_FILE_SIZE = 10 * 1024 * 1024
SUPPORTED_EXTENSIONS = ("pdf", "docx", "txt")
TXT_ENCODINGS = {"UTF-8": "utf-8-sig", "UTF-16": "utf-16", "Windows-1251": "cp1251"}


class ExtractionError(ValueError):
    """An input problem that can be displayed safely to the user."""


def _docx_blocks(container):
    """Yield body text and table rows in document order, including nested tables."""
    for block in container.iter_inner_content():
        if isinstance(block, Paragraph):
            yield block.text
        elif isinstance(block, Table):
            for row in block.rows:
                cells = []
                previous_cell = None
                for cell in row.cells:
                    # Horizontally merged cells share an XML element.
                    if cell._tc is not previous_cell:
                        cells.append("\n".join(_docx_blocks(cell)))
                    previous_cell = cell._tc
                yield "\t".join(cells)


def extract_text(filename: str, data: bytes, *, encoding: str = "utf-8-sig") -> str:
    """Read PDF, DOCX or TXT bytes; raise ExtractionError for invalid input.

    TXT defaults to UTF-8 (with or without BOM). Encoding is explicit to avoid
    silently interpreting invalid UTF-8 as a different character set.
    """
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in SUPPORTED_EXTENSIONS:
        raise ExtractionError("Неподдерживаемый формат. Выберите PDF, DOCX или TXT.")
    if not data:
        raise ExtractionError("Файл пуст. Выберите документ с содержимым.")
    if len(data) > MAX_FILE_SIZE:
        raise ExtractionError("Файл слишком большой. Максимальный размер — 10 МБ.")

    if extension == "txt":
        if encoding not in TXT_ENCODINGS.values():
            raise ExtractionError("Неподдерживаемая кодировка TXT.")
        try:
            text = data.decode(encoding)
        except UnicodeError as exc:
            raise ExtractionError(
                "Не удалось прочитать TXT. Выберите подходящую кодировку "
                "или сохраните файл в UTF-8."
            ) from exc
        # NUL and other control bytes usually mean a binary file was renamed.
        if any(ord(char) < 32 and char not in "\n\r\t\f" for char in text):
            raise ExtractionError("TXT содержит бинарные данные или неверную кодировку.")
        return text

    try:
        if extension == "pdf":
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                raise ExtractionError(
                    "PDF защищён паролем. Загрузите копию без защиты."
                )
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)
        return "\n".join(_docx_blocks(Document(BytesIO(data))))
    except ExtractionError:
        raise
    except Exception as exc:
        # Parsers may raise different exceptions for malformed third-party files.
        raise ExtractionError(
            f"Не удалось прочитать {extension.upper()}. "
            "Файл повреждён или его содержимое не соответствует расширению."
        ) from exc
