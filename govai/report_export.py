"""DOCX and ZIP presentations of the existing report; no generation or provider calls."""

from io import BytesIO
import json
import re
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from govai.opportunity_report import render_markdown


def _plain(text):
    # Undo only the escaping performed by the shared Markdown renderer.
    return re.sub(r'\\([\\`*_{}\[\]()#+.!<>|])', r'\1', text)


def _runs(paragraph, text):
    """Render the shared template's bold spans, never active links or HTML."""
    start = 0
    for match in re.finditer(r'(?<!\\)\*\*(.*?)(?<!\\)\*\*', text):
        paragraph.add_run(_plain(text[start:match.start()]))
        paragraph.add_run(_plain(match.group(1))).bold = True
        start = match.end()
    paragraph.add_run(_plain(text[start:]))


def _table(document, rows):
    cells = [[part.strip() for part in re.split(r'(?<!\\)\|', row)[1:-1]] for row in rows]
    cells = [row for row in cells if not all(re.fullmatch(r':?-+:?', part.strip()) for part in row)]
    count = len(cells[0])
    if not count or any(len(row) != count for row in cells):
        raise ValueError('Inconsistent report table.')
    table = document.add_table(rows=0, cols=count)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    table.style = 'Table Grid'
    widths = [4.7, 4.0, 4.6, 3.7] if count == 4 else [17 / count] * count
    for column, width in zip(table.columns, widths):
        column.width = Cm(width)
    for index, row in enumerate(cells):
        target = table.add_row()
        if index == 0:
            header = OxmlElement('w:tblHeader')
            target._tr.get_or_add_trPr().append(header)
        for cell, value, width in zip(target.cells, row, widths):
            cell.width = Cm(width)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
            paragraph = cell.paragraphs[0]
            _runs(paragraph, value)
            fmt = paragraph.paragraph_format
            fmt.space_before, fmt.space_after = Pt(2), Pt(4)
            fmt.line_spacing = 1.0
            fmt.keep_with_next = False
            fmt.widow_control = True
            for run in paragraph.runs:
                run.font.size = Pt(9)
                if index == 0:
                    run.bold = True
                    run.font.color.rgb = RGBColor.from_string('243B53')
            if index == 0:
                shading = OxmlElement('w:shd')
                shading.set(qn('w:fill'), 'EAF0F5')
                cell._tc.get_or_add_tcPr().append(shading)
    # A small separator only, not a page break; long rows may continue naturally.
    document.add_paragraph().paragraph_format.space_after = Pt(2)


