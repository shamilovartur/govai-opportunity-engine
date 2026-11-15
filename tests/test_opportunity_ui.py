import json
from io import BytesIO
from zipfile import ZipFile

from docx import Document
from types import SimpleNamespace
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from govai.opportunities import generate_opportunities
from govai.normalization import normalize_analysis
from govai.processes import ProcessError
from test_map_ui import APP, setup_inputs, upload, button
from test_normalization import Normalizer
from opportunity_helpers import OpportunityMock, proposal
from test_opportunities import plan


def inputs(report=None, normalized=None):
    a,np,original,old_uploader=setup_inputs()
    n=normalized or normalize_analysis(a,Normalizer(np))
    def uploader(label,**kw):
        if label=='Нормализованный normalized_processes.json': return upload('normalized_processes.json',json.dumps(n).encode())
        if label=='Готовый opportunities.json' and report: return upload('opportunities.json',report)
        return old_uploader(label,**kw)
    return a,n,original,uploader


class UIProvider(OpportunityMock):
    prepared=None
    count=0
    def __init__(self,*args): super().__init__(self.prepared)
    def opportunities(self,*args,**kw):
        type(self).count+=1
        return super().opportunities(*args,**kw)


def load_saved_processes(app):
    app.run()
    button(app,'Импортировать результат').click().run()
    button(app,'Восстановить карту').click().run()
    assert not app.exception and not app.error
    return app


def test_report_on_main_screen_no_reextract_renormalize_or_navigation_calls(monkeypatch):
    a,n,original,uploader=inputs(); UIProvider.prepared=plan(proposal(n,['register'])); UIProvider.count=0
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder'); monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch('streamlit.file_uploader',side_effect=uploader), patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider), \
         patch('govai.process_ui.extract_with_checkpoint',side_effect=AssertionError('No extraction')), \
         patch('govai.map_ui.normalize_analysis',side_effect=AssertionError('No normalization')), \
         patch('streamlit.download_button',wraps=st.download_button) as download:
        app=load_saved_processes(AppTest.from_file(str(APP)))
        assert UIProvider.count==0
        button(app,'Сформировать отчёт').click().run()
        assert not app.exception and not app.error and UIProvider.count==1
        assert app.session_state['process_result_bytes']==original
        assert len(app.session_state['opportunity_reports'])==1
        assert all(b.proto.ignore_rerun for b in app.get('download_button'))
        assert any(s.value=='Отчёт готов.' for s in app.success)
        labels={'Скачать отчёт PDF','Скачать все материалы ZIP'}
        shown=[b for b in app.get('download_button') if b.label in labels]
        assert {b.label for b in shown}==labels
        assert all(not b.proto.HasField('deferred_file_id') for b in shown)
        technical=next(e for e in app.expander if e.label=='Технические данные')
        assert not any(b.label in labels for b in technical.get('download_button'))
        saved=next(iter(app.session_state['opportunity_reports'].values()))
        data={c.args[0]:c.args[1] for c in download.call_args_list if c.args and c.args[0] in labels}
        assert all(isinstance(value,bytes) for value in data.values())
        with ZipFile(BytesIO(data['Скачать все материалы ZIP'])) as bundle:
            assert json.loads(bundle.read('opportunities.json'))==saved
            assert bundle.read('opportunity-report.pdf')==data['Скачать отчёт PDF']
            assert Document(BytesIO(bundle.read('opportunity-report.docx'))).paragraphs
            assert set(bundle.namelist())=={'opportunity-report.pdf','opportunity-report.docx','opportunity-report.md',
                'opportunities.json','opportunity-details.txt','README.txt'}
            from govai.opportunity_report import render_markdown
            assert bundle.read('opportunity-report.md').decode('utf-8')==render_markdown(saved)
        from pypdf import PdfReader
        assert PdfReader(BytesIO(data['Скачать отчёт PDF'])).pages
        assert not any(b.label=='Сформировать отчёт' for b in app.button)
        next(c for c in app.checkbox if c.label=='Показать исходный текст и структуру').check().run()
        app.run(); assert UIProvider.count==1


def test_saved_report_restores_without_key_or_generation(monkeypatch):
    a,n,_,_=inputs(); saved=generate_opportunities(n,a,OpportunityMock(plan(proposal(n,['register']))))
    _,_,_,uploader=inputs(json.dumps(saved).encode(),n)
    monkeypatch.delenv('OPENAI_API_KEY',raising=False); monkeypatch.delenv('OPENAI_MODEL',raising=False)
    with patch('streamlit.file_uploader',side_effect=uploader), \
         patch('govai.opportunity_ui.generate_opportunities',side_effect=AssertionError('No generation')):
        app=load_saved_processes(AppTest.from_file(str(APP)))
        button(app,'Восстановить отчёт').click().run()
        assert not app.exception and not app.error
        assert next(iter(app.session_state['opportunity_reports'].values()))==saved
        app.run(); assert not app.exception


def test_downloaded_json_restores_in_new_session_without_any_generation(monkeypatch):
    _,n,_,uploader=inputs(); UIProvider.prepared=plan(proposal(n,['register'])); UIProvider.count=0
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder'); monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch('streamlit.file_uploader',side_effect=uploader), \
         patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider), \
         patch('streamlit.download_button',wraps=st.download_button) as download:
        app=load_saved_processes(AppTest.from_file(str(APP)))
        button(app,'Сформировать отчёт').click().run()
        assert not app.exception and UIProvider.count==1
        saved=next(iter(app.session_state['opportunity_reports'].values()))
        downloaded=next(c.args[1] for c in reversed(download.call_args_list)
                        if c.args and c.args[0]=='Скачать данные анализа (JSON)')
        assert isinstance(downloaded,bytes)
    _,_,_,restore_uploader=inputs(report=downloaded,normalized=n)
    monkeypatch.delenv('OPENAI_API_KEY')
    with patch('streamlit.file_uploader',side_effect=restore_uploader), \
         patch('govai.opportunity_ui.generate_opportunities',side_effect=AssertionError('No report generation')), \
         patch('govai.process_ui.extract_with_checkpoint',side_effect=AssertionError('No extraction')), \
         patch('govai.map_ui.normalize_analysis',side_effect=AssertionError('No normalization')):
        restored=load_saved_processes(AppTest.from_file(str(APP)))
        button(restored,'Восстановить отчёт').click().run()
        assert not restored.exception and not restored.error
        assert next(iter(restored.session_state['opportunity_reports'].values()))==saved
        assert any(s.value=='Отчёт готов.' for s in restored.success)
        assert any(b.label=='Скачать данные анализа (JSON)' for b in restored.get('download_button'))
        restored.run(); assert UIProvider.count==1


