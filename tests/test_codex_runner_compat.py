"""Contract tests for Codex chat client behavior without a live subscription.

The scripted app-server exercises the actual JSON-RPC envelope and response adapter.
An authenticated real-app-server smoke test is still required before production.
"""
import asyncio
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from serein.codex_runner import (
    CodexAppServer,
    RunnerConfig,
    completion_options,
    create_runner_app,
    prepare_messages,
)


HEADERS = {"Authorization": "Bearer runner-secret"}
IMAGE = "data:image/png;base64,iVBORw0KGgo="


class ScriptedAppServer(CodexAppServer):
    def __init__(self, workdir, *, emit_tool=False):
        super().__init__(RunnerConfig("codex", Path(workdir), "runner-secret"))
        self.calls = []
        self.emit_tool = emit_tool
        self.notifications = []

    async def _start(self):
        return None

    async def _request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-test"}}
        if method == "turn/start":
            if self.emit_tool:
                self.notifications = [{
                    "id": 991, "method": "item/tool/call",
                    "params": {"threadId": "thread-test", "turnId": "turn-test",
                               "tool": "lookup", "callId": "call-123",
                               "arguments": {"query": "snow"}}}]
            else:
                self.notifications = [
                    {"method": "item/started", "params": {"threadId": "thread-test",
                        "item": {"id": "msg-1", "type": "agentMessage", "phase": "final_answer"}}},
                    {"method": "item/agentMessage/delta",
                        "params": {"threadId": "thread-test", "itemId": "msg-1",
                                   "phase": "final_answer", "delta": "A test reply"}},
                    {"method": "item/completed", "params": {"threadId": "thread-test",
                        "item": {"id": "msg-1", "type": "agentMessage",
                                 "phase": "final_answer", "text": "A test reply"}}},
                    {"method": "turn/completed", "params": {"threadId": "thread-test",
                        "turn": {"id": "turn-test", "status": "completed"}}},
                ]
            return {"turn": {"id": "turn-test"}}
        return {}

    async def _read(self):
        if not self.notifications:
            raise AssertionError("Scripted App Server read after completion")
        return self.notifications.pop(0)


def test_preserves_image_in_current_user_message_and_history(tmp_path):
    server = ScriptedAppServer(tmp_path)
    payload = {"model": "test-codex", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Earlier image"},
            {"type": "image_url", "image_url": {"url": IMAGE}}]},
        {"role": "assistant", "content": "I saw it."},
        {"role": "user", "content": [{"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": IMAGE}}]},
    ]}
    async def run():
        return [event async for event in server.events(payload)]
    events = asyncio.run(run())
    injected = next(params for method, params in server.calls if method == "thread/inject_items")
    earlier = next(item for item in injected["items"] if item.get("role") == "user")
    assert {"type": "input_image", "image_url": IMAGE} in earlier["content"]
    turn = next(params for method, params in server.calls if method == "turn/start")
    assert turn["input"] == [{"type": "text", "text": "What is this?"},
                             {"type": "image", "url": IMAGE}]
    assert events[-1] == {"type": "completed", "text": "A test reply"}


def test_image_only_message_works_and_remote_url_is_rejected(tmp_path):
    _, prompt = prepare_messages({"messages": [
        {"role": "user", "content": [{"type": "image_url",
                                       "image_url": {"url": IMAGE}}]}]})
    assert prompt == ""
    server = ScriptedAppServer(tmp_path)
    with TestClient(create_runner_app(server.config, server)) as client:
        no_fetch = client.post("/v1/chat/completions", headers=HEADERS,
            json={"model": "test-codex", "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "http://169.254.169.254/latest/meta-data"}}]}]})
        assert no_fetch.status_code == 400
        assert "inline" in no_fetch.json()["detail"].lower()
        yes = client.post("/v1/chat/completions", headers=HEADERS, json={
            "model": "test-codex", "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": IMAGE}}]}]})
        assert yes.status_code == 200
        assert yes.json()["choices"][0]["message"]["content"] == "A test reply"


