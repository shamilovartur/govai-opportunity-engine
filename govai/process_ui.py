"""Streamlit presentation for the provider-independent analysis result."""

import json
import os
from hashlib import sha256
from types import SimpleNamespace

import streamlit as st

from govai.llm import OpenAIProvider, configuration
from govai.checkpoints import (
    AnalysisInterrupted, combine_checkpoints, compatible_checkpoint, dump_checkpoint,
    extract_with_checkpoint, load_checkpoint, new_checkpoint,
)
from govai.processes import ProcessError
from govai.analysis_io import verify_analysis
from govai.map_ui import render_workspace


def render_processes(document, *, technical=False):
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
                document['source']['format'])
    if st.session_state.get('process_document') != identity:
        for name in ('process_result', 'process_result_bytes', 'verified_analysis', 'process_origin'):
            st.session_state.pop(name, None)
        st.session_state.process_document = identity
    checkpoints = st.session_state.setdefault('process_checkpoints', {})
    analysis_id = expected['analysis_id'] if expected else None
    if technical or 'process_result' not in st.session_state:
        with st.expander('Импортировать готовый исходный анализ'):
            completed_file = st.file_uploader('Готовый processes.json', type=['json'], max_upload_size=64,
                                               key='completed_analysis_upload')
            if st.button('Импортировать результат', disabled=completed_file is None):
                try:
                    verified = verify_analysis(completed_file.getvalue(), document)
                    st.session_state.process_result = verified.raw
                    st.session_state.process_result_bytes = verified.original_bytes
                    st.session_state.verified_analysis = verified
                    st.session_state.process_origin = 'imported'
                    st.rerun()
                except ProcessError as exc:
                    st.error('Импорт результата: ' + str(exc))
    if technical:
        st.subheader('Извлечение и checkpoint')
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
                    for name in ('process_result', 'process_result_bytes', 'verified_analysis', 'process_origin'):
                        st.session_state.pop(name, None)
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
        if checkpoint_rendered or not technical:
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

    run_extraction = st.button('Извлечь процессы', type='primary') if technical or 'process_result' not in st.session_state else False
    if run_extraction:
        for name in ('process_result', 'process_result_bytes', 'verified_analysis', 'process_origin'):
            st.session_state.pop(name, None)
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
            st.session_state.process_origin = 'generated'
        except ProcessError as exc:
            st.error(str(exc))
            if not isinstance(exc, AnalysisInterrupted) and analysis_id in checkpoints:
                saved = checkpoints[analysis_id]
                st.info(f"Успешно обработано блоков: {len(saved['results'])}/{len(saved['chunk_inputs'])}.")
        finally:
            if provider:
                provider.close()
    if not checkpoint_rendered and analysis_id in checkpoints and (technical or 'process_result' not in st.session_state):
        show_checkpoint(checkpoints[analysis_id])
    result = st.session_state.get('process_result')
    if not result:
        return
    if 'verified_analysis' not in st.session_state:
        data = st.session_state.get('process_result_bytes') or json.dumps(result, ensure_ascii=False, indent=2).encode()
        try:
            st.session_state.verified_analysis = verify_analysis(data, document)
            st.session_state.process_result_bytes = data
        except ProcessError as exc:
            st.error('Проверка исходного результата: ' + str(exc))
            st.download_button('Скачать процессы JSON', data, file_name='processes.json',
                               mime='application/json', on_click='ignore')
            return
    render_workspace(st.session_state.verified_analysis, st.session_state.get('process_origin', 'generated'),
                     technical=technical)
