"""Run with: python -m streamlit run app.py."""

from pathlib import Path

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
        st.caption('Публичная HTTPS-ссылка на PDF или HTML-страницу с текстом НПА · До 10 МБ')
        url = st.text_input('Ссылка на документ', placeholder='https://example.org/document.pdf')
        if st.button('Загрузить по ссылке', type='primary'):
            # Clear the old result before every attempt, including failed retries.
            st.session_state.pop('url_document', None)
            st.session_state.pop('loaded_url', None)
            try:
                with st.spinner('Загружаем документ…'):
                    st.session_state.url_document = extract_url_document(url)
                st.session_state.loaded_url = url
            except ExtractionError as exc:
                st.error(str(exc))
                return None
        if st.session_state.get('loaded_url') == url:
            return st.session_state.get('url_document')
        st.info('Укажите публичную ссылку и нажмите «Загрузить по ссылке».')
        return None

    uploaded_file = st.file_uploader(
        'Выберите документ', type=list(SUPPORTED_EXTENSIONS), accept_multiple_files=False,
        help='PDF должен содержать текстовый слой. Распознавание сканов пока не поддерживается.',
    )
    if uploaded_file is None:
        st.info('Добавьте документ, чтобы начать анализ.')
        return None
    encoding = 'utf-8-sig'
    if Path(uploaded_file.name).suffix.lower() == '.txt':
        label = st.selectbox('Кодировка TXT', options=list(TXT_ENCODINGS))
        encoding = TXT_ENCODINGS[label]
    try:
        with st.spinner('Извлекаем текст…'):
            document = extract_document(uploaded_file.name, uploaded_file.getvalue(), encoding=encoding)
        return document
    except ExtractionError as exc:
        st.error(str(exc))
        return None


def main() -> None:
    st.set_page_config(page_title="GovAI — анализ НПА", page_icon="📄")
    st.title("GovAI Opportunity Engine")
    st.write("Загрузите НПА → извлеките процессы → получите отчёт об AI-возможностях.")
    st.subheader('1. Загрузите документ')
    with st.expander('Документ и загрузка', expanded=not bool(st.session_state.get('process_result'))):
        st.caption("PDF, DOCX или TXT · Один файл до 10 МБ")
        document = _load_input()
    if document is None:
        for key in ('process_result', 'process_document', 'process_result_bytes', 'verified_analysis', 'process_origin', 'process_error'):
            st.session_state.pop(key, None)
        return

    text = document["text"]
    if not text.strip():
        st.warning(
            "В документе не найден текст. Возможно, документ пуст или содержит "
            "только изображения. Для PDF-сканов потребуется OCR, который пока не поддерживается."
        )
        return

    if not st.session_state.get('process_result'):
        st.success("Документ загружен.")
    document_header(document)
    render_processes(document)


if __name__ == "__main__":
    main()
