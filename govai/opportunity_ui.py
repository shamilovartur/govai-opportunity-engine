"""Report generation and display; imports and diagnostics stay in the technical section."""

import json
import os
import time

import streamlit as st

from govai.analysis_io import digest
from govai.llm import OpenAIOpportunityProvider, configuration
from govai.opportunities import (OpportunityError, compatibility, generate_opportunities,
                                 restore_opportunities, operational_action_ids)
from govai.opportunity_report import business_text, economic_summary, render_markdown, _safe as escape_text
from govai.processes import ProcessError


def opportunity_state(normalized):
    model = os.environ.get('OPENAI_MODEL', '').strip() or normalized['compatibility']['model']
    provider = OpenAIOpportunityProvider.name
    key = digest(compatibility(normalized, model, provider))
    return key, model, provider, st.session_state.setdefault('opportunity_reports', {})


def render_opportunities(normalized, analysis):
    key, model, provider_name, reports = opportunity_state(normalized)
    plans = st.session_state.setdefault('opportunity_plans', {})
    failures = st.session_state.setdefault('opportunity_failures', {})
    pending = plans.get(key)
    artifact = reports.get(key)
    if not artifact:
        if key in failures:
            st.error('Отчёт не сформирован: ' + failures[key]['message'])
            st.info('План сохранён в этой сессии. Повторите сборку без нового запроса к модели.' if pending else
                    'Процессы сохранены. Устраните указанную причину и повторите формирование отчёта.')
        label = 'Повторить сборку без API' if pending else 'Сформировать отчёт'
        if st.button(label, type='primary'):
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
                reports[key] = result
                plans.pop(key, None); failures.pop(key, None)
                st.session_state.setdefault('opportunity_origins', {})[key] = 'generated'
            except ProcessError as exc:
                metrics = exc.metrics if isinstance(exc, OpportunityError) else {
                    'requests': 0, 'elapsed_seconds': time.perf_counter()-started, 'usage': None}
                failures[key] = {**metrics, 'message': str(exc)}
            finally:
                if provider: provider.close()
            st.rerun()
        return
    # Eager immutable data avoids Streamlit's session-bound deferred download operation.
    # Both files describe this same saved result; downloading never calls the model.
    # Each export is independent: a presentation error must not hide the complete JSON.
    try: json_data = json.dumps(artifact, ensure_ascii=False, indent=2).encode('utf-8')
    except Exception: json_data = None
    try: markdown_data = render_markdown(artifact).encode('utf-8')
    except Exception: markdown_data = None
    if markdown_data is not None:
        st.success('Отчёт готов.')
        st.download_button('Скачать отчёт', markdown_data, type='primary',
                           file_name='opportunity-report.md', mime='text/markdown', on_click='ignore', width='stretch')
    else:
        st.error('Не удалось подготовить файл отчёта. Результат сохранён в текущей сессии; повторная генерация не нужна.'
                 + (' Скачайте JSON для восстановления.' if json_data is not None else ''))
    if json_data is not None:
        st.download_button('Скачать данные анализа (JSON)', json_data,
                           type='secondary' if markdown_data is not None else 'primary',
                           file_name='opportunities.json', mime='application/json', on_click='ignore', width='stretch')
    else:
        st.warning('Не удалось подготовить JSON для восстановления. Данные остаются только в текущей сессии; '
                   'повторная генерация не нужна.' + (' Сохраните доступный Markdown-отчёт.' if markdown_data is not None else ''))
    if markdown_data is not None and json_data is not None:
        st.caption('Сохраните оба файла. Без скачанной копии результат может быть потерян после завершения сессии.')
    report = artifact['report']
    st.warning('Перспективность пилота не доказывает окупаемость или готовность к внедрению. '
               'Сценарные оценки — предположения для проверки, а не фактические показатели ведомства.'
               + (' Часть группировки процессов не подтверждена; это ограничивает числовые оценки.'
                  if report['normalization_status'] != 'completed' else ''))
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
    if st.button('Восстановить отчёт', disabled=uploaded is None):
        try:
            reports[key] = restore_opportunities(uploaded.getvalue(), normalized, analysis, model, provider_name)
            st.session_state.setdefault('opportunity_origins', {})[key] = 'imported'
            st.session_state.setdefault('opportunity_failures', {}).pop(key, None)
            st.session_state.setdefault('opportunity_plans', {}).pop(key, None)
            st.rerun()
        except ProcessError as exc: st.error('Восстановление отчёта: ' + str(exc))
    artifact = reports.get(key)
    if artifact:
        st.caption('Отчёт восстановлен без API.' if st.session_state.get('opportunity_origins', {}).get(key) == 'imported'
                   else 'Отчёт сформирован в текущей сессии.')
        st.json(artifact['metrics'])
        st.caption('Метрики относятся к сохранённому запуску; локальная пересборка не создаёт API-запросов.')
    pending = st.session_state.get('opportunity_plans', {}).get(key)
    if pending:
        st.info('Структурированный план сохранён в памяти сессии, но отчёт ещё не готов. После потери сессии план недоступен.')
        st.json(pending['metrics'])
        if st.button('Удалить план для нового запроса', help='Только удаление. Новый платный запрос возможен после нажатия «Сформировать отчёт».'):
            st.session_state.opportunity_plans.pop(key, None)
            st.session_state.setdefault('opportunity_failures', {}).pop(key, None)
            st.rerun()
    if key in st.session_state.get('opportunity_failures', {}):
        st.caption('Последняя ошибка формирования отчёта')
        st.json(st.session_state.opportunity_failures[key])
