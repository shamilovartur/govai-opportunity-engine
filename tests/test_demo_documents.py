"""Public-law source preservation checks, not a test of real model quality."""

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from docx import Document
import pytest
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

from govai.extraction import extract_document


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'tests/fixtures/law_59_article8_excerpt.txt'


def _format_copy(extension):
    """Format the saved excerpt for parser checks; these are not publisher files."""
    text = SOURCE.read_text()
    if extension == 'txt':
        return text.encode()
    stream = BytesIO()
    if extension == 'docx':
        document = Document()
        for line in text.splitlines():
            document.add_paragraph(line)
        document.save(stream)
    else:
        font = 'GovAIDemoSource'
        pdfmetrics.registerFont(TTFont(font, str(ROOT / 'govai/fonts/LiberationSans-Regular.ttf')))
        style = ParagraphStyle('source', fontName=font, fontSize=11, leading=14)
        story = []
        for line in text.splitlines():
            if line == '3.':
                story.append(PageBreak())
            if line.strip():
                story.append(Paragraph(escape(line), style))
        SimpleDocTemplate(stream).build(story)
    return stream.getvalue()


@pytest.mark.parametrize('extension', ['txt', 'docx', 'pdf'])
def test_real_independent_excerpt_preserves_exception_and_participant_scope(extension):
    data = _format_copy(extension)
    result = extract_document('law59-fragment.' + extension, data)
    comparable = ' '.join(result['text'].split())
    source = ' '.join(SOURCE.read_text().split())
    assert comparable == source
    assert ''.join(fragment['text'] for fragment in result['fragments']) == result['text']
    assert result['source']['sha256'] == sha256(data).hexdigest()
    assert result['source']['text_sha256'] == sha256(result['text'].encode()).hexdigest()
    assert result['metadata']['number']['value'] == '59-ФЗ'
    assert result['metadata']['revision_date']['value'] == '2024-12-28'
    assert [clause['number'] for clause in result['clauses']] == ['2', '3']
    for phrase in ('в течение трех дней', 'в течение семи дней',
                   'органа местного самоуправления', 'с уведомлением гражданина',
                   'за исключением случая, указанного в части 4 статьи 11'):
        assert phrase in comparable
    assert comparable.count('Письменное обращение подлежит обязательной регистрации') == 1
    assert comparable.count('за исключением случая') == 1
    clauses = {clause['id']: clause['number'] for clause in result['clauses']}
    exception = next(fragment for fragment in result['fragments']
                     if 'исключением' in fragment['text'])
    assert clauses[exception['clause_id']] == '3'
    if extension == 'pdf':
        assert exception['page_number'] == 2
    else:
        assert all(fragment['page_number'] is None for fragment in result['fragments'])


def test_independent_excerpt_cp1251_is_explicit_and_keeps_original_file_hash():
    data = SOURCE.read_text().encode('cp1251')
    document = extract_document('law59-fragment.txt', data, encoding='cp1251')
    assert document['text'] == SOURCE.read_text()
    assert document['source']['sha256'] == sha256(data).hexdigest()
    assert document['source']['sha256'] != sha256(SOURCE.read_bytes()).hexdigest()
