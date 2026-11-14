"""Extract document text in memory, independently of the Streamlit interface."""

from hashlib import sha256
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
            yield from _table_rows(block)


def _table_rows(table):
    seen = set()
    for row in table.rows:
        cells = []
        for cell in row.cells:
            # Merged cells (horizontal or vertical) share the same XML element.
            # Deduplicate by identity only: equal text in distinct cells is real.
            if cell._tc not in seen:
                cells.append("\n".join(_docx_blocks(cell)))
                seen.add(cell._tc)
        yield "\t".join(cells)


def _read_parts(filename: str, data: bytes, encoding: str):
    """Return text units, known heading offsets and format warnings."""
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
        return [text], [], []

    try:
        if extension == "pdf":
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                raise ExtractionError(
                    "PDF защищён паролем. Загрузите копию без защиты."
                )
            parts = [page.extract_text() or "" for page in reader.pages]
            warnings = [f"pdf_page_without_text:{i}" for i, text in enumerate(parts, 1)
                        if not text.strip()]
            return parts, [], warnings
        document = Document(BytesIO(data))
        texts, headings, offset = [], [], 0
        for block in document.iter_inner_content():
            if isinstance(block, Paragraph):
                text = block.text
                style = block.style.style_id if block.style is not None else ""
                if text and style.lower().startswith(("heading", "заголовок")):
                    headings.append((offset, offset + len(text)))
                texts.append(text)
                offset += len(text) + 1
            else:
                for text in _table_rows(block):
                    texts.append(text)
                    offset += len(text) + 1
        return ["\n".join(texts)], headings, ["docx_automatic_numbering_not_resolved"]
    except ExtractionError:
        raise
    except Exception as exc:
        # Parsers may raise different exceptions for malformed third-party files.
        raise ExtractionError(
            f"Не удалось прочитать {extension.upper()}. "
            "Файл повреждён или его содержимое не соответствует расширению."
        ) from exc


def extract_text(filename: str, data: bytes, *, encoding: str = "utf-8-sig") -> str:
    """Backward-compatible plain text, including all PDF pages in source order."""
    parts, _, _ = _read_parts(filename, data, encoding)
    return "\n\n".join(parts)


def extract_document(filename: str, data: bytes, *, encoding: str = "utf-8-sig") -> dict:
    """Return JSON-compatible text, source locations and conservative structure."""
    from govai.structure import structure_document

    parts, headings, warnings = _read_parts(filename, data, encoding)
    extension = Path(filename).suffix.lower().lstrip(".")
    result = structure_document(
        filename, sha256(data).hexdigest(), extension, parts, headings, warnings
    )
    result['source']['type'] = 'file'
    return result
