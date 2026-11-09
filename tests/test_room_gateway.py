"""Synthetic M2 gateway authorization / routing tests; no VPS services accessed."""
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from serein.room_manager import RoomManager, RoomError
from serein.room_docker import DockerRuntime
from serein.room_gateway import create_app


@pytest.fixture
def rooms(tmp_path):
    manager = RoomManager(tmp_path / "test-root")
    first = manager.create("Memory A", source="blank")
    second = manager.create("Memory B", source="blank")
    # Unit test drives the registry directly; production uses Docker health.
    manager._set_state(first["room"]["room_id"], "ready")
    manager._set_state(second["room"]["room_id"], "ready")
    return manager, first, second


def fake_transport(manager, calls):
    tokens = {}
    for room in manager.list_rooms():
        token = (manager.room_dir(room["room_id"]) / "secrets" /
                 "api-token").read_text(encoding="utf-8").strip()
        tokens[token] = room["room_id"]

    async def handle(request):
        auth = request.headers.get("authorization")
        assert auth and auth.startswith("Bearer ")
        token = auth[7:]
        assert token in tokens, "Backend received an unrecognized internal token"
        room_id = tokens[token]
        assert request.url.host == "room-" + room_id
        assert request.url.port == 8011
        # These values never belong to untrusted users' routing controls.
        assert request.headers.get("x-serein-room-id") is None
        assert request.headers.get("x-forwarded-for") is None
        assert request.headers.get("x-real-ip") is None
        data = await request.aread()
        calls.append((room_id, request.url.path, request.method, request.headers, data))
        if request.url.path in ("/mcp", "/serein/mcp"):
            sse = 'event: message\n' + 'data: {"result":"private-mcp"}\n\n'
            return httpx.Response(200, headers={
                "Content-Type": "text/event-stream", "Mcp-Session-Id": "session-" + room_id,
            }, stream=httpx.ByteStream(sse.encode("utf-8")))
        payload = {
            "room": room_id,
            "choices": [{"message": {"role": "assistant", "content": "reply-" + room_id}}],
        }
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(json.dumps(payload).encode("utf-8")))
    return httpx.MockTransport(handle)


def client_for(rooms):
    manager, _, _ = rooms
    calls = []
    return TestClient(create_app(manager.gateway_routes,
                                 transport=fake_transport(manager, calls))), calls


def auth(key):
    return {"Authorization": "Bearer " + key}


def test_two_keys_route_chat_to_distinct_rooms(rooms):
    manager, first, second = rooms
    client, calls = client_for(rooms)
    for room in (first, second):
        key = room["gateway_key"]
        room_id = room["room"]["room_id"]
        window_id = room["room"]["room_slug"]
        result = client.post("/v1/chat/completions", headers={
            **auth(key), "X-Serein-Window-ID": window_id,
            "X-Forwarded-Host": "evil.example", "X-Real-IP": "evil",
        }, json={
            "model": "a-model",
            "room_id": room_id,
            "messages": [{"role": "user", "content": "synthetic"}],
        })
        assert result.status_code == 200, result.text
        assert result.json()["room"] == room_id
    assert calls[0][0] != calls[1][0]
    assert calls[0][3].get("x-serein-window-id") == first["room"]["room_slug"]
    assert calls[1][3].get("x-serein-window-id") == second["room"]["room_slug"]
    assert calls[0][3].get("x-serein-window-id") != calls[1][3].get("x-serein-window-id")
    assert all(call[3].get("authorization") != "Bearer " + first["gateway_key"] for call in calls)
    assert manager.resolve_gateway_key(first["gateway_key"]) == first["room"]["room_id"]


def test_api_model_listing_does_not_cross_key_boundary(rooms):
    _, first, second = rooms
    client, calls = client_for(rooms)
    a = client.get("/v1/models", headers=auth(first["gateway_key"]))
    b = client.get("/v1/models", headers=auth(second["gateway_key"]))
    assert a.status_code == b.status_code == 200
    assert a.json()["room"] == first["room"]["room_id"]
    assert b.json()["room"] == second["room"]["room_id"]
    assert len(calls) == 2


def test_mismatched_room_hints_and_missing_key_fail_before_upstream(rooms):
    _, first, second = rooms
    client, calls = client_for(rooms)
    a = first["gateway_key"]
    b_id = second["room"]["room_id"]
    for headers, data in [
        ({**auth(a), "X-Serein-Room-ID": b_id},
         {"messages": [{"role": "user", "content": "hello"}]}),
        (auth(a), {"room_id": b_id, "messages": [{"role": "user", "content": "hello"}]}),
        (auth(a), {"serein": {"room_id": b_id},
                   "messages": [{"role": "user", "content": "hello"}]}),
    ]:
        result = client.post("/v1/chat/completions", headers=headers, json=data)
        assert result.status_code == 403
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers=auth("not-a-valid-key")).status_code == 401
    assert len(calls) == 0


