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


@pytest.mark.parametrize(('options', 'message'), [
    ({'max_tokens': None}, 'max_tokens must be a positive integer'),
    ({'max_tokens': 0}, 'max_tokens must be a positive integer'),
    ({'max_tokens': -1}, 'max_tokens must be a positive integer'),
    ({'max_tokens': '64000'}, 'max_tokens must be a positive integer'),
    ({'max_completion_tokens': 0}, 'max_completion_tokens must be a positive integer'),
    ({'max_output_tokens': 1.5}, 'max_output_tokens must be a positive integer'),
    ({'thinking': None}, 'thinking must be an object'),
    ({'thinking': 'adaptive'}, 'thinking must be an object'),
    ({'output_config': None}, 'output_config must be an object'),
    ({'output_config': 'high'}, 'output_config must be an object'),
])
def test_invalid_explicit_controls_fail_instead_of_silently_using_defaults(options, message):
    with pytest.raises(ClaudeThinkingOptionsError, match=message):
        wire(**options)


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


def test_native_block_order_is_cached_for_tool_continuation():
    native = [
        {'type':'thinking','thinking':'first','signature':'sig-1'},
        {'type':'text','text':'progress'},
        {'type':'tool_use','id':'call-1','name':'one','input':{'x':1}},
        {'type':'redacted_thinking','data':'encrypted'},
        {'type':'text','text':'more'},
        {'type':'tool_use','id':'call-2','name':'two','input':{}},
    ]
    message = normalize_response(MODEL, {'content': native})['choices'][0]['message']
    adapter = ClientContext()
    assert adapter._openai_message_to_anthropic_content(message) == native

    adapter._update_reasoning_cache('window', message)
    replay = {'role':'assistant', 'content':message['content'],
              'tool_calls':deepcopy(message['tool_calls'])}
    adapter._restore_cached_reasoning_content('window', [
        replay, {'role':'tool','tool_call_id':'call-1','content':'result'}])
    assert adapter._openai_message_to_anthropic_content(replay) == native


def test_stream_collects_provider_blocks_without_reordering():
    stream = AnthropicStream(MODEL['model'])
    events = [
        {'type':'message_start','message':{'id':'synthetic','usage':{}}},
        {'type':'content_block_start','index':0,
         'content_block':{'type':'thinking','thinking':''}},
        {'type':'content_block_delta','index':0,
         'delta':{'type':'thinking_delta','thinking':'summary'}},
        {'type':'content_block_delta','index':0,
         'delta':{'type':'signature_delta','signature':'signed'}},
        {'type':'content_block_start','index':1,
         'content_block':{'type':'text','text':'before '}},
        {'type':'content_block_delta','index':1,
         'delta':{'type':'text_delta','text':'tool'}},
        {'type':'content_block_start','index':2,
         'content_block':{'type':'tool_use','id':'call','name':'lookup','input':{}}},
        {'type':'content_block_delta','index':2,
         'delta':{'type':'input_json_delta','partial_json':'{"q":'}},
        {'type':'content_block_delta','index':2,
         'delta':{'type':'input_json_delta','partial_json':'"value"}'}},
    ]
    for event in events:
        stream.convert(event)
    assert stream.native_content() == [
        {'type':'thinking','thinking':'summary','signature':'signed'},
        {'type':'text','text':'before tool'},
        {'type':'tool_use','id':'call','name':'lookup','input':{'q':'value'}},
    ]
