"""Document presentation; heavy source views are rendered only in Technical data."""

import json
from pathlib import Path

import streamlit as st


def document_header(document):
    m = document['metadata']
    def value(key):
        return m[key]['value'] if m.get(key) else 'не определено'
    st.subheader(f"{value('act_type')} № {value('number')}")
    st.caption(f"Дата принятия: {value('adoption_date')} · Редакция: {value('revision_date')}")
    if m.get('title'):
        title = ' '.join(m['title']['value'].split())
        st.caption(title if len(title) <= 180 else title[:177] + '…')
    if document['source'].get('type') == 'url_html':
        st.warning('Извлечён основной текст HTML-страницы. Полнота НПА автоматически не подтверждена; '
                   'сверьте документ с источником. Номера страниц для HTML неизвестны.')
    if any(w.startswith('pdf_page_without_text:') for w in document['warnings']):
        st.warning('На некоторых страницах PDF нет извлекаемого текста. Проверьте оригинал.')


def technical_document(document):
    st.subheader('Структура документа')
    for label, key in [('Название', 'title'), ('Номер', 'number'),
                       ('Дата принятия', 'adoption_date'), ('Редакция', 'revision_date')]:
        item = document['metadata'][key]
        st.write(f"{label}: {item['value'] if item else 'не определено'}")
    if document['source'].get('original_url'):
        st.write('Источник:', document['source']['original_url'])
        st.caption('Получено (UTC): ' + document['source']['retrieved_at'])
    st.caption(f"Символов: {len(document['text']):,}")
    st.text_area('Извлечённый текст', value=document['text'], height=420, disabled=True)
    st.caption('Пункты определены эвристически. Смещения относятся к извлечённому тексту; '
               'у PDF сохраняются физические страницы, у DOCX/TXT/HTML страницы неизвестны.')
    if 'numbered_clauses_not_recognized' in document['warnings']:
        st.info('Нумерованные пункты не распознаны. Исходный текст сохранён полностью.')
    if st.checkbox('Показать фрагменты и ссылки на пункты', key='show_document_fragments'):
        clauses = {c['id']: c for c in document['clauses']}
        st.dataframe([{'Фрагмент': f['id'], 'Страница': f['page_number'],
                       'Пункт': clauses.get(f['clause_id'], {}).get('marker'),
                       'Привязка': f['attribution'], 'Текст': f['text']}
                      for f in document['fragments'] if f['text'].strip()], hide_index=True)
    if st.checkbox('Показать реквизиты источника и хеши', key='show_source_metadata'):
        st.json(document['source'])
    st.download_button('Скачать структуру JSON', json.dumps(document, ensure_ascii=False, indent=2),
        file_name=f"{Path(document['source']['filename']).stem}.structure.json", mime='application/json', on_click='ignore')
