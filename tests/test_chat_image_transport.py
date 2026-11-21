"""Exercise the client route and real request_for/HTTP serialization offline."""
import base64
from copy import deepcopy
import json

import httpx
import pytest

from test_public_settings import deployment


PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII='
REMOTE = 'https://images.invalid/synthetic.png?token=synthetic-url-secret'
TEXT = 'Describe this image.<attachment type="message_insert_extra_bundle">Synthetic Operit context</attachment>'


def configure_transport(client, protocol, operit, eyes=False):
    client.patch('/v1/settings', json={
        'models': [{'id': 'vision', 'label': 'Synthetic vision', 'model': 'synthetic-vision',
                    'base_url': 'https://provider.invalid/v1', 'protocol': protocol,
                    'api_key': 'synthetic-key'}],
        'assignments': {'chat': 'vision', 'image_transcription': 'vision'},
        'upstream': {'operit_enabled': operit},
        'features': {'image_eyes': eyes},
    }).raise_for_status()


def install_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    clients = []
    def client(**kwargs):
        value = original(**kwargs, transport=transport)
        clients.append(value)
        return value
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    return clients


def reply(protocol, stream, text='Synthetic answer'):
    if not stream:
        if protocol == 'anthropic':
            return httpx.Response(200, json={'id': 'synthetic', 'type': 'message',
                'role': 'assistant', 'content': [{'type': 'text', 'text': text}],
                'stop_reason': 'end_turn', 'usage': {}})
        return httpx.Response(200, json={'choices': [{'message': {
            'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]})
    events = ([{'type': 'message_start', 'message': {'id': 'synthetic', 'usage': {}}},
               {'type': 'content_block_delta', 'index': 0,
                'delta': {'type': 'text_delta', 'text': text}},
               {'type': 'message_stop'}] if protocol == 'anthropic' else
              [{'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': text}}]}])
    content = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
    if protocol == 'openai':
        content += 'data: [DONE]\n\n'
    return httpx.Response(200, text=content, headers={'content-type': 'text/event-stream'})


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('operit', [False, True])
@pytest.mark.parametrize('url', [PNG, REMOTE], ids=['data-url', 'remote-url'])
def test_standard_user_image_reaches_http_transport(deployment, monkeypatch, protocol, stream, operit, url):
    _, client = deployment
    configure_transport(client, protocol, operit)
    seen = []
    def handler(request):
        assert request.method == 'POST'
        assert str(request.url) == 'https://provider.invalid/v1/' + (
            'messages' if protocol == 'anthropic' else 'chat/completions')
        seen.append(json.loads(request.content))
        return reply(protocol, stream)
    install_transport(monkeypatch, handler)
    # No image fetch is allowed while Eyes is off, including remote URLs.
    def no_download(*args):
        raise AssertionError('Unexpected remote image fetch')
    monkeypatch.setattr('serein.extensions.pipeline_images._remote_image_bytes', no_download)
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': TEXT},
        {'type': 'image_url', 'image_url': {'url': url, 'detail': 'low'}}]}]
    original = deepcopy(messages)
    response = client.post('/v1/chat/completions', json={'messages': messages,
        'stream': stream, 'thinking': {'type': 'low'}, 'serein': {'memory': False}})
    assert response.status_code == 200, response.text
    assert len(seen) == 1 and messages == original
    payload = seen[0]
    assert payload['model'] == 'synthetic-vision' and payload['stream'] is stream
    assert payload['thinking'] == {'type': 'low'}  # No provider guess or auto-disable.
    if protocol == 'openai':
        assert payload['messages'] == messages
    else:
        expected = ({'type': 'base64', 'media_type': 'image/png', 'data': PNG.split(',', 1)[1]}
                    if url == PNG else {'type': 'url', 'url': url})
        assert payload['messages'] == [{'role': 'user', 'content': [
            {'type': 'text', 'text': TEXT}, {'type': 'image', 'source': expected}]}]


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('operit', [False, True])
@pytest.mark.parametrize('url', [PNG, REMOTE], ids=['data-url', 'remote-url'])
def test_eyes_transcribes_over_http_before_text_only_chat(deployment, monkeypatch, protocol, stream, operit, url):
    _, client = deployment
    configure_transport(client, protocol, operit, eyes=True)
    downloads = []
    def synthetic_download(value):
        assert value == REMOTE
        downloads.append(value)
        return base64.b64decode(PNG.split(',', 1)[1])
    monkeypatch.setattr('serein.extensions.pipeline_images._remote_image_bytes', synthetic_download)
    seen = []
    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            assert PNG.split(',', 1)[1] in json.dumps(payload)
            return reply(protocol, False, json.dumps({'image_transcriptions': [
                {'input_image': 1, 'text': 'Visible synthetic title', 'unreadable': False}]}))
        return reply(protocol, stream)
    install_transport(monkeypatch, handler)
    response = client.post('/v1/chat/completions', json={'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': TEXT}, {'type': 'image_url', 'image_url': {'url': url}}]}],
        'stream': stream, 'serein': {'memory': False}})
    assert response.status_code == 200, response.text
    assert len(seen) == 2
    encoded = json.dumps(seen[1])
    assert 'Visible synthetic title' in encoded
    assert 'image_url' not in encoded and PNG not in encoded and REMOTE not in encoded
    assert ('message_insert_extra_bundle' not in encoded) is operit
    assert downloads == ([REMOTE] if url == REMOTE else [])


