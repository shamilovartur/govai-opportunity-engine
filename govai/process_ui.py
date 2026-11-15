"""The single user flow: extraction then normalization, with technical recovery kept aside."""

import json
import os
import subprocess
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import streamlit as st

from govai.llm import OpenAIProvider, configuration
from govai.checkpoints import (
    combine_checkpoints, compatible_checkpoint, dump_checkpoint,
    extract_with_checkpoint, load_checkpoint, new_checkpoint,
)
from govai.processes import ProcessError
from govai.analysis_io import verify_analysis
from govai.map_ui import map_state, normalize_processes, render_workspace, request_timing
from govai.document_ui import technical_document, analysis_error_message
from govai.opportunity_ui import opportunity_state, render_opportunities, render_opportunity_technical


def _verify_current(document):
    if 'verified_analysis' not in st.session_state and st.session_state.get('process_result'):
        data = st.session_state.get('process_result_bytes') or json.dumps(
            st.session_state.process_result, ensure_ascii=False, indent=2).encode()
        st.session_state.process_result_bytes = data
        st.session_state.verified_analysis = verify_analysis(data, document)
    return st.session_state.get('verified_analysis')


def _run_analysis(document, checkpoints, analysis_id, preparation_error, on_checkpoint, *, identity, run_key, run):
    """No analysis logic here: sequence the existing verified extraction and normalizer."""
    if not st.session_state.get('process_result'):
        provider = None
        try:
            try: secrets = st.secrets.to_dict()
            except FileNotFoundError: secrets = {}
            key, model = configuration(secrets)
            if preparation_error: raise ProcessError(preparation_error)
            provider = OpenAIProvider(key, model)
            observed_wait, wait_known, previous_done, finished = 0.0, True, None, False
            def consume_request_time():
                nonlocal observed_wait
                measured = getattr(provider, 'last_call_elapsed_seconds', None)
                if type(measured) in (int, float):
                    observed_wait += measured
                    provider.last_call_elapsed_seconds = None
                    return True
                return False
            bar = st.progress(0, text='Извлекаем процессы из документа…')
            def progress(done, total):
                nonlocal previous_done, wait_known
                measured = consume_request_time()
                # The first callback describes reused blocks before any request.
                # Each later increase follows a new, successfully saved response.
                if previous_done is not None and done > previous_done and not measured:
                    wait_known = False
                previous_done = done
                run['completed_blocks'], run['total_blocks'] = done, total
                bar.progress(done / total, text=f'Проверено блоков текста: {done} из {total}')
            run['stage'] = 'Извлекаем процессы из документа'
            started = time.perf_counter()
            try:
                with st.spinner('Извлекаем процессы из документа…', show_time=True):
                    result = extract_with_checkpoint(document, provider,
                        checkpoint=checkpoints.get(analysis_id),
                        on_checkpoint=on_checkpoint, progress=progress)
                    finished = True
                    # Save before the next UI yield. A change of document must not
                    # attach this response to the newly selected source.
                    st.session_state.setdefault('completed_process_runs', {})[run_key] = result
                    if st.session_state.get('process_document') != identity:
                        return
                    st.session_state.process_result = result
            finally:
                run['extraction_seconds'] = time.perf_counter()-started
                final_request_measured = consume_request_time()
                all_saved = (run.get('total_blocks') is not None and
                             run.get('completed_blocks') == run.get('total_blocks'))
                if not finished and not final_request_measured and not all_saved:
                    # Without transport timing we cannot infer whether a failed
                    # attempt reached the API or failed locally.
                    wait_known = False
                measured = request_timing(run['extraction_seconds'], observed_wait if wait_known else None)
                run.update(extraction_api_wait_seconds=measured['api_wait_seconds'],
                           extraction_local_seconds=measured['local_seconds'])
            st.session_state.process_origin = 'generated'
        finally:
            if provider: provider.close()
    analysis = _verify_current(document)
    key, _, _, maps = map_state(analysis)
    if key not in maps:
        run['stage'] = 'Проверяем и объединяем процессы'
        with st.spinner('Проверяем и объединяем процессы…', show_time=True):
            normalize_processes(analysis, timing=run)


@st.fragment(run_every=1)
def _wait_for_processes(run_key):
    current = st.session_state.get('process_in_flight', {}).get(run_key)
    if not current:
        return st.rerun(scope='app')
    elapsed = max(0, int(time.perf_counter()-current['started']))
    st.info(f"{current['stage']}. Прошло {elapsed // 60} мин {elapsed % 60:02d} с. "
            'Повторный запрос не отправляется.')
    total = current.get('total_blocks')
    if total:
        st.caption(f"Проверено блоков текста: {current.get('completed_blocks', 0)} из {total}.")


@st.cache_data(show_spinner=False, ttl=60)
def _published_revision():
    """Observed checkout metadata only; absence must not be replaced by an expected SHA."""
    root = Path(__file__).resolve().parents[1]
    def read(*args):
        try:
            return subprocess.check_output(['git', '-C', str(root), *args], text=True,
                stderr=subprocess.DEVNULL, timeout=2).strip() or 'не определена'
        except (OSError, subprocess.SubprocessError):
            return 'не определена'
    return read('rev-parse', '--short=12', 'HEAD'), read('branch', '--show-current')


