"""Report generation and display; imports and diagnostics stay in the technical section."""

import json
import os
import time
from uuid import uuid4

import streamlit as st

from govai.analysis_io import digest
from govai.llm import OpenAIOpportunityProvider, configuration
from govai.opportunities import (OpportunityError, compatibility, generate_opportunities,
                                 restore_opportunities, operational_action_ids,
                                 export_opportunity_plan, restore_opportunity_plan)
from govai.opportunity_report import (management_view, render_markdown, render_details,
                                      _safe as escape_text)
from govai.processes import ProcessError
from govai.document_ui import analysis_error_message
from govai.map_ui import request_timing
from govai.report_export import EXPORT_VERSION, render_pdf, render_docx, render_bundle, pdf_layout_status


def _export_failure(label, error):
    """Keep the cause for diagnostics and give visitors an actionable explanation."""
    kind = type(error).__name__
    if kind == 'LayoutError':
        reason = 'Не удалось разместить один из блоков текста на странице.'
    elif isinstance(error, (FileNotFoundError, OSError)):
        reason = 'Не удалось прочитать ресурс оформления или записать файл в память.'
    elif isinstance(error, (UnicodeError, TypeError)):
        reason = 'Не удалось преобразовать часть данных в этот формат.'
    elif isinstance(error, MemoryError):
        reason = 'Для подготовки этого файла не хватило памяти.'
    else:
        reason = 'Локальная подготовка файла завершилась ошибкой.'
    return {'format': label, 'reason': reason, 'type': kind, 'detail': str(error)}


def opportunity_state(normalized):
    model = os.environ.get('OPENAI_MODEL', '').strip() or normalized['compatibility']['model']
    provider = OpenAIOpportunityProvider.name
    key = digest(compatibility(normalized, model, provider))
    reports = st.session_state.setdefault('opportunity_reports', {})
    analysis = st.session_state.get('verified_analysis')
    # A code update may change the generation contract while this session still
    # holds a known older result. Revalidate it once; do not offer a second paid
    # generation merely because the current prompt has a different cache key.
    identity = compatibility(normalized, model, provider)
    for cache_name, restore in (('opportunity_reports', restore_opportunities),
                                ('opportunity_plans', restore_opportunity_plan)):
        cache = st.session_state.setdefault(cache_name, {})
        if key in cache or analysis is None:
            continue
        for previous_key, candidate in list(cache.items()):
            saved_identity = candidate.get('compatibility', {}) if isinstance(candidate, dict) else {}
            if not all(saved_identity.get(field) == identity[field] for field in
                       ('document_sha256', 'normalized_sha256', 'model', 'provider', 'settings')):
                continue
            try:
                checked = restore(json.dumps(candidate, ensure_ascii=False).encode(), normalized, analysis, model, provider)
            except ProcessError:
                continue
            cache[key] = checked
            cache.pop(previous_key, None)
            if cache_name == 'opportunity_reports':
                origins = st.session_state.setdefault('opportunity_origins', {})
                origins[key] = origins.get(previous_key, 'restored_session')
            break
    return key, model, provider, reports



