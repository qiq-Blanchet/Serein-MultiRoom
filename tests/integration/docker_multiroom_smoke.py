"""Real Docker M6 smoke: two disposable rooms, one real gateway and Dashboard.

Run only on an isolated CI runner with Docker and the locally built images:
    serein-multiroom:ci
    serein-multiroom-web:dev

Never mounts or reads any real VPS memory, credentials or project runtime.
All keys, example memories and upstream model IDs are generated inside a
TemporaryDirectory and deleted when this test finishes.
"""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from serein.core.store import Store
from serein.room_archive import create_room_backup, preview_room_backup, room_backups
from serein.room_restore import plan_room_restore, restore_room_backup
from serein.deployment import save_settings
from serein.room_admin import initialize_admin_key, save_dashboard_credentials
from serein.room_docker import DockerRuntime
from serein.room_manager import RoomManager

GATEWAY = "http://127.0.0.1:18219"
DASHBOARD = "http://127.0.0.1:18217"
USERNAME = "m6-test-only"
PASSWORD = "synthetic-m6-dashboard-password-not-for-production"


def request(url: str, *, token: str | None = None,
            basic: bool = False, headers: dict | None = None):
    labels = {"Accept": "application/json"}
    if token:
        labels["Authorization"] = "Bearer " + token
    if basic:
        data = base64.b64encode((USERNAME + ":" + PASSWORD).encode()).decode()
        labels["Authorization"] = "Basic " + data
    if headers:
        labels.update(headers)
    req = urllib.request.Request(url, headers=labels)
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


