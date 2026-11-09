"""Synthetic M3 Docker integration: original web auth and isolated services."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from serein.room_admin import dashboard_credentials_path, initialize_admin_key, save_dashboard_credentials
from serein.room_docker import DockerRuntime
from serein.room_manager import RoomError, RoomManager


class FakeMultiRoomDocker:
    def __init__(self, *, bridge="172.28.0.1"):
        self.calls = []
        self.containers = {}
        self.bridge = bridge

    def __call__(self, args, *, capture_output=False, text=False):
        self.calls.append(args)
        operation = tuple(args[1:3])
        if operation == ("image", "inspect"):
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if operation == ("network", "inspect"):
            return subprocess.CompletedProcess(args, 0, json.dumps([{
                "IPAM": {"Config": [{"Gateway": self.bridge}]}
            }]), "")
        if operation == ("container", "inspect"):
            name = args[3]
            current = self.containers.get(name)
            if current is None:
                return subprocess.CompletedProcess(args, 1, "", "")
            return subprocess.CompletedProcess(args, 0, json.dumps([{
                "Config": {"Labels": current["labels"]},
                "State": {"Running": current["running"],
                          "Status": "running" if current["running"] else "exited",
                          "Health": {"Status": "healthy"}},
            }]), "")
        if args[1] == "run":
            name = args[args.index("--name") + 1]
            labels = [args[i + 1] for i, v in enumerate(args) if v == "--label"]
            self.containers[name] = {
                "running": True,
                "labels": dict(entry.split("=", 1) for entry in labels),
            }
        if operation == ("container", "stop"):
            self.containers[args[-1]]["running"] = False
        if operation == ("container", "start"):
            self.containers[args[-1]]["running"] = True
        return subprocess.CompletedProcess(args, 0, "", "")


def test_custom_dashboard_account_uses_original_scrypt_contract(tmp_path):
    manager = RoomManager(tmp_path / "managed")
    save_dashboard_credentials(manager, "雪的记忆花园", "synthetic-passphrase-1234")
    record = json.loads(dashboard_credentials_path(manager).read_text())
    assert record["username"] == "雪的记忆花园"
    assert record["hash"] == hashlib.scrypt(
        "synthetic-passphrase-1234".encode(), salt=bytes.fromhex(record["salt"]),
        n=16384, r=8, p=1, dklen=32,
    ).hex()
    assert "synthetic-passphrase-1234" not in dashboard_credentials_path(manager).read_text()
    assert dashboard_credentials_path(manager).stat().st_mode & 0o077 == 0
    save_dashboard_credentials(manager, "不再叫admin", "another-synthetic-password")
    assert json.loads(dashboard_credentials_path(manager).read_text())["username"] == "不再叫admin"
    with pytest.raises(RoomError):
        save_dashboard_credentials(manager, "", "synthetic-passphrase-1234")
    with pytest.raises(RoomError):
        save_dashboard_credentials(manager, "invalid:colon", "synthetic-passphrase-1234")
    with pytest.raises(RoomError):
        save_dashboard_credentials(manager, "Valid", "too-short")


def test_admin_service_private_network_bridge_address(tmp_path):
    manager = RoomManager(tmp_path / "rooms")
    fake = FakeMultiRoomDocker()
    runtime = DockerRuntime(runner=fake)
    assert runtime.network_gateway_ip(manager) == "172.28.0.1"
    fake.bridge = "8.8.8.8"
    with pytest.raises(RoomError):
        runtime.network_gateway_ip(manager)


def test_original_dashboard_container_uses_internal_routes_not_room_db_mounts(tmp_path):
    manager = RoomManager(tmp_path / "rooms")
    manager.create("A", source="blank")
    initialize_admin_key(manager)
    save_dashboard_credentials(manager, "my-own-login", "synthetic-password-1234")
    fake = FakeMultiRoomDocker()
    runtime = DockerRuntime(runner=fake)
    with pytest.raises(RoomError):
        runtime.start_dashboard(manager)
    # Start the authenticated M2 gateway first; no room data mounts into it.
    runtime.start_gateway(manager)
    assert runtime.start_dashboard(manager, port=18217)["container"] == "running"
    dashboard_cmd = [
        args for args in fake.calls
        if len(args) >= 2 and args[1] == "run"
        and runtime.dashboard_name(manager) in args
    ][0]
    envs = [dashboard_cmd[i + 1] for i, item in enumerate(dashboard_cmd) if item == "--env"]
    mounts = [dashboard_cmd[i + 1] for i, item in enumerate(dashboard_cmd) if item == "--mount"]
    assert dashboard_cmd[dashboard_cmd.index("--publish") + 1] == "127.0.0.1:18217:8080"
    assert "--add-host" not in dashboard_cmd
    assert "SEREIN_MULTIROOM_ADMIN_URL=http://172.28.0.1:18220" in envs
    assert ("SEREIN_MULTIROOM_CHAT_URL=http://" +
            runtime.gateway_name(manager) + ":8080") in envs
    assert "SEREIN_PUBLIC_ORIGIN=http://127.0.0.1:18217" in envs
    assert "SEREIN_WEB_AUTH_FILE=/control/secrets/web-auth.json" in envs
    assert "SEREIN_MULTIROOM_ROUTES_FILE=/control/gateway/routes.json" in envs
    assert all("dst=/control/" in value and "readonly" in value for value in mounts)
    assert len(mounts) == 2
    assert not any("dst=/data" in value or "rooms/" in value for value in mounts)
    assert "--read-only" in dashboard_cmd and "--cap-drop" in dashboard_cmd
    runtime.stop_dashboard(manager)
    assert runtime.dashboard_status(manager)["container"] == "exited"


def test_public_dashboard_requires_https_and_never_exposes_room_ports(tmp_path):
    manager = RoomManager(tmp_path / "rooms")
    manager.create("A", source="blank")
    initialize_admin_key(manager)
    save_dashboard_credentials(manager, "My Username", "synthetic-password-1234")
    fake = FakeMultiRoomDocker()
    runtime = DockerRuntime(runner=fake)
    runtime.start_gateway(manager)
    with pytest.raises(RoomError):
        runtime.start_dashboard(manager, public_origin="http://public.example.invalid/")
    with pytest.raises(RoomError):
        runtime.start_dashboard(manager, public_origin="https://user:password@example.invalid/")
    result = runtime.start_dashboard(
        manager, port=18227, public_origin="https://serein.example.invalid/"
    )
    assert result["container"] == "running"
    cmds = [item for item in fake.calls if item[1] == "run"]
    assert len(cmds) == 2
    dashboard = cmds[-1]
    assert "SEREIN_PUBLIC_ORIGIN=https://serein.example.invalid/" in dashboard
    assert dashboard[dashboard.index("--publish") + 1] == "127.0.0.1:18227:8080"
