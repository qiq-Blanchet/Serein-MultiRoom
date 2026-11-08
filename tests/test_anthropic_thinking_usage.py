"""Offline regression tests for Anthropic thinking and cache-aware usage."""
import json
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from serein.api.http import create_app
from serein.bootstrap import initialize
from serein.chat_context import ClientContext
from serein.config import Settings
from serein.model_runtime import (
    AnthropicStream, complete, normalize_anthropic_usage,
    normalize_response, relax_plan, request_for,
)


def model():
    return {
        'protocol': 'anthropic',
        'base_url': 'https://anthropic.example/v1',
        'model': 'claude-sonnet',
        'api_key': 'synthetic-key',
    }


def user_turn(**extra):
    return {'messages': [{'role': 'user', 'content': 'hello'}], **extra}


def native_reply(usage=None):
    return {
        'id': 'synthetic-msg',
        'content': [
            {'type': 'thinking', 'thinking': 'Reasoned.', 'signature': 'synthetic-sig'},
            {'type': 'text', 'text': 'Answer.'},
        ],
        'stop_reason': 'end_turn',
        'usage': usage or {
            'input_tokens': 10,
            'cache_creation_input_tokens': 20,
            'cache_read_input_tokens': 30,
            'output_tokens': 5,
        },
    }


def sse_events():
    return [
        {'type': 'message_start', 'message': {
            'id': 'synthetic-msg', 'usage': {
                'input_tokens': 10,
                'cache_creation_input_tokens': 20,
                'cache_read_input_tokens': 30,
                'output_tokens': 1,
            }}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'Reasoned.'}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'synthetic-sig'}},
        {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': 'Answer.'}},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 5}},
        {'type': 'message_stop'},
    ]


def test_auto_thinking_budget_and_explicit_disable():
    _, _, body = request_for(model(), user_turn(temperature=0.3, top_p=0.7))
    assert body['max_tokens'] == 65536
    assert body['thinking'] == {'type': 'adaptive', 'display': 'summarized'}
    assert 'temperature' not in body and 'top_p' not in body

    _, _, small = request_for(model(), user_turn(max_tokens=1500))
    assert small['thinking'] == {'type': 'adaptive', 'display': 'summarized'}

    _, _, tiny = request_for(model(), user_turn(max_tokens=1024))
    assert tiny['thinking'] == {'type': 'adaptive', 'display': 'summarized'}

    _, _, disabled = request_for(model(), user_turn(thinking={'type': 'disabled'}))
    assert disabled['thinking'] == {'type': 'disabled'}

    _, _, explicit = request_for(model(), user_turn(
        thinking={'type': 'adaptive', 'display': 'summarized'}, max_tokens=4096, temperature=0.5))
    assert explicit['thinking'] == {'type': 'adaptive', 'display': 'summarized'}
    assert 'temperature' not in explicit


def test_fallback_never_changes_explicit_thinking_or_increases_budget():
    assert relax_plan(model(), user_turn(thinking={'type': 'low'})) == []
    plan = relax_plan(model(), user_turn())
    assert len(plan) == 4
    bodies = [request_for(model(), item)[2] for item in plan]
    assert [b['max_tokens'] for b in bodies] == [65536, 16384, 8192, 4096]
    assert all('thinking' not in body for body in bodies)
    # Caller-specified limits cannot be reduced by automatic fallback.
    assert [request_for(model(), p)[2]['max_tokens']
            for p in relax_plan(model(), user_turn(max_tokens=2048))] == [2048]
    assert [request_for(model(), p)[2]['max_tokens']
            for p in relax_plan(model(), user_turn(max_tokens=1024))] == [1024]


def test_nonstream_reasoning_content_and_usage():
    result = normalize_response(model(), native_reply())
    message = result['choices'][0]['message']
    assert message['reasoning_content'] == 'Reasoned.'
    assert message['reasoning_details'][0]['signature'] == 'synthetic-sig'
    assert message['content'] == 'Answer.'
    assert result['usage'] == {
        'prompt_tokens': 60, 'completion_tokens': 5, 'total_tokens': 65,
        'prompt_tokens_details': {'cached_tokens': 30},
        'cache_read_input_tokens': 30, 'cache_creation_input_tokens': 20,
    }
    assert normalize_anthropic_usage({'input_tokens': 7, 'output_tokens': 9})['total_tokens'] == 16
    assert normalize_anthropic_usage({'input_tokens': None})['total_tokens'] == 0


