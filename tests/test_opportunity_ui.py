"""Public one-button path and internal recovery; no public technical controls."""
import json
import re
from copy import deepcopy
from html import unescape
from io import BytesIO
from unittest.mock import patch
from types import SimpleNamespace

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from pypdf import PdfReader, PdfWriter

from govai.analysis_io import digest
from govai.normalization import normalize_analysis, compatibility as map_compatibility
from govai.opportunities import generate_opportunities, restore_opportunities, compatibility
from govai.processes import ProcessError
from test_map_ui import APP, setup_inputs, button, upload
from test_normalization import Normalizer
from opportunity_helpers import OpportunityMock, proposal
from test_opportunities import plan


def inputs():
    a,np,original,uploader=setup_inputs()
    return a,normalize_analysis(a,Normalizer(np)),original,uploader


def rendered_cards(app):
    pattern=r'<div class="govai-card-heading"><span>(\d+)</span><strong>(.*?)</strong></div>'
    return [(int(m[1]),unescape(m[2])) for e in app.markdown if (m:=re.fullmatch(pattern,e.value))]


def seed(app,a,n,original,report=None):
    """Developer-only restored stages; this is not an import feature of the site."""
    app.run()
    app.session_state['process_result']=a.raw
    app.session_state['process_result_bytes']=original
    app.session_state['verified_analysis']=a
    app.session_state['normalized_maps']={digest(map_compatibility(a,'test-model',a.raw['provider'])):n}
    if report:
        app.session_state['opportunity_reports']={digest(compatibility(n,'test-model','openai')):report}
    return app.run()


class UIProvider(OpportunityMock):
    prepared=None
    count=0
    def __init__(self,*args): super().__init__(self.prepared)
    def opportunities(self,*args,**kw):
        type(self).count+=1
        return super().opportunities(*args,**kw)


@pytest.fixture(autouse=True)
def model(monkeypatch):
    monkeypatch.setenv('OPENAI_MODEL','test-model')
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder')
    UIProvider.count=0


def assert_public(app):
    assert not app.exception
    assert not app.json and not app.text_area and not app.dataframe
    assert not any(e.label=='Технические данные' for e in app.expander)
    assert all(w.proto.label=='Выберите документ' for w in app.get('file_uploader'))
    labels=[b.label for b in app.get('download_button')]
    assert not any(any(x in label for x in ('JSON','ZIP','DOCX','checkpoint')) for label in labels)
    assert all(b.proto.ignore_rerun and not b.proto.HasField('deferred_file_id') for b in app.get('download_button'))


def test_one_button_uses_restored_stages_and_never_repeats_on_rerun():
    a,n,original,uploader=inputs(); UIProvider.prepared=plan(proposal(n,['register']))
    with patch('streamlit.file_uploader',side_effect=uploader), patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider), \
         patch('govai.process_ui.extract_with_checkpoint',side_effect=AssertionError('No extraction')), \
         patch('govai.map_ui.normalize_analysis',side_effect=AssertionError('No normalization')), \
         patch('govai.opportunity_ui.render_docx',side_effect=AssertionError('No public DOCX')), \
         patch('govai.opportunity_ui.render_bundle',side_effect=AssertionError('No public ZIP')), \
         patch('streamlit.download_button',wraps=st.download_button) as downloads:
        app=seed(AppTest.from_file(str(APP)),a,n,original)
        assert UIProvider.count==0
        button(app,'Проанализировать документ').click().run()
        assert_public(app); assert not app.error and UIProvider.count==1
        assert rendered_cards(app)[0][0]==1
        assert app.session_state['process_result_bytes']==original
        saved=next(iter(app.session_state['opportunity_reports'].values()))
        files=next(iter(app.session_state['opportunity_exports'].values()))
        assert 'json' not in files and 'docx' not in files
        calls=[c.kwargs for c in downloads.call_args_list if c.kwargs.get('label','').startswith('Скачать полный отчёт')]
        assert len(calls)==2 and calls[0]['data'] is calls[1]['data']
        assert calls[0]['key'].startswith('report_pdf_top_')
        assert calls[1]['key'].startswith('report_pdf_bottom_')
        pdf=calls[0]['data']
        assert isinstance(pdf,bytes) and PdfReader(BytesIO(pdf)).pages
        assert not app.button
        app.run(); app.run()
        assert UIProvider.count==1
        assert next(iter(app.session_state['opportunity_reports'].values()))==saved
        assert next(iter(app.session_state['opportunity_exports'].values()))==files


def test_saved_report_restores_in_new_session_without_generation(monkeypatch):
    a,n,original,uploader=inputs()
    saved=generate_opportunities(n,a,OpportunityMock(plan(proposal(n,['register']))))
    restored=restore_opportunities(json.dumps(saved).encode(),n,a,'test-model','openai')
    monkeypatch.delenv('OPENAI_API_KEY')
    with patch('streamlit.file_uploader',side_effect=uploader), \
         patch('govai.opportunity_ui.generate_opportunities',side_effect=AssertionError('No generation')):
        for _ in range(2):
            app=seed(AppTest.from_file(str(APP)),a,n,original,restored)
            assert_public(app); assert len(rendered_cards(app))==1
            app.run(); assert not app.error


