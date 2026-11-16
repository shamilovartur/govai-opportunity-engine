"""Single report stage and public cards/PDF; recovery remains session-bound."""

import json
import base64
from html import escape as html_escape
import os
from pathlib import Path
import time
from uuid import uuid4

import streamlit as st

from govai.analysis_io import digest
from govai.llm import OpenAIOpportunityProvider, configuration
from govai.opportunities import (OpportunityError, compatibility, generate_opportunities,
                                 restore_opportunities, operational_action_ids,
                                 restore_opportunity_plan)
from govai.opportunity_report import (management_view, render_markdown, render_details,
                                      _safe as escape_text)
from govai.processes import ProcessError
from govai.map_ui import request_timing
from govai.report_export import EXPORT_VERSION, render_pdf, render_docx, render_bundle, pdf_layout_status


@st.cache_data(show_spinner=False)
def _result_font_css(export_version):
    """Bundle result fonts locally; no third-party font request or server setup."""
    rules = []
    for weight, name in ((400, 'Regular'), (600, 'SemiBold')):
        try:
            data = Path(__file__).with_name('fonts').joinpath(f'IBMPlexSans-{name}.ttf').read_bytes()
        except OSError:
            return ''  # The document exports report their own missing-font errors.
        rules.append('@font-face{font-family:GovAIResult;src:url(data:font/ttf;base64,' +
                     base64.b64encode(data).decode('ascii') + ') format("truetype");font-weight:' +
                     str(weight) + ';font-display:swap;}')
    return ''.join(rules)


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



def _exports(key, artifact, *, public=False):
    """Prepare each format once per result/version, including failed outcomes.

    Reopening the technical section or downloading must not repeat rendering.
    Explicit restoration clears this cache and permits a new local attempt.
    """
    cache = st.session_state.setdefault('opportunity_exports', {})
    fingerprint = digest({'artifact': artifact, 'export_version': EXPORT_VERSION, 'public': public})
    key = ('public-' if public else '') + key
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
    if public:
        preparation = {'pdf': preparation['pdf']}
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
    if not public and files['json'] is not None:
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


def prepare_opportunity_report(normalized, analysis):
    """Run the existing final stage once; session state owns recovery, not the UI."""
    key, model, provider_name, reports = opportunity_state(normalized)
    if key in reports:
        return reports[key]
    plans = st.session_state.setdefault('opportunity_plans', {})
    failures = st.session_state.setdefault('opportunity_failures', {})
    in_flight = st.session_state.setdefault('opportunity_in_flight', {})
    owner = uuid4().hex
    if in_flight.setdefault(key, owner) != owner:
        raise ProcessError('Отчёт уже формируется. Дождитесь завершения анализа.')
    provider = None; started = time.perf_counter()
    st.session_state.setdefault('opportunity_started', {})[key] = started
    try:
        pending = plans.get(key)
        if pending is None and operational_action_ids(normalized['map']):
            try: secrets = st.secrets.to_dict()
            except FileNotFoundError: secrets = {}
            secret, configured_model = configuration(secrets)
            provider = OpenAIOpportunityProvider(secret, configured_model)
        result = generate_opportunities(normalized, analysis, provider, model=model,
            provider_name=provider_name, saved_plan=pending,
            on_plan=lambda plan: plans.update({key: plan}))
        reports[key] = result
        plans.pop(key, None); failures.pop(key, None)
        st.session_state.setdefault('opportunity_origins', {})[key] = 'generated'
        return result
    except ProcessError as exc:
        metrics = exc.metrics if isinstance(exc, OpportunityError) else {
            'requests': 0, 'elapsed_seconds': time.perf_counter()-started, 'usage': None}
        failures[key] = {**metrics, 'message': str(exc)}
        raise
    finally:
        try:
            if provider: provider.close()
        finally:
            elapsed = time.perf_counter()-started
            wait = getattr(provider, 'last_call_elapsed_seconds', None) if provider else 0.0
            st.session_state.setdefault('opportunity_stage_metrics', {})[key] = {
                'elapsed_seconds': elapsed, **request_timing(elapsed, wait)}
            if in_flight.get(key) == owner: in_flight.pop(key, None)
            st.session_state.get('opportunity_started', {}).pop(key, None)