def test_gateway_blocks_admin_and_oauth_endpoints_even_with_valid_key(rooms):
    _, first, _ = rooms
    client, calls = client_for(rooms)
    for path in ("/v1/settings", "/token", "/authorize", "/register",
                 "/.well-known/oauth-protected-resource/mcp",
                 "/rooms/other/v1/chat/completions", "/health/other", "/"):
        result = client.get(path, headers=auth(first["gateway_key"]))
        assert result.status_code == 404, (path, result.text)
    assert client.get("/health").json() == {"status": "ok"}
    assert not calls


def test_mcp_routes_are_authenticated_and_preserve_room_session(rooms):
    _, first, second = rooms
    client, calls = client_for(rooms)
    for room in (first, second):
        room_id = room["room"]["room_id"]
        result = client.post("/serein/mcp", headers={
            **auth(room["gateway_key"]), "Origin": "http://testserver",
            "Host": "testserver", "Mcp-Session-Id": "client-session",
            "X-Forwarded-Proto": "https",
        }, json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        assert result.status_code == 200, result.text
        assert result.headers["Mcp-Session-Id"] == "session-" + room_id
        assert calls[-1][0] == room_id
        assert calls[-1][3]["Mcp-Session-Id"] == "client-session"
        assert calls[-1][3]["X-Forwarded-Proto"] == "http"
    assert client.post("/mcp", headers={
        **auth(first["gateway_key"]), "Origin": "https://evil.example",
    }, json={"jsonrpc": "2.0"}).status_code == 403
    assert client.post("/mcp", json={"jsonrpc": "2.0"}).status_code == 401
    assert len(calls) == 2


def test_stop_revokes_external_routes_atomically(rooms):
    manager, first, second = rooms
    client, calls = client_for(rooms)
    manager._set_state(first["room"]["room_id"], "stopped")
    assert client.get("/v1/models", headers=auth(first["gateway_key"])).status_code == 401
    assert client.get("/v1/models", headers=auth(second["gateway_key"])).status_code == 200
    assert len(calls) == 1
    routing_text = manager.gateway_routes.read_text(encoding="utf-8")
    assert first["gateway_key"] not in routing_text
    assert second["gateway_key"] not in routing_text
    # Manifest is gateway-only secrets; there are no user memories or paths.
    data = json.loads(routing_text)
    assert len(data["rooms"]) == 1 and data["rooms"][0]["room_id"] == second["room"]["room_id"]
    assert "database" not in routing_text and "backup" not in routing_text
    if os.name != "nt":
        assert manager.gateway_routes.stat().st_mode & 0o077 == 0


class GatewayFakeDocker:
    def __init__(self):
        self.calls = []
        self.labels = None
        self.running = False

    def __call__(self, command, *, capture_output=False, text=False):
        self.calls.append(command)
        opcode = command[1:3]
        if opcode == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, "[]", "")
        if opcode == ["network", "inspect"]:
            return subprocess.CompletedProcess(command, 1, "", "")
        if opcode == ["container", "inspect"]:
            if self.labels is None:
                return subprocess.CompletedProcess(command, 1, "", "")
            info = [{"Config": {"Labels": self.labels}, "State": {
                "Status": "running" if self.running else "exited", "Running": self.running,
                "Health": {"Status": "healthy" if self.running else "unhealthy"},
            }}]
            return subprocess.CompletedProcess(command, 0, json.dumps(info), "")
        if command[1] == "run":
            labels = [command[i + 1] for i, item in enumerate(command) if item == "--label"]
            self.labels = dict(label.split("=", 1) for label in labels)
            self.running = True
        elif opcode == ["container", "stop"]:
            self.running = False
        elif opcode == ["container", "start"]:
            self.running = True
        return subprocess.CompletedProcess(command, 0, "", "")


