"""Regression coverage for the opt-in Anthropic client context projection."""
from copy import deepcopy

import pytest

from serein.api.settings import ModelEntry
from serein.model_runtime import request_for


MODEL = {'model': 'synthetic-sonnet', 'base_url': 'http://127.0.0.1/v1',
         'protocol': 'anthropic', 'api_key': 'synthetic',
         'prompt_cache': 'anthropic-explicit'}


def host(text):
    return {'role': 'system', 'name': 'orbis_host_data_v1', 'content': text}


def user(text):
    return {'role': 'user', 'content': text}


def text_of(message):
    content = message['content']
    return content if isinstance(content, str) else ''.join(
        block.get('text', '') for block in content if isinstance(block, dict))


def wire(messages, mode='late-system', **options):
    return request_for({**MODEL, 'anthropic_system_layout': mode, **options},
                       {'messages': messages})[2]


def test_three_modes_keep_original_request():
    messages = [
        {'role': 'system', 'content': 'Fixed instructions'},
        host('<orbis_user_message_time>old</orbis_user_message_time>'),
        user('old question ' * 3000),
        {'role': 'assistant', 'content': 'old answer ' * 3000},
        {'role': 'system', 'content': 'keyword worldbook'},
        host('<orbis_user_message_time>new</orbis_user_message_time>'),
        user('new question'),
    ]
    original = deepcopy(messages)
    legacy = wire(messages, 'legacy')
    known = wire(messages, 'host-data')
    late = wire(messages, anthropic_cache_tail_tokens=0)
    assert 'old</orbis_user_message_time>' in legacy['system'][0]['text']
    assert 'keyword worldbook' in known['system'][0]['text']
    assert late['system'][0]['text'] == 'Fixed instructions'
    assert 'old</orbis_user_message_time>' in text_of(late['messages'][0])
    assert 'keyword worldbook' in text_of(late['messages'][-1])
    assert 'new</orbis_user_message_time>' in text_of(late['messages'][-1])
    assert late['messages'][1]['content'][-1]['cache_control']['type'] == 'ephemeral'
    assert messages == original


def test_stable_timestamps_across_turns_with_worldbook():
    history = [{'role': 'system', 'content': 'Fixed instructions'},
               host('time one'), user('first'),
               {'role': 'assistant', 'content': 'reply one'}]
    first = wire(history + [{'role': 'system', 'content': 'book A'},
                            host('time two'), user('second')])
    second = wire(history + [host('time two'), user('second'),
                             {'role': 'assistant', 'content': 'reply two'},
                             {'role': 'system', 'content': 'book B'},
                             host('time three'), user('third')])
    assert first['system'] == second['system']
    assert first['messages'][:2] == second['messages'][:2]
    assert 'time two' in text_of(second['messages'][2])
    assert 'book A' not in text_of(second['messages'][2])
    assert 'book B' in text_of(second['messages'][-1])


def test_tool_sequence_and_thinking_config_are_preserved():
    messages = [{'role': 'system', 'content': 'Fixed'},
                host('time'), user('question'),
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'call_1', 'type': 'function',
                     'function': {'name': 'lookup', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'result'}]
    body = request_for({**MODEL, 'anthropic_system_layout': 'late-system'},
                       {'messages': messages, 'thinking': {'type': 'enabled', 'budget_tokens': 1024}})[2]
    assert body['thinking'] == {'type': 'enabled', 'budget_tokens': 1024}
    assert body['messages'][-2]['content'][-1]['type'] == 'tool_use'
    assert body['messages'][-1]['content'][0]['type'] == 'tool_result'
    assert body['messages'][-1]['content'][0]['tool_use_id'] == 'call_1'


def test_default_reserve_and_settings_validation():
    base = dict(id='test', label='test', model='claude', base_url='http://127.0.0.1/v1')
    assert ModelEntry(**base).anthropic_cache_tail_tokens == 4000
    assert ModelEntry(**base, protocol='anthropic', anthropic_system_layout='late-system',
                      anthropic_cache_tail_tokens=0).anthropic_cache_tail_tokens == 0
    with pytest.raises(ValueError):
        ModelEntry(**base, anthropic_system_layout='late-system')
    with pytest.raises(ValueError):
        ModelEntry(**base, protocol='anthropic', anthropic_cache_tail_tokens=-1)
