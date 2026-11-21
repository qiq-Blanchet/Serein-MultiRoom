from pathlib import Path

from fastapi.testclient import TestClient

from serein.codex_runner import RunnerConfig, create_runner_app, dynamic_tools, prepare_messages


class FakeCodex:
    closed = False

    async def models(self):
        return ['synthetic-codex']

    async def events(self, payload):
        assert payload['model'] == 'synthetic-codex'
        yield {'type':'delta', 'text':'Synthetic '}
        yield {'type':'delta', 'text':'answer'}
        yield {'type':'completed', 'text':'Synthetic answer'}

    async def close(self):
        self.closed = True


class FakeToolCodex(FakeCodex):
    async def events(self, payload):
        assert payload['tools'][0]['function']['name'] == 'lookup'
        yield {'type':'tool_call', 'id':'call-synthetic', 'name':'lookup',
               'arguments':'{"query":"synthetic"}'}


def client(tmp_path):
    config = RunnerConfig('codex', Path(tmp_path), 'runner-secret')
    return TestClient(create_runner_app(config, FakeCodex()))


def test_prepare_messages_keeps_history_and_current_user_separate():
    history, prompt = prepare_messages({'messages':[
        {'role':'system','content':'Synthetic persona'},
        {'role':'user','content':'Earlier question'},
        {'role':'assistant','content':'Earlier answer'},
        {'role':'user','content':'Current question'},
    ]})
    assert prompt == 'Current question'
    assert [item['role'] for item in history] == ['developer','developer','user','assistant']
    assert history[-1]['content'][0] == {'type':'output_text','text':'Earlier answer'}


def test_runner_exposes_authenticated_models_and_chat(tmp_path):
    with client(tmp_path) as api:
        assert api.get('/v1/models').status_code == 401
        headers = {'Authorization':'Bearer runner-secret'}
        models = api.get('/v1/models', headers=headers)
        assert models.json()['data'][0]['id'] == 'synthetic-codex'
        response = api.post('/v1/chat/completions', headers=headers, json={
            'model':'synthetic-codex','messages':[{'role':'user','content':'Hello'}]})
        response.raise_for_status()
        assert response.json()['choices'][0]['message']['content'] == 'Synthetic answer'


def test_runner_stream_is_chat_completions_compatible(tmp_path):
    with client(tmp_path) as api:
        response = api.post('/v1/chat/completions', headers={'Authorization':'Bearer runner-secret'}, json={
            'model':'synthetic-codex','messages':[{'role':'user','content':'Hello'}], 'stream':True})
        response.raise_for_status()
        assert 'Synthetic ' in response.text and 'answer' in response.text
        assert '"finish_reason":"stop"' in response.text
        assert response.text.rstrip().endswith('data: [DONE]')


def test_runner_maps_client_tools_and_continuation_history():
    payload = {'tools':[{'type':'function','function':{'name':'lookup','description':'Lookup',
        'parameters':{'type':'object','properties':{'query':{'type':'string'}}}}}]}
    assert dynamic_tools(payload) == [{'type':'function','name':'lookup','description':'Lookup',
        'inputSchema':payload['tools'][0]['function']['parameters']}]
    history, prompt = prepare_messages({**payload, 'messages':[
        {'role':'user','content':'Use the tool'},
        {'role':'assistant','content':None,'tool_calls':[{'id':'call-1','type':'function',
            'function':{'name':'lookup','arguments':'{"query":"synthetic"}'}}]},
        {'role':'tool','tool_call_id':'call-1','content':'Synthetic result'},
    ]})
    assert any(item.get('type') == 'function_call' and item['call_id'] == 'call-1' for item in history)
    assert any(item.get('type') == 'function_call_output' and item['output'] == 'Synthetic result' for item in history)
    assert prompt.startswith('Continue the existing user turn')


def test_runner_returns_client_tool_calls_in_chat_completions_shapes(tmp_path):
    config = RunnerConfig('codex', Path(tmp_path), 'runner-secret')
    headers = {'Authorization':'Bearer runner-secret'}
    body = {'model':'synthetic-codex', 'messages':[{'role':'user','content':'Use lookup'}],
        'tools':[{'type':'function','function':{'name':'lookup','parameters':{'type':'object'}}}]}
    with TestClient(create_runner_app(config, FakeToolCodex())) as api:
        response = api.post('/v1/chat/completions', headers=headers, json=body)
        response.raise_for_status()
        choice = response.json()['choices'][0]
        assert choice['finish_reason'] == 'tool_calls'
        assert choice['message']['tool_calls'][0]['function']['name'] == 'lookup'
        streamed = api.post('/v1/chat/completions', headers=headers, json={**body, 'stream':True})
        streamed.raise_for_status()
        assert '"finish_reason":"tool_calls"' in streamed.text
        assert 'call-synthetic' in streamed.text
