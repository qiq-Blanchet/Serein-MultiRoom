import asyncio
import httpx
import pytest
from test_public_settings import deployment
from serein.deployment import read_settings, save_settings
from serein.model_discovery import discover


def prepare(deployment):
    settings, api = deployment
    save_settings(settings.database, {'upstreams':[{'id':'provider','name':'Provider','base_url':'https://provider.example/v1',
        'api_key':'server-only-key','models':[{'id':'existing','upstream_model':'existing'}]}]})
    return settings, api, {'upstream_id':'provider','base_url':'https://provider.example/v1','api_key':'','clear_key':False,'protocol':'openai'}


def run(settings, payload, handler):
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False) as client:
            return await discover(settings,payload,client=client)
    return asyncio.run(invoke())


def test_saved_secret_normalization_and_no_writes(deployment):
    settings, _, p = prepare(deployment); before=read_settings(settings.database)
    def handle(req):
        assert str(req.url)=='https://provider.example/v1/models'
        assert req.headers['authorization']=='Bearer server-only-key'
        return httpx.Response(200,json={'data':[{'id':v} for v in ['z','a','z','bad\nname','server-only-key',2]]})
    assert run(settings,p,handle)=={'models':['a','z'],'truncated':False}
    assert read_settings(settings.database)==before


def test_legacy_group_discovery_uses_its_saved_credential(deployment):
    settings,_=deployment
    save_settings(settings.database,{'models':[{'id':'old','label':'Chat','model':'chat','protocol':'openai',
        'base_url':'https://provider.example/v1','api_key':'legacy-key'}]})
    group=read_settings(settings.database,public=True)['upstreams'][0]
    request={'upstream_id':group['id'],'base_url':group['base_url'],'protocol':'openai','api_key':''}
    def handle(req):
        assert req.headers['authorization']=='Bearer legacy-key'
        return httpx.Response(200,json={'data':[{'id':'chat'}]})
    assert run(settings,request,handle)['models']==['chat']


def test_changed_address_draft_key_clear_and_anthropic(deployment):
    settings, _, p = prepare(deployment); p['base_url']='https://other.example/v1'
    with pytest.raises(ValueError,match='地址已改变'):
        run(settings,p,lambda _:pytest.fail('must not contact changed provider'))
    p.update(api_key='draft-key',protocol='anthropic')
    def handle(req):
        assert req.headers['x-api-key']=='draft-key'
        assert req.headers['anthropic-version']=='2023-06-01'
        assert 'authorization' not in req.headers
        return httpx.Response(200,json={'data':[]})
    run(settings,p,handle)
    p['clear_key']=True
    def cleared(req):
        assert 'authorization' not in req.headers and 'x-api-key' not in req.headers
        return httpx.Response(200,json={'data':[]})
    run(settings,p,cleared)


@pytest.mark.parametrize('response',[httpx.Response(401,text='server-only-key'),httpx.Response(404,text='server-only-key'),
    httpx.Response(302,headers={'Location':'https://other.example'}),httpx.Response(200,text='server-only-key'),
    httpx.Response(200,json={'error':'server-only-key'}),httpx.Response(200,content=b'x'*2_000_001)])
def test_error_bodies_and_redirects_are_not_relayed(deployment,response):
    settings, _, p=prepare(deployment); calls=[]
    def handle(req):calls.append(req);return response
    with pytest.raises(ValueError) as error:run(settings,p,handle)
    assert 'server-only-key' not in str(error.value) and len(calls)==1


def test_endpoint_auth_validation_and_dispatch(deployment,monkeypatch):
    settings, api, p=prepare(deployment)
    async def fake(s,body):
        assert s==settings and body==p
        return {'models':['available'],'truncated':False}
    monkeypatch.setattr('serein.model_discovery.discover',fake)
    assert api.post('/v1/settings/models/discover',json=p).json()['models']==['available']
    assert api.post('/v1/settings/models/discover',json=p,headers={'Authorization':'Bearer wrong'}).status_code==401
    p['api_key']='private-string'*400
    result=api.post('/v1/settings/models/discover',json=p)
    assert result.status_code==422 and 'private-string' not in result.text


def test_environment_secret_and_catalog_limit(deployment,monkeypatch):
    settings, _, p=prepare(deployment)
    save_settings(settings.database,{'upstreams':[{'id':'provider','name':'Provider','api_key':'','api_key_env':'TEST_DISCOVERY_KEY'}]})
    monkeypatch.setenv('TEST_DISCOVERY_KEY','env-secret')
    def handle(req):
        assert req.headers['authorization']=='Bearer env-secret'
        return httpx.Response(200,json={'data':[{'id':str(n)} for n in range(2001)]})
    result=run(settings,p,handle)
    assert len(result['models'])==2000 and result['truncated']
    assert run(settings,p,lambda req:httpx.Response(200,json={'data':[{'id':'first-page'}],'has_more':True}))['truncated']


def test_owned_codex_discovery_bypasses_environment_proxy(deployment, monkeypatch):
    settings, _, request = prepare(deployment)
    request['protocol'] = 'codex'
    original = httpx.AsyncClient
    options = []
    def client(**kwargs):
        options.append(kwargs.copy())
        return original(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={'data':[{'id':'codex-model'}]})), **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    assert asyncio.run(discover(settings, request))['models'] == ['codex-model']
    assert len(options) == 1
    assert options[0]['timeout'] == 15
    assert options[0]['follow_redirects'] is False
    assert options[0]['trust_env'] is False