def test_gateway_container_has_no_private_data_mount_and_is_loopback_only(rooms):
    manager, _, _ = rooms
    fake = GatewayFakeDocker()
    runtime = DockerRuntime(runner=fake, sleeper=lambda _: None)
    with pytest.raises(RoomError):
        runtime.start_gateway(manager, bind="0.0.0.0")
    assert runtime.start_gateway(manager, port=18999)["container"] == "running"
    run = next(command for command in fake.calls if command[1] == "run")
    mounts = [run[i + 1] for i, entry in enumerate(run) if entry == "--mount"]
    assert len(mounts) == 1 and "/control,readonly" in mounts[0]
    assert all("/rooms/" not in mount for mount in mounts)
    assert run[run.index("--publish") + 1] == "127.0.0.1:18999:8080"
    assert "--read-only" in run and "--cap-drop" in run
    runtime.stop_gateway(manager)
    assert runtime.gateway_status(manager)["container"] == "exited"


def test_actual_serein_core_models_remain_room_scoped(rooms):
    """Proxy two real Serein HTTP apps instead of test-only echo handlers."""
    from serein.api.http import create_app as create_core
    from serein.config import Settings
    from serein.deployment import save_settings

    manager, first, second = rooms
    backends = {}
    for item, label in ((first, "alpha"), (second, "beta")):
        room_id = item["room"]["room_id"]
        db = manager.room_dir(room_id) / "data" / "serein.db"
        save_settings(db, {"upstreams": [{
            "name": label, "base_url": "https://synthetic.invalid/v1",
            "models": [label + "-model"],
        }]})
        internal_key = (manager.room_dir(room_id) / "secrets" /
                        "api-token").read_text(encoding="utf-8").strip()
        settings = Settings(db, index=manager.room_dir(room_id) / "data" /
                            "recall-index.sqlite", writable=True, mcp_tools=[])
        backend = create_core(settings, token=internal_key, live=True)
        backends["room-" + room_id] = httpx.ASGITransport(app=backend)

    class DispatchToRealCore(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.url.host in backends
            return await backends[request.url.host].handle_async_request(request)

        async def aclose(self):
            for transport in backends.values():
                await transport.aclose()

    proxy = TestClient(create_app(manager.gateway_routes,
                                  transport=DispatchToRealCore()))
    models_a = proxy.get("/v1/models", headers=auth(first["gateway_key"]))
    models_b = proxy.get("/v1/models", headers=auth(second["gateway_key"]))
    assert models_a.status_code == models_b.status_code == 200
    assert [item["id"] for item in models_a.json()["data"]] == ["alpha/alpha-model"]
    assert [item["id"] for item in models_b.json()["data"]] == ["beta/beta-model"]
    assert "beta" not in models_a.text and "alpha" not in models_b.text


def test_custom_window_id_can_match_room_id_without_becoming_authentication(tmp_path):
    """Same text may be used for Room ID and Window ID; Key selects room."""
    manager = RoomManager(tmp_path / "rooms")
    a = manager.create("A", room_slug="xiaogi-main", source="blank")
    b = manager.create("B", room_slug="other-main", source="blank")
    manager._set_state(a["room"]["room_id"], "ready")
    manager._set_state(b["room"]["room_id"], "ready")
    calls = []
    client = TestClient(create_app(manager.gateway_routes,
                                   transport=fake_transport(manager, calls)))
    for item, window in ((a, "xiaogi-main"), (b, "other-main")):
        response = client.post("/v1/chat/completions", headers={
            **auth(item["gateway_key"]),
            "X-Serein-Window-ID": window,
            "X-Serein-Room-ID": window,
        }, json={"messages":[{"role":"user","content":"synthetic"}]})
        assert response.status_code == 200, response.text
        assert response.json()["room"] == item["room"]["room_id"]
        assert calls[-1][3]["x-serein-window-id"] == window

    # Neither the room ID nor the window ID is a credential.
    denied = client.post("/v1/chat/completions",
        headers={**auth(a["gateway_key"]), "X-Serein-Window-ID": "other-main",
                 "X-Serein-Room-ID": "other-main"},
        json={"messages":[{"role":"user","content":"synthetic"}]})
    assert denied.status_code == 403
    # A's key + B's window header alone is also forbidden, even when
    # X-Serein-Room-ID is omitted: no silent acceptance in A's room.
    denied_window_only = client.post("/v1/chat/completions",
        headers={**auth(a["gateway_key"]), "X-Serein-Window-ID": "other-main"},
        json={"messages":[{"role":"user","content":"synthetic"}]})
    assert denied_window_only.status_code == 403
    denied = client.post("/v1/chat/completions",
        headers={"X-Serein-Window-ID":"other-main"},
        json={"messages":[{"role":"user","content":"synthetic"}]})
    assert denied.status_code == 401
    assert len(calls) == 2
