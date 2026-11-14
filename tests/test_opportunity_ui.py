import json
from unittest.mock import patch

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
         patch('govai.map_ui.normalize_analysis',side_effect=AssertionError('No normalization')):
        app=load_saved_processes(AppTest.from_file(str(APP)))
        assert UIProvider.count==0
        button(app,'Сформировать отчёт').click().run()
        assert not app.exception and not app.error and UIProvider.count==1
        assert app.session_state['process_result_bytes']==original
        assert len(app.session_state['opportunity_reports'])==1
        assert all(b.proto.ignore_rerun for b in app.get('download_button'))
        assert any(s.value=='Отчёт готов.' for s in app.success)
        assert any(b.label=='Скачать отчёт' for b in app.get('download_button'))
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
        assert not app.exception and 'OPENAI_API_KEY' in app.error[0].value
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
        assert any('группировки требует проверки' in x.value for x in app.warning)
        assert any('Часть группировки процессов не подтверждена' in x.value for x in app.warning)
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
        assert 'операционных оснований' in artifact['report']['explanation']


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
        assert any(b.label=='Скачать отчёт' and b.proto.ignore_rerun for b in app.get('download_button'))
        technical=next(e for e in app.expander if e.label=='Технические данные')
        assert not technical.proto.expanded
        assert not any(b.label=='Скачать отчёт' for b in technical.get('download_button'))
        report=next(iter(app.session_state['opportunity_reports'].values()))
        assert report['plan']['proposals'][0]['required_parameters']==list(REQUIRED)
        app.run(); assert Extraction.calls==AutomaticNormalizer.calls_total==UIProvider.count==1
    with patch('streamlit.file_uploader',side_effect=lambda label,**kw:
               document_upload('Другой нормативный документ.') if label=='Выберите документ' else None):
        app.run()
        assert not any(s.value=='Отчёт готов.' for s in app.success)
        assert not any(b.label=='Скачать отчёт' for b in app.get('download_button'))
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
        assert not any(b.label=='Скачать отчёт' for b in app.get('download_button'))
        app.run(); assert UIProvider.count==1
        monkeypatch.setattr(module,'assemble_report',assemble)
        monkeypatch.delenv('OPENAI_API_KEY')
        button(app,'Повторить сборку без API').click().run()
        assert not app.exception and not app.error and UIProvider.count==1
        assert any(s.value=='Отчёт готов.' for s in app.success)
        assert not app.session_state['opportunity_plans']
        assert app.session_state['process_result_bytes']==original
        assert any(b.label=='Скачать отчёт' for b in app.get('download_button'))
