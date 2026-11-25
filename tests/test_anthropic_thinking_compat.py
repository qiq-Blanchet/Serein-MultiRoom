"""Claude chat defaults and visible thinking compatibility."""
from copy import deepcopy

import pytest

from serein.model_runtime import (
    AnthropicStream, anthropic_chat_defaults, normalize_response, request_for, ClaudeThinkingOptionsError,
)
from serein.chat_context import ClientContext


MODEL = {'model': 'claude-opus-4-6', 'base_url': 'http://127.0.0.1/v1',
         'protocol': 'anthropic', 'api_key': 'synthetic',
         'prompt_cache': 'anthropic-explicit'}
PAYLOAD = {'messages': [{'role': 'user', 'content': 'question'}]}


def wire(model=MODEL, **options):
    payload = {**PAYLOAD, **options}
    original = deepcopy(payload)
    body = request_for(model, anthropic_chat_defaults(model, payload))[2]
    assert payload == original
    return body


@pytest.mark.parametrize('name', ['claude-opus-4-6', 'claude-sonnet-4-6', 'claude-opus-4.6'])
def test_adaptive_default_and_generic_extra_high(name):
    model = {**MODEL, 'model': name}
    body = wire(model)
    assert body['max_tokens'] == 64000
    assert body['thinking'] == {'type': 'adaptive', 'display': 'summarized'}
    assert body['output_config'] == {'effort': 'high'}
    with pytest.raises(ClaudeThinkingOptionsError, match='does not support xhigh'):
        wire(model, reasoning_effort='xhigh')
    assert wire(model, reasoning_effort='max')['output_config']['effort'] == 'max'
    assert wire(model, reasoning_effort='high')['output_config']['effort'] == 'high'


@pytest.mark.parametrize('name', ['claude-opus-4-7', 'claude-opus-4-8', 'claude-opus-5',
                                 'claude-opus-5-5', 'claude-sonnet-5.5', 'claude-haiku-5-5',
                                 'claude-fable-5-2', 'claude-mythos-5-2',
                                 'anthropic.claude-sonnet-5-5-v1:0', 'claude-opus-6-1'])
def test_newer_claude_requests_visible_summary(name):
    body = wire({**MODEL, 'model':name}, reasoning_effort='xhigh')
    assert body['thinking'] == {'type':'adaptive', 'display':'summarized'}
    assert body['output_config']['effort'] == 'xhigh'
    assert body['max_tokens'] == 64000
    assert wire({**MODEL, 'model':name}, reasoning_effort='max')['output_config']['effort'] == 'max'
    body = wire({**MODEL, 'model':'claude-opus-4-8'},
                thinking={'type':'adaptive','display':'omitted'})
    assert body['thinking']['display'] == 'omitted'


def test_explicit_controls_win():
    body = wire(max_tokens=18000, max_completion_tokens=9000,
                output_config={'effort':'low'}, reasoning_effort='xhigh',
                temperature=0.7, top_p=0.8)
    assert body['max_tokens'] == 18000
    assert body['output_config']['effort'] == 'low'
    assert 'temperature' not in body and 'top_p' not in body
    assert wire(max_completion_tokens=12345)['max_tokens'] == 12345
    assert wire(max_output_tokens=12345)['max_tokens'] == 12345
    assert wire(reasoning_effort='none')['thinking'] == {'type':'disabled'}
    assert wire(thinking={'type':'disabled'}, reasoning_effort='high')['thinking'] == {'type':'disabled'}
    assert wire(thinking={'type':'enabled','budget_tokens':2048}, max_tokens=8000)['thinking'] == {
        'type':'enabled','budget_tokens':2048}


def test_pre_46_unknown_and_other_providers_keep_original_transport():
    for model in ({**MODEL,'model':'claude-sonnet-4-5'},
                  {**MODEL,'model':'claude-sonnet-4-20250514'},
                  {**MODEL,'model':'claude-3-7-sonnet-20250219'},
                  {**MODEL,'model':'hidden-alias'},
                  {**MODEL,'model':'deepseek-v4-pro'},
                  {**MODEL,'protocol':'openai'}):
        assert anthropic_chat_defaults(model, PAYLOAD) is PAYLOAD
    # Direct internal task requests do not receive chat-only defaults.
    body = request_for(MODEL, PAYLOAD)[2]
    assert 'thinking' not in body and body['max_tokens'] == 1024


def test_visible_stream_and_signature_roundtrip():
    stream = AnthropicStream(MODEL['model'])
    state = {'message': {'role':'assistant','content':'','reasoning_content':''},
             'tool_calls_by_index': {}}
    adapter = ClientContext()
    events = [
        {'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':'First '}},
        {'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'second'}},
        {'type':'content_block_delta','index':0,'delta':{'type':'signature_delta','signature':'signed'}},
        {'type':'content_block_start','index':1,'content_block':{'type':'text','text':'Answer'}},
    ]
    for event in events:
        delta = stream.convert(event)['choices'][0]['delta']
        if event.get('delta', {}).get('type') == 'signature_delta':
            assert 'reasoning_content' not in delta
        adapter._merge_stream_message_delta(state, delta)
    message = adapter._build_stream_assistant_message(state)
    assert message['reasoning_content'] == 'First second'
    assert message['reasoning_details'][0]['signature'] == 'signed'
    assert adapter._openai_message_to_anthropic_content(message)[0] == {
        'type':'thinking','thinking':'First second','signature':'signed'}


def test_nonstream_visible_summary_and_redaction():
    response = normalize_response(MODEL, {'content': [
        {'type':'thinking','thinking':'Visible summary','signature':'signed'},
        {'type':'redacted_thinking','data':'encrypted'},
        {'type':'text','text':'Answer'}]})
    message = response['choices'][0]['message']
    assert message['reasoning_content'] == message['reasoning'] == 'Visible summary'
    assert message['reasoning_details'][1]['data'] == 'encrypted'
    state = {'message': {'role':'assistant','content':'','reasoning_content':''}}
    ClientContext()._merge_stream_message_delta(state, {'reasoning_content':'one','reasoning':'one'})
    assert state['message']['reasoning_content'] == 'one'
