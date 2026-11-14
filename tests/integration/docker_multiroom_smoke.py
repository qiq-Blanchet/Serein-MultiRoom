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
                status, page = request(DASHBOARD + "/", basic=True)
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
