"""The single user flow: extraction then normalization, with technical recovery kept aside."""

import json
import os
from hashlib import sha256
from types import SimpleNamespace

import streamlit as st

from govai.llm import OpenAIProvider, configuration
from govai.checkpoints import (
    combine_checkpoints, compatible_checkpoint, dump_checkpoint,
    extract_with_checkpoint, load_checkpoint, new_checkpoint,
)
from govai.processes import ProcessError
from govai.analysis_io import verify_analysis
from govai.map_ui import map_state, normalize_processes, render_workspace
from govai.document_ui import technical_document
from govai.opportunity_ui import opportunity_state, render_opportunities, render_opportunity_technical


def _verify_current(document):
    if 'verified_analysis' not in st.session_state and st.session_state.get('process_result'):
        data = st.session_state.get('process_result_bytes') or json.dumps(
            st.session_state.process_result, ensure_ascii=False, indent=2).encode()
        st.session_state.process_result_bytes = data
        st.session_state.verified_analysis = verify_analysis(data, document)
    return st.session_state.get('verified_analysis')


def _run_analysis(document, checkpoints, analysis_id, preparation_error, on_checkpoint):
    """No analysis logic here: sequence the existing verified extraction and normalizer."""
    if not st.session_state.get('process_result'):
        provider = None
        try:
            try: secrets = st.secrets.to_dict()
            except FileNotFoundError: secrets = {}
            key, model = configuration(secrets)
            if preparation_error: raise ProcessError(preparation_error)
            provider = OpenAIProvider(key, model)
            bar = st.progress(0, text='Извлекаем процессы…')
            def progress(done, total):
                bar.progress(done / total, text=f'Обработано блоков: {done}/{total}')
            st.session_state.process_result = extract_with_checkpoint(document, provider,
                checkpoint=checkpoints.get(analysis_id),
                on_checkpoint=on_checkpoint, progress=progress)
            st.session_state.process_origin = 'generated'
        finally:
            if provider: provider.close()
    analysis = _verify_current(document)
    key, _, _, maps = map_state(analysis)
    if key not in maps:
        normalize_processes(analysis)