def test_main_cards_translate_internal_diagnostics_and_show_no_numeric_rank(monkeypatch):
    _,n,_,uploader=inputs()
    row=proposal(n,['register'])
    row['prospect_assessment']['value_reason']='review: не объединено с a98'
    row['prospect_assessment']['main_uncertainty']='incomplete; source_action_ids требуют проверки'
    UIProvider.prepared=plan(row); UIProvider.count=0
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder'); monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch('streamlit.file_uploader',side_effect=uploader), patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider):
        app=load_saved_processes(AppTest.from_file(str(APP)))
        button(app,'Сформировать отчёт').click().run()
        assert not app.exception and not app.error and UIProvider.count==1
        visible_text=' '.join(str(e.value) for group in (app.caption,app.markdown,app.subheader,app.warning) for e in group)
        assert 'Средняя' in visible_text
        assert 'ссылки на исходные действия требуют проверки' in visible_text
        assert 'часть группировки ещё не подтверждена' in visible_text
        assert all(token not in visible_text for token in ('review','incomplete','a98','source_action_ids','/100'))


def test_markdown_failure_keeps_other_formats_and_caches_failed_preparation(monkeypatch):
    _,n,_,uploader=inputs(); UIProvider.prepared=plan(proposal(n,['register'])); UIProvider.count=0
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder'); monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch('streamlit.file_uploader',side_effect=uploader), \
         patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider), \
         patch('streamlit.download_button',wraps=st.download_button) as download:
        app=load_saved_processes(AppTest.from_file(str(APP)))
        with patch('govai.opportunity_ui.render_markdown',side_effect=RuntimeError('private export detail')) as failed:
            button(app,'Сформировать отчёт').click().run()
            assert not app.exception and UIProvider.count==1
            assert any('Не удалось подготовить: Markdown' in e.value for e in app.warning)
            assert all('private' not in e.value for e in app.warning)
            cached=next(iter(app.session_state['opportunity_exports'].values()))
            assert cached['errors'] and cached['errors'][0]['detail'].startswith('private')
            assert cached['errors'][0]['reason']
            assert any(s.value=='Отчёт готов.' for s in app.success)
            assert any(b.label=='Скачать отчёт (DOCX)' for b in app.get('download_button'))
            assert any(b.label=='Скачать доступные материалы ZIP' for b in app.get('download_button'))
            backup=next(b for b in app.get('download_button') if b.label=='Скачать данные анализа (JSON)')
            assert backup.proto.ignore_rerun
            saved=next(iter(app.session_state['opportunity_reports'].values()))
            assert saved['plan']['proposals']
            data=next(c.args[1] for c in reversed(download.call_args_list)
                      if c.args and c.args[0]=='Скачать данные анализа (JSON)')
            assert json.loads(data)==saved
            app.run(); assert UIProvider.count==1 and failed.call_count==1
        app.run()
        assert failed.call_count==1
        assert not app.exception and not app.error and UIProvider.count==1
        assert next(iter(app.session_state['opportunity_reports'].values()))==saved
        assert any(s.value=='Отчёт готов.' for s in app.success)
        assert any('Markdown' in w.value for w in app.warning)


def test_json_preparation_failure_keeps_markdown_and_artifact_without_leaking_details(monkeypatch):
    _,n,_,uploader=inputs(); UIProvider.prepared=plan(proposal(n,['register'])); UIProvider.count=0
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder'); monkeypatch.setenv('OPENAI_MODEL','test-model')
    def broken_json(*args,**kwargs): raise TypeError('private serialization detail')
    with patch('streamlit.file_uploader',side_effect=uploader), patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider):
        app=load_saved_processes(AppTest.from_file(str(APP)))
        with patch('govai.opportunity_ui.json',SimpleNamespace(dumps=broken_json)):
            button(app,'Сформировать отчёт').click().run()
            assert not app.exception and UIProvider.count==1
            assert any('Не удалось подготовить: JSON' in w.value for w in app.warning)
            assert all('private' not in w.value for w in app.warning)
            assert any(b.label=='Скачать отчёт (DOCX)' for b in app.get('download_button'))
            assert not any(b.label=='Скачать данные анализа (JSON)' for b in app.get('download_button'))
            saved=next(iter(app.session_state['opportunity_reports'].values()))
            assert saved['plan']['proposals']
            app.run(); assert UIProvider.count==1
        app.run()
        assert not app.exception and not app.error and UIProvider.count==1
        assert next(iter(app.session_state['opportunity_reports'].values()))==saved
        assert not any(b.label=='Скачать данные анализа (JSON)' for b in app.get('download_button'))
        assert any(b.label=='Скачать отчёт PDF' for b in app.get('download_button'))


def test_failure_keeps_map_and_original_downloads(monkeypatch):
    _,n,original,uploader=inputs()
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder'); monkeypatch.setenv('OPENAI_MODEL','test-model')
    class Broken(UIProvider):
        def opportunities(self,*args,**kw): raise ProcessError('Неполный ответ.')
    with patch('streamlit.file_uploader',side_effect=uploader), patch('govai.opportunity_ui.OpenAIOpportunityProvider',Broken):
        app=load_saved_processes(AppTest.from_file(str(APP)))
        button(app,'Сформировать отчёт').click().run()
        assert not app.exception and 'Отчёт не сформирован' in app.error[0].value
        assert app.session_state['process_result_bytes']==original
        assert next(iter(app.session_state['normalized_maps'].values()))==n
        assert not app.session_state['opportunity_reports']
        assert any(b.label=='Скачать normalized_processes.json' for b in app.get('download_button'))
        assert next(iter(app.session_state['opportunity_failures'].values()))['requests']==1