def _render_management(view):
    """Only the concise agenda is rendered here; full analysis remains in exports."""
    st.html('<style>' + _result_font_css(EXPORT_VERSION) + """
    div[class*="st-key-govai_card_"] {background:#F0F4F2;color:#23312C;padding:1.2rem;border-radius:0.8rem;margin:0.5rem 0;font-family:GovAIResult,Arial,sans-serif;}
    div[class*="st-key-govai_card_"] p {font-family:inherit;}
    .st-key-govai_card_1 {background:#E5F2E9 !important;}
    .st-key-govai_card_2 {background:#EDF3F8 !important;}
    .st-key-govai_card_3 {background:#F4F0E7 !important;}
    .govai-card-heading {display:flex;align-items:baseline;gap:1rem;line-height:1.3;margin-bottom:0.7rem;}
    .govai-card-heading span {font-size:2.2rem;font-weight:600;color:#26744B;flex:0 0 auto;}
    .govai-card-heading strong {font-size:1.25rem;overflow-wrap:anywhere;}
    @media(max-width:600px){div[class*="st-key-govai_card_"]{padding:0.9rem;}.govai-card-heading{gap:0.65rem;}}
    </style>""")
    cards = view.get('cards', [])
    for card in cards:
        with st.container(key=f"govai_card_{card['rank']}"):
            st.markdown('<div class="govai-card-heading"><span>' + str(card['rank']) +
                        '</span><strong>' + html_escape(card['title']) + '</strong></div>',
                        unsafe_allow_html=True)
            st.write(escape_text(card['description']))
            figures = [card[field] for field in ('effect', 'resources') if card.get(field) and card[field] != card['description']]
            if figures: st.write(escape_text(' '.join(figures)))
            if card.get('priority_reason'): st.write(escape_text(card['priority_reason']))
            if card.get('condition') and card['condition'] not in card['description']:
                st.write(escape_text(card['condition']))



def render_ready_report(key, artifact, *, document=None):
    """One public result screen; only the PDF is prepared for visitors."""
    files = _exports(key, artifact, public=True)
    view = files['view']
    if view is None:
        st.error('Не удалось подготовить представление отчёта. Полученный анализ сохранён; сообщите владельцу приложения.')
        return
    if view.get('reference_label'):
        st.caption(view['reference_label'])
    outcome = view.get('outcome', {})
    if outcome.get('state') in ('rejected', 'partial', 'unfinished', 'none'):
        from govai.opportunity_report import business_text
        st.info(business_text(outcome['message']))
    st.subheader('Рекомендации')
    st.write(f"Найдено рекомендаций: {len(view['cards'])}")
    download = None
    if files['pdf'] is not None:
        pages = files['pdf_layout']['pages']
        ending = ('страница' if pages % 10 == 1 and pages % 100 != 11 else
                  'страницы' if pages % 10 in (2, 3, 4) and pages % 100 not in (12, 13, 14) else 'страниц')
        download = {'label': f'Скачать полный отчёт (PDF, {pages} {ending})', 'data': files['pdf'],
                    'file_name': 'opportunity-report.pdf', 'mime': 'application/pdf', 'type': 'primary',
                    'on_click': 'ignore', 'width': 'stretch'}
        st.download_button(**download, key=f'report_pdf_top_{key}')
        st.caption('Полный отчёт содержит карту процессов, подробные предложения и расчёт экономики')
    else:
        st.warning('Не удалось подготовить PDF. Рекомендации сохранены; можно повторить подготовку файла без нового анализа.')
        if st.button('Повторить подготовку PDF'):
            st.session_state.get('opportunity_exports', {}).pop('public-' + key, None)
            st.rerun()
    _render_management(view)
    if download:
        st.download_button(**download, key=f'report_pdf_bottom_{key}')
