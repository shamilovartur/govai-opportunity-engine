"""Small technical entry point; no financial calculations or automatic paid calls."""

import json
import os
import time

import streamlit as st

from govai.analysis_io import digest
from govai.llm import OpenAIOpportunityProvider, configuration
from govai.opportunities import (OpportunityError, compatibility, generate_opportunities,
                                 restore_opportunities, operational_action_ids)
from govai.opportunity_report import economic_summary, render_markdown
from govai.processes import ProcessError


def render_opportunity_testing(normalized, analysis):
    st.subheader('AI-инициативы и экономика — тестирование')
    st.caption('Используется сохранённая карта. Извлечение НПА и нормализация не повторяются.')
    model = os.environ.get('OPENAI_MODEL', '').strip() or normalized['compatibility']['model']
    provider_name = OpenAIOpportunityProvider.name
    key = digest(compatibility(normalized, model, provider_name))
    cache = st.session_state.setdefault('opportunity_reports', {})
    with st.expander('Восстановить готовый отчёт без API'):
        uploaded = st.file_uploader('Готовый opportunities.json', type=['json'], max_upload_size=64,
                                   key='opportunities-upload')
        if st.button('Восстановить отчёт', disabled=uploaded is None):
            try:
                cache[key] = restore_opportunities(uploaded.getvalue(), normalized, analysis, model, provider_name)
                st.session_state.setdefault('opportunity_origins', {})[key] = 'imported'
                st.rerun()
            except ProcessError as exc:
                st.error('Восстановление отчёта: ' + str(exc))
    if key not in cache and st.button('Сформировать отчёт', type='primary'):
        provider = None; started = time.perf_counter()
        try:
            if operational_action_ids(normalized['map']):
                try:
                    secrets = st.secrets.to_dict()
                except FileNotFoundError:
                    secrets = {}
                secret, configured_model = configuration(secrets)
                provider = OpenAIOpportunityProvider(secret, configured_model)
            with st.spinner('Формируем AI-инициативы, проверяем источники и рассчитываем сценарии…'):
                artifact = generate_opportunities(normalized, analysis, provider, model=model)
            cache[key] = artifact
            st.session_state.setdefault('opportunity_origins', {})[key] = 'generated'
            st.rerun()
        except ProcessError as exc:
            st.error('Этап AI-возможностей: ' + str(exc) + ' Исходная карта сохранена; повторный анализ НПА не требуется.')
            metrics = exc.metrics if isinstance(exc, OpportunityError) else {
                'requests': 0, 'elapsed_seconds': time.perf_counter()-started, 'usage': None}
            st.session_state.setdefault('opportunity_failures', {})[key] = metrics
        finally:
            if provider: provider.close()
    artifact = cache.get(key)
    if artifact:
        report = artifact['report']
        st.caption('Отчёт восстановлен без API.' if st.session_state.get('opportunity_origins', {}).get(key) == 'imported'
                   else 'Отчёт сформирован в текущей сессии.')
        st.warning('Предварительные гипотезы. Без индивидуальных параметров числовой эффект не оценён; сценарные допущения не являются показателями ведомства или экономией бюджета.')
        if report['normalization_status'] != 'completed':
            st.warning('Входная карта неполная или спорная. Подтверждённые операции учтены в поиске; замечания ограничивают числовую оценку.')
        st.write(f"Инициатив: {len(report['initiatives'])}; отклонённых сценариев: {len(report['rejected'])}.")
        if report['initiatives']:
            st.dataframe([{'Место': a['rank'], 'Инициатива': a['title'], 'Решение': a['ai_role'], 'Механизм эффекта': a['effect_mechanism'],
                           'Главное ограничение': a['uncertainties'][0], 'Приоритет': a['priority']['label'],
                           'Балл': a['priority']['score'], 'Экономика': economic_summary(a)}
                          for a in report['initiatives']], hide_index=True)
            for a in report['initiatives'][:3]:
                st.text(a['title'] + ': ' + a['ai_role'])
        else:
            st.info(report['explanation'])
        st.caption('Совокупный эффект не суммируется без подтверждения независимости. Подробности и основания — в отчёте.')
        st.caption(f"Сохранённый запуск: запросов {artifact['metrics']['requests']} · Время: {artifact['metrics']['elapsed_seconds']:.1f} с · "
                   f"Токены: {artifact['metrics']['usage'] or 'неизвестны'}")
        st.download_button('Скачать opportunities.json', lambda: json.dumps(artifact, ensure_ascii=False, indent=2),
                           file_name='opportunities.json', mime='application/json', on_click='ignore')
        st.download_button('Скачать opportunity-report.md', lambda: render_markdown(artifact),
                           file_name='opportunity-report.md', mime='text/markdown', on_click='ignore')
    if key in st.session_state.get('opportunity_failures', {}):
        metrics = st.session_state.opportunity_failures[key]
        st.caption(f"Последняя ошибка: запросов {metrics['requests']}, время {metrics['elapsed_seconds']:.1f} с; "
                   f"токены {metrics['usage'] or 'неизвестны'}.")
