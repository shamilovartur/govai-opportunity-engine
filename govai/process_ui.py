"""Streamlit presentation for the provider-independent analysis result."""

import json
import os
import re
from hashlib import sha256
from types import SimpleNamespace

import streamlit as st

from govai.llm import OpenAIProvider, configuration
from govai.checkpoints import (
    AnalysisInterrupted, combine_checkpoints, compatible_checkpoint, dump_checkpoint,
    extract_with_checkpoint, load_checkpoint, new_checkpoint,
)
from govai.processes import ProcessError


def _evidence(items):
    for e in items:
        clause = f"пункт {e['clause_number']}" if e['clause_number'] else "пункт не определён"
        page = f", страница PDF {e['page_number']}" if e['page_number'] else ""
        st.caption(f"{e['fragment_id']} · {clause}{page} · смещения {e['matches']}")
        st.text(e['quote'])


def render_processes(document):
    model = os.environ.get('OPENAI_MODEL', '').strip()
    descriptor = SimpleNamespace(name=OpenAIProvider.name, model=model,
                                 prompt_version=OpenAIProvider.prompt_version,
                                 prompt_sha256=OpenAIProvider.prompt_sha256)
    expected, preparation_error = None, None
    if model:
        try:
            expected = new_checkpoint(document, descriptor)
        except ProcessError as exc:
            preparation_error = str(exc)
    identity = (document['source']['sha256'], sha256(document['text'].encode()).hexdigest(),
                expected['analysis_id'] if expected else model)
    if st.session_state.get('process_document') != identity:
        st.session_state.pop('process_result', None)
        st.session_state.process_document = identity
    checkpoints = st.session_state.setdefault('process_checkpoints', {})
    analysis_id = expected['analysis_id'] if expected else None
    st.subheader('Административные процедуры')
    st.caption('Кнопка отправляет ещё не обработанные фрагменты в OpenAI API. Возможна плата по тарифу API. '
               'Анализируется исходный извлечённый текст; правки в поле просмотра не учитываются.')
    with st.expander('Восстановить анализ из checkpoint'):
        uploaded = st.file_uploader('Checkpoint JSON', type=['json'], key='process_checkpoint_upload',
                                    max_upload_size=64,
                                    help='После перезагрузки сначала загрузите тот же НПА и настройте ту же модель.')
        if st.button('Восстановить checkpoint', disabled=uploaded is None):
            try:
                if not expected:
                    raise ProcessError(preparation_error or 'Для восстановления задайте OPENAI_MODEL.')
                restored = compatible_checkpoint(load_checkpoint(uploaded.getvalue()), expected)
                if analysis_id in checkpoints:
                    restored = combine_checkpoints(checkpoints[analysis_id], restored)
                checkpoints[analysis_id] = restored
                st.session_state.pop('process_result', None)
                st.success(f"Checkpoint восстановлен: {len(restored['results'])}/{len(restored['chunk_inputs'])} блоков. "
                           'Нажмите «Извлечь процессы» для продолжения и проверки итогового результата.')
            except ProcessError as exc:
                st.error(str(exc))
    checkpoint_info = st.empty()
    checkpoint_slot = st.empty()
    checkpoint_rendered = False

    def show_checkpoint(snapshot):
        nonlocal checkpoint_rendered
        count, total = len(snapshot['results']), len(snapshot['chunk_inputs'])
        checkpoint_info.info(f'Сохранено ответов блоков: {count}/{total}. Checkpoint содержит промежуточные ответы, '
                             'а не завершённый анализ.')
        if checkpoint_rendered:
            return
        saved_id = snapshot['analysis_id']
        with checkpoint_slot.container():
            # Generate the latest immutable snapshot on demand, without registering
            # a large downloadable copy after every block of a long-running script.
            st.download_button('Скачать checkpoint JSON', lambda: dump_checkpoint(checkpoints[saved_id]),
                file_name=f"{saved_id[:16]}.checkpoint.json", mime='application/json',
                key=f'checkpoint-download-{saved_id}', on_click='ignore')
            st.caption('Скачайте checkpoint перед закрытием страницы. После перезагрузки загрузите НПА '
                       'и этот JSON заново. Файл содержит цитаты и результаты модели; API-ключ в него не входит.')
        checkpoint_rendered = True

    def save_checkpoint(snapshot):
        checkpoints[snapshot['analysis_id']] = snapshot
        show_checkpoint(snapshot)

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
                bar.progress(done / total, text=f'Сохранено ответов блоков: {done}/{total}')
            if preparation_error:
                raise ProcessError(preparation_error)
            st.session_state.process_result = extract_with_checkpoint(document, provider,
                checkpoint=checkpoints.get(analysis_id), on_checkpoint=save_checkpoint, progress=progress)
        except ProcessError as exc:
            st.error(str(exc))
            if not isinstance(exc, AnalysisInterrupted) and analysis_id in checkpoints:
                saved = checkpoints[analysis_id]
                st.info(f"Успешно обработано блоков: {len(saved['results'])}/{len(saved['chunk_inputs'])}.")
        finally:
            if provider:
                provider.close()
    if not checkpoint_rendered and analysis_id in checkpoints:
        show_checkpoint(checkpoints[analysis_id])
    result = st.session_state.get('process_result')
    if not result:
        return
    st.text(result['explanation'])
    st.caption(f"Провайдер: {result['provider']} · Модель: {result['model']} · "
               f"Блоков: {result['coverage']['chunks_completed']}/{result['coverage']['chunks_total']}")
    execution = result.get('execution', {})
    st.caption(f"Повторно использовано блоков: {execution.get('blocks_reused', 0)} · "
               f"Новых запросов по блокам: {execution.get('blocks_requested', 0)}")
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
                       file_name='processes.json', mime='application/json', on_click='ignore')