def render_docx(artifact):
    """Format the shared report text as an editable A4 document in memory."""
    markdown = render_markdown(artifact)
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin, section.bottom_margin = Cm(1.8), Cm(1.8)
    section.left_margin, section.right_margin = Cm(2), Cm(2)
    section.header_distance, section.footer_distance = Cm(0.8), Cm(0.8)
    styles = document.styles
    normal = styles['Normal']
    normal.font.name, normal.font.size = 'Arial', Pt(10.5)
    normal.paragraph_format.space_after = Pt(5)
    normal.paragraph_format.line_spacing = 1.08
    normal.paragraph_format.widow_control = True
    for name, size in [('Title', 20), ('Heading 1', 15), ('Heading 2', 12), ('Heading 3', 11)]:
        style = styles[name]
        style.font.name, style.font.size = 'Arial', Pt(size)
        style.font.color.rgb = RGBColor.from_string('243B53')
        style.paragraph_format.space_before = Pt(12 if name != 'Title' else 0)
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.keep_with_next = True
    styles['Quote'].font.name, styles['Quote'].font.size = 'Arial', Pt(9.5)
    styles['Quote'].font.italic = False
    styles['Quote'].paragraph_format.left_indent = Cm(0.5)
    styles['Quote'].paragraph_format.right_indent = Cm(0.2)
    styles['Quote'].paragraph_format.keep_together = True
    header = section.header.paragraphs[0]
    header.add_run('GovAI Opportunity Engine • Предварительный анализ').font.size = Pt(8)
    header.paragraph_format.space_after = Pt(0)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run('Страница ').font.size = Pt(8)
    field = OxmlElement('w:fldSimple')
    field.set(qn('w:instr'), 'PAGE')
    footer._p.append(field)
    details = artifact['report']['document']
    # OOXML core properties have a 255-character limit; the full title remains in
    # the visible report. Use short requisites for file metadata rather than cut it.
    requisites = ' '.join(str(details.get(k) or '') for k in ('act_type', 'number', 'adoption_date')).strip()
    document.core_properties.title = ('AI-возможности: ' + requisites)[:255] if requisites else 'AI-возможности и ожидаемый эффект'
    document.core_properties.subject = 'Предварительные гипотезы для пилота, не инвестиционное заключение'
    document.core_properties.author = 'GovAI Opportunity Engine'
    document.core_properties.keywords = ''
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if re.fullmatch(r'`{3,}text', line):
            fence = line[:-4]
            index += 1
            quote = []
            while index < len(lines) and lines[index] != fence:
                quote.append(lines[index]); index += 1
            if index == len(lines):
                raise ValueError('Unclosed report quotation.')
            # Source quotations are literal, not Markdown instructions.
            document.add_paragraph('\n'.join(quote), style='Quote')
        elif line.startswith('|'):
            rows = []
            while index < len(lines) and lines[index].startswith('|'):
                rows.append(lines[index]); index += 1
            _table(document, rows)
            continue
        elif line.startswith('# '):
            _runs(document.add_paragraph(style='Title'), line[2:])
        elif line.startswith('## '):
            _runs(document.add_heading(level=1), line[3:])
        elif line.startswith('### '):
            _runs(document.add_heading(level=2), line[4:])
        elif line.startswith('- '):
            _runs(document.add_paragraph(style='List Bullet'), line[2:])
        else:
            paragraph = document.add_paragraph()
            _runs(paragraph, line)
            if (line.startswith('Пункт ') and line.endswith(':')) or re.fullmatch(r'\*\*.+\*\*', line):
                paragraph.paragraph_format.keep_with_next = True
        index += 1
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def render_bundle(artifact, *, docx_data=None, markdown_data=None, json_data=None):
    """One complete portable bundle, never an archive silently missing its JSON."""
    docx_data = render_docx(artifact) if docx_data is None else docx_data
    markdown_data = render_markdown(artifact).encode('utf-8') if markdown_data is None else markdown_data
    json_data = json.dumps(artifact, ensure_ascii=False, indent=2).encode('utf-8') if json_data is None else json_data
    # Prevent accidentally bundling a different cached artifact with this report.
    if json.loads(json_data) != artifact:
        raise ValueError('Bundle JSON does not describe this result.')
    details = artifact['report']['document']
    values = [str(details.get(k)) for k in ('act_type', 'number', 'adoption_date') if details.get(k)]
    description = '\n'.join([
        'GovAI Opportunity Engine — предварительные результаты',
        'Документ: ' + (' '.join(values) or 'Реквизиты не определены'),
        'Название: ' + str(details.get('title') or 'Не указано'),
        'Редакция: ' + str(details.get('revision_date') or 'Не определена'),
        '',
        'Рекомендации — гипотезы для проверки на пилоте. Сценарные числа не являются фактическими показателями ведомства.',
        'Нормативные цитаты подтверждают операции, а не эффективность ИИ, фактические трудозатраты или окупаемость.',
        'DOCX и Markdown содержат один отчёт. opportunities.json — полный структурированный результат для проверки и восстановления.',
        'Для восстановления нужны соответствующие исходный НПА, processes.json и normalized_processes.json. Они не включены в архив.',
        'Сохраните этот архив у себя: приложение без постоянного хранилища не гарантирует восстановление после потери сессии.',
    ]) + '\n'
    files = {'opportunity-report.docx': docx_data, 'opportunity-report.md': markdown_data,
             'opportunities.json': json_data, 'README.txt': description.encode('utf-8')}
    stream = BytesIO()
    with ZipFile(stream, 'w', compression=ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return stream.getvalue()
