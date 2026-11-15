import json
import httpx
import pytest

from govai.llm import OpenAIProvider, OpenAINormalizer, OpenAIOpportunityProvider
from govai.opportunities import OpportunityError, generate_opportunities, prepare_request, PROMPT_VERSION
from test_llm import provider as transport_provider, response
from opportunity_helpers import simple_map, proposal
from test_opportunities import plan, presented_plan


def adapter(handler):
    p=transport_provider(handler)
    p.__class__=OpenAIOpportunityProvider
    return p


def test_opportunity_schema_uses_shared_transport_and_its_own_prompt():
    _,_,a,n=simple_map(); ready=presented_plan(proposal(n)); seen=[]
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
        assert p.last_call_elapsed_seconds is not None and p.last_call_elapsed_seconds>=0
        assert OpenAIProvider.prompt_version=='1.0' and OpenAINormalizer.prompt_version=='normalization-1.0'
        assert 'исходный акт' in body['instructions']
        assert r['schema_version']=='2.3' and r['plan']['executive_summary']==ready.executive_summary
        schema=body['text']['format']['schema']
        assert 'executive_summary' in schema['required']
        assert 'presentation' in schema['$defs']['PresentedProposal']['required']
        for definition in [schema,*schema['$defs'].values()]:
            assert definition['additionalProperties'] is False
            assert set(definition['required'])==set(definition['properties'])
        payload,measured=prepare_request(n,a)
        assert body['input']==json.dumps(payload,ensure_ascii=False,separators=(',',':'))
        # The guard uses the exact compact input string. Schema whitespace is
        # conservatively counted as before; the budget is not relaxed for display.
        assert measured['input_bytes']==len((body['input']+body['instructions']+json.dumps(schema)).encode())
    finally: p.close()


def test_current_strict_schema_has_required_nullable_fields_and_resolvable_references():
    from govai.opportunities import OpportunityGenerationPlan
    schema=OpportunityGenerationPlan.model_json_schema()
    assert schema['type']=='object' and 'anyOf' not in schema
    def visit(value):
        if isinstance(value,dict):
            assert 'default' not in value
            if value.get('type')=='object':
                assert value['additionalProperties'] is False
                assert set(value['required'])==set(value['properties'])
            if '$ref' in value:
                assert value['$ref'].startswith('#/$defs/')
                assert value['$ref'].split('/')[-1] in schema['$defs']
            for child in value.values():visit(child)
        elif isinstance(value,list):
            for child in value:visit(child)
    visit(schema)
    for definition,field in [('Measurement','secondary'),('Measurement','quality_protocol'),
                             ('PresentedProposal','incremental_value'),('TimeParameter','value')]:
        parent=schema['$defs'][definition]
        assert field in parent['required']
        assert {'type':'null'} in parent['properties'][field]['anyOf']


def test_current_adapter_does_not_accept_a_legacy_response_missing_compact_fields():
    _,_,a,n=simple_map(); old=plan(proposal(n)); calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(200,json=response(old.model_dump_json()))
    p=adapter(handle); saved=[]
    try:
        with pytest.raises(OpportunityError,match='схеме'):
            generate_opportunities(n,a,p,on_plan=saved.append)
        assert len(calls)==1 and saved==[]
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
        assert p.last_call_elapsed_seconds is not None and p.last_call_elapsed_seconds>=0
        assert 'private' not in str(caught.value)
        if fault in ('refusal','incomplete','bad_json'): assert caught.value.metrics['usage']['total_tokens']==5
    finally: p.close()
