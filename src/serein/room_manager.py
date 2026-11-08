"""M1 control plane: isolated Serein rooms and Docker lifecycle.

No shared memory database, no exposed per-room ports, and no private VPS access.
The central SQLite database contains room metadata only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import time
import tomllib
from datetime import datetime, timezone
from uuid import uuid4

DEFAULT_IMAGE = "serein-multiroom:dev"
KEY_BYTES = 48
CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
  room_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  state TEXT NOT NULL,
  source_room_id TEXT,
  image TEXT NOT NULL,
  gateway_key_sha256 TEXT NOT NULL UNIQUE,
  last_error TEXT NOT NULL DEFAULT ''
);
"""


class RoomError(RuntimeError):
    pass


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=False)


def _secret_file(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(value + "\n")


def _private_text(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(value)


def _room_toml(mcp_tools: list[str]) -> str:
    # Paths refer to the inside of the container, never to another room.
    return (
        '[storage]\n'
        'database = "/data/serein.db"\n'
        'index = "/data/recall-index.sqlite"\n'
        '[runtime]\n'
        'writable = true\n'
        '[mcp]\n'
        'tools = ' + json.dumps(mcp_tools, ensure_ascii=False) + '\n'
    )


def _safe_clone_settings(source_db: Path, target_db: Path, *, reuse_provider_keys: bool) -> None:
    """Field-level configuration allowlist; never clone private memory or identity."""
    from copy import deepcopy
    from .deployment import (
        DEFAULT_FEATURES, DEFAULT_RESUME, DEFAULT_UPSTREAM, TASKS,
        read_settings, save_settings,
    )

    source = read_settings(source_db)

    def fields(data, keys):
        return {key: deepcopy(value) for key, value in data.items() if key in keys}

    credentials = {"api_key", "api_key_env"}
    connection_fields = {
        "id", "name", "base_url", "api_key", "api_key_env", "protocol",
        "prompt_cache", "prompt_cache_retention", "prompt_cache_ttl",
        "anthropic_version", "anthropic_beta", "default_model",
        "request_timeout_seconds",
    }
    model_fields = {
        "id", "upstream_model", "model", "label", "base_url", "api_key",
        "api_key_env", "protocol", "prompt_cache", "prompt_cache_retention",
        "prompt_cache_ttl", "anthropic_version", "anthropic_beta",
        "request_timeout_seconds", "dimension", "query_instruction",
        "document_instruction", "tokenizer", "reasoning_content_compat",
    }

    def clean_credentials(values):
        if not reuse_provider_keys:
            for name in credentials:
                values.pop(name, None)
        return values

    groups = []
    for group in source["upstreams"]:
        copy = clean_credentials(fields(group, connection_fields))
        copy["models"] = [
            clean_credentials(fields(entry, model_fields)) if isinstance(entry, dict) else entry
            for entry in group.get("models", [])
        ]
        groups.append(copy)
    models = [clean_credentials(fields(model, model_fields)) for model in source["models"]]

    pipeline_fields = {
        "auto_enabled", "execution_mode", "max_input_chars", "max_prompt_chars",
        "timeout_seconds", "event_writer_concurrency", "track_lookback_days",
        "track_candidates_enabled", "track_direct_hours", "track_candidate_limit",
        "router_compact_cards", "joint_review_enabled", "material_review_enabled",
        "round_gate_enabled", "append_protected_enabled",
        "event_history_enabled", "curator_summary_first",
    }
    recall_fields = {
        "direct_threshold", "body_candidate_threshold", "cue_candidate_threshold",
        "direct_pool_limit", "max_cards", "candidate_limit",
        "passages_enabled", "passage_min_chars", "embedding_batch_size",
        "embedding_concurrency", "index_catch_up",
    }
    changes = {
        "upstream": fields(source["upstream"], DEFAULT_UPSTREAM),
        "upstreams": groups,
        "models": models,
        "assignments": fields(source["assignments"], TASKS),
        "features": fields(source["features"], DEFAULT_FEATURES),
        "clock": fields(source["clock"], {"timezone"}),
        "pipeline": fields(source["pipeline"], pipeline_fields),
        "recall": fields(source["recall"], recall_fields),
        "resume": fields(source["resume"], set(DEFAULT_RESUME) - {"selected_ids"}),
    }
    if not reuse_provider_keys:
        changes["upstream"]["api_key"] = ""
    # Intentionally omit identity, dream prompts, tagging catalog, resume
    # selected_ids, any unknown settings and all original evidence/diary tables.
    save_settings(target_db, changes)


class RoomManager:
    """Authoritative room registry; all room IDs are server-generated UUIDs."""

    def __init__(self, root: Path, *, image: str = DEFAULT_IMAGE):
        root = Path(root).expanduser()
        if root.is_symlink():
            raise RoomError("The multi-room root cannot be a symbolic link")
        self.root = root.resolve()
        if self.root == Path(self.root.anchor):
            raise RoomError("Refusing to use a filesystem root for room storage")
        self.image = image
        self.namespace = hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        self.network = "serein-multiroom-" + self.namespace

    @property
    def registry(self) -> Path:
        return self.root / "control" / "rooms.sqlite"

    def initialize(self) -> None:
        if not self.root.exists():
            _private_dir(self.root)
        if not (self.root / "rooms").exists():
            _private_dir(self.root / "rooms")
        if not (self.root / "control").exists():
            _private_dir(self.root / "control")
        with self._connect() as database:
            database.executescript(CONTROL_SCHEMA)

    def _connect(self):
        # The registry contains no memory content, but its path is private.
        self.registry.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(self.registry, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=20000")
        return connection

    def list_rooms(self) -> list[dict]:
        self.initialize()
        with self._connect() as database:
            rows = database.execute(
                "SELECT room_id,name,created_at,state,source_room_id,image,last_error "
                "FROM rooms ORDER BY created_at,room_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, room_id: str) -> dict:
        if not isinstance(room_id, str) or len(room_id) != 32 or any(
                character not in "0123456789abcdef" for character in room_id):
            raise RoomError("Invalid immutable room ID")
        self.initialize()
        with self._connect() as database:
            row = database.execute(
                "SELECT room_id,name,created_at,state,source_room_id,image,last_error "
                "FROM rooms WHERE room_id=?", (room_id,)
            ).fetchone()
        if row is None:
            raise RoomError("Unknown room ID")
        return dict(row)

    def room_dir(self, room_id: str) -> Path:
        self.get(room_id)
        return self.root / "rooms" / room_id

    def _set_state(self, room_id: str, state: str, error: str = "") -> None:
        with self._connect() as database:
            database.execute(
                "UPDATE rooms SET state=?,last_error=? WHERE room_id=?",
                (state, error[:180], room_id),
            )
            database.commit()

    def create(self, name: str, *, source: str = "first",
               reuse_provider_keys: bool = False) -> dict:
        """Create a blank isolated DB or a configuration-only clone.

        'first' selects the oldest existing room, otherwise the explicit ID.
        The new room is stopped until an operator calls start().
        """
        name = str(name).strip()
        if not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name):
            raise RoomError("Room name must contain 1-80 printable characters")
        if source != "first" and source != "blank":
            self.get(source)
        self.initialize()
        room_id = uuid4().hex
        gateway_key = secrets.token_urlsafe(KEY_BYTES)
        internal_token = secrets.token_urlsafe(KEY_BYTES)
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            if source == "first":
                first = database.execute(
                    "SELECT room_id FROM rooms ORDER BY created_at,room_id LIMIT 1"
                ).fetchone()
                source_id = first["room_id"] if first else None
            else:
                source_id = None if source == "blank" else source
            if source_id:
                existing = database.execute(
                    "SELECT state FROM rooms WHERE room_id=?", (source_id,)
                ).fetchone()
                if not existing or existing["state"] in ("creating", "error"):
                    raise RoomError("Source room is incomplete or unavailable")
            database.execute(
                "INSERT INTO rooms(room_id,name,created_at,state,source_room_id,image,gateway_key_sha256) "
                "VALUES (?,?,?,?,?,?,?)",
                (room_id, name, created_at, "creating", source_id, self.image,
                 hashlib.sha256(gateway_key.encode()).hexdigest()),
            )
            database.commit()
        home = self.root / "rooms" / room_id
        try:
            _private_dir(home)
            for directory in ("config", "secrets", "data", "backups"):
                _private_dir(home / directory)
            _secret_file(home / "secrets" / "api-token", internal_token)
            _secret_file(home / "secrets" / "gateway-key", gateway_key)
            mcp_tools: list[str] = []
            if source_id:
                source_home = self.root / "rooms" / source_id
                source_config = source_home / "config" / "config.toml"
                with source_config.open("rb") as file:
                    source_toml = tomllib.load(file)
                mcp_tools = list(source_toml.get("mcp", {}).get("tools", []))
                if not all(isinstance(tool, str) for tool in mcp_tools):
                    raise RoomError("Source MCP tool allowlist is invalid")
            _private_text(home / "config" / "config.toml", _room_toml(mcp_tools))
            from .bootstrap import initialize
            from .config import Settings
            target_db = home / "data" / "serein.db"
            initialize(Settings(target_db, index=home / "data" / "recall-index.sqlite",
                                writable=True, mcp_tools=mcp_tools))
            if source_id:
                _safe_clone_settings(
                    self.root / "rooms" / source_id / "data" / "serein.db",
                    target_db, reuse_provider_keys=reuse_provider_keys,
                )
            self._set_state(room_id, "stopped")
        except Exception as exc:
            # Preserve private data for diagnosis; never register a usable route.
            self._set_state(room_id, "error", type(exc).__name__)
            raise
        return {"room": self.get(room_id), "gateway_key": gateway_key}

    def resolve_gateway_key(self, key: str) -> str | None:
        """For M2: map a verified high-entropy key to one room."""
        if not isinstance(key, str) or not key:
            return None
        digest = hashlib.sha256(key.encode()).hexdigest()
        with self._connect() as database:
            row = database.execute(
                "SELECT room_id FROM rooms WHERE gateway_key_sha256=? AND state='ready'",
                (digest,),
            ).fetchone()
        return row["room_id"] if row else None

    def start(self, room_id: str, runtime: "DockerRuntime", *, wait_seconds: int = 45) -> dict:
        room = self.get(room_id)
        if room["state"] not in ("stopped", "ready"):
            raise RoomError("Only stopped or ready rooms may start")
        try:
            runtime.start(self, room)
            if not runtime.wait_healthy(self, room, timeout=wait_seconds):
                raise RoomError("Room did not pass its Docker health check")
            self._set_state(room_id, "ready")
        except Exception as exc:
            self._set_state(room_id, "error", type(exc).__name__)
            raise
        return self.get(room_id)

    def stop(self, room_id: str, runtime: "DockerRuntime") -> dict:
        room = self.get(room_id)
        if room["state"] not in ("ready", "stopped", "error"):
            raise RoomError("Room is not in a stoppable state")
        runtime.stop(self, room)
        self._set_state(room_id, "stopped")
        return self.get(room_id)