def test_missing_key_is_a_stage_specific_error_without_model_call(monkeypatch):
    _,_,_,uploader=inputs()
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    with patch('streamlit.file_uploader',side_effect=uploader), patch('streamlit.runtime.secrets.Secrets.to_dict',return_value={}):
        app=load_saved_processes(AppTest.from_file(str(APP)))
        button(app,'Сформировать отчёт').click().run()
        assert not app.exception and 'Владельцу приложения' in app.error[0].value
        assert 'OPENAI_API_KEY' not in app.error[0].value and 'secrets.toml' not in app.error[0].value
        assert 'Отчёт не сформирован' in app.error[0].value
        assert next(iter(app.session_state['opportunity_failures'].values()))['requests']==0


def test_review_only_map_creates_provider_and_keeps_hypothesis_without_money(monkeypatch):
    from test_normalization import plan_for
    from govai.opportunities import economic_restrictions
    a,_,_,_=setup_inputs()
    n=normalize_analysis(a,Normalizer(plan_for(a,[])))
    _,_,original,uploader=inputs(normalized=n)
    aid=n['map']['actions'][0]['id']
    assert len(economic_restrictions(n['map']))==len(n['map']['actions'])
    UIProvider.prepared=plan(proposal(n,[aid])); UIProvider.count=0
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder'); monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch('streamlit.file_uploader',side_effect=uploader), patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider), \
         patch('govai.process_ui.extract_with_checkpoint',side_effect=AssertionError('No extraction')), \
         patch('govai.map_ui.normalize_analysis',side_effect=AssertionError('No normalization')):
        app=load_saved_processes(AppTest.from_file(str(APP)))
        button(app,'Сформировать отчёт').click().run()
        assert not app.exception and not app.error and UIProvider.count==1
        report=next(iter(app.session_state['opportunity_reports'].values()))['report']
        assert report['initiatives'][0]['economics']['scenarios']==[]
        assert report['normalization_status']=='incomplete'
        assert any('Для части действий нужно уточнить границы операции и исполнителя' in x.value for x in app.warning)
        view=next(iter(app.session_state['opportunity_exports'].values()))['view']
        assert any('границ' in text or 'участник' in text for text in view['conditions'])
        from govai.opportunity_ui import escape_text
        assert view['input_limitation']
        assert any(x.value==escape_text(view['input_limitation']) for x in app.main.caption)
        assert app.session_state['process_result_bytes']==original
        app.run(); assert UIProvider.count==1
        assert all(b.proto.ignore_rerun for b in app.get('download_button'))


def test_context_only_map_has_empty_result_without_provider_or_key(monkeypatch):
    from test_normalization import plan_for,node
    a,_,_,_=setup_inputs()
    n=normalize_analysis(a,Normalizer(plan_for(a,[node(a,[sid],key=f'context-{i}',kind='context')
                                               for i,sid in enumerate(a.actions)])))
    _,_,_,uploader=inputs(normalized=n)
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    with patch('streamlit.file_uploader',side_effect=uploader), \
         patch('govai.opportunity_ui.OpenAIOpportunityProvider',side_effect=AssertionError('No provider')):
        # Preserve provider.name without creating an instance.
        import govai.opportunity_ui
        govai.opportunity_ui.OpenAIOpportunityProvider.name='openai'
        app=load_saved_processes(AppTest.from_file(str(APP)))
        button(app,'Сформировать отчёт').click().run()
        assert not app.exception and not app.error
        artifact=next(iter(app.session_state['opportunity_reports'].values()))
        assert artifact['metrics']['requests']==0 and not artifact['report']['initiatives']
        assert 'нет конкретных операций' in artifact['report']['explanation']
        view=next(iter(app.session_state['opportunity_exports'].values()))['view']
        assert view['outcome']['state']=='none'
        assert any(view['outcome']['message']==x.value for x in app.info)


def test_main_three_step_flow_including_empty_llm_parameters(monkeypatch):
    from test_process_ui import Provider, upload as document_upload
    from test_map_ui import AutomaticNormalizer
    from govai.economics import REQUIRED
    class Extraction(Provider):
        model='test-model'
        calls=0
        def extract(self,payload):
            type(self).calls+=1
            return super().extract(payload)
    AutomaticNormalizer.calls_total=0
    monkeypatch.setenv('OPENAI_API_KEY','unit-test-placeholder'); monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch('streamlit.file_uploader',side_effect=lambda label,**kw:
               document_upload('1. Исполнитель проверяет заявление и готовит проект ответа.') if label=='Выберите документ' else None), \
         patch('govai.process_ui.OpenAIProvider',Extraction), patch('govai.map_ui.OpenAINormalizer',AutomaticNormalizer), \
         patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider):
        app=AppTest.from_file(str(APP)).run()
        button(app,'Извлечь процессы').click().run()
        assert not app.exception and not app.error
        assert Extraction.calls==AutomaticNormalizer.calls_total==1
        assert any(s.value=='Процессы готовы.' for s in app.success)
        assert not any(b.label=='Построить карту процессов' for b in app.button)
        normalized=next(iter(app.session_state['normalized_maps'].values()))
        UIProvider.prepared=plan(proposal(normalized,['op-0'],required_parameters=[])); UIProvider.count=0
        button(app,'Сформировать отчёт').click().run()
        assert not app.exception and not app.error and UIProvider.count==1
        assert any(s.value=='Отчёт готов.' for s in app.success)
        labels={'Скачать отчёт PDF','Скачать все материалы ZIP'}
        assert {b.label for b in app.get('download_button') if b.label in labels and b.proto.ignore_rerun}==labels
        technical=next(e for e in app.expander if e.label=='Технические данные')
        assert not technical.proto.expanded
        assert not any(b.label in labels for b in technical.get('download_button'))
        report=next(iter(app.session_state['opportunity_reports'].values()))
        assert report['plan']['proposals'][0]['required_parameters']==list(REQUIRED)
        app.run(); assert Extraction.calls==AutomaticNormalizer.calls_total==UIProvider.count==1
    with patch('streamlit.file_uploader',side_effect=lambda label,**kw:
               document_upload('Другой нормативный документ.') if label=='Выберите документ' else None):
        app.run()
        assert not any(s.value=='Отчёт готов.' for s in app.success)
        assert not any(b.label in labels for b in app.get('download_button'))
        assert button(app,'Сформировать отчёт').disabled