def render_processes(document):
    identity = (document['source']['sha256'], sha256(document['text'].encode()).hexdigest(),
                document['source']['format'])
    if st.session_state.get('process_document') != identity:
        for name in ('process_result', 'process_result_bytes', 'verified_analysis', 'process_origin', 'process_error'):
            st.session_state.pop(name, None)
        st.session_state.process_document = identity
    model = os.environ.get('OPENAI_MODEL', '').strip()
    descriptor = SimpleNamespace(name=OpenAIProvider.name, model=model,
        prompt_version=OpenAIProvider.prompt_version, prompt_sha256=OpenAIProvider.prompt_sha256)
    expected, preparation_error = None, None
    if model:
        try: expected = new_checkpoint(document, descriptor)
        except ProcessError as exc: preparation_error = str(exc)
    checkpoints = st.session_state.setdefault('process_checkpoints', {})
    analysis_id = expected['analysis_id'] if expected else None
    analysis = None
    try: analysis = _verify_current(document)
    except ProcessError as exc: st.session_state.process_error = str(exc)
    artifact = None
    if analysis:
        key, _, _, maps = map_state(analysis)
        artifact = maps.get(key)
    st.subheader('2. Извлеките процессы')
    run_analysis = False
    if not artifact:
        if st.session_state.get('process_error'):
            st.error('Анализ не завершён: ' + st.session_state.process_error)
        if analysis:
            st.info('Извлечение сохранено. Продолжение обработает только оставшийся этап.')
        label = 'Продолжить анализ' if analysis or st.session_state.get('process_error') else 'Извлечь процессы'
        if st.button(label, type='primary'):
            st.session_state.pop('process_error', None)
            run_analysis = True
    else:
        report_key, _, _, reports = opportunity_state(artifact)
        if report_key in reports or report_key in st.session_state.get('opportunity_failures', {}):
            with st.expander('Процессы готовы', expanded=False):
                render_workspace(analysis, st.session_state.get('process_origin', 'generated'))
        else:
            render_workspace(analysis, st.session_state.get('process_origin', 'generated'))
    analysis_progress = st.empty()
    st.subheader('3. Сформируйте отчёт')
    if artifact:
        render_opportunities(artifact, analysis)
    else:
        st.button('Сформировать отчёт', disabled=True, help='Сначала завершите анализ процессов.')

    # All imports, JSON, checkpoint and metrics live in one closed technical section.
    with st.expander('Технические данные', expanded=False):
        st.caption('Импорт сохранённых этапов не запускает извлечение, нормализацию или формирование отчёта.')
        completed = st.file_uploader('Готовый processes.json', type=['json'], max_upload_size=64,
                                     key='completed_analysis_upload')
        if st.button('Импортировать результат', disabled=completed is None):
            try:
                verified = verify_analysis(completed.getvalue(), document)
                st.session_state.process_result = verified.raw
                st.session_state.process_result_bytes = verified.original_bytes
                st.session_state.verified_analysis = verified
                st.session_state.process_origin = 'imported'
                st.session_state.pop('process_error', None)
                st.rerun()
            except ProcessError as exc: st.error('Импорт результата: ' + str(exc))
        uploaded = st.file_uploader('Checkpoint JSON', type=['json'], max_upload_size=64,
                                    key='process_checkpoint_upload')
        if st.button('Восстановить checkpoint', disabled=uploaded is None):
            try:
                if not expected: raise ProcessError(preparation_error or 'Для восстановления задайте OPENAI_MODEL.')
                restored = compatible_checkpoint(load_checkpoint(uploaded.getvalue()), expected)
                if analysis_id in checkpoints:
                    restored = combine_checkpoints(checkpoints[analysis_id], restored)
                checkpoints[analysis_id] = restored
                # A matching already-completed analysis need not be discarded to import an older snapshot.
                if not analysis:
                    for name in ('process_result', 'process_result_bytes', 'process_error'):
                        st.session_state.pop(name, None)
                st.success(f"Checkpoint восстановлен: {len(restored['results'])}/{len(restored['chunk_inputs'])} блоков. "
                           'Продолжите анализ на основном экране, если он ещё не завершён.')
            except ProcessError as exc: st.error(str(exc))
        checkpoint_info, checkpoint_slot = st.empty(), st.empty()
        checkpoint_rendered = False
        def save_checkpoint(snapshot):
            nonlocal checkpoint_rendered
            saved_id = snapshot['analysis_id']
            checkpoints[saved_id] = snapshot
            checkpoint_info.caption(f"Checkpoint: {len(snapshot['results'])}/{len(snapshot['chunk_inputs'])} блоков. "
                                    'Скачайте его для восстановления в новой сессии.')
            if not checkpoint_rendered:
                with checkpoint_slot.container():
                    st.download_button('Скачать checkpoint JSON', lambda: dump_checkpoint(checkpoints[saved_id]),
                        file_name=f'{saved_id[:16]}.checkpoint.json', mime='application/json', on_click='ignore')
                checkpoint_rendered = True
        if analysis_id in checkpoints: save_checkpoint(checkpoints[analysis_id])
        if analysis:
            render_workspace(analysis, st.session_state.get('process_origin', 'generated'), technical=True)
            if artifact: render_opportunity_technical(artifact, analysis)
        elif st.session_state.get('process_result_bytes'):
            st.download_button('Скачать процессы JSON', st.session_state.process_result_bytes,
                               file_name='processes.json', mime='application/json', on_click='ignore')
        if st.checkbox('Показать исходный текст и структуру', key='inspect-source'):
            technical_document(document)
    # Register technical download slots before a long run, so its newest checkpoint is downloadable mid-analysis.
    if run_analysis:
        try:
            with analysis_progress.container(), st.spinner('Анализ выполняется…'):
                _run_analysis(document, checkpoints, analysis_id, preparation_error, save_checkpoint)
        except ProcessError as exc:
            st.session_state.process_error = str(exc)
        st.rerun()
