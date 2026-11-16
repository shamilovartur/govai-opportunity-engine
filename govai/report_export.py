"""PDF, DOCX and ZIP from one checked view; exports never call a provider."""

from html import escape
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from lxml import etree

from govai.opportunity_report import (
    management_view, management_blocks, _block_paragraphs, render_markdown, render_details,
)

EXPORT_VERSION = 'management-7'
_FONT_DIR = Path(__file__).with_name('fonts')
_FONT_NAME = 'IBM Plex Sans'
_PAGE_SIZE = (450, 800)
_MARGIN = 28
_UNSET = object()
_COLORS = {'bg': 'F6F5F1', 'navy': '14213D', 'text': '3D4658', 'muted': '626A78',
           'paper': 'FFFDF8', 'line': 'D8D2C4', 'orange': 'A34E0C', 'peach': 'F3E3D3',
           'teal': '2F766D', 'mint': 'E3EEE9', 'blue': '2F6FB0', 'pale': 'E4EBF3'}


def _register_pdf_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for name, filename in [('GovAIPlex', 'IBMPlexSans-Regular.ttf'),
                           ('GovAIPlexBold', 'IBMPlexSans-Bold.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(_FONT_DIR / filename)))
    pdfmetrics.registerFontFamily('GovAIPlex', normal='GovAIPlex', bold='GovAIPlexBold',
                                 italic='GovAIPlex', boldItalic='GovAIPlexBold')


def _font(style, size, *, bold=False, color='text'):
    style.font.name = _FONT_NAME
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = RGBColor.from_string(_COLORS[color])
    fonts = style.element.get_or_add_rPr().find(qn('w:rFonts'))
    if fonts is not None:
        for script in ('ascii', 'hAnsi', 'eastAsia', 'cs'):
            fonts.set(qn('w:' + script), _FONT_NAME)


def _shade(paragraph, color):
    shade = OxmlElement('w:shd'); shade.set(qn('w:fill'), _COLORS[color])
    paragraph._p.get_or_add_pPr().append(shade)


def _docx_table(document, data):
    headers, rows = data['headers'], data['rows']
    if not headers or any(len(row) != len(headers) for row in rows):
        raise ValueError('Inconsistent report table.')
    widths = [(_PAGE_SIZE[0] - 2 * _MARGIN) * fraction
              for fraction in data.get('widths', [1 / len(headers)] * len(headers))]
    table = document.add_table(rows=0, cols=len(headers)); table.autofit = False
    for column, width in zip(table.columns, widths): column.width = Pt(width)
    for index, values in enumerate([headers, *rows]):
        row = table.add_row()
        if index == 0: row._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
        # Long rows remain splittable. No setting may force an oversized cell
        # off the printable page or silently lose its final paragraphs.
        for cell, value, width in zip(row.cells, values, widths):
            cell.width = Pt(width); cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(5)
            paragraph.paragraph_format.space_before = Pt(5)
            paragraph.paragraph_format.line_spacing = 1.08
            run = paragraph.add_run(str(value)); run.font.name = _FONT_NAME
            run.font.size = Pt(10.5); run.bold = index == 0
            if index == 0 or index % 2 == 0:
                shade = OxmlElement('w:shd')
                shade.set(qn('w:fill'), _COLORS['pale'] if index == 0 else _COLORS['paper'])
                cell._tc.get_or_add_tcPr().append(shade)
    document.add_paragraph().paragraph_format.space_after = Pt(3)


def _document(view):
    document = Document(); section = document.sections[0]
    section.page_width, section.page_height = (Pt(value) for value in _PAGE_SIZE)
    section.left_margin = section.right_margin = section.top_margin = section.bottom_margin = Pt(_MARGIN)
    section.header_distance = section.footer_distance = Pt(10)
    styles = document.styles
    _font(styles['Normal'], 12)
    styles['Normal'].paragraph_format.space_after = Pt(7)
    styles['Normal'].paragraph_format.line_spacing = 1.12
    styles['Normal'].paragraph_format.widow_control = True
    for name, size in [('Title', 24.5), ('Heading 1', 24.5), ('Heading 2', 15)]:
        _font(styles[name], size, bold=True, color='navy')
        styles[name].paragraph_format.space_before = Pt(12 if name != 'Title' else 0)
        styles[name].paragraph_format.space_after = Pt(10)
        styles[name].paragraph_format.keep_with_next = True
    _font(styles['List Bullet'], 12)
    for part in document.part.package.parts:
        if str(part.partname) == '/word/fontTable.xml':
            fonts = parse_xml(part.blob)
            font = OxmlElement('w:font'); font.set(qn('w:name'), _FONT_NAME)
            alternate = OxmlElement('w:altName'); alternate.set(qn('w:val'), 'Arial'); font.append(alternate)
            family = OxmlElement('w:family'); family.set(qn('w:val'), 'swiss'); font.append(family)
            fonts.append(font)
            part._blob = etree.tostring(fonts, xml_declaration=True, encoding='UTF-8', standalone=True)
            break
    footer = section.footer.paragraphs[0]; footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run('Страница ').font.size = Pt(10.5)
    field = OxmlElement('w:fldSimple'); field.set(qn('w:instr'), 'PAGE'); footer._p.append(field)
    document.core_properties.title = view['title'][:255]
    document.core_properties.subject = 'AI-доработки, порядок проверки и ожидаемый эффект'
    document.core_properties.author = 'GovAI'
    document.core_properties.keywords = ''
    return document


def render_docx(artifact, *, view=None):
    """Editable full report. Automatic pagination keeps all accepted content."""
    view = management_view(artifact) if view is None else view
    document = _document(view)
    if artifact.get('kind') == 'govai_verified_reference':
        # The fixed PDF is the approved artifact. Its editable companion keeps
        # every approved paragraph rather than rebuilding it from summary cards.
        from govai.reference import reference_full_blocks
        previous = None
        for block in reference_full_blocks(artifact):
            if previous is not None and previous != block['page']: document.add_page_break()
            document.add_paragraph(block['text'], style='Heading 1' if block['kind'] == 'heading' else None)
            previous = block['page']
    else:
        for kind, value in management_blocks(view):
            if kind == 'page_break':
                document.add_page_break()
            elif kind in ('heading', 'subheading'):
                document.add_paragraph(str(value), style='Heading 1' if kind == 'heading' else 'Heading 2')
            elif kind == 'conclusion':
                document.add_paragraph(value['heading'], style='Title')
                paragraph = document.add_paragraph(value['text']); _shade(paragraph, 'mint')
            elif kind == 'process_group':
                paragraphs = list(_block_paragraphs(kind, value))
                paragraph = document.add_paragraph()
                paragraph.add_run(paragraphs[0] + '. ').bold = True
                paragraph.add_run(' '.join(paragraphs[1:]))
            elif kind in ('ranked_card', 'card'):
                paragraphs = list(_block_paragraphs(kind, value))
                heading = document.add_paragraph(paragraphs[0], style='Heading 2')
                _shade(heading, 'pale' if kind == 'process_group' else 'mint')
                for text in paragraphs[1:]: document.add_paragraph(text)
            elif kind == 'table': _docx_table(document, value)
            elif kind == 'notice':
                paragraph = document.add_paragraph(value['text']); _shade(paragraph, 'peach')
            else:
                document.add_paragraph(str(value), style='List Bullet' if kind == 'bullet' else None)
    stream = BytesIO(); document.save(stream); return stream.getvalue()


def render_pdf(artifact, *, view=None):
    """Full paginated report with selectable Cyrillic; no page-count truncation."""
    if artifact.get('kind') == 'govai_verified_reference':
        from govai.reference import reference_pdf
        return reference_pdf(artifact)
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    view = management_view(artifact) if view is None else view
    _register_pdf_fonts()
    styles = {
        'body': ParagraphStyle('body', fontName='GovAIPlex', fontSize=12, leading=15.2,
            spaceAfter=6, textColor=colors.HexColor('#' + _COLORS['text']), allowWidows=False, allowOrphans=False),
        'heading': ParagraphStyle('heading', fontName='GovAIPlexBold', fontSize=24.5, leading=28,
            spaceBefore=14, spaceAfter=12, textColor=colors.HexColor('#' + _COLORS['navy']), keepWithNext=True),
        'subheading': ParagraphStyle('subheading', fontName='GovAIPlexBold', fontSize=15, leading=19,
            spaceBefore=5, spaceAfter=9, textColor=colors.HexColor('#' + _COLORS['navy']), keepWithNext=True),
        'table': ParagraphStyle('table', fontName='GovAIPlex', fontSize=10.5, leading=13, spaceAfter=0),
        'tablehead': ParagraphStyle('tablehead', fontName='GovAIPlexBold', fontSize=10.5, leading=13),
    }
    def paragraph(text, style='body'):
        return Paragraph(escape(str(text)).replace('\n', '<br/>'), styles[style])
    stream = BytesIO()
    doc = BaseDocTemplate(stream, pagesize=_PAGE_SIZE, leftMargin=_MARGIN, rightMargin=_MARGIN,
        topMargin=_MARGIN, bottomMargin=_MARGIN, title=view['title'], author='GovAI',
        subject='AI-доработки, порядок проверки и ожидаемый эффект')

    def panel(contents, fill='paper'):
        table = Table([[contents]], colWidths=[doc.width], splitInRow=1)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#' + _COLORS[fill])),
            ('LEFTPADDING', (0, 0), (-1, -1), 9), ('RIGHTPADDING', (0, 0), (-1, -1), 9),
            ('TOPPADDING', (0, 0), (-1, -1), 8), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
        # Keep a measured card together only if it fits on a complete page.
        # Oversized cards split at paragraph/line boundaries without clipping.
        limit = doc.height
        if table.wrap(doc.width, doc.height)[1] <= limit:
            table.splitInRow = 0
        return table

    story = []
    for kind, value in management_blocks(view):
        if kind == 'page_break':
            # A trailing spacer can spill onto an otherwise empty page just
            # before the explicit section break. It carries no report content.
            while story and isinstance(story[-1], Spacer): story.pop()
            story.append(PageBreak())
        elif kind in ('heading', 'subheading'): story.append(paragraph(value, kind))
        elif kind == 'conclusion':
            story += [paragraph(value['heading'], 'heading'), panel([paragraph(value['text'])], 'mint'), Spacer(1, 8)]
        elif kind == 'process_group':
            texts = list(_block_paragraphs(kind, value))
            story.append(Paragraph('<b>' + escape(texts[0]) + '.</b> ' + escape(' '.join(texts[1:])), styles['body']))
        elif kind in ('ranked_card', 'card'):
            texts = list(_block_paragraphs(kind, value))
            contents = [paragraph(texts[0], 'subheading'), *[paragraph(text) for text in texts[1:]]]
            fill = ('pale', 'peach', 'mint')[(value['number'] - 1) % 3]
            story += [panel(contents, fill), Spacer(1, 8)]
        elif kind == 'notice': story += [panel([paragraph(value['text'])], 'peach'), Spacer(1, 8)]
        elif kind == 'table':
            headers, data = value['headers'], value['rows']
            if not headers or any(len(row) != len(headers) for row in data):
                raise ValueError('Inconsistent report table.')
            cells = [[paragraph(cell, 'tablehead' if index == 0 else 'table') for cell in row]
                     for index, row in enumerate([headers, *data])]
            widths = [doc.width * fraction for fraction in value.get('widths', [1 / len(headers)] * len(headers))]
            table = Table(cells, colWidths=widths, repeatRows=1, splitInRow=1)
            table.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#' + _COLORS['pale'])),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#' + _COLORS['paper']), colors.HexColor('#' + _COLORS['bg'])]),
                ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 7)]))
            story += [table, Spacer(1, 8)]
        else: story.append(paragraph(('• ' if kind == 'bullet' else '') + str(value)))

    def page(canvas, document):
        canvas.saveState(); canvas.setFillColor(colors.HexColor('#' + _COLORS['bg']))
        canvas.rect(0, 0, *_PAGE_SIZE, fill=1, stroke=0)
        canvas.setFont('GovAIPlex', 10.5); canvas.setFillColor(colors.HexColor('#' + _COLORS['muted']))
        canvas.drawRightString(_PAGE_SIZE[0] - _MARGIN, 12, f'Страница {document.page}'); canvas.restoreState()
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates(PageTemplate(id='management', frames=[frame], onPage=page))
    doc.build(story)
    return stream.getvalue()