def _exports(key, artifact):
    """Prepare each format once per result/version, including failed outcomes.

    Reopening the technical section or downloading must not repeat rendering.
    Explicit restoration clears this cache and permits a new local attempt.
    """
    cache = st.session_state.setdefault('opportunity_exports', {})
    fingerprint = digest({'artifact': artifact, 'export_version': EXPORT_VERSION})
    saved = cache.get(key)
    if saved and saved['fingerprint'] == fingerprint:
        return saved
    started = time.perf_counter()
    files = {'fingerprint': fingerprint, 'failed': [], 'errors': [], 'view': None}
    try:
        files['view'] = management_view(artifact)
    except Exception as exc:
        files['failed'].append('Представление отчёта')
        files['errors'].append(_export_failure('Представление отчёта', exc))
    preparation = {
        'json': ('JSON', lambda: json.dumps(artifact, ensure_ascii=False, indent=2).encode('utf-8')),
        'details': ('Подробные материалы', lambda: render_details(artifact).encode('utf-8')),
        'markdown': ('Markdown', lambda: render_markdown(artifact, view=files['view']).encode('utf-8')),
        'docx': ('DOCX', lambda: render_docx(artifact, view=files['view'])),
        'pdf': ('PDF', lambda: render_pdf(artifact, view=files['view'])),
    }
    for name, (label, prepare) in preparation.items():
        try:
            if files['view'] is None and name in ('markdown', 'docx', 'pdf'):
                raise ValueError('Report presentation unavailable')
            files[name] = prepare()
        except Exception as exc:
            files[name] = None
            files['failed'].append(label)
            files['errors'].append(_export_failure(label, exc))
    files['zip'] = None
    files['pdf_layout'] = None
    if files['pdf'] is not None:
        try:
            files['pdf_layout'] = pdf_layout_status(files['pdf'])
        except Exception as exc:
            files['pdf'] = None
            files['failed'].append('PDF')
            files['errors'].append(_export_failure('Проверка PDF', exc))
    if files['json'] is not None:
        try:
            files['zip'] = render_bundle(artifact, pdf_data=files['pdf'], docx_data=files['docx'],
                markdown_data=files['markdown'], json_data=files['json'], details_data=files['details'],
                view=files['view'])
        except Exception as exc:
            files['failed'].append('ZIP')
            files['errors'].append(_export_failure('ZIP', exc))
    files['elapsed_seconds'] = time.perf_counter() - started
    cache[key] = files
    return files


