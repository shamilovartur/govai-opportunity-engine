"""One compact renderer for both freshly generated and imported normalized maps."""

import json
import math
import os
import time

import streamlit as st

from govai.analysis_io import digest
from govai.llm import OpenAINormalizer, configuration
from govai.normalization import (
    KINDS, LANES, MODES, PARTICIPANTS, NormalizationError, compatibility,
    markdown_report, normalize_analysis, prepare_request, restore_normalized,
)
from govai.processes import ProcessError


def _source_notices(rejected_count):
    st.warning('Цитаты сверены с НПА. Смысл, полнота и группировка требуют содержательной проверки. '
               'Нормализация не обнаруживает действия, пропущенные первым этапом.')
    if rejected_count:
        st.warning(f'Первый этап отклонил кандидатов/полей: {rejected_count}. '
                   'Причины сохранены в технических данных и исходном JSON.')


def render_map(artifact):
    m = artifact['map']
    processes, actions = m['processes'], m['actions']
    st.write(f"Процессов: {len(processes)} · Операций: {sum(a['kind'] == 'operation' for a in actions)} · "
             f"Обобщений и дополнительного материала: {sum(a['kind'] != 'operation' for a in actions)}")
    if m['status'] == 'incomplete':
        st.error(f"Нормализация неполная: {len(m['deferred_source_action_ids'])} кандидатов сохранено в «Требует проверки». "
                 'Они не потеряны и не включены в число операций.')
    elif m['status'] == 'needs_review':
        st.warning('Карта собрана с замечаниями. Проверьте отмеченные действия и классификацию.')
    else:
        st.success('Карта собрана. Все исходные кандидаты учтены.')
    _source_notices(len(m['source_diagnostics']['rejected_candidates']))
    if not processes:
        st.info(m['explanation'] or 'В исходном результате нет кандидатов процессов.')
        return
    categories = list(dict.fromkeys(p['lane'] for p in processes))
    selected_lane = st.selectbox('Раздел карты', ['all'] + categories,
        format_func=lambda v: 'Все разделы' if v == 'all' else LANES[v],
        key='map-lane-' + artifact['normalization_id'])
    visible = [p for p in processes if selected_lane == 'all' or p['lane'] == selected_lane]
    page_key = f"map-page-{artifact['normalization_id']}-{selected_lane}"
    page = int(st.number_input('Страница процессов', min_value=1, max_value=max(1, math.ceil(len(visible)/10)),
                               value=1, step=1, key=page_key)) if len(visible) > 10 else 1
    visible = visible[(page-1)*10:page*10]
    st.dataframe([{'Процесс': p['title'], 'Содержание': p['summary'],
                   'Операций': sum(a['process_id'] == p['id'] and a['kind'] == 'operation' for a in actions)}
                  for p in visible], hide_index=True)
    by_id = {p['id']: p for p in visible}
    pid = st.selectbox('Выберите процесс', list(by_id), format_func=lambda v: by_id[v]['title'], key=page_key+'-process')
    st.text(by_id[pid]['title'])
    st.text(by_id[pid]['summary'])
    chosen = [a for a in actions if a['process_id'] == pid]
    st.caption(f"В выбранном процессе: операций — {sum(a['kind'] == 'operation' for a in chosen)}, "
               f"обобщений и других узлов — {sum(a['kind'] != 'operation' for a in chosen)}.")
    st.dataframe([{'Действие': a['title'], 'Тип': KINDS[a['kind']], 'Участник': PARTICIPANTS[a['participant']]}
                  for a in chosen], hide_index=True)
    lookup = {a['id']: a for a in chosen}
    aid = st.selectbox('Выберите действие', list(lookup), format_func=lambda v: lookup[v]['title'], key=page_key+'-'+pid)
    action = lookup[aid]
    st.text(action['title'])
    for reason in action['review_reasons']:
        st.warning(reason)
    # No source details/evidence are rendered for other processes or unchecked views.
    if st.checkbox('Показать детали действия', key='details-'+artifact['normalization_id']+'-'+aid):
        st.text('Участник: ' + PARTICIPANTS[action['participant']])
        st.text('Способ выполнения по документу: ' + MODES[action['mode']])
        if action['mode'] != 'unknown':
            st.caption('Это формулировка НПА, а не подтверждение фактически внедрённой системы.')
        if action['parent_id']:
            parent = next(n for n in actions if n['id'] == action['parent_id'])
            st.text('Обобщение: ' + parent['title'])
        for sid in action['source_action_ids']:
            a = m['source_actions'][sid]
            st.text('Исходная формулировка: ' + a['description'])
            st.text('Исполнитель в исходных данных: ' + (a['actor']['text'] if a['actor'] else 'не определён'))
            for field, label in [('inputs', 'Входы'), ('outputs', 'Результаты и альтернативы'),
                                 ('conditions', 'Условия, сроки и исключения'), ('dependencies', 'Зависимости')]:
                if a[field]:
                    st.text(label + ': ' + '; '.join(v['text'] for v in a[field]))
        if action['split_reason']:
            st.text('Разделение составного кандидата: ' + action['split_reason'])
        if action['merge_reason']:
            st.text('Основание объединения: ' + action['merge_reason'])
    if st.checkbox('Показать подтверждающие цитаты', key='evidence-'+artifact['normalization_id']+'-'+aid):
        all_details = st.checkbox('Включить доказательства всех исходных условий и деталей', key='all-proof-'+aid)
        for eid in action['retained_evidence_refs'] if all_details else list(dict.fromkeys(
                action['evidence_refs'] + action['participant_refs'] + action['mode_refs'])):
            e = m['evidence'][eid]
            page = f" · страница PDF {e['page_number']}" if e['page_number'] else ''
            st.caption(f"Пункт {e['clause_number'] or 'не определён'}{page}")
            st.text(e['quote'])


