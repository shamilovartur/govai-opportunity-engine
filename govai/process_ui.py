"""Streamlit presentation for the provider-independent analysis result."""

import json
import re
from hashlib import sha256

import streamlit as st

from govai.llm import OpenAIProvider, configuration
from govai.processes import ProcessError, extract_processes


def _evidence(items):
    for e in items:
        clause = f"пункт {e['clause_number']}" if e['clause_number'] else "пункт не определён"
        page = f", страница PDF {e['page_number']}" if e['page_number'] else ""
        st.caption(f"{e['fragment_id']} · {clause}{page} · смещения {e['matches']}")
        st.text(e['quote'])


def render_processes(document):
    identity = (document['source']['sha256'], sha256(document['text'].encode()).hexdigest())
    if st.session_state.get('process_document') != identity:
        st.session_state.pop('process_result', None)
        st.session_state.process_document = identity
    st.subheader('Административные процедуры')
    st.caption('Кнопка отправляет фрагменты документа в OpenAI API. Возможна плата по тарифу API. '
               'Анализируется исходный извлечённый текст; правки в поле просмотра не учитываются.')
    if st.button('Извлечь процессы', type='primary'):
        st.session_state.pop('process_result', None)
        provider = None
        try:
            try:
                secrets = st.secrets.to_dict()
            except FileNotFoundError:
                secrets = {}
            key, model = configuration(secrets)
            provider = OpenAIProvider(key, model)
            bar = st.progress(0, text='Подготовка блоков…')
            def progress(done, total):
                bar.progress(done / total, text=f'Обработано блоков: {done}/{total}')
            st.session_state.process_result = extract_processes(document, provider, progress=progress)
        except ProcessError as exc:
            st.error(str(exc))
        finally:
            if provider:
                provider.close()
    result = st.session_state.get('process_result')
    if not result:
        return
    st.text(result['explanation'])
    st.caption(f"Провайдер: {result['provider']} · Модель: {result['model']} · "
               f"Блоков: {result['coverage']['chunks_completed']}/{result['coverage']['chunks_total']}")
    st.warning('Цитаты проверены на точное совпадение. Смысл действий, их полнота и принадлежность '
               'процедурам требуют проверки человеком. Совпадение цитаты не доказывает вывод модели.')
    if result['rejected_candidates']:
        st.warning(f"Отклонено кандидатов или полей: {len(result['rejected_candidates'])}. "
                   'Причины включены в JSON.')
    for procedure in result['procedures']:
        title = re.sub(r'([\\`*_{}\[\]()#+.!<>|])', r'\\\1', ' '.join(procedure['title'].split()))
        st.markdown('#### ' + title)
        with st.expander('Основание процедуры · ' + procedure['id']):
            _evidence(procedure['anchor_evidence'])
        for action in procedure['actions']:
            st.text('• ' + action['description'])
            st.text('Исполнитель: ' + (action['actor']['text'] if action['actor'] else 'не указан'))
            for field, label in [('inputs', 'Входные данные'), ('outputs', 'Результаты'),
                                 ('conditions', 'Условия'), ('dependencies', 'Зависимости')]:
                if action[field]:
                    st.text(label + ': ' + '; '.join(v['text'] for v in action[field]))
            with st.expander('Подтверждающие пункты и цитаты · ' + action['id']):
                _evidence(action['evidence'])
                for field in ('actor', 'inputs', 'outputs', 'conditions', 'dependencies'):
                    values = ([action['actor']] if action['actor'] else []) if field == 'actor' else action[field]
                    for value in values:
                        st.text(value['text'])
                        _evidence(value['evidence'])
                if action['review_reasons']:
                    st.caption('Неподтверждённые поля удалены: ' + ', '.join(action['review_reasons']))
    st.download_button('Скачать процессы JSON', json.dumps(result, ensure_ascii=False, indent=2),
                       file_name='processes.json', mime='application/json')
