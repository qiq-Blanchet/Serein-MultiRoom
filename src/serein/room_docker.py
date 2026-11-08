"""Docker data-plane for M1: one Serein memory process per isolated room.

No room port is published to the host. The future M2 gateway will attach to
this installation-specific Docker network after authenticating the room key.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

from .room_manager import RoomError

HEALTH_CMD = (
    "python -c \"import urllib.request; "
    "urllib.request.urlopen('http://127.0.0.1:8011/health', timeout=3)\""
)


class DockerRuntime:
    def __init__(self, *, runner=None, sleeper=None, memory="2g", cpus="1.0"):
        self.runner = runner or subprocess.run
        self.sleeper = sleeper or time.sleep
        self.memory = memory
        self.cpus = cpus

    def _docker(self, *arguments, check=True):
        try:
            result = self.runner(["docker", *arguments], capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise RoomError("Docker is not installed or not on PATH") from exc
        if check and result.returncode != 0:
            # Avoid propagating secret-bearing daemon or container logs.
            raise RoomError("Docker operation failed: " + " ".join(arguments[:3]))
        return result

    @staticmethod
    def container_name(manager, room):
        return f"serein-{manager.namespace}-{room['room_id'][:16]}"

    def inspect(self, manager, room):
        name = self.container_name(manager, room)
        result = self._docker("container", "inspect", name, check=False)
        if result.returncode:
            return None
        try:
            data = json.loads(result.stdout)[0]
        except (ValueError, IndexError, TypeError) as exc:
            raise RoomError("Malformed Docker inspect response") from exc
        labels = data.get("Config", {}).get("Labels") or {}
        if (labels.get("serein.multiroom.room") != room["room_id"] or
                labels.get("serein.multiroom.installation") != manager.namespace):
            raise RoomError("Container name is occupied by an unrelated service")
        return data

    def _ensure_network(self, manager):
        inspected = self._docker("network", "inspect", manager.network, check=False)
        if inspected.returncode:
            self._docker("network", "create", "--driver", "bridge", manager.network)

    def build(self, repo_root: Path, image: str):
        repo_root = Path(repo_root).resolve()
        if not (repo_root / "Dockerfile").is_file():
            raise RoomError("Dockerfile not found at the selected project root")
        self._docker("build", "--tag", image, str(repo_root))

    def start(self, manager, room):
        # The shared image must be built explicitly once. Never auto-pull an
        # untrusted registry image or build N duplicate images.
        if self._docker("image", "inspect", room["image"], check=False).returncode:
            raise RoomError("Room image not present: build the shared image first")
        self._ensure_network(manager)
        found = self.inspect(manager, room)
        name = self.container_name(manager, room)
        if found is not None:
            if not found.get("State", {}).get("Running"):
                self._docker("container", "start", name)
            return
        home = manager.room_dir(room["room_id"])
        config = home / "config" / "config.toml"
        secret_dir = home / "secrets"
        data_dir = home / "data"
        for path in (config, secret_dir / "api-token", data_dir):
            if not path.exists() or path.is_symlink():
                raise RoomError("Room runtime path missing or unsafe")
        self._docker(
            "run", "--detach", "--init", "--name", name,
            "--restart", "unless-stopped",
            "--label", "serein.multiroom.room=" + room["room_id"],
            "--label", "serein.multiroom.installation=" + manager.namespace,
            "--network", manager.network,
            "--network-alias", "room-" + room["room_id"],
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", "256",
            "--health-cmd", HEALTH_CMD,
            "--health-interval", "10s",
            "--health-timeout", "5s",
            "--health-retries", "12",
            "--mount", f"type=bind,src={config},dst=/config/config.toml,readonly",
            "--mount", f"type=bind,src={secret_dir},dst=/secrets,readonly",
            "--mount", f"type=bind,src={data_dir},dst=/data",
            "--env", "SEREIN_HTTP_TOKEN_FILE=/secrets/api-token",
            "--entrypoint", "python",
            room["image"], "-m", "serein.launch",
            "--config", "/config/config.toml", "http", "--live",
            "--host", "0.0.0.0", "--port", "8011",
        )

    def status(self, manager, room):
        found = self.inspect(manager, room)
        if not found:
            return {"container": "absent", "health": "unknown"}
        state = found.get("State", {})
        return {
            "container": state.get("Status", "unknown"),
            "health": (state.get("Health") or {}).get("Status", "unknown"),
        }

    def wait_healthy(self, manager, room, *, timeout=45):
        deadline = time.monotonic() + max(0, timeout)
        while True:
            state = self.status(manager, room)
            if state["container"] == "running" and state["health"] == "healthy":
                return True
            if state["container"] in ("exited", "dead"):
                return False
            if time.monotonic() >= deadline:
                return False
            self.sleeper(min(1, max(0, deadline - time.monotonic())))

    def stop(self, manager, room):
        state = self.inspect(manager, room)
        if state and state.get("State", {}).get("Running"):
            self._docker("container", "stop", "--time", "15", self.container_name(manager, room))

    # The M2 gateway is also a Docker service, but it gets ONLY the central
    # routing manifest, never other rooms' private DB/index/backup directories.
    @staticmethod
    def gateway_name(manager):
        return f"serein-gateway-{manager.namespace}"

    def gateway_inspect(self, manager):
        name = self.gateway_name(manager)
        response = self._docker("container", "inspect", name, check=False)
        if response.returncode:
            return None
        try:
            state = json.loads(response.stdout)[0]
        except (ValueError, IndexError, TypeError) as exc:
            raise RoomError("Malformed gateway Docker inspect response") from exc
        labels = state.get("Config", {}).get("Labels") or {}
        if (labels.get("serein.multiroom.gateway") != "true" or
                labels.get("serein.multiroom.installation") != manager.namespace):
            raise RoomError("Gateway container name is occupied by another service")
        return state

    def gateway_status(self, manager):
        state = self.gateway_inspect(manager)
        if state is None:
            return {"container": "absent", "health": "unknown"}
        status = state.get("State") or {}
        return {
            "container": status.get("Status", "unknown"),
            "health": (status.get("Health") or {}).get("Status", "unknown"),
        }

    def start_gateway(self, manager, *, bind="127.0.0.1", port=18219):
        if bind != "127.0.0.1":
            raise RoomError("M2 gateway may only bind to loopback; use a TLS reverse proxy")
        if type(port) is not int or not 1 <= port <= 65535:
            raise RoomError("Invalid gateway port")
        manager.initialize()
        if self._docker("image", "inspect", manager.image, check=False).returncode:
            raise RoomError("Shared image not found; build the multi-room image first")
        self._ensure_network(manager)
        prior = self.gateway_inspect(manager)
        if prior:
            if not prior.get("State", {}).get("Running"):
                self._docker("container", "start", self.gateway_name(manager))
            return self.gateway_status(manager)
        routes_dir = manager.gateway_routes.parent
        if routes_dir.is_symlink() or not manager.gateway_routes.is_file():
            raise RoomError("Gateway route manifest is not available")
        self._docker(
            "run", "--detach", "--init",
            "--name", self.gateway_name(manager),
            "--restart", "unless-stopped",
            "--label", "serein.multiroom.gateway=true",
            "--label", "serein.multiroom.installation=" + manager.namespace,
            "--network", manager.network,
            "--publish", f"{bind}:{port}:8080",
            "--memory", "256m", "--cpus", "0.5", "--pids-limit", "128",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:rw,nosuid,noexec,size=16m",
            "--health-cmd", HEALTH_CMD.replace("8011", "8080"),
            "--health-interval", "10s",
            "--health-timeout", "5s",
            "--health-retries", "12",
            "--mount", f"type=bind,src={routes_dir},dst=/control,readonly",
            "--entrypoint", "python",
            manager.image, "-m", "serein.room_gateway",
            "--routes-file", "/control/routes.json",
            "--host", "0.0.0.0", "--port", "8080",
        )
        return self.gateway_status(manager)

    def stop_gateway(self, manager):
        state = self.gateway_inspect(manager)
        if state and state.get("State", {}).get("Running"):
            self._docker("container", "stop", "--time", "15",
                         self.gateway_name(manager))
        return self.gateway_status(manager)