def map_state(analysis):
    model = os.environ.get('OPENAI_MODEL', '').strip() or analysis.raw['model']
    provider = analysis.raw['provider']
    key = digest(compatibility(analysis, model, provider))
    return key, model, provider, st.session_state.setdefault('normalized_maps', {})


def normalize_processes(analysis):
    """The existing second stage, called only by an explicit analysis/retry action."""
    key, model, _, maps = map_state(analysis)
    provider = None; started = time.perf_counter()
    try:
        prepare_request(analysis)
        if analysis.actions:
            try: secrets = st.secrets.to_dict()
            except FileNotFoundError: secrets = {}
            secret, configured_model = configuration(secrets)
            provider = OpenAINormalizer(secret, configured_model)
        artifact = normalize_analysis(analysis, provider, model=model)
        maps[key] = artifact
        st.session_state.setdefault('normalized_origins', {})[key] = 'generated'
        st.session_state.setdefault('normalization_failures', {}).pop(key, None)
        return artifact
    except ProcessError as exc:
        metrics = exc.metrics if isinstance(exc, NormalizationError) else {
            'requests': 0, 'elapsed_seconds': time.perf_counter()-started, 'usage': None}
        st.session_state.setdefault('normalization_failures', {})[key] = metrics
        raise
    finally:
        if provider: provider.close()


def render_workspace(analysis, origin, *, technical=False):
    key, model, provider_name, maps = map_state(analysis)
    origins = st.session_state.setdefault('normalized_origins', {})
    artifact = maps.get(key)
    if not technical:
        if artifact:
            st.success('Процессы готовы.')
            m = artifact['map']
            visible = [p for p in m['processes'] if p['id'] != '__review__']
            st.caption(f'Обнаружено процессов: {len(visible)}')
            for p in visible[:8]: st.text('• ' + p['title'])
            if len(visible) > 8:
                with st.expander(f'Все процессы ({len(visible)})'):
                    for p in visible[8:]: st.text('• ' + p['title'])
            if m['status'] != 'completed':
                st.warning('Часть группировки требует проверки. Ограничения будут учтены в отчёте.')
            if not visible: st.info('Конкретные процессы не определены. Отчёт объяснит ограничения анализа.')
        return artifact

    st.subheader('Сохранённые процессы и карта')
    st.caption('Исходный анализ: ' + ('импортирован; извлечение не запускалось.' if origin == 'imported' else 'получен в сессии.'))
    st.write(f"Исходные данные: {len(analysis.raw['procedures'])} групп · {len(analysis.actions)} кандидатов.")
    uploaded = st.file_uploader('Нормализованный normalized_processes.json', type=['json'],
                               max_upload_size=64, key='normalized_upload')
    if st.button('Восстановить карту', disabled=uploaded is None):
        try:
            maps[key] = restore_normalized(uploaded.getvalue(), analysis, model, provider_name)
            origins[key] = 'imported'
            st.session_state.pop('process_error', None)
            st.rerun()
        except ProcessError as exc:
            st.error('Восстановление карты: ' + str(exc))
    if artifact:
        if st.button('Повторить только нормализацию', help='Один новый платный запрос; извлечение не повторяется.'):
            try:
                with st.spinner('Анализ выполняется…'): normalize_processes(analysis)
                st.rerun()
            except ProcessError as exc: st.error('Не удалось обновить карту: ' + str(exc))
        st.caption('Карта: ' + ('восстановлена без API' if origins.get(key) == 'imported' else 'получена в сессии'))
        if st.checkbox('Показать карту и доказательства', key='inspect-map'): render_map(artifact)
        if st.checkbox('Показать учёт ID и спорные решения', key='show-accounting'):
            st.json({k: artifact['map'][k] for k in ('accounting', 'issues', 'missing_from_plan', 'deferred_source_action_ids')})
        if st.checkbox('Показать проверенные доказательства и координаты', key='show-normalized-proofs'):
            st.json(artifact['map']['evidence'])
        st.json(artifact['metrics'])
        st.download_button('Скачать normalized_processes.json', lambda: json.dumps(artifact, ensure_ascii=False, indent=2),
                           file_name='normalized_processes.json', mime='application/json', on_click='ignore')
        st.download_button('Скачать карту Markdown', lambda: markdown_report(artifact, analysis.raw['metadata']),
                           file_name='process-map.md', mime='text/markdown', on_click='ignore')
    if key in st.session_state.get('normalization_failures', {}):
        st.caption('Последняя ошибка нормализации'); st.json(st.session_state.normalization_failures[key])
    st.json({k: analysis.raw.get(k) for k in ('coverage', 'execution', 'rejected_candidates', 'warnings', 'limitations')})
    st.download_button('Скачать процессы JSON', analysis.original_bytes, file_name='processes.json',
                       help='Исходный результат без изменений.', mime='application/json', on_click='ignore')
    return artifact
