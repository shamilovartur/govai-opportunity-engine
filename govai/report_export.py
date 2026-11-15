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

EXPORT_VERSION = 'management-1'
_FONT_DIR = Path(__file__).with_name('fonts')
_FONT_NAME = 'Liberation Sans'
_UNSET = object()
_COLORS = {'ink': '283438', 'green': '3F6B57', 'blue': 'EAF0F4', 'light': 'F4F5F4', 'rule': 'D5DDDF'}


def _blocks(view):
    """One layout sequence for both document formats, containing only shared wording."""
    count = len(view['initiatives'])
    yield ('title', view['title'])
    yield ('requisites', view['requisites'])
    yield ('heading', 'Главное для руководителя')
    for block in view['summary_blocks']:
        yield ('summary', block)
    if count:
        if count >= 5:
            yield ('page', None)
        yield ('heading', 'Предлагаемые инициативы')
        yield ('paragraph', view.get('ordering_note') or 'Номера обозначают предварительную очередь проверки. Категории не являются оценкой окупаемости.')
        if count > 12:
            # Long lists need the full text width, not ever narrower table cells.
            # Every title, category, benefit and condition stays visible.
            for initiative in view['initiatives']:
                yield ('brief', initiative)
        else:
            yield ('table', {'headers': ['№ и название', 'Перспективность', 'Практическая польза', 'Главное условие'],
                            'rows': [[f"№ {a['number']}. {a['title']}", a['category_label'], a['benefit'], a['condition']]
                                     for a in view['initiatives']],
                            'widths': [4.5, 3.7, 4.4, 4.4]})
    if view['pilots']:
        if 5 <= count <= 12:
            yield ('page', None)
        yield ('heading', 'С чего начать')
        for pilot in view['pilots']:
            yield ('card', pilot)
    if view['effects']:
        if 5 <= count <= 12:
            yield ('page', None)
        yield ('heading', 'Какой эффект можно получить')
        for effect in view['effects']:
            yield ('subheading', f"№ {effect['number']}. {effect['title']}")
            yield ('notice', effect['notice'])
            yield ('paragraph', effect['boundary'])
            yield ('table', {'headers': effect['headers'], 'rows': effect['rows'],
                            'widths': [17 / len(effect['headers'])] * len(effect['headers'])})
            yield ('paragraph', effect['formula'])
    if count and view.get('effect_note'):
        if not view['effects']:
            yield ('heading', 'Какой эффект можно получить')
        yield ('paragraph', view['effect_note'])
    if 5 <= count <= 12 and view['effects']:
        yield ('page', None)
    yield ('heading', 'Условия запуска и основания')
    for condition in view['conditions']:
        yield ('bullet', condition)
    if view['source_index']:
        yield ('subheading', 'Пункты нормативного акта')
        for start in range(0, len(view['source_index']), 2):
            yield ('source', '; '.join(f"№ {source['number']} — {source['clauses']}"
                                      for source in view['source_index'][start:start+2]))
    yield ('paragraph', view['detail_note'])
    if view['full_title']:
        yield ('subheading', 'Документ')
        yield ('paragraph', view['full_title'])


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
            paragraph.paragraph_format.keep_with_next = False
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
    document.add_paragraph().paragraph_format.space_after = Pt(2)


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
    for name, size in [('Title', 18), ('Heading 1', 16), ('Heading 2', 12)]:
        _font(styles[name], size, bold=True, color=_COLORS['green'] if name == 'Heading 1' else _COLORS['ink'])
        styles[name].paragraph_format.space_before = Pt(12 if name != 'Title' else 0)
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
    header = section.header.paragraphs[0]
    header.add_run('GovAI · Предварительные рекомендации').font.size = Pt(10.5)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run('Страница ').font.size = Pt(10.5)
    field = OxmlElement('w:fldSimple'); field.set(qn('w:instr'), 'PAGE'); footer._p.append(field)
    document.core_properties.title = view['title'][:255]
    document.core_properties.subject = 'Предложения для проверки на пилоте'
    document.core_properties.author = 'GovAI Opportunity Engine'
    document.core_properties.keywords = ''
    new_page = False
    for kind, value in _blocks(view):
        if kind == 'page':
            new_page = True
        elif kind in ('title', 'heading', 'subheading'):
            style = {'title': 'Title', 'heading': 'Heading 1', 'subheading': 'Heading 2'}[kind]
            paragraph = document.add_paragraph(str(value), style=style)
            if new_page:
                paragraph.paragraph_format.page_break_before = True
                new_page = False
        elif kind == 'summary':
            label = document.add_paragraph(value['label'])
            label.runs[0].bold = True
            label.runs[0].font.color.rgb = RGBColor.from_string(_COLORS['green'])
            label.paragraph_format.keep_with_next = True
            document.add_paragraph(value['text'])
        elif kind == 'card':
            title = document.add_paragraph(f"№ {value['number']}. {value['title']} · {value['category_label']}", style='Heading 2')
            parts = []
            for block in value['blocks']:
                paragraph = document.add_paragraph()
                paragraph.add_run(block['label'] + ' ').bold = True
                paragraph.add_run(block['text'])
                paragraph.paragraph_format.space_after = Pt(4)
                parts.append(paragraph)
            parts.append(document.add_paragraph('Пункты НПА: ' + value['clauses']))
            for paragraph in [title, *parts[:-1]]:
                paragraph.paragraph_format.keep_with_next = True
            parts[-1].paragraph_format.space_after = Pt(12)
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
        else:
            paragraph = document.add_paragraph(str(value), style='List Bullet' if kind == 'bullet' else None)
            if kind == 'requisites':
                paragraph.paragraph_format.space_after = Pt(8)
                for run in paragraph.runs: run.font.size = Pt(10.5)
            if kind == 'notice':
                _shade(paragraph, _COLORS['blue'])
                paragraph.paragraph_format.space_before = Pt(4)
                paragraph.paragraph_format.space_after = Pt(8)
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
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
    view = management_view(artifact) if view is None else view
    for name, filename in [('GovAISans', 'LiberationSans-Regular.ttf'), ('GovAISansBold', 'LiberationSans-Bold.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(_FONT_DIR / filename)))
    pdfmetrics.registerFontFamily('GovAISans', normal='GovAISans', bold='GovAISansBold', italic='GovAISans', boldItalic='GovAISansBold')
    styles = {
        'body': ParagraphStyle('body', fontName='GovAISans', fontSize=11, leading=14, spaceAfter=7,
                               textColor=colors.HexColor('#'+_COLORS['ink']), alignment=TA_LEFT),
        'title': ParagraphStyle('title', fontName='GovAISansBold', fontSize=18, leading=22, spaceAfter=10),
        'heading': ParagraphStyle('heading', fontName='GovAISansBold', fontSize=16, leading=19, spaceBefore=10,
                                  spaceAfter=10, textColor=colors.HexColor('#'+_COLORS['green']), keepWithNext=True),
        'subheading': ParagraphStyle('subheading', fontName='GovAISansBold', fontSize=12, leading=15,
                                     spaceBefore=8, spaceAfter=7, keepWithNext=True),
        'table': ParagraphStyle('table', fontName='GovAISans', fontSize=10.5, leading=12.3, spaceAfter=0),
        'tablehead': ParagraphStyle('tablehead', fontName='GovAISansBold', fontSize=10.5, leading=12.3),
    }
    styles['card'] = ParagraphStyle('card', parent=styles['body'], spaceAfter=4)
    styles['brief_title'] = ParagraphStyle('brief_title', parent=styles['body'], spaceAfter=3)
    def paragraph(text, style='body'):
        return Paragraph(escape(str(text)).replace('\n', '<br/>'), styles[style])
    stream = BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=1.8*cm, bottomMargin=1.8*cm, title=view['title'],
                            author='GovAI Opportunity Engine', subject='Предложения для проверки на пилоте')
    story = []
    for kind, value in _blocks(view):
        if kind == 'page': story.append(PageBreak())
        elif kind in ('title', 'heading', 'subheading'): story.append(paragraph(value, kind))
        elif kind == 'summary':
            story += [paragraph(value['label'], 'subheading'), paragraph(value['text'])]
        elif kind == 'card':
            card = [paragraph(f"№ {value['number']}. {value['title']} · {value['category_label']}", 'subheading')]
            for block in value['blocks']:
                card.append(Paragraph('<b>'+escape(block['label'])+'</b> '+escape(block['text']).replace('\n','<br/>'), styles['card']))
            card += [paragraph('Пункты НПА: '+value['clauses']), Spacer(1, 9)]
            story.append(KeepTogether(card))
        elif kind == 'brief':
            title = Paragraph('<b>' + escape(f"№ {value['number']}. {value['title']}") + '</b>',styles['brief_title'])
            text = paragraph(value['category_label'] + '. ' + value['benefit'] + ' Условие: ' + value['condition'])
            story.append(KeepTogether([title,text,Spacer(1,3)]))
        elif kind == 'table':
            rows = [[paragraph(cell, 'tablehead' if index == 0 else 'table') for cell in row]
                    for index, row in enumerate([value['headers'], *value['rows']])]
            table = Table(rows, colWidths=[width*cm for width in value['widths']], repeatRows=1, hAlign='LEFT')
            commands = [('VALIGN',(0,0),(-1,-1),'TOP'), ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#'+_COLORS['blue'])),
                        ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),
                        ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),6),
                        ('LINEBELOW',(0,0),(-1,0),0.5,colors.HexColor('#'+_COLORS['rule']))]
            for row in range(2,len(rows),2): commands.append(('BACKGROUND',(0,row),(-1,row),colors.HexColor('#'+_COLORS['light'])))
            table.setStyle(TableStyle(commands)); story += [table, Spacer(1,8)]
        elif kind == 'notice':
            panel = Table([[paragraph(value)]], colWidths=[17*cm])
            panel.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#'+_COLORS['blue'])),
                                      ('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),
                                      ('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),4)]))
            story += [panel,Spacer(1,8)]
        else:
            story.append(paragraph(('• ' if kind == 'bullet' else '')+str(value)))
    def page(canvas, document):
        if document.page > 6:
            raise ValueError('Основной отчёт превышает шесть страниц. Сократите редакционные формулировки; исходный результат сохранён.')
        canvas.saveState(); canvas.setFont('GovAISans',10.5); canvas.setFillColor(colors.HexColor('#'+_COLORS['ink']))
        canvas.drawString(2*cm,A4[1]-1.1*cm,'GovAI · Предварительные рекомендации')
        canvas.drawRightString(A4[0]-2*cm,0.9*cm,f'Страница {document.page}');canvas.restoreState()
    doc.build(story,onFirstPage=page,onLaterPages=page)
    return stream.getvalue()


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
        *(['ВНИМАНИЕ: не удалось подготовить '+', '.join(missing)+'. Остальные файлы и полный JSON сохранены.'] if missing else []),
    ])+'\n'
    stream=BytesIO()
    with ZipFile(stream,'w',compression=ZIP_DEFLATED) as archive:
        for name,data in candidates.items():
            if data is not None: archive.writestr(name,data)
        archive.writestr('README.txt',description.encode('utf-8'))
    return stream.getvalue()