def test_stream_reasoning_and_merged_usage_are_not_double_counted():
    converter = AnthropicStream('claude-sonnet')
    context = ClientContext()
    state = {'message': {'role': 'assistant', 'content': '', 'reasoning_content': ''},
             'tool_calls_by_index': {}}
    chunks = [converter.convert(event) for event in sse_events()]
    assert chunks[-1] is None
    assert all('usage' not in chunk for chunk in chunks[:-2] if chunk)
    for chunk in chunks:
        if chunk and chunk.get('choices'):
            context._merge_stream_message_delta(state, chunk['choices'][0]['delta'])
    message = context._build_stream_assistant_message(state)
    assert message['reasoning_content'] == 'Reasoned.'
    assert message['reasoning_details'][0]['signature'] == 'synthetic-sig'
    assert message['content'] == 'Answer.'
    assert converter.final_usage == {
        'prompt_tokens': 60, 'completion_tokens': 5, 'total_tokens': 65,
        'prompt_tokens_details': {'cached_tokens': 30},
        'cache_read_input_tokens': 30, 'cache_creation_input_tokens': 20,
    }


def test_nonstream_auto_thinking_retries_only_http_400(monkeypatch):
    seen = []
    original = httpx.AsyncClient

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        if 'thinking' in body:
            return httpx.Response(400, json={'error': {'message': 'private-synthetic-rejection'}})
        return httpx.Response(200, json=native_reply())

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    result = asyncio.run(complete(model(), user_turn()))
    assert result['choices'][0]['message']['reasoning_content'] == 'Reasoned.'
    assert len(seen) == 2
    assert 'thinking' in seen[0] and 'thinking' not in seen[1]


@pytest.fixture
def deployment(tmp_path):
    settings = Settings(tmp_path / 'memory.db', index=tmp_path / 'index.db', writable=True)
    initialize(settings)
    client = TestClient(create_app(settings, token='synthetic', live=True),
                        headers={'Authorization': 'Bearer synthetic'})
    saved = client.patch('/v1/settings', json={'upstreams': [{
        'name': 'provider', 'base_url': 'https://anthropic.example/v1',
        'api_key': 'synthetic-key', 'protocol': 'anthropic',
        'models': ['claude-sonnet'],
    }]})
    assert saved.status_code == 200, saved.text
    return client


def test_http_stream_emits_single_final_openai_usage_chunk(deployment, monkeypatch):
    original = httpx.AsyncClient
    sent = []
    def handle(request):
        sent.append(json.loads(request.content))
        event_text = ''.join('data: ' + json.dumps(event) + '\n\n' for event in sse_events())
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=event_text)

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    result = deployment.post('/v1/chat/completions', json={
        'model': 'provider/claude-sonnet',
        'messages': [{'role': 'user', 'content': 'hello'}],
        'stream': True, 'stream_options': {'include_usage': True},
        'serein': {'memory': False},
    })
    assert result.status_code == 200, result.text
    payloads = [json.loads(line.removeprefix('data: ')) for line in result.text.splitlines()
                if line.startswith('data: {')]
    assert '[DONE]' in result.text
    assert sent and sent[0]['thinking'] == {'type': 'adaptive', 'display': 'summarized'}
    assert any(row.get('choices') and row['choices'][0]['delta'].get('reasoning_content') == 'Reasoned.'
               for row in payloads)
    receipts = [row for row in payloads if 'usage' in row]
    assert len(receipts) == 1
    assert receipts[0]['choices'] == []
    assert receipts[0]['usage']['prompt_tokens'] == 60
    assert receipts[0]['usage']['prompt_tokens_details']['cached_tokens'] == 30


def test_explicit_thinking_rejection_is_not_silently_retried(deployment, monkeypatch):
    original = httpx.AsyncClient
    seen = []
    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(400, json={'error': {'message': 'secret-rejection'}})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    result = deployment.post('/v1/chat/completions', json={
        'model': 'provider/claude-sonnet',
        'messages': [{'role': 'user', 'content': 'hello'}],
        'thinking': {'type': 'low'},
        'stream': True, 'serein': {'memory': False},
    })
    assert result.status_code == 400
    assert len(seen) == 1
    assert 'secret-rejection' not in result.text