def test_failed_assembly_retains_plan_and_rechecks_locally_without_provider():
    a,n,original,uploader=inputs(); UIProvider.prepared=plan(proposal(n,['register']))
    with patch('streamlit.file_uploader',side_effect=uploader),patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider):
        app=seed(AppTest.from_file(str(APP)),a,n,original)
        with patch('govai.opportunities.assemble_report',side_effect=ProcessError('Local assembly failure')):
            button(app,'Проанализировать документ').click().run()
        assert_public(app); assert app.error
        assert app.session_state['opportunity_plans'] and not app.session_state['opportunity_reports']
        pending=deepcopy(app.session_state['opportunity_plans'])
        with patch.object(UIProvider,'__init__',side_effect=AssertionError('No second API')):
            button(app,'Продолжить анализ').click().run()
        assert_public(app); assert not app.error and UIProvider.count==1
        assert not app.session_state['opportunity_plans'] and app.session_state['opportunity_reports']
        from govai.opportunities import export_opportunity_plan, restore_opportunity_plan
        item=next(iter(pending.values()))
        assert restore_opportunity_plan(export_opportunity_plan(item,n,a,'test-model','openai'),n,a,'test-model','openai')==item


@pytest.mark.parametrize('kind',['timeout','integrity','invalid_structure'])
def test_failed_report_preserves_stages_and_has_no_diagnostics(kind):
    a,n,original,uploader=inputs()
    message={'timeout':'Превышено время ожидания OpenAI.',
             'integrity':'Неизвестный source_action_ids: a-paper-personal actor=null',
             'invalid_structure':'OpenAI вернул незавершённый ответ.'}[kind]
    with patch('streamlit.file_uploader',side_effect=uploader), \
         patch('govai.opportunity_ui.generate_opportunities',side_effect=ProcessError(message)):
        app=seed(AppTest.from_file(str(APP)),a,n,original)
        button(app,'Проанализировать документ').click().run()
        assert_public(app); assert app.error
        assert app.session_state['process_result_bytes']==original and app.session_state['normalized_maps']
        assert not app.session_state['opportunity_reports']
        assert not app.session_state['opportunity_plans']
        visible=' '.join(e.value for e in app.error)
        assert all(t not in visible for t in ('source_action_ids','a-paper-personal','actor=null','OpenAI'))
        assert any(b.label=='Продолжить анализ' for b in app.button)


def test_pdf_failure_retains_report_and_explicit_local_retry_uses_no_api():
    a,n,original,uploader=inputs()
    saved=generate_opportunities(n,a,OpportunityMock(plan(proposal(n,['register']))))
    with patch('streamlit.file_uploader',side_effect=uploader), \
         patch('govai.opportunity_ui.generate_opportunities',side_effect=AssertionError('No generation')):
        with patch('govai.opportunity_ui.render_pdf',side_effect=ValueError('private error')) as export:
            app=seed(AppTest.from_file(str(APP)),a,n,original,saved)
            assert_public(app); assert len(rendered_cards(app))==1
            app.run(); assert export.call_count==1
            assert all('private error' not in w.value for w in app.warning)
        button(app,'Повторить подготовку PDF').click().run()
        assert_public(app)
        assert any(b.label.startswith('Скачать полный отчёт') for b in app.get('download_button'))


@pytest.mark.parametrize(('page_count','ending'),[(1,'страница'),(2,'страницы'),(7,'страниц'),
    (11,'страниц'),(12,'страниц'),(14,'страниц'),(21,'страница'),(22,'страницы'),(25,'страниц')])
def test_actual_pdf_page_count_never_hides_long_result(page_count,ending):
    a,n,original,uploader=inputs(); saved=generate_opportunities(n,a,OpportunityMock(plan(proposal(n,['register']))))
    writer=PdfWriter()
    for _ in range(page_count): writer.add_blank_page(width=450,height=800)
    data=BytesIO(); writer.write(data)
    with patch('streamlit.file_uploader',side_effect=uploader),patch('govai.opportunity_ui.render_pdf',return_value=data.getvalue()):
        app=seed(AppTest.from_file(str(APP)),a,n,original,saved)
        assert_public(app)
        buttons=app.get('download_button')
        assert len(buttons)==2 and len({b.key for b in buttons})==2
        assert all(b.label==f'Скачать полный отчёт (PDF, {page_count} {ending})' for b in buttons)
        assert sum(c.value.startswith('Полный отчёт содержит') for c in app.caption)==1


def test_active_report_request_prevents_reentry():
    from govai.opportunity_ui import prepare_opportunity_report
    a,n,_,_=inputs(); key=digest(compatibility(n,'test-model','openai'))
    state={'verified_analysis':a,'opportunity_in_flight':{key:'owner'}}
    with patch.object(st,'session_state',state),patch('govai.opportunity_ui.OpenAIOpportunityProvider.__init__') as provider:
        with pytest.raises(ProcessError,match='уже формируется'): prepare_opportunity_report(n,a)
        provider.assert_not_called()
        assert state['opportunity_in_flight'][key]=='owner'


@pytest.mark.parametrize('format',['render_pdf','render_docx','render_markdown','render_bundle'])
def test_internal_exports_remain_independent_and_cached(format):
    from govai.opportunity_ui import _exports
    a,n,_,_=inputs(); saved=generate_opportunities(n,a,OpportunityMock(plan(proposal(n,['register']))))
    state={}
    with patch.object(st,'session_state',state),patch('govai.opportunity_ui.'+format,side_effect=ValueError('private failure')) as fail:
        files=_exports('test',saved)
        assert files['json'] and json.loads(files['json'])==saved
        assert files['errors'] and _exports('test',saved) is files
        assert fail.call_count==1
        assert restore_opportunities(files['json'],n,a,'test-model','openai')==saved
