"""OpenAI SDK runs against httpx.MockTransport only; no API/network calls."""

import json

import httpx
import pytest
from openai import OpenAI

from govai.llm import OpenAIProvider, configuration
from govai.processes import Candidates, ProcessError


def response(body=None, **changes):
    result = {'id': 'resp_test', 'object': 'response', 'created_at': 1, 'model': 'test-model',
              'status': 'completed', 'output': [{'type': 'message', 'id': 'msg_test',
                'status': 'completed', 'role': 'assistant', 'content': [
                    {'type': 'output_text', 'annotations': [], 'text': body or json.dumps(
                        {'procedures': [], 'explanation': 'В тексте только определения.'})}]}]}
    result.update(changes)
    return result


def provider(handler):
    adapter = OpenAIProvider('unit-test-placeholder', 'test-model')
    adapter.close()
    adapter.client = OpenAI(api_key='unit-test-placeholder', max_retries=0,
                            http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return adapter


def test_strict_schema_no_storage_no_truncation_and_real_sdk_serialization():
    def handler(request):
        payload = json.loads(request.content)
        assert request.url.path == '/v1/responses'
        assert payload['model'] == 'test-model'
        assert payload['store'] is False and payload['truncation'] == 'disabled'
        schema = payload['text']['format']
        assert schema['type'] == 'json_schema' and schema['strict'] is True
        for definition in [schema['schema'], *schema['schema']['$defs'].values()]:
            assert definition['additionalProperties'] is False
            assert set(definition['required']) == set(definition['properties'])
        assert 'недоверенные' in payload['instructions']
        assert json.loads(payload['input']) == {'core': 'Кириллица'}
        return httpx.Response(200, json=response())
    adapter = provider(handler)
    try:
        result = adapter.extract({'core': 'Кириллица'})
        assert isinstance(result, Candidates) and result.procedures == []
    finally:
        adapter.close()


@pytest.mark.parametrize('status', [400, 401, 403, 404, 429, 500])
def test_api_errors_are_safe_and_clear(status):
    adapter = provider(lambda request: httpx.Response(status, json={
        'error': {'message': 'SENSITIVE_DOCUMENT_AND_KEY', 'type': 'test', 'code': 'test'}}))
    try:
        with pytest.raises(ProcessError) as error:
            adapter.extract({})
        assert 'SENSITIVE' not in str(error.value) and 'OpenAI' in str(error.value)
    finally:
        adapter.close()


@pytest.mark.parametrize('body', [
    response('{'), response('{}'), response(output=[]), response(output=None),
    response(output=[{'type': 'message', 'id': 'msg_invalid', 'role': 'assistant',
                      'status': 'completed', 'content': None}]),
    response(status='incomplete', incomplete_details={'reason': 'max_output_tokens'}),
    response(output=[{'type': 'message', 'id': 'msg_refuse', 'role': 'assistant',
                      'status': 'completed', 'content': [{'type': 'refusal', 'refusal': 'No'}]}]),
])
def test_refusal_incomplete_invalid_and_missing_output(body):
    adapter = provider(lambda request: httpx.Response(200, json=body))
    try:
        with pytest.raises(ProcessError):
            adapter.extract({})
    finally:
        adapter.close()


@pytest.mark.parametrize('error', [httpx.ReadTimeout, httpx.ConnectError])
def test_network_errors(error):
    def handler(request):
        raise error('sensitive transport detail', request=request)
    adapter = provider(handler)
    try:
        with pytest.raises(ProcessError) as caught:
            adapter.extract({})
        assert 'sensitive' not in str(caught.value)
    finally:
        adapter.close()


def test_missing_key_model_and_secret_configuration(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    with pytest.raises(ProcessError, match='OPENAI_API_KEY'):
        configuration()
    with pytest.raises(ProcessError, match='OPENAI_MODEL'):
        configuration({'OPENAI_API_KEY': 'unit-test-placeholder'})
    monkeypatch.setenv('OPENAI_MODEL', 'configured-model')
    assert configuration({'OPENAI_API_KEY': 'unit-test-placeholder'}) == ('unit-test-placeholder', 'configured-model')
