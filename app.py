"""Run with: python -m streamlit run app.py."""

from pathlib import Path

import streamlit as st

from govai.extraction import (
    SUPPORTED_EXTENSIONS,
    TXT_ENCODINGS,
    ExtractionError,
    extract_text,
)


def main() -> None:
    st.set_page_config(page_title="GovAI — извлечение текста", page_icon="📄")
    st.title("GovAI Opportunity Engine")
    st.write("Загрузите документ, чтобы извлечь и просмотреть его текст.")
    st.caption("PDF, DOCX или TXT · Один файл до 10 МБ")

    uploaded_file = st.file_uploader(
        "Выберите документ",
        type=list(SUPPORTED_EXTENSIONS),
        accept_multiple_files=False,
        help="PDF должен содержать текстовый слой. Распознавание сканов пока не поддерживается.",
    )
    if uploaded_file is None:
        st.info("Добавьте файл — извлечённый текст появится здесь.")
        return

    encoding = "utf-8-sig"
    if Path(uploaded_file.name).suffix.lower() == ".txt":
        label = st.selectbox("Кодировка TXT", options=list(TXT_ENCODINGS))
        encoding = TXT_ENCODINGS[label]

    try:
        with st.spinner("Извлекаем текст…"):
            text = extract_text(
                uploaded_file.name, uploaded_file.getvalue(), encoding=encoding
            )
    except ExtractionError as exc:
        st.error(str(exc))
        return

    if not text.strip():
        st.warning(
            "В документе не найден текст. Возможно, документ пуст или содержит "
            "только изображения. Для PDF-сканов потребуется OCR, который пока не поддерживается."
        )
        return

    st.success("Текст успешно извлечён.")
    st.caption(f"Символов: {len(text):,}")
    st.text_area("Извлечённый текст", value=text, height=420)


if __name__ == "__main__":
    main()
