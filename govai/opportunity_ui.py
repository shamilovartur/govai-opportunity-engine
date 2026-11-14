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
from govai.opportunity_report import business_text, economic_summary, render_markdown, _safe as escape_text
from govai.processes import ProcessError
from govai.report_export import render_docx, render_bundle


def opportunity_state(normalized):
    model = os.environ.get('OPENAI_MODEL', '').strip() or normalized['compatibility']['model']
    provider = OpenAIOpportunityProvider.name
    key = digest(compatibility(normalized, model, provider))
    return key, model, provider, st.session_state.setdefault('opportunity_reports', {})



def _exports(key, artifact):
    """Prepare presentation bytes locally; keep a successful set tied to the full artifact."""
    cache = st.session_state.setdefault('opportunity_exports', {})
    fingerprint = digest(artifact)
    saved = cache.get(key)
    if saved and saved['fingerprint'] == fingerprint and all(saved[name] is not None for name in ('docx', 'zip', 'markdown', 'json')):
        return saved
    files = {'fingerprint': fingerprint}
    try: files['json'] = json.dumps(artifact, ensure_ascii=False, indent=2).encode('utf-8')
    except Exception: files['json'] = None
    try: files['markdown'] = render_markdown(artifact).encode('utf-8')
    except Exception: files['markdown'] = None
    try: files['docx'] = render_docx(artifact)
    except Exception: files['docx'] = None
    files['zip'] = None
    if all(files[name] is not None for name in ('docx', 'markdown', 'json')):
        try:
            files['zip'] = render_bundle(artifact, docx_data=files['docx'],
                                        markdown_data=files['markdown'], json_data=files['json'])
        except Exception: pass
    cache[key] = files
    return files


def _source_downloads(files, *, prefix):
    """Fallback and technical exports remain independently available."""
    if files['markdown'] is not None:
        st.download_button('Скачать Markdown', files['markdown'], file_name='opportunity-report.md',
                           mime='text/markdown', on_click='ignore', key=prefix+'-markdown', width='stretch')
    else:
        st.warning('Не удалось подготовить Markdown. Сохраните доступный JSON; повторная генерация не нужна.')
    if files['json'] is not None:
        st.download_button('Скачать данные анализа (JSON)', files['json'], file_name='opportunities.json',
                           mime='application/json', on_click='ignore', key=prefix+'-json', width='stretch')
    else:
        st.warning('Не удалось подготовить JSON для восстановления. Сохраните доступный отчёт; '
                   'данные остаются только в текущей сессии.')

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
    # Eager immutable exports share one artifact. Cache only a complete export set;
    # failed preparation is retried locally on rerender, never by calling the model.
    files = _exports(key, artifact)
    docx_data, bundle_data = files['docx'], files['zip']
    if docx_data is not None:
        st.success('Отчёт готов.')
        st.download_button('Скачать отчёт (DOCX)', docx_data, type='primary',
                           file_name='opportunity-report.docx',
                           mime='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                           on_click='ignore', width='stretch')
    else:
        st.error('Не удалось подготовить DOCX. Результат сохранён в текущей сессии; повторная генерация не нужна. '
                 'Сохраните доступные JSON и Markdown.')
    if bundle_data is not None:
        st.download_button('Скачать все результаты (ZIP)', bundle_data,
                           file_name='govai-results.zip', mime='application/zip', on_click='ignore', width='stretch')
    else:
        st.warning('Полный архив пока не подготовлен. Доступные файлы можно сохранить отдельно; '
                   'перерисовка страницы повторит только локальную подготовку файлов.')
        _source_downloads(files, prefix='fallback')
    st.caption('Сохраните архив: он содержит отчёт и полный JSON для восстановления. '
               'Без скачанной копии результат может быть потерян после завершения сессии.')
    report = artifact['report']
    st.warning('Перспективность пилота не доказывает окупаемость или готовность к внедрению. '
               'Сценарные оценки — предположения для проверки, а не фактические показатели ведомства.'
               + (' Часть группировки процессов не подтверждена; это ограничивает числовые оценки.'
                  if report['normalization_status'] != 'completed' else ''))
    quarantined = sum(r.get('origin') == 'integrity_validator' for r in report['rejected'])
    if quarantined:
        st.warning(f'Инициатив с неподтверждёнными источниками отложено: {quarantined}. '
                   'Они не включены в принятые рекомендации и числовые эффекты; причины сохранены в отчёте.')
    st.caption(f"Инициатив: {len(report['initiatives'])}. Эффекты разных инициатив не суммируются без подтверждения независимости.")
    category = None
    for a in report['initiatives']:
        prospect = a['prospect']
        if prospect['category'] != category:
            category = prospect['category']
            st.subheader(prospect['label'])
        with st.container(border=True):
            st.markdown(f"**{escape_text(business_text(a['title']))}**")
            st.write(escape_text(business_text(a['ai_role'])))
            st.caption(escape_text(business_text(prospect['reason'])))
            st.caption('Эффект: ' + escape_text(economic_summary(a)))
            st.caption('Что проверить: ' + escape_text(business_text(prospect['main_uncertainty'])))
    if not report['initiatives']: st.info(report['explanation'])


def render_opportunity_technical(normalized, analysis):
    key, model, provider_name, reports = opportunity_state(normalized)
    st.subheader('Отчёт: восстановление и диагностика')
    uploaded = st.file_uploader('Готовый opportunities.json', type=['json'], max_upload_size=64,
                               key='opportunities-upload')
    busy = key in st.session_state.get('opportunity_in_flight', {})
    if st.button('Восстановить отчёт', disabled=uploaded is None or busy):
        try:
            reports[key] = restore_opportunities(uploaded.getvalue(), normalized, analysis, model, provider_name)
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
        _source_downloads(_exports(key, artifact), prefix='technical')
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