def _server_diagnostics():
    commit, branch = _published_revision()
    st.caption(f'Версия приложения: {commit} · Ветка: {branch}')
    try:
        try: secrets = st.secrets.to_dict()
        except FileNotFoundError: secrets = {}
        _, model = configuration(secrets)
        configured = True
    except ProcessError:
        configured = False
        model = os.environ.get('OPENAI_MODEL', '').strip() or 'не задана'
    st.caption('Серверные настройки анализа: ' + ('заполнены' if configured else 'не заполнены') +
               f' · Модель: {model}. Это проверка настроек, без запроса к модели.')
    if st.session_state.get('document_read_metrics'):
        st.caption('Чтение документа, секунд: ' +
                   f"{st.session_state.document_read_metrics['elapsed_seconds']:.2f}")


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
    run_key = analysis_id or sha256(repr((identity, model)).encode()).hexdigest()
    in_flight = st.session_state.setdefault('process_in_flight', {})
    completed = st.session_state.setdefault('completed_process_runs', {})
    if not st.session_state.get('process_result') and run_key in completed:
        st.session_state.process_result = completed[run_key]
        st.session_state.process_origin = 'generated'
    analysis = None
    try: analysis = _verify_current(document)
    except ProcessError as exc: st.session_state.process_error = str(exc)
    artifact = None
    if analysis:
        key, _, _, maps = map_state(analysis)
        artifact = maps.get(key)
    st.subheader('2. Извлеките процессы')
    run_analysis = False
    if run_key in in_flight:
        _wait_for_processes(run_key)
    elif not artifact:
        if st.session_state.get('process_error'):
            st.error('Анализ не завершён: ' + analysis_error_message(st.session_state.process_error))
            if analysis:
                st.caption('Найденные действия сохранены. Продолжение повторит только их проверку и объединение.')
            elif analysis_id in checkpoints:
                snapshot = checkpoints[analysis_id]
                st.caption(f"Сохранено блоков текста: {len(snapshot['results'])} из {len(snapshot['chunk_inputs'])}. "
                           'Продолжение использует эти результаты без повторных запросов.')
            else:
                st.caption('Документ сохранён. После устранения причины нажмите «Продолжить анализ».')
        if analysis:
            st.info('Действия из документа найдены. Осталось проверить и объединить их в процессы.')
        label = 'Продолжить анализ' if analysis or st.session_state.get('process_error') else 'Извлечь процессы'
        if st.button(label, type='primary'):
            owner = uuid4().hex
            run = {'owner': owner, 'started': time.perf_counter(),
                   'stage': 'Подготавливаем анализ документа'}
            if in_flight.setdefault(run_key, run)['owner'] == owner:
                st.session_state.pop('process_error', None)
                run_analysis = True
    else:
        report_key, _, _, reports = opportunity_state(artifact)
        if report_key in reports or report_key in st.session_state.get('opportunity_failures', {}):
            label = 'Процессы готовы' if artifact['map']['status'] == 'completed' else 'Предварительный список процессов'
            with st.expander(label, expanded=False):
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
        _server_diagnostics()
        if st.session_state.get('process_error'):
            st.caption('Техническая причина последнего сбоя анализа')
            st.text(st.session_state.process_error)
        if run_key in st.session_state.get('process_run_metrics', {}):
            st.caption('Последний запуск процессов в этой сессии, секунды. '
                       'api_wait — сеть и ожидание ответа API, не внутреннее время модели; '
                       'local — оставшаяся локальная обработка. null означает, что разделение неизвестно.')
            st.json(st.session_state.process_run_metrics[run_key])
        st.caption('Импорт сохранённых этапов не запускает извлечение, нормализацию или формирование отчёта.')
        completed = st.file_uploader('Готовый processes.json', type=['json'], max_upload_size=64,
                                     key='completed_analysis_upload')
        if st.button('Импортировать результат', disabled=completed is None or run_key in in_flight):
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
        if st.button('Восстановить checkpoint', disabled=uploaded is None or run_key in in_flight):
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
            with analysis_progress.container():
                _run_analysis(document, checkpoints, analysis_id, preparation_error, save_checkpoint,
                              identity=identity, run_key=run_key, run=run)
        except ProcessError as exc:
            if st.session_state.get('process_document') == identity:
                st.session_state.process_error = str(exc)
        except Exception:
            if st.session_state.get('process_document') == identity:
                st.session_state.process_error = ('Не удалось завершить обработку документа. '
                    'Повторите продолжение анализа; '
                    'если ошибка повторится, сообщите владельцу приложения.')
        finally:
            run['elapsed_seconds'] = time.perf_counter()-run['started']
            st.session_state.setdefault('process_run_metrics', {})[run_key] = {
                name: run[name] for name in ('extraction_seconds', 'extraction_api_wait_seconds', 'extraction_local_seconds',
                    'normalization_seconds', 'normalization_api_wait_seconds', 'normalization_local_seconds', 'elapsed_seconds') if name in run}
            if in_flight.get(run_key, {}).get('owner') == owner:
                in_flight.pop(run_key, None)
        st.rerun()
