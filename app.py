"""Run with: python -m streamlit run app.py."""

from pathlib import Path
from hashlib import sha256
import time

import streamlit as st

from govai.extraction import (
    SUPPORTED_EXTENSIONS,
    TXT_ENCODINGS,
    ExtractionError,
    extract_document,
)
from govai.url_input import extract_url_document
from govai.process_ui import render_processes
from govai.document_ui import document_header


def _load_input():
    mode = st.radio('Способ загрузки', ['Загрузить файл', 'Загрузить по ссылке'], horizontal=True)
    if mode == 'Загрузить по ссылке':
        st.caption('Прямая HTTPS-ссылка на PDF или публичную страницу с текстом документа · до 10 МБ')
        url = st.text_input('Ссылка на документ', placeholder='https://example.org/document.pdf')
        if st.button('Загрузить по ссылке', type='primary'):
            # Clear the old result before every attempt, including failed retries.
            st.session_state.pop('url_document', None)
            st.session_state.pop('loaded_url', None)
            try:
                started = time.perf_counter()
                with st.spinner('Загружаем и читаем документ…', show_time=True):
                    st.session_state.url_document = extract_url_document(url)
                st.session_state.document_read_metrics = {'elapsed_seconds': time.perf_counter()-started,
                                                         'source': 'url'}
                st.session_state.loaded_url = url
            except ExtractionError as exc:
                st.error(str(exc))
                return None
        if st.session_state.get('loaded_url') == url:
            return st.session_state.get('url_document')
        st.caption('Страница должна открываться без входа в аккаунт.')
        return None

    uploaded_file = st.file_uploader(
        'Выберите документ', type=list(SUPPORTED_EXTENSIONS), accept_multiple_files=False,
        help='PDF должен содержать текстовый слой. Распознавание сканов пока не поддерживается.',
    )
    if uploaded_file is None:
        return None
    encoding = 'utf-8-sig'
    if Path(uploaded_file.name).suffix.lower() == '.txt':
        with st.expander('Дополнительные настройки TXT', expanded=False):
            label = st.selectbox('Кодировка TXT', options=list(TXT_ENCODINGS),
                help='Если русские буквы отображаются неверно, выберите кодировку исходного файла.')
        encoding = TXT_ENCODINGS[label]
    data = uploaded_file.getvalue()
    identity = (uploaded_file.name, sha256(data).hexdigest(), encoding)
    cached = st.session_state.get('document_read_cache')
    if cached and cached['identity'] == identity:
        if cached.get('error'):
            st.error(cached['error'])
            return None
        st.session_state.document_read_metrics = cached['metrics']
        return cached['document']
    try:
        started = time.perf_counter()
        with st.spinner('Читаем документ…', show_time=True):
            document = extract_document(uploaded_file.name, data, encoding=encoding)
        st.session_state.document_read_metrics = {'elapsed_seconds': time.perf_counter()-started,
                                                  'source': 'file'}
        st.session_state.document_read_cache = {'identity': identity, 'document': document,
                                                'metrics': st.session_state.document_read_metrics}
        return document
    except ExtractionError as exc:
        message = str(exc)
        if message.startswith('Не удалось прочитать PDF.'):
            message += ' Попробуйте другую копию PDF с распознанным текстом.'
        st.session_state.pop('document_read_metrics', None)
        st.session_state.document_read_cache = {'identity': identity, 'error': message}
        st.error(message)
        return None


def main() -> None:
    st.set_page_config(page_title="GovAI — возможности ИИ в нормативном акте", page_icon="📄")
    st.badge('GovAI', color='green')
    if not st.session_state.get('process_result'):
        st.title('Найдите возможности для применения ИИ в нормативном акте')
        st.write('Загрузите документ. GovAI выделит описанные в нём процессы и предложит, '
                 'где ИИ может помочь сотрудникам. В отчёте — первые кандидаты для пилота, '
                 'ожидаемая польза и ссылки на положения документа.')
    st.subheader('1. Загрузите нормативный акт')
    with st.expander('Документ и загрузка', expanded=not bool(st.session_state.get('process_result'))):
        st.caption('PDF с текстом, DOCX или TXT · до 10 МБ')
        document = _load_input()
        st.caption('Скан без распознанного текста пока не поддерживается.')
    if document is None:
        for key in ('process_result', 'process_document', 'process_result_bytes', 'verified_analysis', 'process_origin', 'process_error'):
            st.session_state.pop(key, None)
        return

    text = document["text"]
    if not text.strip():
        st.warning(
            "В документе не найден текст. Загрузите текстовую копию документа. "
            "Скан без распознанного текста пока не поддерживается."
        )
        return

    if not st.session_state.get('process_result'):
        st.success("Документ загружен.")
    document_header(document)
    render_processes(document)


if __name__ == "__main__":
    main()