def test_main_failed_assembly_retains_plan_and_retries_without_api(monkeypatch):
    import govai.opportunities as module
    _,n,original,uploader=inputs()
    UIProvider.prepared=plan(proposal(n,['register'],required_parameters=['annual_cases']));UIProvider.count=0
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder');monkeypatch.setenv('OPENAI_MODEL','test-model')
    assemble=module.assemble_report
    def broken(*args,**kwargs): raise RuntimeError('internal detail must not be shown')
    with patch('streamlit.file_uploader',side_effect=uploader), patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider):
        app=load_saved_processes(AppTest.from_file(str(APP)))
        monkeypatch.setattr(module,'assemble_report',broken)
        button(app,'Сформировать отчёт').click().run()
        assert not app.exception and UIProvider.count==1
        assert app.error[0].value.startswith('Отчёт не сформирован:')
        assert 'internal' not in app.error[0].value
        assert app.session_state['opportunity_plans'] and not app.session_state['opportunity_reports']
        assert not any(b.label=='Скачать отчёт (DOCX)' for b in app.get('download_button'))
        app.run(); assert UIProvider.count==1
        monkeypatch.setattr(module,'assemble_report',assemble)
        monkeypatch.delenv('OPENAI_API_KEY')
        button(app,'Повторить сборку без API').click().run()
        assert not app.exception and not app.error and UIProvider.count==1
        assert any(s.value=='Отчёт готов.' for s in app.success)
        assert not app.session_state['opportunity_plans']
        assert app.session_state['process_result_bytes']==original
        assert any(b.label=='Скачать отчёт (DOCX)' for b in app.get('download_button'))


def test_active_request_and_queued_second_click_do_not_create_provider(monkeypatch):
    from govai.analysis_io import digest
    from govai.opportunities import compatibility
    _,n,_,uploader=inputs(); UIProvider.prepared=plan(proposal(n,['register'])); UIProvider.count=0
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder');monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch('streamlit.file_uploader',side_effect=uploader), patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider):
        app=load_saved_processes(AppTest.from_file(str(APP)))
        key=digest(compatibility(n,'test-model','openai'))
        app.session_state['opportunity_in_flight']={key: 'already-running'}
        app.run()
        assert not app.exception and UIProvider.count==0
        assert not any(b.label=='Сформировать отчёт' for b in app.button)
        assert any('Отчёт формируется' in i.value for i in app.info)
        app.session_state['opportunity_in_flight']={}
        app.run()
        launch=button(app,'Сформировать отчёт')
        launch.click().run()
        assert not app.exception and UIProvider.count==1
        # Simulate a queued stale click after the first run has already saved a report.
        launch.click().run()
        assert not app.exception and UIProvider.count==1
        assert not app.session_state['opportunity_in_flight']
        assert any(s.value=='Отчёт готов.' for s in app.success)


def test_failed_plan_backup_restores_in_new_session_and_rebuilds_without_api(monkeypatch):
    import govai.opportunities as module
    _,n,_,uploader=inputs(); UIProvider.prepared=plan(proposal(n,['register'])); UIProvider.count=0
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder');monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch('streamlit.file_uploader',side_effect=uploader), \
         patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider), \
         patch('streamlit.download_button',wraps=st.download_button) as download:
        app=load_saved_processes(AppTest.from_file(str(APP)))
        with patch.object(module,'assemble_report',side_effect=RuntimeError('local assembly failure')):
            button(app,'Сформировать отчёт').click().run()
            assert not app.exception and UIProvider.count==1
            assert not app.session_state['opportunity_reports']
            assert not app.session_state['opportunity_in_flight']
            backup=next(c.args[1] for c in reversed(download.call_args_list)
                        if c.args and c.args[0]=='Скачать резервный план')
            assert isinstance(backup,bytes)
            envelope=json.loads(backup)
            assert envelope['kind']!='govai_opportunities' and 'report' not in envelope
            assert not any(s.value=='Отчёт готов.' for s in app.success)
            main_backup=next(b for b in app.get('download_button') if b.label=='Скачать резервный план')
            assert main_backup.proto.ignore_rerun and not main_backup.proto.HasField('deferred_file_id')
            app.run(); assert UIProvider.count==1
    _,_,_,ordinary_uploader=inputs(normalized=n)
    def restored_uploader(label,**kw):
        if label=='Резервный opportunities-plan.json': return upload('opportunities-plan.json',backup)
        return ordinary_uploader(label,**kw)
    monkeypatch.delenv('OPENAI_API_KEY')
    with patch('streamlit.file_uploader',side_effect=restored_uploader), \
         patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider), \
         patch('govai.process_ui.extract_with_checkpoint',side_effect=AssertionError('No extraction')), \
         patch('govai.map_ui.normalize_analysis',side_effect=AssertionError('No normalization')):
        restored=load_saved_processes(AppTest.from_file(str(APP)))
        button(restored,'Восстановить план без API').click().run()
        assert not restored.exception and not restored.error and UIProvider.count==1
        assert restored.session_state['opportunity_plans'] and not restored.session_state['opportunity_reports']
        restored.run(); assert UIProvider.count==1
        button(restored,'Повторить сборку без API').click().run()
        assert not restored.exception and not restored.error and UIProvider.count==1
        assert any(s.value=='Отчёт готов.' for s in restored.success)


def test_invalid_structured_reply_does_not_offer_nonexistent_plan_backup(monkeypatch):
    _,n,_,uploader=inputs(); UIProvider.prepared={'proposals':'not a plan'}; UIProvider.count=0
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder');monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch('streamlit.file_uploader',side_effect=uploader), patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider):
        app=load_saved_processes(AppTest.from_file(str(APP)))
        button(app,'Сформировать отчёт').click().run()
        assert not app.exception and UIProvider.count==1
        assert app.error and not app.session_state['opportunity_plans']
        assert not app.session_state['opportunity_in_flight']
        assert not any(b.label=='Скачать резервный план' for b in app.get('download_button'))
        assert not any('План сохранён в этой сессии' in i.value for i in app.info)


