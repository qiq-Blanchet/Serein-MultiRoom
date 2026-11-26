"""Synthetic regressions for cache-repair follow-up; no external services."""
from copy import deepcopy

import pytest

from serein.chat_context import ClientContext
from serein.model_runtime import AnthropicStream, normalize_response, request_for

MODEL = {'model': 'claude-opus-5-5', 'protocol': 'anthropic',
         'base_url': 'http://127.0.0.1/v1', 'anthropic_system_layout': 'late-system'}
NATIVE = [
    {'type': 'thinking', 'thinking': 'first', 'signature': 'signed-first'},
    {'type': 'text', 'text': 'progress'},
    {'type': 'tool_use', 'id': 'original-a', 'name': 'lookup', 'input': {'q': 1}},
    {'type': 'redacted_thinking', 'data': 'encrypted'},
    {'type': 'text', 'text': 'more'},
    {'type': 'tool_use', 'id': 'original-b', 'name': 'lookup', 'input': {'q': 2}},
]


def cached_response(streamed):
    if not streamed:
        return normalize_response(MODEL, {'content': NATIVE})['choices'][0]['message']
    converter = AnthropicStream(MODEL['model'])
    context = ClientContext()
    state = {'message': {'role': 'assistant', 'content': '', 'reasoning_content': ''},
             'tool_calls_by_index': {}}
    for index, block in enumerate(NATIVE):
        start = deepcopy(block)
        if block['type'] == 'tool_use':
            start['input'] = {}
        event = converter.convert({'type': 'content_block_start', 'index': index,
                                   'content_block': start})
        if event:
            context._merge_stream_message_delta(state, event['choices'][0]['delta'])
        if block['type'] == 'tool_use':
            import json
            event = converter.convert({'type': 'content_block_delta', 'index': index,
                                       'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}})
            context._merge_stream_message_delta(state, event['choices'][0]['delta'])
    message = context._build_stream_assistant_message(state)
    message['_serein_anthropic_content'] = converter.native_content()
    assert message['_serein_anthropic_content'] == NATIVE
    return message


def tool_result(call_id, native):
    if native:
        return {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': call_id, 'content': 'result'}]}
    return {'role': 'tool', 'tool_call_id': call_id, 'content': 'result'}


def replay_of(message):
    return {'role': 'assistant', 'content': message['content'],
            'tool_calls': deepcopy(message['tool_calls'])}


@pytest.mark.parametrize('streamed', [False, True])
@pytest.mark.parametrize('native_result', [False, True])
@pytest.mark.parametrize('rewritten', [False, True])
def test_full_signed_native_replay_with_parallel_results(streamed, native_result, rewritten):
    ctx = ClientContext()
    message = cached_response(streamed)
    ctx._update_reasoning_cache('w', message)
    replay = replay_of(message)
    if rewritten:
        for index, call in enumerate(replay['tool_calls']):
            call['id'] = 'client-' + str(index)
    # Native/Chat Completions results can arrive in a different order.
    results = [tool_result(call['id'], native_result) for call in reversed(replay['tool_calls'])]
    messages = [{'role': 'user', 'content': 'question'}, replay, *results]
    ctx._restore_cached_reasoning_content('w', messages)
    wire = request_for(MODEL, {'messages': messages})[2]['messages']
    assert wire[1]['content'] == NATIVE
    assert [block['tool_use_id'] for block in wire[2]['content']] == ['original-b', 'original-a']
    assert len(wire) == 3


@pytest.mark.parametrize('streamed', [False, True])
def test_edit_never_reintroduces_cached_text_or_stale_signed_blocks(streamed):
    ctx = ClientContext()
    message = cached_response(streamed)
    ctx._update_reasoning_cache('w', message)
    replay = replay_of(message)
    replay['content'] = 'edited response'
    messages = [replay, tool_result('original-a', False), tool_result('original-b', False)]
    ctx._restore_cached_reasoning_content('w', messages)
    blocks = request_for(MODEL, {'messages': messages})[2]['messages'][0]['content']
    assert [block['text'] for block in blocks if block['type'] == 'text'] == ['edited response']
    assert not any(block['type'] in ('thinking', 'redacted_thinking') for block in blocks)


def test_rewritten_ids_with_incomplete_results_are_not_guessed():
    ctx = ClientContext()
    message = cached_response(False)
    ctx._update_reasoning_cache('w', message)
    replay = replay_of(message)
    replay['tool_calls'][0]['id'] = 'changed-a'
    replay['tool_calls'][1]['id'] = 'changed-b'
    messages = [replay, tool_result('changed-a', False)]
    ctx._restore_cached_reasoning_content('w', messages)
    assert '_serein_anthropic_content' not in replay
    assert 'reasoning_details' not in replay
    assert replay['tool_calls'][0]['id'] == 'changed-a'
    assert messages[1]['tool_call_id'] == 'changed-a'


@pytest.mark.parametrize('mode', ['host-data', 'late-system'])
@pytest.mark.parametrize('native_result', [False, True])
def test_host_data_is_placed_after_all_parallel_tool_results(mode, native_result):
    message = cached_response(False)
    messages = [replay_of(message),
                {'role': 'system', 'name': 'orbis_host_data_v1', 'content': 'host time 1'},
                tool_result('original-a', native_result),
                {'role': 'system', 'name': 'orbis_host_data_v1', 'content': 'host time 2'},
                tool_result('original-b', not native_result)]
    original = deepcopy(messages)
    wire = request_for({**MODEL, 'anthropic_system_layout': mode}, {'messages': messages})[2]
    assert 'system' not in wire
    assert messages == original
    assert len(wire['messages']) == 2
    blocks = wire['messages'][-1]['content']
    assert [block['type'] for block in blocks] == ['tool_result', 'tool_result', 'text', 'text']
    assert [block['tool_use_id'] for block in blocks[:2]] == ['original-a', 'original-b']
    assert 'host time 1' in blocks[2]['text'] and 'host time 2' in blocks[3]['text']


@pytest.mark.parametrize('mode', ['host-data', 'late-system'])
def test_orphan_host_data_is_not_silently_lost(mode):
    messages = [{'role': 'user', 'content': 'question'},
                {'role': 'system', 'name': 'orbis_host_data_v1', 'content': 'orphan time'}]
    wire = request_for({**MODEL, 'anthropic_system_layout': mode}, {'messages': messages})[2]
    assert 'orphan time' in str(wire)


def test_reasoning_cache_has_ttl_and_session_and_entry_limits(monkeypatch):
    import serein.chat_context as module
    clock = [1.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    ctx = ClientContext()
    ctx.reasoning_cache_ttl_seconds = 10
    ctx.reasoning_cache_max_sessions = 2
    ctx.reasoning_cache_max_entries_per_session = 2
    message = cached_response(False)
    for session in ('old', 'middle', 'new'):
        ctx._update_reasoning_cache(session, message)
        clock[0] += 1
    assert set(ctx.pending_tool_reasoning) == {'middle', 'new'}
    for index in range(4):
        other = deepcopy(message)
        other['tool_calls'][0]['id'] = 'call-' + str(index)
        ctx._update_reasoning_cache('new', other)
    assert sum(map(len, ctx.pending_tool_reasoning['new'].values())) <= 2
    clock[0] += 20
    ctx._restore_cached_reasoning_content('new', [replay_of(message), tool_result('original-a', False)])
    assert not ctx.pending_tool_reasoning


def test_reasoning_cache_byte_limits():
    ctx = ClientContext()
    ctx.reasoning_cache_max_bytes = 3000
    ctx.reasoning_cache_max_session_bytes = 3000
    message = cached_response(False)
    for index in range(10):
        ctx._update_reasoning_cache(str(index), message)
    assert len(ctx.pending_tool_reasoning) < 10
    oversized = deepcopy(message)
    oversized['_serein_anthropic_content'][0]['thinking'] = 'x' * 4000
    ctx._update_reasoning_cache('oversized', oversized)
    assert 'oversized' not in ctx.pending_tool_reasoning


def test_partial_eviction_disables_ambiguous_semantic_fallback():
    ctx = ClientContext()
    ctx.reasoning_cache_max_entries_per_session = 1
    message = cached_response(False)
    another = deepcopy(message)
    another['tool_calls'][0]['id'] = 'another-a'
    another['tool_calls'][1]['id'] = 'another-b'
    for block in another['_serein_anthropic_content']:
        if block['type'] == 'tool_use':
            block['id'] = 'another-a' if block['id'] == 'original-a' else 'another-b'
    ctx._update_reasoning_cache('w', message)
    ctx._update_reasoning_cache('w', another)
    replay = replay_of(another)
    replay['tool_calls'][0]['id'] = 'client-a'
    replay['tool_calls'][1]['id'] = 'client-b'
    ctx._restore_cached_reasoning_content('w', [
        replay, tool_result('client-a', False), tool_result('client-b', False)])
    assert 'reasoning_details' not in replay
    exact = replay_of(another)
    ctx._restore_cached_reasoning_content('w', [
        exact, tool_result('another-a', False), tool_result('another-b', False)])
    assert exact['_serein_anthropic_content'] == another['_serein_anthropic_content']


def test_identical_parallel_calls_with_rewritten_ids_are_ambiguous():
    ctx = ClientContext()
    native = deepcopy(NATIVE)
    native[-1]['input'] = native[2]['input']
    message = normalize_response(MODEL, {'content': native})['choices'][0]['message']
    ctx._update_reasoning_cache('w', message)
    replay = replay_of(message)
    replay['tool_calls'][0]['id'] = 'client-a'
    replay['tool_calls'][1]['id'] = 'client-b'
    ctx._restore_cached_reasoning_content('w', [
        replay, tool_result('client-a', False), tool_result('client-b', False)])
    assert 'reasoning_details' not in replay
    assert '_serein_anthropic_content' not in replay


def test_edited_openai_tool_reply_does_not_borrow_old_reasoning():
    ctx = ClientContext()
    original = {'role': 'assistant', 'content': 'original', 'reasoning_content': 'old reasoning',
                'tool_calls': [{'type': 'function', 'id': 'call', 'function': {'name': 'lookup', 'arguments': '{}'}}]}
    ctx._update_reasoning_cache('w', original)
    replay = replay_of(original)
    replay['content'] = 'edited'
    ctx._restore_cached_reasoning_content('w', [replay, tool_result('call', False)])
    assert 'reasoning_content' not in replay


@pytest.mark.parametrize('streamed', [False, True])
def test_chat_proxy_signed_tool_continuation_end_to_end(tmp_path, monkeypatch, streamed):
    import json
    import httpx
    from fastapi.testclient import TestClient
    from serein.api.http import create_app
    from serein.bootstrap import initialize
    from serein.config import Settings

    settings = Settings(tmp_path / 'memory.db', index=tmp_path / 'index.db', writable=True)
    initialize(settings)
    client = TestClient(create_app(settings, token='synthetic', live=True),
                        headers={'Authorization': 'Bearer synthetic'})
    saved = client.patch('/v1/settings', json={
        'models': [{**MODEL, 'id': 'claude', 'label': 'Synthetic Claude',
                    'api_key': 'synthetic-provider-secret'}],
        'assignments': {'chat': 'claude'},
        'upstream': {'memory_enabled': False},
    })
    assert saved.is_success, saved.text
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            if not streamed:
                return httpx.Response(200, json={'id': 'synthetic', 'content': NATIVE,
                                                'stop_reason': 'tool_use', 'usage': {'input_tokens': 12, 'output_tokens': 10}})
            events = [{'type': 'message_start', 'message': {'id': 'synthetic', 'usage': {'input_tokens': 12}}}]
            for i, block in enumerate(NATIVE):
                start = deepcopy(block)
                if block['type'] == 'tool_use':
                    start['input'] = {}
                events.append({'type': 'content_block_start', 'index': i, 'content_block': start})
                if block['type'] == 'tool_use':
                    events.append({'type': 'content_block_delta', 'index': i, 'delta': {
                        'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}})
                events.append({'type': 'content_block_stop', 'index': i})
            events += [{'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 10}},
                       {'type': 'message_stop'}]
            return httpx.Response(200, text=''.join('data: '+json.dumps(event)+'\n\n' for event in events),
                                  headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, json={'id': 'synthetic-final', 'content': [{'type': 'text', 'text': 'done'}],
                                         'stop_reason': 'end_turn', 'usage': {'input_tokens': 10, 'output_tokens': 2}})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs))
    headers = {'X-Serein-Window-ID': 'signed-tool-round'}
    question = {'role': 'user', 'content': 'synthetic question'}
    response = client.post('/v1/chat/completions', headers=headers,
                           json={'messages': [question], 'stream': streamed})
    assert response.is_success, response.text
    assert '_serein_anthropic_content' not in response.text
    if streamed:
        ctx = ClientContext()
        state = {'message': {'role': 'assistant', 'content': '', 'reasoning_content': ''},
                 'tool_calls_by_index': {}}
        for line in response.text.splitlines():
            if line.startswith('data: ') and line[6:] != '[DONE]':
                event = json.loads(line[6:])
                for choice in event.get('choices', []):
                    ctx._merge_stream_message_delta(state, choice.get('delta', {}))
        assistant = ctx._build_stream_assistant_message(state)
    else:
        assistant = response.json()['choices'][0]['message']
    replay = replay_of(assistant)
    # A private client field must not override the verified replay.
    replay['_serein_anthropic_content'] = [{'type': 'text', 'text': 'untrusted'}]
    for i, call in enumerate(replay['tool_calls']):
        call['id'] = 'client-' + str(i)
    results = [tool_result(call['id'], True) for call in replay['tool_calls']]
    response = client.post('/v1/chat/completions', headers=headers, json={
        'messages': [question, replay, {'role': 'system', 'name': 'orbis_host_data_v1',
                                       'content': 'continuation time'}, *results]})
    assert response.is_success, response.text
    prepared = calls[1]['messages']
    assert prepared[-2]['content'] == NATIVE
    assert [block['tool_use_id'] for block in prepared[-1]['content'][:2]] == ['original-a', 'original-b']
    assert 'continuation time' in prepared[-1]['content'][2]['text']
    assert 'untrusted' not in str(prepared)


def test_partial_eviction_does_not_make_reused_exact_id_unambiguous():
    ctx = ClientContext()
    ctx.reasoning_cache_max_entries_per_session = 1
    first = cached_response(False)
    second = deepcopy(first)
    second['_serein_anthropic_content'][0]['signature'] = 'other-signature'
    second['reasoning_details'][0]['signature'] = 'other-signature'
    ctx._update_reasoning_cache('w', first)
    ctx._update_reasoning_cache('w', second)
    replay = replay_of(second)
    ctx._restore_cached_reasoning_content('w', [
        replay, tool_result('original-a', False), tool_result('original-b', False)])
    assert '_serein_anthropic_content' not in replay


def test_reasoning_cache_logs_counts_not_tool_arguments(caplog):
    import logging
    ctx = ClientContext()
    message = cached_response(False)
    message['tool_calls'][0]['function']['arguments'] = '{"secret":"private-synthetic-argument"}'
    with caplog.at_level(logging.INFO):
        ctx._update_reasoning_cache('w', message)
    assert 'private-synthetic-argument' not in caplog.text
