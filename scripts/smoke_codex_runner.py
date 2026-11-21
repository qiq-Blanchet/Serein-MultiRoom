"""Live, read-only acceptance check for an already running Serein Codex Runner.

Never reads, prints or persists Codex OAuth credentials. Uses only the Runner's
loopback bearer token; the script's synthetic client tool never accesses files
or external APIs. Run this manually under the Runner machine's user account.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit


def get_json(url: str, token: str) -> dict:
    request = Request(url, headers={"Authorization": "Bearer " + token})
    with urlopen(request, timeout=40) as response:
        return json.load(response)


def post_json(url: str, token: str, body: dict, timeout: int = 300) -> dict:
    request = Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def post_sse(url: str, token: str, body: dict, timeout: int = 300) -> list[dict]:
    request = Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/json"}, method="POST")
    events = []
    finished = False
    with urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                finished = True
                break
            item = json.loads(data)
            if item.get("error"):
                raise RuntimeError("Upstream reported a streaming error")
            events.append(item)
    if not finished:
        raise RuntimeError("Stream ended before [DONE]")
    return events


def text_of(reply: dict) -> str:
    choices = reply.get("choices") or []
    if not choices:
        raise RuntimeError("The model returned no choices")
    message = choices[0].get("message") or {}
    return str(message.get("content") or "").strip()


def verify_model(base: str, token: str, model: str) -> None:
    reply = post_json(base + "/chat/completions", token, {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the word READY."}],
        "stream": False,
    })
    if not text_of(reply):
        raise RuntimeError("Model returned an empty normal reply")
    print("PASS: authenticated model inference")


def verify_stream(base: str, token: str, model: str) -> None:
    events = post_sse(base + "/chat/completions", token, {
        "model": model,
        "messages": [{"role": "user", "content": "Reply briefly with STREAM_OK."}],
        "stream": True,
        "stream_options": {"include_usage": True},
    })
    chunks = [choice.get("delta", {}).get("content", "")
              for event in events for choice in event.get("choices", [])]
    if not "".join(chunks).strip():
        raise RuntimeError("No text arrived in the streaming response")
    if not any(choice.get("finish_reason") == "stop"
               for event in events for choice in event.get("choices", [])):
        raise RuntimeError("Stream did not finish normally")
    usage = any(isinstance(event.get("usage"), dict) for event in events)
    print("PASS: streaming chat" + (" (with usage)" if usage else " (usage not reported)"))


def verify_client_tool(base: str, token: str, model: str) -> None:
    marker = "CLIENT_TOOL_RESULT_7294"
    messages = [{"role": "user", "content":
        "Call the lookup tool to get the exact verification marker. "
        "Then repeat the marker in your final answer. Do not guess the marker."}]
    tool = {"type": "function", "function": {
        "name": "lookup", "description": "Return a local synthetic verification marker. "
        "You must call this tool to answer correctly.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
    for attempt in range(3):
        reply = post_json(base + "/chat/completions", token, {
            "model": model, "messages": messages, "tools": [tool], "tool_choice": "auto",
            "parallel_tool_calls": False})
        choice = reply["choices"][0]
        message = choice["message"]
        if choice.get("finish_reason") == "tool_calls":
            calls = message.get("tool_calls") or []
            if not calls or len(calls) > 1:
                raise RuntimeError("Expected one client-executed tool call per response")
            call = calls[0]
            if call.get("function", {}).get("name") != "lookup" or not call.get("id"):
                raise RuntimeError("Tool call lost its name or call ID")
            messages.append({"role": "assistant", "content": message.get("content"),
                             "tool_calls": calls})
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": marker})
            continue
        if marker not in text_of(reply):
            raise RuntimeError("Tool continuation did not return the client-side tool result")
        print("PASS: external client tool call and role:tool continuation")
        return
    raise RuntimeError("Tool continuation did not reach a final response in three attempts")


def verify_image(base: str, token: str, model: str, image_path: Path) -> None:
    if not image_path.is_file():
        raise ValueError("Image path does not exist")
    image = image_path.read_bytes()
    if len(image) > 10_000_000:
        raise ValueError("Use an image smaller than 10 MB for this smoke test")
    mime, _ = mimetypes.guess_type(str(image_path))
    if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        raise ValueError("Use a PNG, JPEG, WEBP or GIF for this test")
    url = "data:" + mime + ";base64," + base64.b64encode(image).decode("ascii")
    reply = post_json(base + "/chat/completions", token, {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Describe the visible subject briefly."},
            {"type": "image_url", "image_url": {"url": url}}]}]})
    if not text_of(reply):
        raise RuntimeError("Vision model returned an empty answer")
    print("PASS: inline image request accepted (judge answer accuracy manually)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("SEREIN_CODEX_RUNNER_URL",
        "http://127.0.0.1:4501/v1"), help="Runner /v1 URL, never the public Serein Gateway")
    parser.add_argument("--model", default=os.environ.get("SEREIN_CODEX_TEST_MODEL", ""),
                        help="Model returned by the authenticated runner")
    parser.add_argument("--image", type=Path, help="Optional local test image")
    args = parser.parse_args()
    url = urlsplit(args.base)
    if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "::1", "localhost")
            or url.username or url.password or url.query or url.fragment
            or not url.port or url.path not in ("", "/", "/v1", "/v1/")):
        raise ValueError("This smoke test only connects to a loopback Runner /v1 endpoint")
    token = os.environ.get("SEREIN_CODEX_RUNNER_TOKEN", "")
    if not token:
        raise ValueError("Set SEREIN_CODEX_RUNNER_TOKEN in this process environment")
    base = args.base.rstrip("/")
    catalog = get_json(base + "/models", token)
    ids = [row["id"] for row in catalog.get("data", [])
           if isinstance(row, dict) and row.get("id")]
    if not ids:
        raise RuntimeError("No models returned; check Codex App Server login")
    model = args.model or ids[0]
    if model not in ids:
        raise ValueError("Selected model is not listed by the Runner")
    print("Testing Codex model:", model)
    verify_model(base, token, model)
    verify_stream(base, token, model)
    verify_client_tool(base, token, model)
    if args.image:
        verify_image(base, token, model, args.image)
    print("Acceptance checks finished; model entitlement is verified only for this run.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (HTTPError, URLError) as exc:
        # Don't print upstream responses because they can contain private inputs.
        print("FAIL: network/authentication error", type(exc).__name__,
              getattr(exc, "code", ""), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print("FAIL:", type(exc).__name__, str(exc), file=sys.stderr)
        sys.exit(1)
