"""Run with: python -m streamlit run app.py."""

import json
from pathlib import Path

import streamlit as st

from govai.extraction import (
    SUPPORTED_EXTENSIONS,
    TXT_ENCODINGS,
    ExtractionError,
    extract_document,
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
            document = extract_document(
                uploaded_file.name, uploaded_file.getvalue(), encoding=encoding
            )
    except ExtractionError as exc:
        st.error(str(exc))
        return

    text = document["text"]
    if not text.strip():
        st.warning(
            "В документе не найден текст. Возможно, документ пуст или содержит "
            "только изображения. Для PDF-сканов потребуется OCR, который пока не поддерживается."
        )
        return

    st.success("Текст успешно извлечён.")
    st.caption(f"Символов: {len(text):,}")
    st.text_area("Извлечённый текст", value=text, height=420)
    st.subheader("Структура документа")
    metadata = document["metadata"]
    for label, key in (("Название", "title"), ("Номер", "number"),
                       ("Дата принятия", "adoption_date"), ("Редакция", "revision_date")):
        value = metadata[key]
        st.write(f"{label}: {value['value'] if value else 'не определено'}")
    st.caption(
        "Структура определена по явным маркерам и требует проверки. "
        "Пустая ссылка на пункт означает, что принадлежность не определена. "
        "Страницы PDF считаются от начала файла; у DOCX и TXT пагинация неизвестна."
    )
    if "numbered_clauses_not_recognized" in document["warnings"]:
        st.info("Нумерованные пункты не распознаны. Исходный текст сохранён полностью.")
    if any(w.startswith("pdf_page_without_text:") for w in document["warnings"]):
        st.warning("На некоторых страницах PDF нет извлекаемого текста. Проверьте оригинал.")
    clauses = {c["id"]: c for c in document["clauses"]}
    with st.expander("Фрагменты и ссылки на пункты"):
        st.dataframe([
            {"Фрагмент": f["id"], "Страница": f["page_number"],
             "Пункт": clauses[f["clause_id"]]["marker"] if f["clause_id"] else None,
             "Привязка": f["attribution"], "Текст": f["text"]}
            for f in document["fragments"] if f["text"].strip()
        ], hide_index=True)
    st.download_button(
        "Скачать структуру JSON",
        data=json.dumps(document, ensure_ascii=False, indent=2),
        file_name=f"{Path(uploaded_file.name).stem}.structure.json",
        mime="application/json",
    )


if __name__ == "__main__":
    main()
