"""Synthetic Anthropic usage regression coverage (no network)."""
from serein.model_runtime import AnthropicStream, normalize_anthropic_usage, normalize_response


def test_full_usage_includes_cache_write_read_and_thinking_once():
    raw = {'input_tokens': 3000, 'cache_read_input_tokens': 45000,
           'cache_creation_input_tokens': 2000, 'output_tokens': 1200,
           'output_tokens_details': {'thinking_tokens': 800},
           'cache_creation': {'ephemeral_5m_input_tokens': 2000}}
    usage = normalize_anthropic_usage(raw)
    assert usage['prompt_tokens'] == 50000
    assert usage['completion_tokens'] == 1200
    assert usage['total_tokens'] == 51200
    assert usage['prompt_tokens_details']['cached_tokens'] == 45000
    assert usage['completion_tokens_details']['reasoning_tokens'] == 800
    assert all(usage[key] == value for key, value in raw.items())
    assert 'prompt_tokens' not in raw


def test_stream_cumulative_updates_preserve_start_cache_usage():
    stream = AnthropicStream('claude-opus-5-5')
    first = stream.convert({'type': 'message_start', 'message': {'id': 'test',
        'usage': {'input_tokens': 3000, 'cache_read_input_tokens': 45000,
                  'cache_creation_input_tokens': 2000, 'output_tokens': 1}}})
    assert first['usage']['prompt_tokens'] == 50000
    for tokens in (400, 1200):
        final = stream.convert({'type':'message_delta', 'delta': {},
                                'usage': {'output_tokens': tokens}})
        assert final['usage']['completion_tokens'] == tokens
        assert final['usage']['total_tokens'] == 50000 + tokens
        assert final['usage']['prompt_tokens_details']['cached_tokens'] == 45000
    assert final['usage']['cache_creation_input_tokens'] == 2000


def test_omitted_cache_is_not_an_explicit_zero():
    missing = normalize_anthropic_usage({'input_tokens': 40, 'output_tokens': 5})
    assert 'prompt_tokens_details' not in missing
    assert 'cache_read_input_tokens' not in missing
    zero = normalize_anthropic_usage({'input_tokens': 40, 'output_tokens': 5,
                                     'cache_read_input_tokens': 0})
    assert zero['prompt_tokens_details']['cached_tokens'] == 0
    assert normalize_anthropic_usage({'output_tokens': 5})['completion_tokens'] == 5
    assert 'prompt_tokens' not in normalize_anthropic_usage({'output_tokens': 5})


def test_nonstream_and_openai_passthrough():
    result = {'content': [{'type':'text', 'text':'test'}],
              'usage': {'input_tokens': 10, 'cache_read_input_tokens': 90,
                        'cache_creation_input_tokens': 20, 'output_tokens': 5}}
    out = normalize_response({'protocol':'anthropic','model':'claude'}, result)
    assert out['usage']['prompt_tokens'] == 120
    assert out['usage']['total_tokens'] == 125
    assert out['usage']['prompt_tokens_details']['cached_tokens'] == 90
    assert normalize_response({'protocol':'openai'}, result) is result