def test_docx_failure_preserves_pdf_json_markdown_and_partial_bundle_without_rerender(monkeypatch):
    _,n,_,uploader=inputs(); UIProvider.prepared=plan(proposal(n,['register'])); UIProvider.count=0
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder');monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch('streamlit.file_uploader',side_effect=uploader), \
         patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider), \
         patch('streamlit.download_button',wraps=st.download_button) as download:
        app=load_saved_processes(AppTest.from_file(str(APP)))
        with patch('govai.opportunity_ui.render_docx',side_effect=RuntimeError('private layout detail')) as failed:
            button(app,'Сформировать отчёт').click().run()
            assert not app.exception and UIProvider.count==1
            assert any('Не удалось подготовить: DOCX' in e.value for e in app.warning)
            assert all('private' not in e.value for e in app.warning)
            cached=next(iter(app.session_state['opportunity_exports'].values()))
            assert cached['errors'] and cached['errors'][0]['detail'].startswith('private')
            assert cached['errors'][0]['reason']
            assert any(s.value=='Отчёт готов.' for s in app.success)
            assert any(b.label=='Скачать отчёт PDF' for b in app.get('download_button'))
            assert any(b.label=='Скачать доступные материалы ZIP' for b in app.get('download_button'))
            main=app.main
            assert any(b.label=='Скачать Markdown' for b in main.get('download_button'))
            assert any(b.label=='Скачать данные анализа (JSON)' for b in main.get('download_button'))
            saved=next(iter(app.session_state['opportunity_reports'].values()))
            backup=next(c.args[1] for c in reversed(download.call_args_list)
                        if c.args and c.args[0]=='Скачать данные анализа (JSON)')
            assert json.loads(backup)==saved
            archive=next(c.args[1] for c in reversed(download.call_args_list)
                         if c.args and c.args[0]=='Скачать доступные материалы ZIP')
            with ZipFile(BytesIO(archive)) as partial:
                assert 'opportunity-report.docx' not in partial.namelist()
                assert 'opportunity-report.pdf' in partial.namelist()
                assert 'opportunity-report.docx' in partial.read('README.txt').decode()
                assert json.loads(partial.read('opportunities.json'))==saved
            app.run(); assert failed.call_count==1
        app.run()
        assert not app.exception and not app.error and UIProvider.count==1
        assert next(iter(app.session_state['opportunity_reports'].values()))==saved
        assert failed.call_count==1
        assert any(b.label=='Скачать доступные материалы ZIP' for b in app.get('download_button'))


def test_bundle_failure_keeps_docx_and_source_files_and_successful_exports_are_cached(monkeypatch):
    import govai.opportunity_ui as ui
    _,n,_,uploader=inputs(); UIProvider.prepared=plan(proposal(n,['register'])); UIProvider.count=0
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder');monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch('streamlit.file_uploader',side_effect=uploader), patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider):
        app=load_saved_processes(AppTest.from_file(str(APP)))
        with patch('govai.opportunity_ui.render_bundle',side_effect=RuntimeError('private zip detail')):
            button(app,'Сформировать отчёт').click().run()
            assert not app.exception and UIProvider.count==1
            assert any('Не удалось подготовить: ZIP' in w.value for w in app.warning)
            assert any(b.label=='Скачать отчёт (DOCX)' for b in app.get('download_button'))
            assert any(b.label=='Скачать данные анализа (JSON)' for b in app.get('download_button'))
            assert any(b.label=='Скачать Markdown' for b in app.get('download_button'))
        with patch('govai.opportunity_ui.render_docx',wraps=ui.render_docx) as word, \
             patch('govai.opportunity_ui.render_bundle',wraps=ui.render_bundle) as bundle:
            app.run()
            assert word.call_count==bundle.call_count==0
            app.run();app.run()
            assert not app.exception and not app.error and UIProvider.count==1
            assert word.call_count==bundle.call_count==0


def test_quarantined_source_is_visible_as_warning_not_as_accepted_initiative(monkeypatch):
    _,n,_,uploader=inputs()
    good=proposal(n,['register'])
    bad=proposal(n,['register'],key='broken-source',title='Неподтверждённая рекомендация',evidence_refs=['nonexistent-evidence'])
    UIProvider.prepared=plan(good,bad); UIProvider.count=0
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder');monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch('streamlit.file_uploader',side_effect=uploader), patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider):
        app=load_saved_processes(AppTest.from_file(str(APP)))
        button(app,'Сформировать отчёт').click().run()
        assert not app.exception and not app.error and UIProvider.count==1
        view=next(iter(app.session_state['opportunity_exports'].values()))['view']
        assert view['outcome']['state']=='partial' and view['outcome']['rejected_count']==1
        assert any(view['outcome']['message']==w.value for w in app.warning)
        artifact=next(iter(app.session_state['opportunity_reports'].values()))
        assert [i['id'] for i in artifact['report']['initiatives']]==[good['key']]
        assert artifact['report']['rejected'][0]['origin']=='integrity_validator'
        assert any(b.label=='Скачать все материалы ZIP' for b in app.get('download_button'))


def test_busy_fragment_only_polls_session_and_refreshes_after_completion():
    import govai.opportunity_ui as ui
    state={'opportunity_in_flight': {'identity':'request-owner'}}
    with patch.object(ui.st,'session_state',state), patch.object(ui.st,'info') as info, \
         patch.object(ui.st,'rerun') as rerun, \
         patch.object(ui,'OpenAIOpportunityProvider',side_effect=AssertionError('No provider in status poll')):
        ui._wait_for_report.__wrapped__('identity')
        assert info.call_count==1
        rerun.assert_not_called()
        state['opportunity_in_flight'].clear()
        ui._wait_for_report.__wrapped__('identity')
        rerun.assert_called_once_with(scope='app')
        assert info.call_count==1


