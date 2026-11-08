"""M1 regression tests: synthetic rooms only, never a private installation."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tomllib

import pytest

from serein.deployment import read_settings, save_settings
from serein.room_docker import DockerRuntime
from serein.room_manager import RoomError, RoomManager


def test_room_creation_isolates_paths_keys_and_private_data(tmp_path):
    manager = RoomManager(tmp_path / "host")
    first = manager.create("First", source="blank")
    source = first["room"]["room_id"]
    source_home = manager.room_dir(source)
    db = source_home / "data" / "serein.db"
    assert db.exists() and (source_home / "data" / "recall-index.sqlite").exists()

    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE private_canary(secret TEXT)")
        conn.execute("INSERT INTO private_canary VALUES ('PRIVATE_MEMORY')")
        conn.commit()
    save_settings(db, {
        "upstreams": [{"name": "test", "base_url": "https://example.invalid/v1",
                       "api_key": "SYNTHETIC_PROVIDER_KEY", "models": ["synthetic-model"]}],
        "assignments": {"chat": "synthetic-model"},
        "features": {"memos": True},
        "identity": {"user_name": "PRIVATE_USER", "ai_name": "PRIVATE_AI"},
        "dream": {"main_prompt": "PRIVATE_DREAM_PROMPT"},
    })
    second = manager.create("Second")  # Default: configuration from first room
    target_id = second["room"]["room_id"]
    target_home = manager.room_dir(target_id)
    target_db = target_home / "data" / "serein.db"
    state = read_settings(target_db)
    assert second["room"]["source_room_id"] == source
    assert state["features"]["memos"] is True
    assert state["assignments"]["chat"] == "synthetic-model"
    assert state["upstreams"][0]["models"] == ["synthetic-model"]
    assert not state["upstreams"][0].get("api_key")
    assert state["identity"]["user_name"] == "User"
    assert state["identity"]["ai_name"] == "AI"
    assert not state["dream"]["main_prompt"]
    assert not state["resume"]["selected_ids"]

    with sqlite3.connect(target_db) as conn:
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='private_canary'"
        ).fetchone()
    assert first["gateway_key"] != second["gateway_key"]
    assert not manager.resolve_gateway_key(first["gateway_key"])
    assert not manager.resolve_gateway_key(second["gateway_key"])
    assert source_home != target_home
    assert source_home.joinpath("secrets/api-token").read_text() != (
        target_home / "secrets" / "api-token").read_text()
    if os.name != "nt":
        assert (target_home / "secrets" / "api-token").stat().st_mode & 0o077 == 0
        assert (target_home / "secrets" / "gateway-key").stat().st_mode & 0o077 == 0
    assert all("gateway_key" not in row for row in manager.list_rooms())
    assert "SYNTHETIC_PROVIDER_KEY" not in repr(manager.list_rooms())


def test_clone_credentials_requires_opt_in_and_explicit_blank(tmp_path):
    manager = RoomManager(tmp_path / "rooms-root")
    source = manager.create("Source", source="blank")["room"]["room_id"]
    db = manager.room_dir(source) / "data" / "serein.db"
    save_settings(db, {"upstreams": [{
        "name": "model-provider", "base_url": "https://test.invalid/v1",
        "api_key": "PROVIDER_SECRET", "models": ["ai"],
    }]})
    copied = manager.create("Copied", source=source, reuse_provider_keys=True)
    copied_state = read_settings(manager.room_dir(copied["room"]["room_id"]) / "data" / "serein.db")
    assert copied_state["upstreams"][0]["api_key"] == "PROVIDER_SECRET"
    blank = manager.create("Blank", source="blank")
    blank_state = read_settings(manager.room_dir(blank["room"]["room_id"]) / "data" / "serein.db")
    assert blank_state["upstreams"] == [] and not blank_state["models"]
    assert blank["room"]["source_room_id"] is None


def test_mcp_allowlist_config_is_cloned_without_private_data(tmp_path):
    manager = RoomManager(tmp_path / "rooms-root")
    source = manager.create("Source", source="blank")["room"]["room_id"]
    config = manager.room_dir(source) / "config" / "config.toml"
    config.write_text(config.read_text() + "\n# Synthetic permitted tool\n", encoding="utf-8")
    current = config.read_text()
    config.write_text(current.replace("tools = []", 'tools = ["read_memory"]'), encoding="utf-8")
    target = manager.create("Target")["room"]["room_id"]
    with (manager.room_dir(target) / "config" / "config.toml").open("rb") as file:
        assert tomllib.load(file)["mcp"]["tools"] == ["read_memory"]


def test_reject_unknown_ids_and_paths(tmp_path):
    manager = RoomManager(tmp_path / "rooms-root")
    manager.create("Original", source="blank")
    for invalid in ("../rooms", "../../secrets", "main", "a" * 32):
        with pytest.raises(RoomError):
            manager.get(invalid)
    with pytest.raises(RoomError):
        manager.create("  ")
    with pytest.raises(RoomError):
        manager.create("B", source="../../other")
    with pytest.raises(RoomError):
        RoomManager(Path("/"))


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.container = None
        self.running = False

    def __call__(self, args, *, capture_output=False, text=False):
        assert args[0] == "docker"
        self.calls.append(args)
        opcode = tuple(args[1:3])
        if opcode == ("image", "inspect"):
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if opcode == ("network", "inspect"):
            return subprocess.CompletedProcess(args, 1, "", "")
        if opcode == ("container", "inspect"):
            if self.container is None:
                return subprocess.CompletedProcess(args, 1, "", "")
            return subprocess.CompletedProcess(args, 0, json.dumps([{
                "Config": {"Labels": self.container},
                "State": {"Running": self.running, "Status": "running" if self.running else "exited",
                          "Health": {"Status": "healthy" if self.running else "unhealthy"}}
            }]), "")
        if args[1] == "run":
            labels = [args[index + 1] for index, value in enumerate(args) if value == "--label"]
            self.container = dict(item.split("=", 1) for item in labels)
            self.running = True
        elif opcode == ("container", "stop"):
            self.running = False
        elif opcode == ("container", "start"):
            self.running = True
        return subprocess.CompletedProcess(args, 0, "", "")


def test_docker_runtime_uses_one_private_network_and_no_host_ports(tmp_path):
    manager = RoomManager(tmp_path / "host")
    first = manager.create("A", source="blank")
    target = first["room"]["room_id"]
    docker = FakeDocker()
    runtime = DockerRuntime(runner=docker, sleeper=lambda _: None)
    assert manager.start(target, runtime)["state"] == "ready"
    assert manager.resolve_gateway_key(first["gateway_key"]) == target
    commands = [item for item in docker.calls if item[1] == "run"]
    assert len(commands) == 1
    command = commands[0]
    assert "--publish" not in command and "-p" not in command
    assert "--network" in command and manager.network in command
    assert "--memory" in command and "--cpus" in command
    assert any("dst=/secrets,readonly" in part for part in command)
    assert any("dst=/config/config.toml,readonly" in part for part in command)
    assert any("dst=/data" in part for part in command)
    assert "SEREIN_HTTP_TOKEN_FILE=/secrets/api-token" in command
    assert runtime.status(manager, manager.get(target))["health"] == "healthy"
    manager.stop(target, runtime)
    assert manager.get(target)["state"] == "stopped"
    assert manager.resolve_gateway_key(first["gateway_key"]) is None


def test_cli_can_create_and_list_rooms_without_docker(tmp_path):
    root = tmp_path / "installation"
    script = Path(__file__).resolve().parents[1] / "scripts" / "rooms.py"
    command = [sys.executable, str(script), "--root", str(root)]
    created = subprocess.run([*command, "create", "CLI", "--blank"],
                             capture_output=True, text=True, check=True)
    result = json.loads(created.stdout)
    room_id = result["room"]["room_id"]
    assert result["room"]["state"] == "stopped"
    assert "gateway_key" not in result
    listing = subprocess.run([*command, "list"], capture_output=True, text=True, check=True)
    assert json.loads(listing.stdout)["rooms"][0]["room_id"] == room_id
    assert "gateway-key" not in listing.stdout
