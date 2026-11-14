import json

import httpx
import pytest

from govai.llm import INSTRUCTIONS as EXTRACTION_PROMPT, OpenAINormalizer, OpenAIProvider
from govai.normalization import INSTRUCTIONS, PROMPT_VERSION, normalize_analysis, NormalizationError
from test_llm import provider, response
from test_normalization import simple_case


def adapter(handler):
    value = provider(handler)
    value.__class__ = OpenAINormalizer
    return value


def test_normalization_uses_own_structured_schema_prompt_usage_and_no_retries():
    calls = []
    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body['instructions'] == INSTRUCTIONS and body['instructions'] != EXTRACTION_PROMPT
        assert body['text']['format']['name'] == 'process_normalization_plan'
        assert body['text']['format']['strict'] is True
        assert body['store'] is False and body['truncation'] == 'disabled'
        assert body['max_output_tokens'] == 32000
        assert 'source_action_ids' in json.dumps(body['text']['format']['schema'])
        return httpx.Response(200, json=response(json.dumps({'processes': [], 'actions': [], 'explanation': 'None'}),
            usage={'input_tokens': 12, 'output_tokens': 5, 'total_tokens': 17}))
    value = adapter(handler)
    try:
        plan, usage = value.normalize({'actions': []}, max_output_tokens=32000)
        assert usage == {'input_tokens': 12, 'output_tokens': 5, 'total_tokens': 17}
        assert plan.actions == [] and len(calls) == 1
        assert value.prompt_version == PROMPT_VERSION
        assert OpenAIProvider.prompt_version == '1.0'  # legacy checkpoint identity is unchanged
    finally:
        value.close()


@pytest.mark.parametrize('kind', ['403', '429', 'refusal', 'incomplete', 'invalid_json'])
def test_second_stage_errors_refusals_and_truncation_are_not_success(kind):
    count = []
    def handler(request):
        count.append(request)
        if kind.isdigit():
            return httpx.Response(int(kind), json={'error': {'message': 'Sensitive', 'type': 'test'}})
        result = response(json.dumps({'processes': [], 'actions': [], 'explanation': 'x'}),
                          usage={'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5})
        if kind == 'refusal':
            result['output'][0]['content'] = [{'type': 'refusal', 'refusal': 'No'}]
        elif kind == 'incomplete':
            result['status'] = 'incomplete'
        else:
            result['output'][0]['content'][0]['text'] = '{'
        return httpx.Response(200, json=result)
    value = adapter(handler)
    try:
        _, _, a = simple_case()
        with pytest.raises(NormalizationError) as caught:
            normalize_analysis(a, value)
        assert len(count) == 1
        assert caught.value.metrics['requests'] == 1
        assert 'Sensitive' not in str(caught.value)
        if not kind.isdigit():
            assert caught.value.metrics['usage']['total_tokens'] == 5
    finally:
        value.close()