@pytest.mark.parametrize('stream', [False, True])
def test_codex_async_transcription_queues_for_summary_without_removing_native_image(
        deployment, monkeypatch, stream):
    """Real user setup: main model sees the image; the summary pipeline transcribes later."""
    from serein.core.store import Store
    settings, client = deployment
    configure_transport(client, 'codex', operit=False, eyes=False)
    configured = client.patch('/v1/settings', json={
        'features': {'image_eyes': False, 'image_transcription_async': True}})
    assert configured.status_code == 200, configured.text

    background_calls = []
    async def defer_work(settings, queued):
        background_calls.append(queued['id'])
    monkeypatch.setattr('serein.api.chat.run_queued_image_turn', defer_work)

    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        assert str(request.url) == 'https://provider.invalid/v1/chat/completions'
        return reply('openai', stream)
    install_transport(monkeypatch, handler)

    messages = [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Describe the picture directly.'},
        {'type': 'image_url', 'image_url': {'url': PNG}}]}]
    response = client.post('/v1/chat/completions', json={
        'messages': messages, 'stream': stream,
        'serein': {'memory': False, 'window_id': 'synthetic-codex-async'}})
    assert response.status_code == 200, response.text
    assert len(seen) == 1
    assert seen[0]['messages'] == messages
    assert background_calls and background_calls[0].startswith('image:')
    with Store(settings.database, read_only=True) as store:
        original = store.conn.execute(
            "SELECT metadata_json FROM raw_events WHERE role='user'").fetchone()
        assert json.loads(original['metadata_json'])['attachments'][0]['url'] == PNG
        work = store.conn.execute(
            "SELECT value_json FROM background_state WHERE name LIKE 'work:image:%'").fetchone()
        assert work is not None
        assert json.loads(work['value_json'])['id'] in background_calls


@pytest.mark.parametrize('first_stream', [False, True])
def test_codex_gateway_forwards_client_tools_and_image_after_tool_roundtrip(
        deployment, monkeypatch, first_stream):
    """The actual Gateway HTTP adapter preserves frontend-owned MCP tool messages."""
    _, client = deployment
    configure_transport(client, 'codex', operit=False, eyes=False)
    seen = []
    tool = {'type': 'function', 'function': {'name': 'lookup',
        'description': 'Synthetic frontend-local lookup',
        'parameters': {'type': 'object', 'properties': {}}}}
    call = {'id': 'call-001', 'type': 'function', 'function': {
        'name': 'lookup', 'arguments': '{}'}}
    def handler(request):
        assert str(request.url) == 'https://provider.invalid/v1/chat/completions'
        body = json.loads(request.content)
        seen.append(body)
        assert body['tools'] == [tool]
        if len(seen) == 1:
            assert body['messages'][0]['content'][1]['image_url']['url'] == PNG
            if first_stream:
                chunks = [
                    {'choices': [{'index': 0, 'delta': {'role': 'assistant',
                        'tool_calls': [{**call, 'index': 0}]}, 'finish_reason': None}]},
                    {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]},
                ]
                wire = ''.join('data: ' + json.dumps(x) + '\n\n' for x in chunks)
                return httpx.Response(200, text=wire+'data: [DONE]\n\n',
                                      headers={'content-type': 'text/event-stream'})
            return httpx.Response(200, json={'choices': [{'index': 0,
                'message': {'role': 'assistant', 'content': None, 'tool_calls': [call]},
                'finish_reason': 'tool_calls'}]})
        assert body['messages'][-2]['role'] == 'assistant'
        assert body['messages'][-2]['tool_calls'][0]['id'] == 'call-001'
        assert body['messages'][-1] == {
            'role': 'tool', 'tool_call_id': 'call-001', 'content': 'Frontend result'}
        return reply('openai', False, 'Frontend result used')

    install_transport(monkeypatch, handler)
    user = {'role': 'user', 'content': [
        {'type': 'text', 'text': 'Use lookup with this image'},
        {'type': 'image_url', 'image_url': {'url': PNG}}]}
    first = client.post('/v1/chat/completions', json={
        'messages': [user], 'tools': [tool], 'stream': first_stream,
        'serein': {'memory': False, 'window_id': 'codex-client-tool-test'}})
    assert first.status_code == 200, first.text
    if first_stream:
        assert 'call-001' in first.text and '"finish_reason": "tool_calls"' in first.text
        assistant = {'role': 'assistant', 'content': None, 'tool_calls': [call]}
    else:
        assistant = first.json()['choices'][0]['message']
        assert first.json()['choices'][0]['finish_reason'] == 'tool_calls'
        assert assistant['tool_calls'][0]['id'] == 'call-001'
    second = client.post('/v1/chat/completions', json={
        'messages': [user, assistant, {
            'role': 'tool', 'tool_call_id': 'call-001', 'content': 'Frontend result'}],
        'tools': [tool], 'serein': {'memory': False, 'window_id': 'codex-client-tool-test'}})
    assert second.status_code == 200, second.text
    assert second.json()['choices'][0]['message']['content'] == 'Frontend result used'
    assert len(seen) == 2
