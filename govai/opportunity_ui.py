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
from govai.report_export import EXPORT_VERSION, render_pdf, render_docx, render_bundle


def opportunity_state(normalized):
    model = os.environ.get('OPENAI_MODEL', '').strip() or normalized['compatibility']['model']
    provider = OpenAIOpportunityProvider.name
    key = digest(compatibility(normalized, model, provider))
    return key, model, provider, st.session_state.setdefault('opportunity_reports', {})



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
    files = {'fingerprint': fingerprint, 'failed': [], 'view': None}
    try:
        files['view'] = management_view(artifact)
    except Exception:
        files['failed'].append('Представление отчёта')
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
        except Exception:
            files[name] = None
            files['failed'].append(label)
    files['zip'] = None
    if files['json'] is not None:
        try:
            files['zip'] = render_bundle(artifact, pdf_data=files['pdf'], docx_data=files['docx'],
                markdown_data=files['markdown'], json_data=files['json'], details_data=files['details'],
                view=files['view'])
        except Exception:
            files['failed'].append('ZIP')
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
        formats.insert(0, ('pdf', 'Скачать отчёт (PDF)', 'opportunity-report.pdf', 'application/pdf'))
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
            st.error('Отчёт не сформирован: ' + failures[key]['message'])
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
            try:
                if pending is None and operational_action_ids(normalized['map']):
                    try: secrets = st.secrets.to_dict()
                    except FileNotFoundError: secrets = {}
                    secret, configured_model = configuration(secrets)
                    provider = OpenAIOpportunityProvider(secret, configured_model)
                with st.spinner('Отчёт формируется…' if pending is None else 'Отчёт собирается без API…'):
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
                    if in_flight.get(key) == owner:
                        in_flight.pop(key, None)
            st.rerun()
        return
    # All presentations share a cached view and already prepared bytes.
    files = _exports(key, artifact)
    st.success('Отчёт готов.')
    if files['pdf'] is not None:
        st.download_button('Скачать отчёт (PDF)', files['pdf'], type='primary',
                           file_name='opportunity-report.pdf', mime='application/pdf',
                           on_click='ignore', width='stretch')
    if files['zip'] is not None:
        label = 'Скачать доступные результаты (ZIP)' if files['failed'] else 'Скачать все результаты (ZIP)'
        st.download_button(label, files['zip'], file_name='govai-results.zip', mime='application/zip',
                           on_click='ignore', width='stretch')
    if files['failed']:
        st.warning('Не удалось подготовить: ' + ', '.join(files['failed']) +
                   '. Остальные файлы доступны. Повторный запрос к модели не нужен. '
                   + ('Сохраните JSON; после его восстановления можно повторить локальную подготовку файлов.'
                      if files['json'] is not None else 'Сохраните доступные документы; полный JSON сейчас подготовить не удалось.'))
        _source_downloads(files, prefix='fallback')
    st.caption('Сохраните архив: он содержит отчёт и полный JSON для восстановления. '
               'Без скачанной копии результат может быть потерян после завершения сессии.')
    report = artifact['report']
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
        st.caption('Отчёт восстановлен без API.' if st.session_state.get('opportunity_origins', {}).get(key) == 'imported'
                   else 'Отчёт сформирован в текущей сессии.')
        st.json(artifact['metrics'])
        st.caption('Метрики относятся к сохранённому запуску; локальная пересборка не создаёт API-запросов.')
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
