import json
import httpx
import pytest

from govai.llm import OpenAIProvider, OpenAINormalizer, OpenAIOpportunityProvider
from govai.opportunities import OpportunityError, generate_opportunities, PROMPT_VERSION
from test_llm import provider as transport_provider, response
from opportunity_helpers import simple_map, proposal
from test_opportunities import plan


def adapter(handler):
    p=transport_provider(handler)
    p.__class__=OpenAIOpportunityProvider
    return p


def test_opportunity_schema_uses_shared_transport_and_its_own_prompt():
    _,_,a,n=simple_map(); ready=plan(proposal(n)); seen=[]
    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json=response(ready.model_dump_json(),usage={'input_tokens':11,'output_tokens':7,'total_tokens':18}))
    p=adapter(handle)
    try:
        r=generate_opportunities(n,a,p)
        assert len(seen)==1 and r['metrics']['usage']['total_tokens']==18
        body=seen[0]
        assert body['text']['format']['name']=='ai_opportunities' and body['text']['format']['strict']
        assert body['store'] is False and body['truncation']=='disabled' and body['max_output_tokens']==20000
        assert p.prompt_version==PROMPT_VERSION
        assert OpenAIProvider.prompt_version=='1.0' and OpenAINormalizer.prompt_version=='normalization-1.0'
        assert 'исходный акт' in body['instructions']
    finally: p.close()


@pytest.mark.parametrize('fault',['403','429','refusal','incomplete','bad_json','timeout'])
def test_no_retry_no_partial_success_and_safe_errors(fault):
    requests=[]
    def handle(request):
        requests.append(request)
        if fault=='timeout': raise httpx.ReadTimeout('private transport context',request=request)
        if fault.isdigit(): return httpx.Response(int(fault),json={'error':{'message':'private key text','type':'test'}})
        r=response('{',usage={'input_tokens':3,'output_tokens':2,'total_tokens':5})
        if fault=='refusal': r['output'][0]['content']=[{'type':'refusal','refusal':'private reason'}]
        if fault=='incomplete': r['status']='incomplete'
        return httpx.Response(200,json=r)
    p=adapter(handle); _,_,a,n=simple_map()
    try:
        with pytest.raises(OpportunityError) as caught: generate_opportunities(n,a,p)
        assert len(requests)==1 and caught.value.metrics['requests']==1
        assert 'private' not in str(caught.value)
        if fault in ('refusal','incomplete','bad_json'): assert caught.value.metrics['usage']['total_tokens']==5
    finally: p.close()