def test_numbered_report_restore_ui_docx_and_bundle_share_order_without_api(monkeypatch):
    """Small synthetic source plan checks the shared presentation, not LLM quality."""
    from docx.oxml.ns import qn
    from govai.opportunity_report import render_markdown, initiative_title, _safe as escape_text
    a,n,_,_=inputs()
    saved=generate_opportunities(n,a,OpportunityMock(plan(
        proposal(n,['register'],key='intake',title='Проверка материалов при приёме'),
        proposal(n,['reply'],key='answer',title='Подготовка проверенного ответа'))))
    initiatives=saved['report']['initiatives']
    assert [i['sequence_number'] for i in initiatives]==[1,2]
    _,_,_,uploader=inputs(json.dumps(saved).encode(),n)
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    monkeypatch.delenv('OPENAI_MODEL',raising=False)
    with patch('streamlit.file_uploader',side_effect=uploader), \
         patch('govai.opportunity_ui.generate_opportunities',side_effect=AssertionError('No generation')), \
         patch('govai.process_ui.extract_with_checkpoint',side_effect=AssertionError('No extraction')), \
         patch('govai.map_ui.normalize_analysis',side_effect=AssertionError('No normalization')), \
         patch('streamlit.download_button',wraps=st.download_button) as download:
        app=load_saved_processes(AppTest.from_file(str(APP)))
        button(app,'Восстановить отчёт').click().run()
        assert not app.exception and not app.error
        cards=[e.value for e in app.markdown if e.value.startswith('**№ ')]
        assert cards==[f"**{escape_text(initiative_title(i))}**" for i in initiatives]
        data={c.args[0]:c.args[1] for c in download.call_args_list if c.args}
        with ZipFile(BytesIO(data['Скачать все материалы ZIP'])) as bundle:
            assert json.loads(bundle.read('opportunities.json'))==saved
            assert bundle.read('opportunity-report.docx')==data['Скачать отчёт (DOCX)']
            assert bundle.read('opportunity-report.md').decode()==render_markdown(saved)
        doc=Document(BytesIO(data['Скачать отчёт (DOCX)']))
        table=next(table for table in doc.tables if table.rows[0].cells[0].text=='Инициатива')
        assert [c.text for c in table.rows[0].cells]==[
            'Инициатива','Практическая польза','Главное условие']
        for number,(r,i) in enumerate(zip(table.rows[1:],initiatives),1):
            assert str(number) in r.cells[0].text and i['title'] in r.cells[0].text
        assert len(table.columns)==3
        assert table.rows[0]._tr.trPr.find(qn('w:tblHeader')) is not None
        assert all(r._tr.trPr.find(qn('w:cantSplit')) is not None for r in table.rows)
        assert min(run.font.size.pt for r in table.rows for c in r.cells
                   for p in c.paragraphs for run in p.runs if run.font.size)>=10.5
        assert not any(p.text=='Приложение с источниками' for p in doc.paragraphs)
        assert any(p.text=='Условия и основания' for p in doc.paragraphs)
        assert any(b.label=='Скачать отчёт PDF' for b in app.get('download_button'))
        app.run()
        assert not app.exception and not app.error
        assert next(iter(app.session_state['opportunity_reports'].values()))==saved


def test_pdf_failure_keeps_docx_and_full_json_and_explicit_restore_retries_exports(monkeypatch):
    _, n, _, uploader = inputs()
    UIProvider.prepared = plan(proposal(n, ['register'])); UIProvider.count = 0
    monkeypatch.setenv('OPENAI_API_KEY', 'test-placeholder'); monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    with patch('streamlit.file_uploader', side_effect=uploader), \
         patch('govai.opportunity_ui.OpenAIOpportunityProvider', UIProvider), \
         patch('streamlit.download_button', wraps=st.download_button) as download:
        app = load_saved_processes(AppTest.from_file(str(APP)))
        with patch('govai.opportunity_ui.render_pdf', side_effect=RuntimeError('private PDF detail')) as failed:
            button(app, 'Сформировать отчёт').click().run()
            assert not app.exception and not app.error and UIProvider.count == 1
            assert not any(b.label == 'Скачать отчёт PDF' for b in app.get('download_button'))
            assert any(b.label == 'Скачать отчёт (DOCX)' for b in app.get('download_button'))
            assert any('Не удалось подготовить: PDF' in w.value for w in app.warning)
            assert all('private' not in w.value for w in app.warning)
            data = next(c.args[1] for c in reversed(download.call_args_list)
                        if c.args and c.args[0] == 'Скачать данные анализа (JSON)')
            saved = next(iter(app.session_state['opportunity_reports'].values()))
            assert json.loads(data) == saved
            app.run(); app.run()
            assert failed.call_count == 1 and UIProvider.count == 1
    _, _, _, restored_uploader = inputs(data, n)
    monkeypatch.delenv('OPENAI_API_KEY')
    with patch('streamlit.file_uploader', side_effect=restored_uploader), \
         patch('govai.opportunity_ui.generate_opportunities', side_effect=AssertionError('No API')), \
         patch('govai.opportunity_ui.render_pdf', wraps=__import__('govai.opportunity_ui', fromlist=['render_pdf']).render_pdf) as pdf:
        app.run()
        button(app, 'Восстановить отчёт').click().run()
        assert not app.exception and not app.error and pdf.call_count == 1
        assert any(b.label == 'Скачать отчёт PDF' for b in app.get('download_button'))
        assert any(b.label == 'Скачать все материалы ZIP' for b in app.get('download_button'))
        assert next(iter(app.session_state['opportunity_reports'].values())) == saved
        app.run(); assert pdf.call_count == 1 and UIProvider.count == 1


def test_shared_summary_and_all_exports_are_cached_with_preparation_time(monkeypatch):
    import govai.opportunity_ui as ui
    from govai.opportunities import rebuild_opportunities
    from test_opportunities import _editorial_inputs
    a, n, _, _ = inputs()
    saved = generate_opportunities(n, a, OpportunityMock(plan(proposal(n, ['register']))))
    editorial = _editorial_inputs(saved['report']['initiatives'], [i['id'] for i in saved['report']['initiatives']])
    editorial['executive_summary'] = 'Начать с проверки заявления. Специалист сопоставляет подсказки с документами и проверяет каждый вывод.'
    saved = rebuild_opportunities(json.dumps(saved).encode(), n, a, 'test-model',
        editorial_inputs=editorial, reason='Редакционная проверка общего краткого представления и повторного использования файлов.')
    _, _, _, uploader = inputs(json.dumps(saved).encode(), n)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    with patch('streamlit.file_uploader', side_effect=uploader), \
         patch('govai.opportunity_ui.generate_opportunities', side_effect=AssertionError('No API')), \
         patch('govai.opportunity_ui.management_view', wraps=ui.management_view) as view, \
         patch('govai.opportunity_ui.render_pdf', wraps=ui.render_pdf) as pdf, \
         patch('govai.opportunity_ui.render_docx', wraps=ui.render_docx) as word, \
         patch('govai.opportunity_ui.render_details', wraps=ui.render_details) as details, \
         patch('govai.opportunity_ui.render_markdown', wraps=ui.render_markdown) as markdown, \
         patch('govai.opportunity_ui.render_bundle', wraps=ui.render_bundle) as bundle:
        app = load_saved_processes(AppTest.from_file(str(APP)))
        button(app, 'Восстановить отчёт').click().run()
        assert not app.exception and not app.error
        assert view.call_count == pdf.call_count == word.call_count == details.call_count == markdown.call_count == bundle.call_count == 1
        cached = next(iter(app.session_state['opportunity_exports'].values()))
        assert cached['elapsed_seconds'] >= 0 and not cached['failed']
        shown=' '.join(e.value for e in [*app.markdown,*app.subheader])
        assert 'Начать с проверки заявления' in shown
        conclusion=cached['view']['conclusion']
        assert conclusion['heading'] in shown and ui.escape_text(conclusion['text']) in shown
        technical = next(e for e in app.expander if e.label == 'Технические данные')
        assert any('Подготовка файлов:' in e.value for e in technical.caption)
        assert not any('Подготовка файлов:' in e.value for e in app.main.caption if e not in technical.caption)
        app.run(); app.run()
        assert view.call_count == pdf.call_count == word.call_count == details.call_count == markdown.call_count == bundle.call_count == 1
        assert next(iter(app.session_state['opportunity_reports'].values())) == saved


