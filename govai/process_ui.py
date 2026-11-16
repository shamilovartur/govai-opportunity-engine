"""One public action sequences the existing stages and retains session recovery."""

import json
import os
import time
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import streamlit as st

from govai.llm import OpenAIProvider, configuration
from govai.checkpoints import (
    extract_with_checkpoint, new_checkpoint,
)
from govai.processes import ProcessError
from govai.analysis_io import verify_analysis
from govai.map_ui import map_state, normalize_processes, request_timing
from govai.document_ui import analysis_error_message
from govai.opportunity_ui import opportunity_state, prepare_opportunity_report, render_ready_report


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

            def progress(done, total):
                nonlocal previous_done, wait_known
                measured = consume_request_time()
                # The first callback describes reused blocks before any request.
                # Each later increase follows a new, successfully saved response.
                if previous_done is not None and done > previous_done and not measured:
                    wait_known = False
                previous_done = done
                run['completed_blocks'], run['total_blocks'] = done, total

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
    if run_key in in_flight:
        _wait_for_processes(run_key)
        return
    report_key = None
    if artifact:
        report_key, _, _, reports = opportunity_state(artifact)
        if report_key in reports:
            render_ready_report(report_key, reports[report_key])
            return
    if st.session_state.get('process_error'):
        st.error('Анализ не завершён: ' + analysis_error_message(st.session_state.process_error))
        st.write('Полученные части анализа сохранены. Продолжение начнётся с незавершённого этапа.')
    label = 'Продолжить анализ' if st.session_state.get('process_error') else 'Проанализировать документ'
    if not st.button(label, type='primary'):
        return
    owner = uuid4().hex
    run = {'owner': owner, 'started': time.perf_counter(), 'stage': 'Подготавливаем анализ документа'}
    if in_flight.setdefault(run_key, run)['owner'] != owner:
        return
    st.session_state.pop('process_error', None)
    try:
        _run_analysis(document, checkpoints, analysis_id, preparation_error,
            lambda snapshot: checkpoints.update({snapshot['analysis_id']: snapshot}),
            identity=identity, run_key=run_key, run=run)
        # If a queued rerun has changed or removed the document, do not start a
        # new paid stage for the stale input or show its report under the new one.
        if st.session_state.get('active_document_key') != identity:
            return
        analysis = _verify_current(document)
        map_key, _, _, maps = map_state(analysis)
        artifact = maps.get(map_key)
        if artifact is None:
            raise ProcessError('Проверка процессов ещё не завершена. Продолжите анализ.')
        run['stage'] = 'Готовим рекомендации'
        with st.spinner('Готовим рекомендации…', show_time=True):
            prepare_opportunity_report(artifact, analysis)
    except ProcessError as exc:
        if st.session_state.get('active_document_key') == identity:
            st.session_state.process_error = str(exc)
    except Exception:
        if st.session_state.get('active_document_key') == identity:
            st.session_state.process_error = 'Не удалось завершить обработку. Повторите продолжение анализа.'
    finally:
        run['elapsed_seconds'] = time.perf_counter()-run['started']
        st.session_state.setdefault('process_run_metrics', {})[run_key] = {
            name: value for name, value in run.items() if name.endswith('_seconds')}
        if in_flight.get(run_key, {}).get('owner') == owner:
            in_flight.pop(run_key, None)
    st.rerun()