def test_dynamic_tool_call_stays_on_client_side(tmp_path):
    server = ScriptedAppServer(tmp_path, emit_tool=True)
    payload = {"model": "test-codex",
        "messages": [{"role": "user", "content": "Find a memory"}],
        "tools": [{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object"}}}]}
    with TestClient(create_runner_app(server.config, server)) as client:
        resp = client.post("/v1/chat/completions", headers=HEADERS, json=payload)
        assert resp.status_code == 200
        choice = resp.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        tool = choice["message"]["tool_calls"][0]
        assert tool["id"] == "call-123"
        assert tool["function"] == {"name": "lookup", "arguments": '{"query":"snow"}'}
        assert any(method == "thread/start" and args["dynamicTools"][0]["name"] == "lookup"
                   for method, args in server.calls)


def test_options_do_not_silently_override_client_tool_choice(tmp_path):
    server = ScriptedAppServer(tmp_path)
    payload = {"model": "test-codex",
        "messages": [{"role": "user", "content": "No tool calls"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
        "tool_choice": "none", "stream_options": {"include_usage": False}}
    with TestClient(create_runner_app(server.config, server)) as client:
        result = client.post("/v1/chat/completions", headers=HEADERS, json=payload)
        assert result.status_code == 200
    start = next(params for method, params in server.calls if method == "thread/start")
    assert "dynamicTools" not in start
    assert completion_options({"tool_choice": "auto"})["tool_choice"] == "auto"


@pytest.mark.parametrize("field, value", [
    ("stop", ["STOP"]), ("temperature", 0.5), ("tool_choice", "required"),
    ("n", 2), ("response_format", {"type": "unknown"}),
    ("stream_options", {"other": True}),
])
def test_unsupported_behavior_changing_controls_return_400(tmp_path, field, value):
    server = ScriptedAppServer(tmp_path)
    with TestClient(create_runner_app(server.config, server)) as client:
        response = client.post("/v1/chat/completions", headers=HEADERS, json={
            "model": "test-codex", "messages": [{"role": "user", "content": "Hello"}],
            field: value,
        })
        assert response.status_code == 400
    assert not server.calls


def test_json_schema_forwarded_to_turn_start(tmp_path):
    server = ScriptedAppServer(tmp_path)
    schema = {"type": "object", "properties": {"answer": {"type": "string"}},
              "required": ["answer"], "additionalProperties": False}
    payload = {"model": "test-codex", "messages": [
        {"role": "user", "content": "Answer as JSON"}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "answer", "schema": schema}}}
    asyncio.run(_consume(server, payload))
    params = next(params for method, params in server.calls if method == "turn/start")
    assert params["outputSchema"] == schema


async def _consume(server, payload):
    return [event async for event in server.events(payload)]

class UsageAppServer(ScriptedAppServer):
    async def _request(self, method, params):
        result = await super()._request(method, params)
        if method == "turn/start" and not self.emit_tool:
            self.notifications.insert(0, {"method": "thread/tokenUsage/updated",
                "params": {"threadId": "thread-test", "turnId": "turn-test",
                    "tokenUsage": {"last": {"inputTokens": 120, "outputTokens": 34,
                                            "cachedInputTokens": 12}}}})
        return result


def test_streaming_usage_chunk_contains_real_codex_counts(tmp_path):
    server = UsageAppServer(tmp_path)
    with TestClient(create_runner_app(server.config, server)) as client:
        result = client.post("/v1/chat/completions", headers=HEADERS, json={
            "model": "test-codex", "messages": [{"role": "user", "content": "Hello"}],
            "stream": True, "stream_options": {"include_usage": True}})
        assert result.status_code == 200
        assert '"choices":[]' in result.text
        assert '"prompt_tokens":120' in result.text
        assert '"completion_tokens":34' in result.text
        assert result.text.rstrip().endswith("data: [DONE]")


def test_nonstream_usage_matches_codex_usage_event(tmp_path):
    server = UsageAppServer(tmp_path)
    with TestClient(create_runner_app(server.config, server)) as client:
        result = client.post("/v1/chat/completions", headers=HEADERS, json={
            "model": "test-codex", "messages": [{"role": "user", "content": "Hello"}]})
        assert result.status_code == 200
        assert result.json()["usage"]["total_tokens"] == 154
        assert result.json()["usage"]["prompt_tokens_details"]["cached_tokens"] == 12


class PhasedAppServer(ScriptedAppServer):
    async def _request(self, method, params):
        response = await super()._request(method, params)
        if method == "turn/start":
            self.notifications.insert(0, {
                "method": "item/completed", "params": {
                    "threadId": "thread-test",
                    "item": {"id": "intermediate", "type": "agentMessage",
                             "phase": "analysis", "text": "private intermediate message"}}})
            self.notifications.insert(0, {
                "method": "item/agentMessage/delta", "params": {
                    "threadId": "thread-test", "itemId": "intermediate",
                    "phase": "analysis", "delta": "private intermediate message"}})
            self.notifications.insert(0, {
                "method": "item/completed", "params": {
                    "threadId": "thread-test",
                    "item": {"id": "commentary", "type": "agentMessage",
                             "phase": "commentary", "text": "not the final answer"}}})
            self.notifications.insert(0, {
                "method": "item/agentMessage/delta", "params": {
                    "threadId": "thread-test", "itemId": "commentary",
                    "phase": "commentary", "delta": "not the final answer"}})
        return response


def test_codex_stream_excludes_intermediate_message_phases(tmp_path):
    server = PhasedAppServer(tmp_path)
    with TestClient(create_runner_app(server.config, server)) as client:
        response = client.post("/v1/chat/completions", headers=HEADERS, json={
            "model": "test-codex", "messages": [{"role": "user", "content": "Hello"}],
            "stream": True})
        assert response.status_code == 200
        assert "A test reply" in response.text
        assert "private intermediate message" not in response.text
        assert "not the final answer" not in response.text


def test_codex_nonstream_prefers_final_answer_over_intermediate_phases(tmp_path):
    server = PhasedAppServer(tmp_path)
    with TestClient(create_runner_app(server.config, server)) as client:
        response = client.post("/v1/chat/completions", headers=HEADERS, json={
            "model": "test-codex", "messages": [{"role": "user", "content": "Hello"}]})
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "A test reply"


@pytest.mark.parametrize("options", [
    {"max_tokens": 0}, {"max_completion_tokens": -1},
    {"max_tokens": "4096"}, {"max_tokens": 1024, "max_completion_tokens": 2048},
])
def test_invalid_or_conflicting_token_bounds_are_rejected(tmp_path, options):
    server = ScriptedAppServer(tmp_path)
    with TestClient(create_runner_app(server.config, server)) as client:
        response = client.post("/v1/chat/completions", headers=HEADERS, json={
            "model": "test-codex", "messages": [{"role": "user", "content": "Hello"}],
            **options})
        assert response.status_code == 400
    assert not server.calls


def test_client_tool_result_can_carry_text_and_inline_image():
    images = [{"type": "image_url", "image_url": {"url": IMAGE}}]
    payload = {"messages": [
        {"role": "user", "content": "What does the tool show?"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-image", "type": "function", "function": {
                "name": "inspect", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-image", "content": [
            {"type": "text", "text": "Tool produced a diagram"}, *images]},
    ]}
    history, prompt = prepare_messages(payload)
    output = next(item for item in history if item["type"] == "function_call_output")
    assert output["call_id"] == "call-image"
    assert output["output"] == [
        {"type": "input_text", "text": "Tool produced a diagram"},
        {"type": "input_image", "image_url": IMAGE},
    ]
    assert prompt.startswith("Continue")


def test_client_tool_result_with_remote_image_is_rejected():
    with pytest.raises(ValueError, match="inline"):
        prepare_messages({"messages": [
            {"role": "user", "content": "Show tool image"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-1", "type": "function", "function": {
                    "name": "inspect", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": [{
                "type": "image_url", "image_url": {"url": "https://remote.invalid/secret"}}]},
        ]})


def test_early_app_server_dynamic_tool_request_is_buffered():
    class RacingAppServer(CodexAppServer):
        def __init__(self, workdir):
            super().__init__(RunnerConfig("codex", Path(workdir), "runner-secret"))
            self.messages = [
                {"id": 99, "method": "item/tool/call", "params": {
                    "threadId": "thread-test", "turnId": "turn-test",
                    "tool": "lookup", "callId": "call-early", "arguments": {}}},
                {"id": "1", "result": {"turn": {"id": "turn-test"}}},
            ]
            self.sent = []

        async def _send(self, message):
            self.sent.append(message)

        async def _read(self):
            return self.messages.pop(0)

    async def exercise():
        server = RacingAppServer(".")
        response = await server._request("turn/start", {"threadId": "thread-test", "input": []})
        assert response["turn"]["id"] == "turn-test"
        assert len(server.backlog) == 1
        assert server.backlog[0]["params"]["callId"] == "call-early"
        assert server.sent[0]["method"] == "turn/start"
    asyncio.run(exercise())


def test_image_in_client_tool_continuation_is_not_sent_twice(tmp_path):
    server = ScriptedAppServer(tmp_path)
    payload = {"model": "test-codex", "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "What is in this picture?"},
            {"type": "image_url", "image_url": {"url": IMAGE}}]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function", "function": {
                "name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "Synthetic result"},
    ]}
    asyncio.run(_consume(server, payload))
    history = next(params for method, params in server.calls
                   if method == "thread/inject_items")["items"]
    earlier = next(item for item in history if item.get("role") == "user")
    assert {"type": "input_image", "image_url": IMAGE} in earlier["content"]
    started = next(params for method, params in server.calls if method == "turn/start")
    assert started["input"] == [{
        "type": "text",
        "text": ("Continue the existing user turn using the supplied function results. Return the final "
                 "answer, or call another client-provided function if needed."),
    }]


def test_codex_does_not_forward_tool_calls_from_a_different_turn(tmp_path):
    class WrongTurnAppServer(ScriptedAppServer):
        async def _request(self, method, params):
            result = await super()._request(method, params)
            if method == "turn/start":
                self.notifications[0]["params"]["turnId"] = "another-turn"
            return result

    server = WrongTurnAppServer(tmp_path, emit_tool=True)
    with TestClient(create_runner_app(server.config, server)) as client:
        resp = client.post("/v1/chat/completions", headers=HEADERS, json={
            "model": "test-codex",
            "messages": [{"role": "user", "content": "Run lookup"}],
            "tools": [{"type": "function", "function": {
                "name": "lookup", "parameters": {"type": "object"}}}]})
        assert resp.status_code == 502
    assert server.process is None