def test_stream_auto_thinking_400_fallback_and_usage_without_stream_options(deployment, monkeypatch):
    """Streaming retries must close rejected 400 responses and retain usage."""
    original = httpx.AsyncClient
    sent = []
    rejected = []
    def handle(request):
        body = json.loads(request.content)
        sent.append(body)
        if 'thinking' in body:
            response = httpx.Response(400, json={'error': {'message': 'private-synthetic-rejection'}})
            rejected.append(response)
            return response
        event_text = ''.join('data: ' + json.dumps(event) + '\n\n' for event in sse_events())
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=event_text)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    response = deployment.post('/v1/chat/completions', json={
        'model': 'provider/claude-sonnet',
        'messages': [{'role': 'user', 'content': 'hello'}],
        'stream': True, 'serein': {'memory': False},
    })
    assert response.status_code == 200, response.text
    assert len(sent) == 2
    assert 'thinking' in sent[0] and 'thinking' not in sent[1]
    assert all(item.is_closed for item in rejected)
    events = [json.loads(line.removeprefix('data: ')) for line in response.text.splitlines()
              if line.startswith('data: {')]
    assert any(row.get('choices') and row['choices'][0]['delta'].get('reasoning_content') == 'Reasoned.'
               for row in events)
    usage = [row['usage'] for row in events if 'usage' in row]
    assert len(usage) == 1 and usage[0]['total_tokens'] == 65
    assert not any(row.get('choices') == [] for row in events)


@pytest.mark.parametrize('model_name', ['claude-opus-4-6', 'claude-opus-5-5'])
def test_adaptive_custom_output_config_and_max_tokens_forwarded(model_name):
    route = {**model(), 'model': model_name}
    _, _, body = request_for(route, user_turn(
        max_tokens=128000,
        output_config={'effort': 'max'},
        top_k=32,
    ))
    assert body['max_tokens'] == 128000
    assert body['thinking'] == {'type': 'adaptive', 'display': 'summarized'}
    assert body['output_config'] == {'effort': 'max'}
    assert body['top_k'] == 32

    _, _, alternative = request_for(route, user_turn(
        max_completion_tokens=100000, output_config={'effort': 'high'},
        thinking={'type': 'adaptive', 'display': 'summarized'},
    ))
    assert alternative['max_tokens'] == 100000
    assert alternative['output_config'] == {'effort': 'high'}
    assert alternative['thinking']['display'] == 'summarized'


def test_explicit_client_limits_and_thinking_override_gateway_defaults():
    _, _, body = request_for(model(), user_turn(
        max_tokens=120000,
        output_config={'effort': 'medium'},
        thinking={'type': 'adaptive', 'display': 'omitted'},
    ))
    assert body['max_tokens'] == 120000
    assert body['thinking'] == {'type': 'adaptive', 'display': 'omitted'}
    assert body['output_config']['effort'] == 'medium'
    assert [request_for(model(), attempt)[2]['max_tokens']
            for attempt in relax_plan(model(), user_turn(max_tokens=120000))] == [120000]


def test_custom_anthropic_options_survive_actual_chat_gateway(deployment, monkeypatch):
    """Client JSON must reach Anthropic /messages with custom effort and output cap."""
    original = httpx.AsyncClient
    seen = []

    def handle(request):
        assert request.url.path.endswith('/messages')
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json=native_reply())

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    response = deployment.post('/v1/chat/completions', json={
        'model': 'provider/claude-sonnet',
        'stream': False,
        'messages': [{'role': 'user', 'content': 'hello'}],
        'serein': {'memory': False},
        'max_tokens': 120000,
        'output_config': {'effort': 'high'},
        'top_k': 15,
    })
    assert response.status_code == 200, response.text
    assert len(seen) == 1
    assert seen[0]['max_tokens'] == 120000
    assert seen[0]['output_config'] == {'effort': 'high'}
    assert seen[0]['thinking'] == {'type': 'adaptive', 'display': 'summarized'}
    assert seen[0]['top_k'] == 15
    assert response.json()['usage']['prompt_tokens'] == 60
