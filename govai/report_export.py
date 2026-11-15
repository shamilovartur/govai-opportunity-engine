"""Portable PDF/DOCX presentations of one management view; no provider calls."""

from html import escape
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from lxml import etree

from govai.opportunity_report import management_view, render_markdown, render_details

EXPORT_VERSION = 'management-4'
_FONT_DIR = Path(__file__).with_name('fonts')
_FONT_NAME = 'Liberation Sans'
_UNSET = object()
_COLORS = {'ink': '24312D', 'green': '2F6B57', 'soft_green': 'EAF4EF',
           'blue': 'EDF3F8', 'light': 'F4F5F6', 'sand': 'FBF3E6', 'rule': 'D5DDDF'}
_CATALOGUE_WIDTHS = [5.3, 5.8, 5.9]


def _register_pdf_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for name, filename in [('GovAISans', 'LiberationSans-Regular.ttf'), ('GovAISansBold', 'LiberationSans-Bold.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(_FONT_DIR / filename)))
    pdfmetrics.registerFontFamily('GovAISans', normal='GovAISans', bold='GovAISansBold',
                                 italic='GovAISans', boldItalic='GovAISansBold')


def _catalogue_row(row):
    return [f"№ {row['number']}. {row['title']}\n{row['category_label']}", row['benefit'], row['condition']]


def _compact_catalogue(rows):
    """Choose a table from measured row heights, never by deleting long content."""
    try:
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import Paragraph
        _register_pdf_fonts()
        style = ParagraphStyle('catalogue-measure', fontName='GovAISans', fontSize=10.5, leading=12.6)
        heights = [max(Paragraph(escape(str(value)).replace('\n', '<br/>'), style).wrap(width*cm-12, 10000)[1]
                       for value, width in zip(_catalogue_row(row), _CATALOGUE_WIDTHS)) + 12 for row in rows]
        return bool(heights) and max(heights) <= 115 and sum(heights) <= 540
    except (OSError, ImportError, ValueError):
        return False


def _group_fill(key):
    return {'first': 'soft_green', 'prepare': 'blue', 'hold': 'sand'}.get(key, 'light')


def _blocks(view):
    """The same semantic blocks and order for PDF and editable DOCX."""
    count = len(view['initiatives'])
    yield ('conclusion', view['conclusion'])
    if view.get('outcome', {}).get('state') == 'partial':
        outcome = view['outcome']
        yield ('notice', {'text': outcome['message'] + ' ' + outcome['action'], 'fill': 'sand'})
    if view.get('highlights'):
        yield ('highlights', view['highlights'])
    if view.get('service_map'):
        yield ('heading', 'Как проходит услуга')
        yield ('paragraph', view['service_map']['scope'])
        yield ('table', {'headers': ['Этап', 'Участник', 'Результат'],
                        'rows': [[stage['stage'] + '\n' + stage['clauses'], stage['participant'], stage['result']]
                                 for stage in view['service_map']['stages']],
                        'widths': [5.2, 5.1, 6.7], 'keep_together': False})
    for group in view.get('priority_groups', []):
        if not group['items']:
            continue
        yield ('group', group)
        if _compact_catalogue(group['items']):
            yield ('table', {'headers': ['Инициатива', 'Практическая польза', 'Главное условие'],
                            'rows': [_catalogue_row(row) for row in group['items']],
                            'widths': _CATALOGUE_WIDTHS,
                            'keep_together': len(group['items']) <= 4})
        else:
            for row in group['items']:
                yield ('brief', row)
    if count and view.get('ordering_note'):
        yield ('paragraph', view['ordering_note'])
    if view['pilots']:
        yield ('heading', 'Первые проверки')
        for pilot in view['pilots']:
            yield ('card', pilot)
    if view['effects']:
        yield ('heading', 'Эффект и границы расчёта')
        for effect in view['effects']:
            yield ('subheading', f"№ {effect['number']}. {effect['title']}")
            yield ('notice', {'text': effect['notice'], 'fill': 'blue'})
            yield ('paragraph', effect['boundary'])
            yield ('table', {'headers': effect['headers'], 'rows': effect['rows'],
                            'widths': [17 / len(effect['headers'])] * len(effect['headers']),
                            'keep_together': len(effect['rows']) <= 3})
            yield ('paragraph', effect['formula'])
    if count and view.get('effect_note'):
        yield ('paragraph', view['effect_note'])
    yield ('heading', 'Условия и основания')
    for condition in view['conditions']:
        yield ('bullet', condition)
    if view['source_index']:
        for start in range(0, len(view['source_index']), 2):
            yield ('source', '; '.join(f"№ {source['number']} — {source['clauses']}"
                                      for source in view['source_index'][start:start+2]))
    yield ('paragraph', view['detail_note'])
    yield ('requisites', view['requisites'])
    if view.get('scope'):
        yield ('paragraph', view['scope'])


def _font(style, size, *, bold=False, color=None):
    style.font.name = _FONT_NAME
    style.font.size = Pt(size)
    style.font.bold = bold
    if color:
        style.font.color.rgb = RGBColor.from_string(color)
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn('w:rFonts'))
    if fonts is not None:
        for script in ('ascii', 'hAnsi', 'eastAsia', 'cs'):
            fonts.set(qn('w:' + script), _FONT_NAME)


def _shade(paragraph, fill):
    shade = OxmlElement('w:shd')
    shade.set(qn('w:fill'), fill)
    paragraph._p.get_or_add_pPr().append(shade)


def _docx_space(document):
    # An empty separator needs spacing, not an extra body-sized text line.
    spacing = document.add_paragraph().paragraph_format
    spacing.line_spacing = Pt(1)
    spacing.space_before = Pt(0)
    spacing.space_after = Pt(6)


def _docx_table(document, data):
    headers, rows = data['headers'], data['rows']
    if not headers or any(len(row) != len(headers) for row in rows):
        raise ValueError('Inconsistent report table.')
    table = document.add_table(rows=0, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    widths = data['widths']
    for column, width in zip(table.columns, widths):
        column.width = Cm(width)
    for index, row in enumerate([headers, *rows]):
        target = table.add_row()
        properties = target._tr.get_or_add_trPr()
        properties.append(OxmlElement('w:cantSplit'))
        if index == 0:
            properties.append(OxmlElement('w:tblHeader'))
        for cell, value, width in zip(target.cells, row, widths):
            cell.width = Cm(width)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_before = Pt(4)
            paragraph.paragraph_format.space_after = Pt(5)
            paragraph.paragraph_format.line_spacing = 1.02
            # Keep short scenario matrices and measured compact priority groups
            # together. Long catalogues retain normal row-by-row pagination.
            paragraph.paragraph_format.keep_with_next = bool(data.get('keep_together') and index < len(rows))
            run = paragraph.add_run(str(value))
            run.font.name = _FONT_NAME
            run.font.size = Pt(10.5)
            if index == 0:
                run.bold = True
                run.font.color.rgb = RGBColor.from_string(_COLORS['ink'])
            if index == 0 or index % 2 == 0:
                shade = OxmlElement('w:shd')
                shade.set(qn('w:fill'), _COLORS['blue'] if index == 0 else _COLORS['light'])
                cell._tc.get_or_add_tcPr().append(shade)
    _docx_space(document)



def _docx_panel(document, *, heading='', text='', fill='soft_green'):
    """A single semantic emphasis block, not an outer border around every paragraph."""
    table = document.add_table(rows=1, cols=1)
    table.autofit = False
    table.columns[0].width = Cm(17)
    cell = table.cell(0, 0); cell.width = Cm(17)
    properties = cell._tc.get_or_add_tcPr()
    shade = OxmlElement('w:shd'); shade.set(qn('w:fill'), _COLORS[fill]); properties.append(shade)
    margins = OxmlElement('w:tcMar')
    for side in ('top', 'left', 'bottom', 'right'):
        margin = OxmlElement('w:' + side); margin.set(qn('w:w'), '150'); margin.set(qn('w:type'), 'dxa'); margins.append(margin)
    properties.append(margins)
    paragraph = cell.paragraphs[0]
    if heading:
        run = paragraph.add_run(heading); run.bold = True; run.font.size = Pt(18); run.font.color.rgb = RGBColor.from_string(_COLORS['green'])
        paragraph.paragraph_format.keep_with_next = True
        paragraph.paragraph_format.space_after = Pt(8)
        paragraph = cell.add_paragraph()
    paragraph.add_run(text)
    paragraph.paragraph_format.space_after = Pt(2)
    _docx_space(document)


def _docx_highlights(document, items):
    # Three measured values fit a row; any additional values remain in later rows.
    for start in range(0, len(items), 3):
        batch = items[start:start+3]
        table = document.add_table(rows=1, cols=len(batch)); table.autofit = False
        width = 17 / len(batch)
        for column in table.columns: column.width = Cm(width)
        for cell, item in zip(table.rows[0].cells, batch):
            cell.width = Cm(width); cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
            shade = OxmlElement('w:shd'); shade.set(qn('w:fill'), _COLORS['blue']); cell._tc.get_or_add_tcPr().append(shade)
            paragraph = cell.paragraphs[0]
            run = paragraph.add_run(str(item['value'])); run.bold = True; run.font.size = Pt(19); run.font.color.rgb = RGBColor.from_string(_COLORS['green'])
            paragraph.paragraph_format.space_before = Pt(7)
            paragraph.paragraph_format.space_after = Pt(4)
            paragraph = cell.add_paragraph(item['label']); paragraph.runs[0].bold = True
            for text in (f"№ {item['number']}. {item['basis']}", item['status'], item.get('note', '')):
                if text:
                    paragraph = cell.add_paragraph(str(text)); paragraph.paragraph_format.space_after = Pt(4)
                    for run in paragraph.runs: run.font.size = Pt(10.5)
        _docx_space(document)


def _docx_flow(document, items):
    paragraph = document.add_paragraph('Предлагаемая работа')
    paragraph.paragraph_format.space_after = Pt(4)
    for run in paragraph.runs: run.font.size = Pt(10.5)
    if not 3 <= len(items) <= 5:
        document.add_paragraph(' → '.join(items))
        return
    count = len(items); arrow = .4; step = (17 - arrow*(count-1)) / count
    table = document.add_table(rows=1, cols=count*2-1); table.autofit = False
    for index, (column, cell) in enumerate(zip(table.columns, table.rows[0].cells)):
        column.width = cell.width = Cm(step if index % 2 == 0 else arrow)
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        paragraph = cell.paragraphs[0]
        paragraph.paragraph_format.space_before = paragraph.paragraph_format.space_after = Pt(5)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.add_run(items[index//2] if index % 2 == 0 else '→')
        for run in paragraph.runs: run.font.size = Pt(10.5)
        if index % 2 == 0:
            shade = OxmlElement('w:shd'); shade.set(qn('w:fill'), _COLORS['blue']); cell._tc.get_or_add_tcPr().append(shade)
    _docx_space(document)

def render_docx(artifact, *, view=None):
    """Editable A4 management memo. Detailed proofs are a separate bundle member."""
    view = management_view(artifact) if view is None else view
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(1.8)
    section.left_margin = section.right_margin = Cm(2)
    section.header_distance = section.footer_distance = Cm(0.8)
    styles = document.styles
    _font(styles['Normal'], 11, color=_COLORS['ink'])
    styles['Normal'].paragraph_format.space_after = Pt(6)
    styles['Normal'].paragraph_format.line_spacing = 1.08
    styles['Normal'].paragraph_format.widow_control = True
    for name, size in [('Heading 1', 16), ('Heading 2', 12)]:
        _font(styles[name], size, bold=True, color=_COLORS['green'] if name == 'Heading 1' else _COLORS['ink'])
        styles[name].paragraph_format.space_before = Pt(12)
        styles[name].paragraph_format.space_after = Pt(7)
        styles[name].paragraph_format.keep_with_next = True
    _font(styles['List Bullet'], 11, color=_COLORS['ink'])
    # Liberation Sans is metrically compatible with Arial. Word can use the
    # declared alternative if the bundled PDF typeface is absent on the device.
    for part in document.part.package.parts:
        if str(part.partname) == '/word/fontTable.xml':
            fonts = parse_xml(part.blob)
            font = OxmlElement('w:font'); font.set(qn('w:name'), _FONT_NAME)
            alternate = OxmlElement('w:altName'); alternate.set(qn('w:val'), 'Arial'); font.append(alternate)
            family = OxmlElement('w:family'); family.set(qn('w:val'), 'swiss'); font.append(family)
            fonts.append(font)
            part._blob = etree.tostring(fonts, xml_declaration=True, encoding='UTF-8', standalone=True)
            break
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run('Страница ').font.size = Pt(10.5)
    field = OxmlElement('w:fldSimple'); field.set(qn('w:instr'), 'PAGE'); footer._p.append(field)
    document.core_properties.title = view['title'][:255]
    document.core_properties.subject = 'Варианты внедрения ИИ и условия проверки'
    document.core_properties.author = 'GovAI Opportunity Engine'
    document.core_properties.keywords = ''
    for kind, value in _blocks(view):
        if kind in ('heading', 'subheading'):
            style = {'heading': 'Heading 1', 'subheading': 'Heading 2'}[kind]
            document.add_paragraph(str(value), style=style)
        elif kind == 'conclusion':
            _docx_panel(document, heading=value['heading'], text=value['text'])
        elif kind == 'highlights':
            _docx_highlights(document, value)
        elif kind == 'group':
            paragraph = document.add_paragraph(value['title'], style='Heading 1')
            _shade(paragraph, _COLORS[_group_fill(value['key'])])
            if value.get('explanation'):
                paragraph = document.add_paragraph(value['explanation'])
                paragraph.paragraph_format.keep_with_next = True
        elif kind == 'card':
            document.add_paragraph(f"№ {value['number']}. {value['title']}", style='Heading 2')
            document.add_paragraph(value['narrative'])
            if value.get('flow'):
                _docx_flow(document, value['flow'])
            if value.get('effect'):
                paragraph = document.add_paragraph(value['effect'])
                _shade(paragraph, _COLORS['blue'])
            if value.get('resources'):
                document.add_paragraph(value['resources'])
            paragraph = document.add_paragraph(value['reason'] + ' ' + value['check'])
            paragraph.paragraph_format.space_after = Pt(5)
            if value.get('condition'):
                paragraph = document.add_paragraph(value['condition']); _shade(paragraph, _COLORS['sand'])
                paragraph.paragraph_format.keep_with_next = True
            paragraph = document.add_paragraph('Основание: ' + value['clauses'])
            paragraph.paragraph_format.space_after = Pt(13)
            for run in paragraph.runs: run.font.size = Pt(10.5)
        elif kind == 'brief':
            heading = document.add_paragraph(f"№ {value['number']}. {value['title']}")
            heading.runs[0].bold = True
            heading.paragraph_format.keep_with_next = True
            heading.paragraph_format.space_after = Pt(3)
            paragraph = document.add_paragraph(value['category_label'] + '. ' + value['benefit'] +
                                               ' Условие: ' + value['condition'])
            paragraph.paragraph_format.space_after = Pt(8)
        elif kind == 'table':
            _docx_table(document, value)
        elif kind == 'notice':
            _docx_panel(document, text=value['text'], fill=value.get('fill', 'blue'))
        else:
            paragraph = document.add_paragraph(str(value), style='List Bullet' if kind == 'bullet' else None)
            if kind == 'requisites':
                paragraph.paragraph_format.space_after = Pt(8)
                for run in paragraph.runs: run.font.size = Pt(10.5)
            if kind == 'source':
                paragraph.paragraph_format.space_after = Pt(3)
    stream = BytesIO(); document.save(stream); return stream.getvalue()


def render_pdf(artifact, *, view=None):
    """Selectable Cyrillic text using bundled fonts; no external conversion service."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
    view = management_view(artifact) if view is None else view
    _register_pdf_fonts()
    styles = {
        'body': ParagraphStyle('body', fontName='GovAISans', fontSize=11, leading=14, spaceAfter=7,
                               textColor=colors.HexColor('#'+_COLORS['ink']), alignment=TA_LEFT,
                               allowWidows=False, allowOrphans=False),
        'title': ParagraphStyle('title', fontName='GovAISansBold', fontSize=18, leading=22, spaceAfter=10,
                                textColor=colors.HexColor('#'+_COLORS['green'])),
        'heading': ParagraphStyle('heading', fontName='GovAISansBold', fontSize=16, leading=19, spaceBefore=10,
                                  spaceAfter=10, textColor=colors.HexColor('#'+_COLORS['green']), keepWithNext=True),
        'subheading': ParagraphStyle('subheading', fontName='GovAISansBold', fontSize=12, leading=15,
                                     spaceBefore=8, spaceAfter=7, keepWithNext=True),
        'table': ParagraphStyle('table', fontName='GovAISans', fontSize=10.5, leading=12.3, spaceAfter=0),
        'tablehead': ParagraphStyle('tablehead', fontName='GovAISansBold', fontSize=10.5, leading=12.3),
    }
    styles['brief_title'] = ParagraphStyle('brief_title', parent=styles['body'], spaceAfter=3)
    # Keep the category heading with its explanation, not with the entire next
    # table. Tables repeat their headers and may use the remaining page space.
    styles['group_intro'] = ParagraphStyle('group_intro', parent=styles['body'])
    styles['highlight'] = ParagraphStyle('highlight', fontName='GovAISansBold', fontSize=19, leading=22, spaceAfter=5, textColor=colors.HexColor('#'+_COLORS['green']))
    styles['small'] = ParagraphStyle('small', parent=styles['body'], fontSize=10.5, leading=13, spaceAfter=4)
    styles['flow'] = ParagraphStyle('flow', parent=styles['small'], alignment=1, spaceAfter=0)
    def paragraph(text, style='body'):
        return Paragraph(escape(str(text)).replace('\n', '<br/>'), styles[style])
    stream = BytesIO()
    doc = BaseDocTemplate(stream, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm,
                           topMargin=1.8*cm, bottomMargin=1.8*cm, title=view['title'],
                           author='GovAI Opportunity Engine', subject='Варианты внедрения ИИ и условия проверки')
    def panel(contents, fill='blue'):
        table = Table([[contents]], colWidths=[17*cm], splitInRow=1)
        table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#'+_COLORS[fill])),
                                  ('LEFTPADDING',(0,0),(-1,-1),10),('RIGHTPADDING',(0,0),(-1,-1),10),
                                  ('TOPPADDING',(0,0),(-1,-1),9),('BOTTOMPADDING',(0,0),(-1,-1),7)]))
        # A small panel belongs together. Otherwise splitInRow can leave only its
        # coloured top padding at the foot of the previous page.
        if table.wrap(doc.width, doc.height)[1] <= doc.height:
            table.splitInRow = 0
        return table

    def flow_steps(items):
        if not 3 <= len(items) <= 5:
            return [paragraph('Предлагаемая работа', 'small'), paragraph(' → '.join(items))]
        arrow = .4*cm; width = (17*cm-arrow*(len(items)-1))/len(items)
        cells=[]; widths=[]; styles_flow=[('VALIGN',(0,0),(-1,-1),'MIDDLE'),
            ('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),4),
            ('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7)]
        for index, item in enumerate(items):
            if index:
                cells.append(paragraph('→', 'flow')); widths.append(arrow)
            cells.append(paragraph(item,'flow')); widths.append(width)
            styles_flow.append(('BACKGROUND',(index*2,0),(index*2,0),colors.HexColor('#'+_COLORS['blue'])))
        table=Table([cells],colWidths=widths,splitInRow=1);table.setStyle(TableStyle(styles_flow))
        if table.wrap(doc.width, doc.height)[1] <= doc.height:
            table.splitInRow = 0
        return [paragraph('Предлагаемая работа', 'small'),table,Spacer(1,8)]

    story = []
    for kind, value in _blocks(view):
        if kind in ('heading', 'subheading'): story.append(paragraph(value, kind))
        elif kind == 'conclusion':
            story += [panel([paragraph(value['heading'],'title'),paragraph(value['text'])], 'soft_green'),Spacer(1,12)]
        elif kind == 'highlights':
            for start in range(0, len(value), 3):
                items=value[start:start+3];cells=[]
                for item in items:
                    cell=[paragraph(item['value'],'highlight'),Paragraph('<b>'+escape(item['label'])+'</b>',styles['body']),
                          paragraph(f"№ {item['number']}. {item['basis']}",'small'),paragraph(item['status'],'small')]
                    if item.get('note'):cell.append(paragraph(item['note'],'small'))
                    cells.append(cell)
                table=Table([cells],colWidths=[17*cm/len(cells)]*len(cells),splitInRow=1)
                table.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),
                    ('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#'+_COLORS['blue'])),
                    ('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),
                    ('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
                if table.wrap(doc.width, doc.height)[1] <= doc.height:
                    table.splitInRow = 0
                story += [table,Spacer(1,12)]
        elif kind == 'group':
            heading=Paragraph(escape(value['title']),ParagraphStyle('group',parent=styles['heading'],
                backColor=colors.HexColor('#'+_COLORS[_group_fill(value['key'])]),borderPadding=7,spaceBefore=9,spaceAfter=12))
            story.append(heading)
            if value.get('explanation'):story.append(paragraph(value['explanation'], 'group_intro'))
        elif kind == 'card':
            story += [paragraph(f"№ {value['number']}. {value['title']}", 'subheading'), paragraph(value['narrative'])]
            if value.get('flow'):story += flow_steps(value['flow'])
            if value.get('effect'):story += [panel(paragraph(value['effect']), 'blue'),Spacer(1,6)]
            if value.get('resources'):story.append(paragraph(value['resources']))
            story.append(paragraph(value['reason'] + ' ' + value['check']))
            if value.get('condition'):
                condition = panel(paragraph(value['condition']), 'sand')
                condition.keepWithNext = True
                gap = Spacer(1,5); gap.keepWithNext = True
                story += [condition,gap]
            story += [paragraph('Основание: '+value['clauses'], 'small'),Spacer(1,10)]
        elif kind == 'brief':
            title = Paragraph('<b>' + escape(f"№ {value['number']}. {value['title']}") + '</b>',styles['brief_title'])
            text = paragraph(value['category_label'] + '. ' + value['benefit'] + ' Условие: ' + value['condition'])
            story.append(KeepTogether([title,text,Spacer(1,3)]))
        elif kind == 'table':
            rows = [[paragraph(cell, 'tablehead' if index == 0 else 'table') for cell in row]
                    for index, row in enumerate([value['headers'], *value['rows']])]
            table = Table(rows, colWidths=[width*cm for width in value['widths']], repeatRows=1,
                          splitInRow=1, hAlign='LEFT')
            commands = [('VALIGN',(0,0),(-1,-1),'TOP'), ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#'+_COLORS['blue'])),
                        ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),
                        ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),6),
                        ('LINEBELOW',(0,0),(-1,0),0.5,colors.HexColor('#'+_COLORS['rule']))]
            for row in range(2,len(rows),2): commands.append(('BACKGROUND',(0,row),(-1,row),colors.HexColor('#'+_COLORS['light'])))
            table.setStyle(TableStyle(commands)); story += [table, Spacer(1,8)]
        elif kind == 'notice':
            story += [panel(paragraph(value['text']), value.get('fill','blue')),Spacer(1,8)]
        else:
            story.append(paragraph(('• ' if kind == 'bullet' else '')+str(value)))
    def page(canvas, document):
        canvas.saveState(); canvas.setFont('GovAISans',10.5); canvas.setFillColor(colors.HexColor('#'+_COLORS['ink']))
        canvas.drawRightString(A4[0]-2*cm,0.9*cm,f'Страница {document.page}');canvas.restoreState()
    # Explicit zero frame padding keeps the text and table within the same 17 cm.
    # The library paginates real block heights; overflow never discards a report.
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates(PageTemplate(id='management', frames=[frame], onPage=page))
    doc.build(story)
    return stream.getvalue()


def pdf_layout_status(pdf_data):
    """Describe the generated PDF without rebuilding it or changing its contents."""
    from pypdf import PdfReader
    pages = len(PdfReader(BytesIO(pdf_data)).pages)
    exceeded = pages > 6
    return {'pages': pages, 'brief_limit': 6, 'exceeded': exceeded,
            'message': (f'В отчёте {pages} страниц. Краткий формат до шести страниц не удалось '
                        'соблюсти без потери рекомендаций и условий; полный PDF доступен для скачивания.'
                        if exceeded else None)}


def render_bundle(artifact, *, pdf_data=_UNSET, docx_data=_UNSET, markdown_data=_UNSET,
                  json_data=_UNSET, details_data=_UNSET, view=None):
    """Bundle prepared bytes. Explicit None omits a failed format, with a clear notice."""
    view = management_view(artifact) if view is None else view
    if json_data is _UNSET: json_data = json.dumps(artifact,ensure_ascii=False,indent=2).encode('utf-8')
    if json_data is None or json.loads(json_data) != artifact:
        raise ValueError('Bundle requires the complete JSON describing this result.')
    if pdf_data is _UNSET: pdf_data = render_pdf(artifact,view=view)
    if docx_data is _UNSET: docx_data = render_docx(artifact,view=view)
    if markdown_data is _UNSET: markdown_data = render_markdown(artifact,view=view).encode('utf-8')
    if details_data is _UNSET: details_data = render_details(artifact).encode('utf-8')
    layout, pdf_unverified = None, False
    if pdf_data is not None:
        try:
            layout = pdf_layout_status(pdf_data)
        except Exception:
            # An unreadable PDF must not invalidate the complete structured
            # result or prevent downloading the other independently prepared files.
            pdf_data, pdf_unverified = None, True
    candidates={'opportunity-report.pdf':pdf_data,'opportunity-report.docx':docx_data,
                'opportunity-report.md':markdown_data,'opportunities.json':json_data,
                'opportunity-details.txt':details_data}
    missing=[name for name,data in candidates.items() if data is None]
    details=artifact['report']['document']
    description='\n'.join([
        'GovAI Opportunity Engine — предварительные рекомендации',
        'Документ: '+str(details.get('title') or view['requisites']),
        'Редакция: '+str(details.get('revision_date') or 'Не определена'),'',
        'PDF, DOCX и Markdown — одна краткая управленческая редакция.',
        'opportunities.json — полный структурированный результат. opportunity-details.txt — полные источники, методика и ограничения.',
        'Ожидаемая польза и иллюстративные сценарии требуют проверки; это не измеренные показатели ведомства или бюджетная экономия.',
        'Для восстановления нужны соответствующие НПА, processes.json и normalized_processes.json, сохранённые отдельно.',
        'Восстановите JSON в техническом разделе приложения. Новые API-запросы не нужны.',
        'Сессия приложения не является постоянным хранилищем; сохраните архив у себя.',
        *([layout['message']] if layout and layout['exceeded'] else []),
        *(['PDF не включён: не удалось проверить подготовленный файл. Остальные форматы доступны.'] if pdf_unverified else []),
        *(['ВНИМАНИЕ: не удалось подготовить '+', '.join(missing)+'. Остальные файлы и полный JSON сохранены.'] if missing else []),
    ])+'\n'
    stream=BytesIO()
    with ZipFile(stream,'w',compression=ZIP_DEFLATED) as archive:
        for name,data in candidates.items():
            if data is not None: archive.writestr(name,data)
        archive.writestr('README.txt',description.encode('utf-8'))
    return stream.getvalue()