def _source_downloads(files, *, prefix, include_pdf=False):
    """Fallback and technical exports remain independently available."""
    formats = [
        ('docx', 'Скачать отчёт (DOCX)', 'opportunity-report.docx',
         'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
        ('markdown', 'Скачать Markdown', 'opportunity-report.md', 'text/markdown'),
        ('details', 'Скачать подробные материалы', 'opportunity-details.txt', 'text/plain'),
        ('json', 'Скачать данные анализа (JSON)', 'opportunities.json', 'application/json'),
    ]
    if include_pdf:
        formats.insert(0, ('pdf', 'Скачать отчёт PDF', 'opportunity-report.pdf', 'application/pdf'))
    for name, label, filename, mime in formats:
        if files[name] is not None:
            st.download_button(label, files[name], file_name=filename, mime=mime,
                               on_click='ignore', key=prefix+'-'+name, width='stretch')


@st.fragment(run_every=1)
def _wait_for_report(key):
    # A fast rerun may display this while the original request is finishing in
    # its stopped script. Poll only session state, never the provider.
    if key not in st.session_state.get('opportunity_in_flight', {}):
        return st.rerun(scope='app')
    st.info('Отчёт формируется. Повторный запрос не отправляется; дождитесь завершения текущего анализа.')
    started = st.session_state.get('opportunity_started', {}).get(key)
    if started is not None:
        st.caption(f'Прошло {max(0, int(time.perf_counter() - started))} с. Подготавливаем рекомендации и проверяем их источники.')


def render_opportunities(normalized, analysis):
    key, model, provider_name, reports = opportunity_state(normalized)
    plans = st.session_state.setdefault('opportunity_plans', {})
    failures = st.session_state.setdefault('opportunity_failures', {})
    in_flight = st.session_state.setdefault('opportunity_in_flight', {})
    pending = plans.get(key)
    artifact = reports.get(key)
    if not artifact:
        if key in in_flight:
            _wait_for_report(key)
            return
        if key in failures:
            st.error('Отчёт не сформирован: ' + analysis_error_message(failures[key]['message']))
            st.info('План сохранён в этой сессии. Повторите сборку без нового запроса к модели.' if pending else
                    'Процессы сохранены. Устраните указанную причину и повторите формирование отчёта.')
        if pending:
            try:
                backup = export_opportunity_plan(pending, normalized, analysis, model, provider_name)
                st.download_button('Скачать резервный план', backup, file_name='opportunities-plan.json',
                                   mime='application/json', on_click='ignore', width='stretch')
                st.caption('Это структурированный план для повторной сборки, ещё не готовый отчёт. '
                           'Скачанная копия позволяет продолжить в новой сессии с теми же документом и картой.')
            except ProcessError as exc:
                st.warning('Резервный план не прошёл проверку: ' + str(exc))
        label = 'Повторить сборку без API' if pending else 'Сформировать отчёт'
        if st.button(label, type='primary'):
            # A queued second click must reuse the result or saved plan. The session
            # guard also covers overlapping reruns while the provider is still busy.
            if key in reports:
                st.rerun()
            owner = uuid4().hex
            if in_flight.setdefault(key, owner) != owner:
                st.info('Отчёт уже формируется. Повторный запрос не отправлен.')
                return
            pending = plans.get(key)
            provider = None; started = time.perf_counter()
            st.session_state.setdefault('opportunity_started', {})[key] = started
            try:
                if pending is None and operational_action_ids(normalized['map']):
                    try: secrets = st.secrets.to_dict()
                    except FileNotFoundError: secrets = {}
                    secret, configured_model = configuration(secrets)
                    provider = OpenAIOpportunityProvider(secret, configured_model)
                with st.spinner('Готовим рекомендации и проверяем источники…' if pending is None else
                                'Собираем отчёт из сохранённого плана…', show_time=True):
                    result = generate_opportunities(normalized, analysis, provider, model=model,
                        provider_name=provider_name, saved_plan=pending,
                        on_plan=lambda plan: plans.update({key: plan}))
                    # Streamlit may interrupt at the spinner's exit on a queued
                    # rerun. Save the result before the next UI yield point.
                    reports[key] = result
                    plans.pop(key, None); failures.pop(key, None)
                st.session_state.setdefault('opportunity_origins', {})[key] = 'generated'
            except ProcessError as exc:
                metrics = exc.metrics if isinstance(exc, OpportunityError) else {
                    'requests': 0, 'elapsed_seconds': time.perf_counter()-started, 'usage': None}
                failures[key] = {**metrics, 'message': str(exc)}
            finally:
                try:
                    if provider: provider.close()
                finally:
                    elapsed = time.perf_counter() - started
                    wait = getattr(provider, 'last_call_elapsed_seconds', None) if provider else 0.0
                    st.session_state.setdefault('opportunity_stage_metrics', {})[key] = {
                        'elapsed_seconds': elapsed, **request_timing(elapsed, wait),
                    }
                    if in_flight.get(key) == owner:
                        in_flight.pop(key, None)
                    st.session_state.get('opportunity_started', {}).pop(key, None)
            st.rerun()
        return
    # All presentations share a cached view and already prepared bytes.
    files = _exports(key, artifact)
    st.success('Отчёт готов.')
    if (files.get('pdf_layout') or {}).get('exceeded'):
        st.warning(files['pdf_layout']['message'])
    if files['pdf'] is not None:
        st.download_button('Скачать отчёт PDF', files['pdf'], type='primary',
                           file_name='opportunity-report.pdf', mime='application/pdf',
                           on_click='ignore', width='stretch')
    if files['zip'] is not None:
        label = 'Скачать доступные материалы ZIP' if files['failed'] else 'Скачать все материалы ZIP'
        st.download_button(label, files['zip'], file_name='govai-results.zip', mime='application/zip',
                           on_click='ignore', width='stretch')
    if files['failed']:
        st.warning('Не удалось подготовить: ' + ', '.join(files['failed']) +
                   '. Остальные файлы доступны. Повторный запрос к модели не нужен. '
                   + ('Сохраните JSON; после его восстановления можно повторить локальную подготовку файлов.'
                      if files['json'] is not None else 'Сохраните доступные документы; полный JSON сейчас подготовить не удалось.'))
        for error in files.get('errors', []):
            st.caption(error['format'] + ': ' + error['reason'])
        _source_downloads(files, prefix='fallback')
    st.caption('Сохраните архив: он содержит отчёт и полный JSON для восстановления. '
               'Без скачанной копии результат может быть потерян после завершения сессии.')
    report = artifact['report']
    if not report['initiatives']:
        st.info('Подходящих инициатив не найдено. В отчёте сохранено объяснение: '
                'это допустимый результат, а не ошибка анализа.')
    warning = ('Это предложения для проверки, а не полученный эффект или доказанная окупаемость. '
               'Сценарные числа не являются измерениями ведомства.')
    if report['normalization_status'] != 'completed':
        warning += ' Для части операций нужно уточнить границы и участников до количественных выводов.'
    quarantined = sum(r.get('origin') == 'integrity_validator' for r in report['rejected'])
    if quarantined:
        warning += (f' Инициатив с неподтверждёнными источниками отложено: {quarantined}. '
                    'Они не включены в принятые рекомендации и числовые эффекты.')
    st.warning(warning)
    view = files['view']
    if view is None:
        st.info('Не удалось подготовить краткое представление. Сохранённый JSON и доступные форматы можно скачать выше.')
        return
    st.subheader('Главное для руководителя')
    for block in view['summary_blocks']:
        st.markdown('**' + escape_text(block['label']) + '**')
        st.write(escape_text(block['text']))
    st.subheader('Предлагаемые инициативы')
    st.caption(f"Инициатив: {len(view['initiatives'])}. Номер — предварительная очередь проверки; эффекты не суммируются автоматически.")
    for row in view['initiatives']:
        with st.container(border=True):
            st.markdown('**' + escape_text(f"№ {row['number']}. {row['title']}") + '**')
            st.caption(escape_text(row['category_label']))
            st.write(escape_text(row['benefit']))
            st.caption('Главное условие: ' + escape_text(row['condition']))
            if row['clauses']:
                st.caption('Основания: ' + escape_text(row['clauses']))


def render_opportunity_technical(normalized, analysis):
    key, model, provider_name, reports = opportunity_state(normalized)
    st.subheader('Отчёт: восстановление и диагностика')
    uploaded = st.file_uploader('Готовый opportunities.json', type=['json'], max_upload_size=64,
                               key='opportunities-upload')
    busy = key in st.session_state.get('opportunity_in_flight', {})
    if st.button('Восстановить отчёт', disabled=uploaded is None or busy):
        try:
            reports[key] = restore_opportunities(uploaded.getvalue(), normalized, analysis, model, provider_name)
            st.session_state.setdefault('opportunity_exports', {}).pop(key, None)
            st.session_state.setdefault('opportunity_origins', {})[key] = 'imported'
            st.session_state.setdefault('opportunity_failures', {}).pop(key, None)
            st.session_state.setdefault('opportunity_plans', {}).pop(key, None)
            st.rerun()
        except ProcessError as exc: st.error('Восстановление отчёта: ' + str(exc))
    plan_upload = st.file_uploader('Резервный opportunities-plan.json', type=['json'], max_upload_size=64,
                                  key='opportunity-plan-upload')
    if st.button('Восстановить план без API', disabled=plan_upload is None or busy or key in reports):
        try:
            restored = restore_opportunity_plan(plan_upload.getvalue(), normalized, analysis, model, provider_name)
            st.session_state.setdefault('opportunity_plans', {})[key] = restored
            st.session_state.setdefault('opportunity_failures', {}).pop(key, None)
            st.rerun()
        except ProcessError as exc: st.error('Восстановление плана: ' + str(exc))
    artifact = reports.get(key)
    if artifact:
        files = _exports(key, artifact)
        _source_downloads(files, prefix='technical')
        st.caption(f"Подготовка файлов: {files['elapsed_seconds']:.2f} с. Повторное открытие использует готовые файлы.")
        if files.get('errors'):
            st.caption('Причины ошибок подготовки файлов; повторный анализ не требуется.')
            st.json(files['errors'])
        st.caption('Отчёт восстановлен без API.' if st.session_state.get('opportunity_origins', {}).get(key) in ('imported', 'restored_session')
                   else 'Отчёт сформирован в текущей сессии.')
        st.json(artifact['metrics'])
        st.caption('Метрики относятся к сохранённому запуску; локальная пересборка не создаёт API-запросов.')
        measured = st.session_state.get('opportunity_stage_metrics', {}).get(key)
        if measured:
            st.caption('Время текущего этапа: API включает сеть и ожидание ответа, остальное — локальная обработка.')
            st.json(measured)
    pending = st.session_state.get('opportunity_plans', {}).get(key)
    if pending:
        st.info('Структурированный план сохранён в памяти сессии, но отчёт ещё не готов. '
                'Скачайте резервный план на основном экране: без копии после потери сессии он недоступен.')
        st.json(pending['metrics'])
        if st.button('Удалить план для нового запроса', disabled=busy,
                     help='Только удаление. Новый платный запрос возможен после нажатия «Сформировать отчёт».'):
            st.session_state.opportunity_plans.pop(key, None)
            st.session_state.setdefault('opportunity_failures', {}).pop(key, None)
            st.rerun()
    if key in st.session_state.get('opportunity_failures', {}):
        st.caption('Последняя ошибка формирования отчёта')
        st.json(st.session_state.opportunity_failures[key])