def wait_status(url: str, *, timeout: float = 90.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            status, content = request(url)
            if status == 200:
                return
            last = f"HTTP {status}: {content[:200]}"
        except (OSError, TimeoutError) as error:
            last = type(error).__name__
        time.sleep(1)
    raise AssertionError(f"Service not ready at {url}: {last}")


def seed_room(manager: RoomManager, room: dict, label: str):
    home = manager.room_dir(room["room"]["room_id"])
    with Store(home / "data" / "serein.db") as store:
        store.create(
            "scene-" + label, "scene", "CI private memory " + label,
            "M6_PRIVATE_CONTENT_ONLY_FOR_" + label,
        )
    save_settings(home / "data" / "serein.db", {"upstreams": [{
        "name": label.lower(),
        "base_url": "https://example.invalid/v1",
        "models": [label.lower() + "-only-model"],
    }]})


def verify_models(a_key: str, b_key: str):
    status_a, body_a = request(GATEWAY + "/v1/models", token=a_key)
    status_b, body_b = request(GATEWAY + "/v1/models", token=b_key)
    assert status_a == status_b == 200, (status_a, body_a[:250], status_b, body_b[:250])
    models_a = [record["id"] for record in json.loads(body_a)["data"]]
    models_b = [record["id"] for record in json.loads(body_b)["data"]]
    assert models_a == ["alpha/alpha-only-model"], models_a
    assert models_b == ["beta/beta-only-model"], models_b
    assert "beta" not in body_a and "alpha" not in body_b


async def verify_mcp(a_key: str, b_key: str):
    """Official MCP streamable HTTP client, over the *actual* Docker gateway."""
    async def probe(key, expected_tools, identifier, required, forbidden):
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + key},
            timeout=httpx.Timeout(45), trust_env=False,
        ) as client:
            async with streamable_http_client(
                GATEWAY + "/serein/mcp", http_client=client,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "Serein"
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    assert names == expected_tools, names
                    if "read_memory" in names:
                        own = await session.call_tool(
                            "read_memory", {"identifier": identifier},
                        )
                        assert not own.isError
                        assert required in str(own)
                        not_ours = await session.call_tool(
                            "read_memory", {"identifier": forbidden},
                        )
                        assert "M6_PRIVATE_CONTENT_ONLY_FOR_BETA" not in str(not_ours)
    await probe(a_key, {"read_memory"}, "scene-ALPHA",
                "M6_PRIVATE_CONTENT_ONLY_FOR_ALPHA", "scene-BETA")
    await probe(b_key, {"recall_memory"}, "scene-BETA", "", "scene-ALPHA")




def dashboard_http(path: str, *, method: str = "GET", cookie: str | None = None,
                   payload: dict | None = None, authenticated: bool = True,
                   extra_headers: dict | None = None):
    """Call the actual web Gateway, never the private control plane directly."""
    headers = {"Accept": "application/json"}
    if authenticated:
        encoded = base64.b64encode((USERNAME + ":" + PASSWORD).encode()).decode()
        headers["Authorization"] = "Basic " + encoded
    if cookie:
        headers["Cookie"] = cookie
    if extra_headers:
        headers.update(extra_headers)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(DASHBOARD + path, data=body,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=100) as response:
            return (response.status,
                    response.read().decode("utf-8", errors="replace"),
                    response.headers.get("Set-Cookie"))
    except urllib.error.HTTPError as error:
        return (error.code, error.read().decode("utf-8", errors="replace"),
                error.headers.get("Set-Cookie"))


async def verify_mcp_lists(a_key: str, b_key: str):
    async def read_tools(key):
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + key},
            timeout=httpx.Timeout(45), trust_env=False,
        ) as client:
            async with streamable_http_client(
                GATEWAY + "/serein/mcp", http_client=client,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return {tool.name for tool in (await session.list_tools()).tools}
    assert await read_tools(a_key) == {"read_memory", "recall_memory"}
    assert await read_tools(b_key) == {"recall_memory"}


def verify_live_dashboard(manager: RoomManager, runtime: DockerRuntime,
                          aid: str, bid: str, a_key: str, b_key: str):
    """M6 second phase: authenticated, real-Docker room-scoped Dashboard writes."""
    print("M6 Dashboard: checking login, room selection and forged headers", flush=True)
    status, _, _ = dashboard_http("/__serein/rooms", authenticated=False)
    assert status == 401, status
    status, _, _ = dashboard_http("/__serein/settings", authenticated=False)
    assert status == 401, status
    status, _, _ = dashboard_http("/__serein/rooms/select", method="POST",
                                  payload={"room_id": aid}, authenticated=False)
    assert status == 401, status
    status, _, _ = dashboard_http("/__serein/rooms/select", method="POST",
                                  payload={"room_id": aid},
                                  extra_headers={"Origin": "https://cross-site.invalid"})
    assert status == 403, status
    status, _, _ = dashboard_http("/__serein/rooms/select", method="POST",
                                  payload={"room_id": "f" * 32})
    assert status == 409, status
    status, _, _ = dashboard_http("/__serein/rooms", method="POST",
                                  payload={"room_slug": "../invalid"})
    assert status == 400, status
    status, _, cookie_a = dashboard_http("/__serein/rooms/select", method="POST",
                                         payload={"room_id": aid})
    assert status == 200 and cookie_a and "HttpOnly" in cookie_a, (status, cookie_a)
    cookie_a = cookie_a.split(";", 1)[0]
    status, _, cookie_b = dashboard_http("/__serein/rooms/select", method="POST",
                                         payload={"room_id": bid}, cookie=cookie_a)
    assert status == 200 and cookie_b and "HttpOnly" in cookie_b, (status, cookie_b)
    cookie_b = cookie_b.split(";", 1)[0]
    for cookie, expected in ((cookie_a, aid), (cookie_b, bid)):
        status, body, _ = dashboard_http("/__serein/rooms/current", cookie=cookie)
        assert status == 200 and json.loads(body)["room_id"] == expected, (status, body)
        status, body, _ = dashboard_http("/__serein/rooms", cookie=cookie)
        assert status == 200 and json.loads(body)["current"] == expected, (status, body)
        assert "gateway_key" not in body.lower()
        assert "internal_token" not in body.lower()

    print("M6 Dashboard: reading and saving model/feature/summary settings", flush=True)
    status, a_body, _ = dashboard_http("/__serein/settings", cookie=cookie_a)
    assert status == 200, (status, a_body[:300])
    status, b_body, _ = dashboard_http("/__serein/settings", cookie=cookie_b)
    assert status == 200, (status, b_body[:300])
    assert "alpha-only-model" in a_body and "beta-only-model" not in a_body
    assert "beta-only-model" in b_body and "alpha-only-model" not in b_body
    a_before, b_before = json.loads(a_body), json.loads(b_body)
    a_model = next(item["id"] for item in a_before["available_models"]
                   if "alpha-only-model" in item["id"])
    assert a_before["features"]["memos"] is False
    assert b_before["features"]["memos"] is False
    assert b_before["pipeline"]["auto_enabled"] is True
    patch = {"expected_version": a_before["settings_version"],
             "identity": {"ai_name": "CI Dashboard Alpha"},
             "features": {"memos": True},
             "assignments": {"event_writer": a_model},
             "pipeline": {"auto_enabled": False, "timeout_seconds": 601}}
    status, updated_body, _ = dashboard_http("/__serein/settings", method="PATCH",
                                              cookie=cookie_a, payload=patch,
                                              extra_headers={"X-Serein-Target-Room": aid})
    assert status == 200, (status, updated_body[:450])
    a_updated = json.loads(updated_body)
    assert a_updated["identity"]["ai_name"] == "CI Dashboard Alpha"
    assert a_updated["features"]["memos"] is True
    assert a_updated["assignments"]["event_writer"] == a_model
    assert a_updated["pipeline"]["auto_enabled"] is False
    assert a_updated["pipeline"]["timeout_seconds"] == 601
    assert a_updated["settings_version"] > a_before["settings_version"]
    status, body, _ = dashboard_http("/__serein/settings", cookie=cookie_b)
    assert status == 200, (status, body[:250])
    b_after = json.loads(body)
    assert b_after["identity"] == b_before["identity"]
    assert b_after["features"]["memos"] is False
    assert b_after["pipeline"]["auto_enabled"] is True
    assert b_after["settings_version"] == b_before["settings_version"]
    assert b_after["assignments"].get("event_writer") != a_model
    status, body, _ = dashboard_http("/__serein/settings", method="PATCH",
                                      cookie=cookie_a, payload=patch,
                                      extra_headers={"X-Serein-Target-Room": aid})
    # Original settings API deliberately maps stale-version conflicts to HTTP 400.
    assert status == 400 and "设置已在其他页面更新" in body, (status, body[:250])
    status, body, _ = dashboard_http(
        "/__serein/settings", cookie=cookie_a,
        extra_headers={"X-Serein-Verified-Room": bid,
                       "X-Serein-Verified-Proof": "forged"})
    assert status == 200 and json.loads(body)["identity"]["ai_name"] == "CI Dashboard Alpha"
    for cookie in (cookie_a, cookie_b):
        status, body, _ = dashboard_http("/__serein/pipeline/status", cookie=cookie)
        assert status == 200 and isinstance(json.loads(body), dict), (status, body[:300])

    # Simulate an old A tab after a different tab switched the signed cookie to B.
    # The stale header is not authority; it can only veto a dangerous write.
    status, body, _ = dashboard_http(
        "/__serein/settings", method="PATCH", cookie=cookie_b,
        payload={"expected_version": b_before["settings_version"],
                 "identity": {"ai_name": "MUST_NOT_TOUCH_B"}},
        extra_headers={"X-Serein-Target-Room": aid})
    assert status == 409, (status, body[:250])
    status, body, _ = dashboard_http(
        "/__serein/pipeline/next", method="POST", cookie=cookie_b,
        payload={}, extra_headers={"X-Serein-Target-Room": aid})
    assert status == 409, (status, body[:250])
    status, body, _ = dashboard_http("/__serein/settings", cookie=cookie_b)
    assert status == 200 and json.loads(body)["identity"] == b_before["identity"]
    # A valid, selected A tab may read its backup list, but not B's.
    status, body, _ = dashboard_http(
        "/__serein/rooms/" + aid + "/backups", cookie=cookie_a,
        extra_headers={"X-Serein-Target-Room": aid})
    assert status == 200 and isinstance(json.loads(body), dict), (status, body[:250])
    status, body, _ = dashboard_http(
        "/__serein/rooms/" + bid + "/backups", cookie=cookie_a,
        extra_headers={"X-Serein-Target-Room": aid})
    assert status == 409, (status, body[:250])

    print("M6 Dashboard: checking per-room MCP administrator access", flush=True)
    status, body, _ = dashboard_http(
        "/__serein/rooms/" + bid + "/mcp-tools", cookie=cookie_a)
    assert status == 403, (status, body)
    status, body, _ = dashboard_http(
        "/__serein/rooms/" + aid + "/mcp-tools", method="POST",
        cookie=cookie_b, payload={"mode": "custom", "tools": []})
    assert status == 403, (status, body)
    for cookie, room_id, expected in (
        (cookie_a, aid, ["read_memory"]),
        (cookie_b, bid, ["recall_memory"]),
    ):
        status, body, _ = dashboard_http(
            "/__serein/rooms/" + room_id + "/mcp-tools", cookie=cookie)
        assert status == 200 and json.loads(body)["tools"] == expected, (status, body)

    print("M6 Dashboard: changing A MCP permission restarts A only", flush=True)
    status, body, _ = dashboard_http(
        "/__serein/rooms/" + aid + "/mcp-tools", method="POST",
        cookie=cookie_a,
        payload={"mode": "custom", "tools": ["read_memory", "recall_memory"]})
    assert status == 200, (status, body[:350])
    assert manager.get(aid)["state"] == manager.get(bid)["state"] == "ready"
    assert manager.mcp_permissions(aid)["tools"] == ["read_memory", "recall_memory"]
    assert manager.mcp_permissions(bid)["tools"] == ["recall_memory"]
    asyncio.run(verify_mcp_lists(a_key, b_key))
    verify_models(a_key, b_key)

    print("M6 Dashboard: stopped selected AI cannot fall through to B", flush=True)
    manager.stop(aid, runtime)
    status, body, _ = dashboard_http(
        "/", cookie=cookie_a, extra_headers={"Accept": "text/html"})
    assert status == 503 and "当前 AI 房间暂时未运行" in body, (status, body[:350])
    status, body, _ = dashboard_http("/__serein/rooms/current", cookie=cookie_a)
    assert status == 200 and json.loads(body)["room_id"] is None, (status, body)
    status, body, _ = dashboard_http(
        "/__serein/rooms/select", method="POST", cookie=cookie_b,
        payload={"room_id": aid})
    assert status == 409, (status, body)
    assert request(GATEWAY + "/v1/models", token=b_key)[0] == 200
    status, body, cleared = dashboard_http(
        "/__serein/rooms/reset", method="POST", cookie=cookie_a, payload={})
    assert status == 200 and json.loads(body)["cleared"] and "Max-Age=0" in cleared
    status, body, _ = dashboard_http("/__serein/rooms/current")
    assert status == 200 and json.loads(body)["room_id"] == bid, (status, body)
    manager.start(aid, runtime, wait_seconds=150)
    status, body, _ = dashboard_http("/__serein/settings", cookie=cookie_a)
    assert status == 200, (status, body[:300])
    assert json.loads(body)["identity"]["ai_name"] == "CI Dashboard Alpha"
    verify_models(a_key, b_key)


class SyntheticChatUpstream:
    """Local-only, disposable OpenAI-compatible fake LLM; no paid provider calls."""

    def __init__(self, private_bridge_ip: str):
        owner = self
        self.calls = []
        self.lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "-1"))
                    if not 0 < size <= 100_000:
                        raise ValueError("Invalid synthetic request size")
                    payload = json.loads(self.rfile.read(size))
                    model = payload["model"]
                    if model not in ("alpha-only-model", "beta-only-model"):
                        raise ValueError("Unrecognized synthetic model")
                    label = "ALPHA" if model.startswith("alpha") else "BETA"
                    with owner.lock:
                        owner.calls.append({"label": label, "messages": payload["messages"]})
                    reply = {
                        "id": "chatcmpl-m6-local-" + label.lower(),
                        "object": "chat.completion",
                        "created": 1794650000,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "M6_SYNTHETIC_MODEL_REPLY_" + label,
                            },
                            "finish_reason": "stop",
                        }],
                        "usage": {"prompt_tokens": 8, "completion_tokens": 5,
                                  "total_tokens": 13},
                    }
                    encoded = json.dumps(reply).encode()
                    self.send_response(200)
                except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                    encoded = b'{"error":"invalid CI model request"}'
                    self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format, *args):
                # User prompts must never be copied into runner output.
                pass

        self.server = ThreadingHTTPServer((private_bridge_ip, 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="m6-local-fake-llm", daemon=True,
        )
        self.base_url = f"http://{private_bridge_ip}:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def chat_completion(key: str, label: str, number: int):
    body = {
        "model": label.lower() + "/" + label.lower() + "-only-model",
        "messages": [{"role": "user",
                      "content": f"M6_SYNTHETIC_PRIVATE_USER_{label}_{number}"}],
        "stream": False,
        "serein": {"memory": False, "window_id": f"ci-{label.lower()}-{number}"},
    }
    encoded = json.dumps(body).encode()
    req = urllib.request.Request(
        GATEWAY + "/v1/chat/completions", data=encoded, method="POST",
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")


def verify_chat_load(manager: RoomManager, runtime: DockerRuntime,
                     aid: str, bid: str, a_key: str, b_key: str):
    """Real two-room HTTP chat -> local mock model -> durable original archive."""
    bridge_ip = runtime.network_gateway_ip(manager)
    with SyntheticChatUpstream(bridge_ip) as fake:
        for room_id, label in ((aid, "ALPHA"), (bid, "BETA")):
            db = manager.room_dir(room_id) / "data" / "serein.db"
            save_settings(db, {"upstreams": [{
                "name": label.lower(),
                "base_url": fake.base_url,
                "models": [label.lower() + "-only-model"],
            }]})
        print("M6 Phase3: concurrent real chat requests through mock LLM", flush=True)
        jobs = [(a_key, "ALPHA", index) for index in range(5)]
        jobs += [(b_key, "BETA", index) for index in range(5)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            outcomes = list(executor.map(lambda args: (*args, chat_completion(*args)),
                                         jobs))
        for key, label, number, (status, raw) in outcomes:
            assert status == 200, (label, number, status, raw[:300])
            response = json.loads(raw)
            answer = response["choices"][0]["message"]["content"]
            assert answer == "M6_SYNTHETIC_MODEL_REPLY_" + label, (label, answer)
            assert response["model"] == label.lower() + "-only-model", response
        with fake.lock:
            received = list(fake.calls)
        assert sum(item["label"] == "ALPHA" for item in received) == 5
        assert sum(item["label"] == "BETA" for item in received) == 5
        for entry in received:
            opposite = "BETA" if entry["label"] == "ALPHA" else "ALPHA"
            assert "M6_SYNTHETIC_PRIVATE_USER_" + opposite not in json.dumps(
                entry["messages"]), "Private cross-room user input forwarded"

        print("M6 Phase3: read archived user and assistant turns from each DB", flush=True)
        for room_id, label in ((aid, "ALPHA"), (bid, "BETA")):
            db = manager.room_dir(room_id) / "data" / "serein.db"
            with Store(db, read_only=True) as store:
                rows = [dict(row) for row in store.conn.execute(
                    "SELECT role,text FROM raw_events WHERE source='serein_chat'")]
            users = [r["text"] for r in rows if r["role"] == "user"]
            answers = [r["text"] for r in rows if r["role"] == "assistant"]
            assert len(users) == len(answers) == 5, (label, users, answers)
            assert set(users) == {
                f"M6_SYNTHETIC_PRIVATE_USER_{label}_{i}" for i in range(5)
            }
            assert set(answers) == {"M6_SYNTHETIC_MODEL_REPLY_" + label}
            opposite = "BETA" if label == "ALPHA" else "ALPHA"
            assert opposite not in json.dumps(rows)

        print("M6 Phase3: crash A application process, Docker restarts A; B stays live",
              flush=True)
        room_a = manager.get(aid)
        before = runtime.inspect(manager, room_a)
        initial_restarts = before.get("RestartCount", 0)
        # docker kill is an explicit administrative stop and intentionally
        # suppresses Docker restart policies. Kill only the Serein Python
        # service *inside* the container, letting PID 1 (--init) exit normally
        # when its child dies. Docker should then restart the failed container.
        synthetic_crash = (
            "import os,glob,signal;"
            "me=os.getpid();"
            "targets=[int(path.split('/')[2]) for path in glob.glob('/proc/[0-9]*/cmdline')"
            " if int(path.split('/')[2]) not in (1,me)"
            " and b'serein.launch' in open(path,'rb').read()];"
            "assert len(targets)==1,targets;"
            "os.kill(targets[0],signal.SIGKILL)"
        )
        killed = subprocess.run(
            ["docker", "exec", runtime.container_name(manager, room_a),
             "python", "-c", synthetic_crash],
            capture_output=True, text=True, timeout=20, check=False,
        )
        assert killed.returncode in (0, 137), (
            killed.returncode, killed.stderr[-300:])
        # A sudden single-container crash is NOT a Docker-host or OS reboot.
        assert request(GATEWAY + "/v1/models", token=b_key)[0] == 200
        deadline = time.monotonic() + 120
        after = None
        while time.monotonic() < deadline:
            after = runtime.inspect(manager, room_a)
            state = (after or {}).get("State") or {}
            health = (state.get("Health") or {}).get("Status")
            if (after and after.get("RestartCount", 0) > initial_restarts
                    and state.get("Running") and health == "healthy"):
                break
            time.sleep(2)
        else:
            raise AssertionError(
                "A did not auto-restart after an unexpected Python process crash: "
                + str({"status": (after or {}).get("State", {}).get("Status"),
                       "restarts": (after or {}).get("RestartCount")}))
        assert request(GATEWAY + "/v1/models", token=b_key)[0] == 200
        verify_models(a_key, b_key)
        status, raw = chat_completion(a_key, "ALPHA", 99)
        assert status == 200, (status, raw[:250])
        assert "M6_SYNTHETIC_MODEL_REPLY_ALPHA" in raw
        for room_id, label, expected_count in ((aid, "ALPHA", 6),
                                               (bid, "BETA", 5)):
            db = manager.room_dir(room_id) / "data" / "serein.db"
            with Store(db, read_only=True) as store:
                rows = [dict(row) for row in store.conn.execute(
                    "SELECT role,text FROM raw_events WHERE source='serein_chat'")]
            assert len(rows) == expected_count * 2, (label, len(rows))
            opposite = "BETA" if label == "ALPHA" else "ALPHA"
            assert opposite not in json.dumps(rows)
        print("M6 Phase3 PASS: fake upstream chats, durable archives and crash recovery",
              flush=True)


def run():
    assert shutil.which("docker"), "Docker executable is required"
    subprocess.run(["docker", "info"], check=True, capture_output=True)
    with tempfile.TemporaryDirectory(prefix="serein-m6-docker-") as temporary:
        root = Path(temporary) / "new-installation-only"
        manager = RoomManager(root, image="serein-multiroom:ci")
        runtime = DockerRuntime(memory="512m", cpus="1.0")
        admin = None
        containers = []
        try:
            manager.initialize()
            a = manager.create("Synthetic Alpha", room_slug="xiaogi-main", source="blank")
            b = manager.create("Synthetic Beta", room_slug="other-main", source="blank")
            aid, bid = a["room"]["room_id"], b["room"]["room_id"]
            assert aid != bid and a["gateway_key"] != b["gateway_key"]
            containers = [
                runtime.container_name(manager, a["room"]),
                runtime.container_name(manager, b["room"]),
                runtime.gateway_name(manager),
                runtime.dashboard_name(manager),
            ]
            seed_room(manager, a, "ALPHA")
            seed_room(manager, b, "BETA")
            manager.set_mcp_permissions(aid, ["read_memory"])
            manager.set_mcp_permissions(bid, ["recall_memory"])
            initialize_admin_key(manager)
            save_dashboard_credentials(manager, USERNAME, PASSWORD)

            print("M6 Docker: starting two independent Serein memory containers", flush=True)
            manager.start(aid, runtime, wait_seconds=150)
            manager.start(bid, runtime, wait_seconds=150)
            runtime.start_gateway(manager, port=18219)
            wait_status(GATEWAY + "/health")
            assert request(GATEWAY + "/v1/models")[0] == 401
            assert request(GATEWAY + "/v1/models", token="invalid-ci-key")[0] == 401
            verify_models(a["gateway_key"], b["gateway_key"])

            # Window-ID is an original window label; Gateway Key is the room identity.
            assert request(
                GATEWAY + "/v1/models", token=a["gateway_key"],
                headers={"X-Serein-Window-ID": "other-main"},
            )[0] == 200
            assert request(
                GATEWAY + "/v1/models", token=a["gateway_key"],
                headers={"X-Serein-Room-ID": "other-main"},
            )[0] == 403

            print("M6 Docker: testing official MCP client over real gateway", flush=True)
            asyncio.run(verify_mcp(a["gateway_key"], b["gateway_key"]))

            print("M6 Docker: checking A stopped does not affect B", flush=True)
            manager.stop(aid, runtime)
            assert request(GATEWAY + "/v1/models", token=a["gateway_key"])[0] == 401
            assert request(GATEWAY + "/v1/models", token=b["gateway_key"])[0] == 200
            manager.start(aid, runtime, wait_seconds=150)
            assert (manager.room_dir(aid) / "secrets" / "gateway-key"
                    ).read_text().strip() == a["gateway_key"]
            verify_models(a["gateway_key"], b["gateway_key"])

            print("M6 Docker: verifying real full-room backup and rollback restore", flush=True)
            archive = create_room_backup(manager, aid, runtime)
            assert archive["room_slug"] == "xiaogi-main"
            assert archive["restarted"] is True
            assert preview_room_backup(manager, aid, archive["name"])["verified"] is True
            assert room_backups(manager, bid) == []
            manager.stop(aid, runtime)
            with Store(manager.room_dir(aid) / "data" / "serein.db") as store:
                store.create("scene-POST_BACKUP", "scene", "CI transient memory",
                             "M6_TEMPORARY_MEMORY_TO_BE_ROLLED_BACK")
            manager.start(aid, runtime, wait_seconds=150)
            plan = plan_room_restore(manager, aid, archive["name"])
            assert plan["schema_compatible"] and plan["restore_available"], plan
            restored = restore_room_backup(
                manager, aid, archive["name"], runtime,
                confirm_slug="xiaogi-main", expected_sha256=plan["archive_sha256"],
            )
            assert restored["restored"] and restored["restarted"]
            assert restored["gateway_key_unchanged"] is True
            assert restored["rollback_backup"] != archive["name"]
            with Store(manager.room_dir(aid) / "data" / "serein.db") as store:
                assert store.conn.execute(
                    "SELECT 1 FROM documents WHERE id=?", ("scene-ALPHA",)
                ).fetchone()
                assert not store.conn.execute(
                    "SELECT 1 FROM documents WHERE id=?", ("scene-POST_BACKUP",)
                ).fetchone()
            with Store(manager.room_dir(bid) / "data" / "serein.db") as store:
                assert store.conn.execute(
                    "SELECT 1 FROM documents WHERE id=?", ("scene-BETA",)
                ).fetchone()
            assert (manager.room_dir(aid) / "secrets" / "gateway-key"
                    ).read_text().strip() == a["gateway_key"]
            verify_models(a["gateway_key"], b["gateway_key"])

            print("M6 Docker: starting private web administrator and Dashboard", flush=True)
            with (Path(temporary) / "admin.log").open("wb") as log:
                admin = subprocess.Popen([
                    sys.executable, "scripts/rooms.py", "--root", str(root),
                    "--image", manager.image, "admin-serve",
                    "--docker-network", "--port", "18220",
                ], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                time.sleep(2)
                assert admin.poll() is None, "Room admin service failed to start"
                runtime.start_dashboard(manager, port=18217)
                wait_status(DASHBOARD + "/ready", timeout=120)
                assert request(DASHBOARD + "/")[0] == 401
                status, page = request(DASHBOARD + "/", basic=True,
                                       headers={"Accept": "text/html"})
                assert status == 200 and "html" in page.lower(), (status, page[:350])
                status, data = request(DASHBOARD + "/__serein/rooms", basic=True)
                assert status == 200, (status, data[:300])
                listing = json.loads(data)
                rooms = listing["rooms"] if isinstance(listing, dict) else listing
                assert {room["room_slug"] for room in rooms} == {
                    "xiaogi-main", "other-main",
                }
                assert "gateway_key" not in data.lower()
                assert manager.get(aid)["state"] == manager.get(bid)["state"] == "ready"
                verify_live_dashboard(manager, runtime, aid, bid,
                                      a["gateway_key"], b["gateway_key"])
                verify_chat_load(manager, runtime, aid, bid,
                                 a["gateway_key"], b["gateway_key"])
            print("M6 Docker PASS: real rooms, MCP, Gateway and Dashboard isolated", flush=True)
        except BaseException:
            print("M6 Docker diagnostic: status and tail of synthetic test containers", flush=True)
            for name in containers:
                state = subprocess.run(
                    ["docker", "container", "inspect", "--format",
                     "{{.State.Status}} exit={{.State.ExitCode}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                     name],
                    capture_output=True, text=True, check=False,
                )
                print(f"[{name}] {state.stdout.strip() or 'not created'}", flush=True)
                logs = subprocess.run(
                    ["docker", "container", "logs", "--tail", "55", name],
                    capture_output=True, text=True, check=False,
                )
                if logs.stdout or logs.stderr:
                    # This CI generates synthetic keys, dummy models, and only
                    # synthetic memory; it never mounts any user data.
                    print((logs.stdout + logs.stderr)[-4500:], flush=True)
            raise
        finally:
            if admin is not None:
                admin.terminate()
                try:
                    admin.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    admin.kill()
                    admin.wait(timeout=5)
            for name in containers:
                subprocess.run(
                    ["docker", "container", "rm", "-f", name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False,
                )
            subprocess.run(
                ["docker", "network", "rm", manager.network],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False,
            )


if __name__ == "__main__":
    run()
