"""Document presentation; heavy source views are rendered only in Technical data."""

import json
from datetime import date
from pathlib import Path

import streamlit as st


def analysis_error_message(message):
    """Explain recoverable service/configuration failures to a visitor, not a server operator."""
    text = str(message)
    if any(marker in text for marker in ('OPENAI_API_KEY', 'API-ключ', 'secrets.toml')):
        return ('Сервис анализа не настроен или доступ к модели отклонён. '
                'Владельцу приложения нужно проверить серверные настройки доступа. '
                'Загруженный документ сохранён в этой сессии.')
    if 'OPENAI_MODEL' in text or 'выбранной модели запрещён' in text:
        return ('Выбранная на сервере модель недоступна. Владельцу приложения нужно '
                'проверить её настройку и доступ. Сохранённые результаты остаются доступны.')
    if 'квота OpenAI' in text:
        return ('Сервис анализа достиг лимита запросов. Сохранённые результаты доступны. '
                'Повторите попытку позже; владельцу приложения нужно проверить квоту сервиса.')
    if 'сертификат' in text or 'сетевые настройки сервера' in text:
        return ('Не удалось соединиться с сервисом анализа. Сохранённые результаты доступны. '
                'Повторите попытку позже; владельцу приложения нужно проверить соединение сервера.')
    return text


def _date(value):
    try:
        return date.fromisoformat(value).strftime('%d.%m.%Y')
    except (TypeError, ValueError):
        return str(value)


def document_header(document):
    m = document['metadata']
    def value(key):
        return m[key]['value'] if m.get(key) else None
    title = value('act_type') or ('Документ' if value('number') else document['source']['filename'])
    if value('number'):
        title += ' № ' + str(value('number'))
    st.subheader(title)
    adopted = _date(value('adoption_date')) if value('adoption_date') else 'не определена'
    revision = _date(value('revision_date')) if value('revision_date') else 'не определена'
    st.caption(f'Дата принятия: {adopted} · Редакция: {revision}')
    if m.get('title'):
        title = ' '.join(m['title']['value'].split())
        if len(title) > 180:
            with st.expander('Полное название документа', expanded=False):
                st.write(title)
        else:
            st.caption(title)
    if document['source'].get('type') == 'url_html':
        st.warning('Прочитан основной текст страницы. Полнота НПА автоматически не подтверждена: '
                   'сверьте его с документом на сайте. Номера страниц для HTML не определяются.')
    if any(w.startswith('pdf_page_without_text:') for w in document['warnings']):
        st.warning('На некоторых страницах PDF не удалось прочитать текст. Анализ этих страниц '
                   'может быть неполным. Для полной проверки загрузите копию с распознанным текстом.')
    if 'docx_automatic_numbering_not_resolved' in document['warnings']:
        st.caption('Автоматическая нумерация Word может не сохраниться. Проверяйте ссылки '
                   'по цитатам из исходного документа.')


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