def pdf_layout_status(pdf_data):
    """Report the real length; independent documents have no arbitrary six-page cap."""
    from pypdf import PdfReader
    return {'pages': len(PdfReader(BytesIO(pdf_data)).pages), 'brief_limit': None,
            'exceeded': False, 'message': None}


def render_bundle(artifact, *, pdf_data=_UNSET, docx_data=_UNSET, markdown_data=_UNSET,
                  json_data=_UNSET, details_data=_UNSET, view=None):
    """Bundle prepared bytes. A failed format cannot hide the validated result."""
    view = management_view(artifact) if view is None else view
    reference = artifact.get('kind') == 'govai_verified_reference'
    if json_data is _UNSET: json_data = json.dumps(artifact, ensure_ascii=False, indent=2).encode('utf-8')
    if json_data is None or json.loads(json_data) != artifact:
        raise ValueError('Bundle requires the complete JSON describing this result.')
    if pdf_data is _UNSET: pdf_data = render_pdf(artifact, view=view)
    if docx_data is _UNSET: docx_data = render_docx(artifact, view=view)
    if markdown_data is _UNSET: markdown_data = render_markdown(artifact, view=view).encode('utf-8')
    if details_data is _UNSET: details_data = render_details(artifact).encode('utf-8')
    pdf_unverified = False
    if pdf_data is not None:
        try: pdf_layout_status(pdf_data)
        except Exception:
            pdf_data, pdf_unverified = None, True
    json_name = 'reference-result.json' if reference else 'opportunities.json'
    candidates = {'opportunity-report.pdf': pdf_data, 'opportunity-report.docx': docx_data,
                  'opportunity-report.md': markdown_data, json_name: json_data, 'opportunity-details.txt': details_data}
    missing = [name for name, data in candidates.items() if data is None]
    description = '\n'.join([
        'GovAI — проверенный сохранённый разбор' if reference else 'GovAI — предварительные рекомендации',
        'Документ: ' + view['requisites'], '',
        ('PDF — точный одобренный шестистраничный файл. DOCX и Markdown сохраняют все его формулировки.' if reference else
         'PDF, DOCX и Markdown содержат полный анализ из одного проверенного результата; число страниц зависит от документа.'),
        json_name + (' — проверяемый сохранённый эталон, не восстановленный ответ модели.' if reference else ' — полный структурированный результат.'),
        'opportunity-details.txt — источники, методика, расчётные предпосылки и ограничения.',
        'Сценарные значения не являются измерениями ведомства или доказанной бюджетной экономией.',
        ('Для повторного получения проверенного разбора загрузите полный исходный документ: приложение распознает его без API. reference-result.json содержит проверяемую копию эталона.' if reference else
         'Для восстановления нужны соответствующие НПА, processes.json и normalized_processes.json, сохранённые отдельно. Восстановите JSON в техническом разделе.'),
        'Восстановление и скачивание не требуют новых API-запросов.',
        'Сессия приложения не является постоянным хранилищем; сохраните архив у себя.',
        *(['PDF не включён: не удалось проверить подготовленный файл. Остальные форматы доступны.'] if pdf_unverified else []),
        *(['ВНИМАНИЕ: не удалось подготовить ' + ', '.join(missing) + '. Остальные файлы и полный JSON сохранены.'] if missing else []),
    ]) + '\n'
    stream = BytesIO()
    with ZipFile(stream, 'w', compression=ZIP_DEFLATED) as archive:
        for name, data in candidates.items():
            if data is not None: archive.writestr(name, data)
        archive.writestr('README.txt', description.encode('utf-8'))
    return stream.getvalue()
