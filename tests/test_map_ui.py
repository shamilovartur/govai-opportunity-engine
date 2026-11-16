import json

from pathlib import Path

from types import SimpleNamespace

from unittest.mock import Mock, patch

import pytest

from streamlit.proto.Common_pb2 import FileURLs

from streamlit.runtime.uploaded_file_manager import UploadedFile, UploadedFileRec

from streamlit.testing.v1 import AppTest

from govai.normalization import normalize_analysis

from govai.analysis_io import verify_analysis

from govai.processes import ProcessError

from govai.normalization import markdown_report

from test_normalization import Normalizer, node, plan_for, simple_case

APP = Path(__file__).resolve().parents[1] / 'app.py'

def upload(name, data):
    return UploadedFile(UploadedFileRec(name, name, 'application/octet-stream', data), FileURLs())

def button(app, label):
    return next(b for b in app.button if b.label == label)

def setup_inputs(normalized=None):
    doc, raw, analysis = simple_case(('Секретарь регистрирует заявление.', 'Исполнитель направляет ответ.'))
    raw['generated_at'] = '2026-11-14T00:00:00+00:00'
    ids = list(analysis.actions)
    plan = plan_for(analysis, [node(analysis, [ids[0]], key='register', process='receipt', title='Регистрация'),
                              node(analysis, [ids[1]], key='reply', process='dispatch', title='Направление ответа')],
                    titles={'receipt': 'Приём обращения', 'dispatch': 'Выдача результата'})
    data = json.dumps(raw, ensure_ascii=False, indent=4).encode()  # preserve exact formatting on download
    analysis = verify_analysis(data, doc)
    def uploader(label, **kw):
        if label == 'Выберите документ': return upload('test.txt', doc['text'].encode())
        if label == 'Готовый processes.json': return upload('processes.json', data)
        if label == 'Нормализованный normalized_processes.json' and normalized:
            return upload('normalized_processes.json', normalized)
        return None
    return analysis, plan, data, uploader

class UIProvider(Normalizer):
    prepared_plan = None
    calls_total = 0
    def __init__(self, *args):
        super().__init__(self.prepared_plan)
    def normalize(self, payload, **kwargs):
        type(self).calls_total += 1
        return super().normalize(payload, **kwargs)
    def close(self):
        pass

class AutomaticNormalizer:
    """A deterministic mock response for UI sequencing, never a live model."""
    name='mock'
    calls_total=0
    def __init__(self, key, model): self.model=model
    def close(self): pass
    def normalize(self, payload, **kwargs):
        from types import SimpleNamespace
        type(self).calls_total += 1
        a=SimpleNamespace(actions={x['id']:x for x in payload['actions']})
        return plan_for(a,[node(a,[sid],key=f'op-{i}',title=x['description'])
                           for i,(sid,x) in enumerate(a.actions.items())],titles={'p1':'Работа с обращением'}),None

def test_request_timing_never_invents_unknown_local_processing():
    from govai.map_ui import request_timing
    for missing in (None, float('nan'), float('inf'), -1.0, 3.0, True):
        assert request_timing(2.0, missing) == {'api_wait_seconds': None, 'local_seconds': None}
    assert request_timing(2.0, 1.5) == {'api_wait_seconds': 1.5, 'local_seconds': 0.5}

@pytest.mark.parametrize('failed',[False,True])
def test_normalization_retains_source_observes_time_and_blocks_reentry(monkeypatch,failed):
    import streamlit as st
    from govai.map_ui import normalize_processes
    from itertools import count
    analysis,plan,original,_=setup_inputs()
    state={};clock=count(1);timing={}
    class Checked(UIProvider):
        prepared_plan=plan
        calls_total=0
        def normalize(self,payload,**kwargs):
            self.last_call_elapsed_seconds=0.25
            with pytest.raises(ProcessError,match='уже выполняются'):normalize_processes(analysis)
            if failed:raise ProcessError('Неполный ответ нормализатора.')
            return super().normalize(payload,**kwargs)
    monkeypatch.setenv('OPENAI_MODEL','test-model')
    with patch.object(st,'session_state',state),patch('govai.map_ui.configuration',return_value=('test','test-model')), \
         patch('govai.map_ui.OpenAINormalizer',Checked),patch('govai.map_ui.time',SimpleNamespace(perf_counter=lambda:next(clock))):
        if failed:
            with pytest.raises(ProcessError):normalize_processes(analysis,timing=timing)
        else:
            result=normalize_processes(analysis,timing=timing)
            from govai.normalization import restore_normalized
            assert restore_normalized(json.dumps(result).encode(),analysis,'test-model','mock')==result
        assert not state['normalization_in_flight']
        assert timing['normalization_api_wait_seconds']==0.25
        assert timing['normalization_local_seconds']==timing['normalization_seconds']-0.25
        assert analysis.original_bytes==original


def test_saved_analysis_and_map_restore_without_provider():
    from govai.normalization import restore_normalized
    a,np,original,_=setup_inputs()
    saved=normalize_analysis(a,Normalizer(np))
    with patch('govai.map_ui.OpenAINormalizer',side_effect=AssertionError('No paid calls')):
        from govai.extraction import extract_document
        restored=verify_analysis(original,extract_document('test.txt',setup_inputs()[3]('Выберите документ').getvalue()))
        assert restored.actions==a.actions
        assert restore_normalized(json.dumps(saved).encode(),restored,'test-model','mock')==saved