def test_known_older_session_result_revalidated_after_prompt_update_without_api(monkeypatch):
    from govai.analysis_io import digest
    from govai.opportunities import compatibility
    a, n, _, uploader = inputs()
    from test_opportunities import historical_opportunity_artifact
    saved = historical_opportunity_artifact(n,a,plan(proposal(n,['register'])),version='2.2')
    assert saved['schema_version'] == '2.2'
    old_key = digest(saved['compatibility'])
    current_key = digest(compatibility(n, 'test-model', 'openai'))
    assert old_key != current_key
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with patch('streamlit.file_uploader', side_effect=uploader), \
         patch('govai.opportunity_ui.generate_opportunities', side_effect=AssertionError('No new generation')), \
         patch('govai.opportunity_ui.restore_opportunities', wraps=__import__('govai.opportunity_ui', fromlist=['restore_opportunities']).restore_opportunities) as restore:
        app = load_saved_processes(AppTest.from_file(str(APP)))
        app.session_state['opportunity_reports'] = {old_key: saved}
        app.run()
        assert not app.exception and not app.error and restore.call_count == 1
        assert app.session_state['opportunity_reports'][current_key] == saved
        assert any(s.value == 'Отчёт готов.' for s in app.success)
        assert not any(b.label == 'Сформировать отчёт' for b in app.button)
        app.run(); assert restore.call_count == 1


def test_other_or_modified_cached_report_is_not_shown_for_current_document(monkeypatch):
    from copy import deepcopy
    a, n, _, uploader = inputs()
    saved = generate_opportunities(n, a, OpportunityMock(plan(proposal(n, ['register']))))
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    with patch('streamlit.file_uploader', side_effect=uploader), \
         patch('govai.opportunity_ui.generate_opportunities', side_effect=AssertionError('No generation')):
        app = load_saved_processes(AppTest.from_file(str(APP)))
        for field in ('document_sha256', 'prompt_sha256'):
            damaged = deepcopy(saved)
            damaged['compatibility'][field] = 'not-the-current-source-or-contract'
            app.session_state['opportunity_reports'] = {'old-cache-key': damaged}
            app.run()
            assert not app.exception
            assert not any(s.value == 'Отчёт готов.' for s in app.success)
            assert any(b.label == 'Сформировать отчёт' for b in app.button)


def test_pdf_overflow_is_visible_and_download_remains_cached(monkeypatch):
    a, n, _, _ = inputs()
    saved = generate_opportunities(n, a, OpportunityMock(plan(proposal(n, ['register']))))
    _, _, _, uploader = inputs(json.dumps(saved).encode(), n)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    status = {'pages': 7, 'brief_limit': 6, 'exceeded': True,
              'message': 'В отчёте 7 страниц. Полный PDF доступен для скачивания.'}
    with patch('streamlit.file_uploader', side_effect=uploader), \
         patch('govai.opportunity_ui.generate_opportunities', side_effect=AssertionError('No generation')), \
         patch('govai.opportunity_ui.pdf_layout_status', return_value=status) as layout:
        app = load_saved_processes(AppTest.from_file(str(APP)))
        button(app, 'Восстановить отчёт').click().run()
        assert not app.exception and not app.error
        assert any('В отчёте 7 страниц' in w.value for w in app.warning)
        assert any(b.label == 'Скачать отчёт PDF' for b in app.get('download_button'))
        assert any(b.label == 'Скачать все материалы ZIP' for b in app.get('download_button'))
        app.run(); assert layout.call_count == 1


def test_known_older_plan_in_session_reassembles_without_provider(monkeypatch):
    from govai.analysis_io import digest
    a, n, _, uploader = inputs()
    from test_opportunities import historical_opportunity_artifact
    historical=historical_opportunity_artifact(n,a,plan(proposal(n,['register'])),version='2.2')
    pending={'kind':'govai_opportunity_plan','schema_version':'2.1',
        'compatibility':historical['compatibility'],'plan':historical['plan'],
        'scenario_inputs':{},'metrics':historical['metrics'],'generated_at':historical['generated_at']}
    old_key = digest(pending['compatibility'])
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with patch('streamlit.file_uploader', side_effect=uploader), \
         patch('govai.opportunity_ui.OpenAIOpportunityProvider.__init__', side_effect=AssertionError('No API provider')):
        app = load_saved_processes(AppTest.from_file(str(APP)))
        app.session_state['opportunity_plans'] = {old_key: pending}
        app.run()
        assert not app.exception and not app.error
        assert any(b.label == 'Скачать резервный план' for b in app.get('download_button'))
        button(app, 'Повторить сборку без API').click().run()
        assert not app.exception and not app.error
        assert any(s.value == 'Отчёт готов.' for s in app.success)
        assert not any(b.label == 'Сформировать отчёт' for b in app.button)
        metrics = next(iter(app.session_state['opportunity_stage_metrics'].values()))
        assert metrics['api_wait_seconds'] == 0
        assert metrics['local_seconds'] == metrics['elapsed_seconds']


def test_invalid_pdf_bytes_keep_a_marked_partial_zip_and_source_json(monkeypatch):
    a, n, _, _ = inputs()
    saved = generate_opportunities(n, a, OpportunityMock(plan(proposal(n, ['register']))))
    _, _, _, uploader = inputs(json.dumps(saved).encode(), n)
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    with patch('streamlit.file_uploader', side_effect=uploader), \
         patch('govai.opportunity_ui.generate_opportunities', side_effect=AssertionError('No generation')), \
         patch('govai.opportunity_ui.render_pdf', return_value=b'not a PDF'), \
         patch('streamlit.download_button', wraps=st.download_button) as download:
        app = load_saved_processes(AppTest.from_file(str(APP)))
        button(app, 'Восстановить отчёт').click().run()
        assert not app.exception and not app.error
        assert any('Не удалось подготовить: PDF' in w.value for w in app.warning)
        assert not any(b.label == 'Скачать отчёт PDF' for b in app.get('download_button'))
        data = next(c.args[1] for c in download.call_args_list
                    if c.args and c.args[0] == 'Скачать доступные материалы ZIP')
        with ZipFile(BytesIO(data)) as archive:
            assert archive.testzip() is None
            assert 'opportunity-report.pdf' not in archive.namelist()
            assert json.loads(archive.read('opportunities.json')) == saved
            assert 'opportunity-report.pdf' in archive.read('README.txt').decode()


def test_partial_process_label_stays_visible_after_report_restore(monkeypatch):
    a, np, _, _ = setup_inputs()
    n = normalize_analysis(a, Normalizer(np.model_copy(update={'actions': np.actions[:1]})))
    assert n['map']['status'] == 'incomplete'
    saved = generate_opportunities(n, a, OpportunityMock(plan(proposal(n, ['register']))))
    _, _, _, uploader = inputs(json.dumps(saved).encode(), n)
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    with patch('streamlit.file_uploader', side_effect=uploader):
        app = load_saved_processes(AppTest.from_file(str(APP)))
        button(app, 'Восстановить отчёт').click().run()
        assert not app.exception and not app.error
        assert any(e.label == 'Предварительный список процессов' for e in app.expander)
        assert not any(e.label == 'Процессы готовы' for e in app.expander)
        assert any(s.value == 'Отчёт готов.' for s in app.success)


def test_rejected_plan_does_not_publish_its_prevalidation_success_summary(monkeypatch):
    _,n,_,uploader=inputs()
    parsed=plan(proposal(n,['register'],evidence_refs=['foreign-evidence']))
    parsed.explanation='Выделена одна содержательная гипотеза для первого пилота.'
    UIProvider.prepared=parsed; UIProvider.count=0
    monkeypatch.setenv('OPENAI_API_KEY','test-placeholder'); monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch('streamlit.file_uploader',side_effect=uploader), \
         patch('govai.opportunity_ui.OpenAIOpportunityProvider',UIProvider):
        app=load_saved_processes(AppTest.from_file(str(APP)))
        button(app,'Сформировать отчёт').click().run()
        assert not app.exception and not app.error and UIProvider.count==1
        artifact=next(iter(app.session_state['opportunity_reports'].values()))
        assert not artifact['report']['initiatives'] and artifact['report']['rejected']
        assert artifact['report']['explanation']==parsed.explanation
        view=next(iter(app.session_state['opportunity_exports'].values()))['view']
        assert view['outcome']['state']=='rejected'
        main_text=' '.join(x.value for x in [*app.markdown,*app.subheader,*app.info,*app.warning])
        assert 'Предложения найдены, но не прошли проверку' in main_text
        assert parsed.explanation not in main_text
        assert 'Обоснованных вариантов применения ИИ не найдено' not in main_text
        assert any(b.label=='Скачать все материалы ZIP' for b in app.get('download_button'))
        app.run(); assert UIProvider.count==1


def test_old_context_rejection_can_be_rechecked_locally_from_technical_section(monkeypatch):
    from test_opportunities import historical_opportunity_artifact,presented_plan
    from govai.opportunities import OpportunityGenerationPlan
    a,n,_,_=inputs()
    nodes={row['id']:row for row in n['map']['actions']}
    refs=list(dict.fromkeys(nodes['register']['evidence_refs']+nodes['reply']['evidence_refs']))
    current=presented_plan(proposal(n,['register'],context_action_ids=['reply'],evidence_refs=refs)).model_dump()
    current.pop('recommendation_title')
    for row in current['proposals']:
        row.pop('resources'); row.pop('proposed_flow')
    parsed=OpportunityGenerationPlan.model_validate(current)
    saved=historical_opportunity_artifact(n,a,parsed,version='2.3')
    original=json.dumps(saved).encode()
    assert not saved['report']['initiatives'] and saved['report']['rejected']
    _,_,_,uploader=inputs(original,n)
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch('streamlit.file_uploader',side_effect=uploader), \
         patch('govai.opportunity_ui.generate_opportunities',side_effect=AssertionError('No API generation')), \
         patch('govai.process_ui.extract_with_checkpoint',side_effect=AssertionError('No extraction')), \
         patch('govai.map_ui.normalize_analysis',side_effect=AssertionError('No normalization')):
        app=load_saved_processes(AppTest.from_file(str(APP)))
        button(app,'Восстановить отчёт').click().run()
        assert not app.exception and not app.error
        assert next(iter(app.session_state['opportunity_reports'].values()))==saved
        button(app,'Повторно проверить источники без API').click().run()
        assert not app.exception and not app.error
        rebuilt=next(iter(app.session_state['opportunity_reports'].values()))
        assert rebuilt['schema_version']=='2.4' and rebuilt['derivation']
        assert [i['id'] for i in rebuilt['report']['initiatives']]==[parsed.proposals[0].key]
        assert rebuilt['report']['initiatives'][0]['source_action_ids']==nodes['register']['source_action_ids']
        assert rebuilt['report']['initiatives'][0]['context_grounding'][0]['action_id']=='reply'
        assert not any(b.label in ('Повторно проверить источники без API','Сформировать отчёт') for b in app.button)
        assert any(b.label=='Скачать отчёт PDF' for b in app.get('download_button'))
        assert json.dumps(saved).encode()==original
        app.run(); assert next(iter(app.session_state['opportunity_reports'].values()))==rebuilt
