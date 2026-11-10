"""M3 (phase 1): opt-in, loopback-only administrator UI and room lifecycle API.

This is a *new* control-plane service, separate from the original Serein web
gateway. It does not mount or proxy any room memory, diary, mood or dream data.
It shares the M1 RoomManager business logic with the VPS CLI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import hmac
import os
from importlib.resources import files
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .room_docker import DockerRuntime
from .room_manager import RoomError, RoomManager, _private_dir, _secret_file


class CreateRoom(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=80)
    # The user-chosen room ID (xiaogi-main), separate from the internal UUID.
    # Optional for old automation/API clients; both Dashboard forms require it.
    room_slug: str | None = Field(default=None, min_length=3, max_length=48,
                                   pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
    source: str = Field(default="first", min_length=1, max_length=40)
    reuse_provider_keys: bool = False


class RestoreRoomBackup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirm_room_id: str = Field(min_length=3, max_length=48,
        pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
    expected_sha256: str = Field(min_length=64, max_length=64,
        pattern=r"^[a-f0-9]{64}$")


class McpPermissionChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["original", "custom"]
    tools: list[str] = Field(default_factory=list, max_length=128)


def dashboard_credentials_path(manager: RoomManager) -> Path:
    """Use the original Serein scrypt web-auth.json credential contract."""
    return manager.root / "control" / "secrets" / "web-auth.json"


def save_dashboard_credentials(manager: RoomManager, username: str, password: str) -> Path:
    """Initial setup or explicit reset; never accept a password via CLI arguments."""
    if not isinstance(username, str) or not username.strip() or ":" in username or len(username) > 128:
        raise RoomError("Dashboard username must be 1-128 characters without colon")
    if not isinstance(password, str) or len(password) < 10:
        raise RoomError("Dashboard password needs at least 10 characters")
    manager.initialize()
    directory = manager.root / "control" / "secrets"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    salt = secrets.token_bytes(16)
    data = {
        "username": username,
        "salt": salt.hex(),
        "hash": hashlib.scrypt(password.encode("utf-8"), salt=salt,
                               n=16384, r=8, p=1, dklen=32).hex(),
    }
    path = dashboard_credentials_path(manager)
    if path.is_symlink():
        raise RoomError("Dashboard credential file cannot be a symbolic link")
    temp = directory / ("." + secrets.token_hex(16) + ".tmp")
    try:
        _secret_file(temp, json.dumps(data, ensure_ascii=False))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def admin_key_path(manager: RoomManager) -> Path:
    return manager.root / "control" / "secrets" / "admin-key"


def initialize_admin_key(manager: RoomManager) -> Path:
    """Create a separate administrator secret, never a room's Gateway Key."""
    manager.initialize()
    path = admin_key_path(manager)
    if not path.parent.exists():
        _private_dir(path.parent)
    if path.is_symlink():
        raise RoomError("Administrator key path may not be a symbolic link")
    if not path.exists():
        _secret_file(path, secrets.token_urlsafe(48))
    return path


def read_admin_key(manager: RoomManager) -> str:
    path = admin_key_path(manager)
    if path.is_symlink() or not path.is_file():
        raise RoomError("Missing private administrator key; run admin-init first")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise RoomError("Administrator key must be readable only by its owner")
    key = path.read_text(encoding="utf-8").strip()
    if len(key) < 48:
        raise RoomError("Administrator key is invalid")
    return key


def create_app(manager: RoomManager, *, admin_key: str,
               runtime: DockerRuntime | None = None) -> FastAPI:
    if not isinstance(admin_key, str) or len(admin_key) < 48:
        raise RoomError("Administrator key is required")
    runtime = runtime or DockerRuntime()
    app = FastAPI(title="Serein room control", docs_url=None,
                  redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'"
        )
        return response

    def authorize(authorization: str = Header(default="")) -> None:
        if (len(authorization) > 512 or
                not hmac.compare_digest(authorization, "Bearer " + admin_key)):
            raise HTTPException(401, "Administrator authorization required",
                                headers={"WWW-Authenticate": "Bearer"})

    def check_origin(request: Request) -> None:
        # API writes use explicit Bearer authentication, not cookies.
        # Still reject cross-origin browsers (defense in depth).
        if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            raise HTTPException(403, "Cross-origin administrator request denied")
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                valid = parsed.scheme in ("https", "http") and (
                    parsed.netloc.lower() == request.headers.get("host", "").lower()
                )
            except ValueError:
                valid = False
            if not valid:
                raise HTTPException(403, "Cross-origin administrator request denied")

    def snapshot_room(room: dict):
        result = dict(room)
        # No second room naming setting. The name shown in the switcher is
        # the existing Serein Appearance -> AI name, when customized.
        # New rooms display their chosen room ID until Appearance is edited.
        result["display_name"] = room.get("room_slug") or room["name"]
        try:
            from .deployment import read_settings
            data = manager.room_dir(room["room_id"]) / "data" / "serein.db"
            identity = read_settings(data).get("identity") or {}
            ai_name = identity.get("ai_name")
            if isinstance(ai_name, str) and ai_name.strip() and ai_name.strip() != "AI":
                result["display_name"] = ai_name.strip()
        except (RoomError, OSError, KeyError, ValueError):
            pass
        try:
            result["docker"] = runtime.status(manager, room)
        except (RoomError, OSError):
            result["docker"] = {"container": "unavailable", "health": "unknown"}
        return result

    @app.get("/")
    def homepage():
        html = files("serein").joinpath("rooms_ui.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/rooms-ui.js")
    def ui_js():
        javascript = files("serein").joinpath("rooms_ui.js").read_text(encoding="utf-8")
        return Response(javascript, media_type="text/javascript; charset=utf-8")

    @app.get("/api/rooms", dependencies=[Depends(authorize)])
    def rooms():
        return {"rooms": [snapshot_room(row) for row in manager.list_rooms()]}

    @app.get("/api/rooms/{room_id}", dependencies=[Depends(authorize)])
    def room(room_id: str):
        try:
            return snapshot_room(manager.get(room_id))
        except RoomError:
            raise HTTPException(404, "Unknown room") from None

    @app.post("/api/rooms", dependencies=[Depends(authorize), Depends(check_origin)],
              status_code=201)
    def create_room(body: CreateRoom):
        try:
            created = manager.create(
                body.name, source=body.source, room_slug=body.room_slug,
                reuse_provider_keys=body.reuse_provider_keys,
            )
        except (RoomError, ValueError):
            raise HTTPException(400, "Invalid room name or configuration source") from None
        # Gateway key is shown exactly once. It is never part of list/show API.
        return {"room": created["room"], "gateway_key": created["gateway_key"]}

    @app.get("/api/rooms/{room_id}/backups",
             dependencies=[Depends(authorize)])
    def list_room_backups(room_id: str):
        from .room_archive import room_backups
        try:
            return {"room_id": manager.get(room_id)["room_id"],
                    "items": room_backups(manager, room_id)}
        except RoomError:
            raise HTTPException(404, "Room backups unavailable") from None

    @app.post("/api/rooms/{room_id}/backups",
              dependencies=[Depends(authorize), Depends(check_origin)],
              status_code=201)
    def create_full_room_backup(room_id: str):
        from .room_archive import create_room_backup
        try:
            return create_room_backup(manager, room_id, runtime)
        except (RoomError, OSError):
            raise HTTPException(409, "Room backup failed; check room state and Docker health") from None

    @app.get("/api/rooms/{room_id}/backups/{name}/preview",
             dependencies=[Depends(authorize)])
    def preview_full_room_backup(room_id: str, name: str):
        from .room_archive import preview_room_backup
        try:
            return preview_room_backup(manager, room_id, name)
        except RoomError:
            raise HTTPException(409, "Backup is missing, damaged, or not this room's archive") from None

    @app.get("/api/rooms/{room_id}/backups/{name}/restore-plan",
             dependencies=[Depends(authorize)])
    def restore_plan(room_id: str, name: str):
        from .room_restore import plan_room_restore
        try:
            return plan_room_restore(manager, room_id, name)
        except (RoomError, OSError):
            raise HTTPException(409,
                "Restore preflight failed: wrong room ID, damaged archive, or incompatible database") from None

    @app.post("/api/rooms/{room_id}/backups/{name}/restore",
              dependencies=[Depends(authorize), Depends(check_origin)])
    def restore_archive(room_id: str, name: str, body: RestoreRoomBackup):
        from .room_restore import restore_room_backup
        try:
            return restore_room_backup(manager, room_id, name, runtime,
                                       confirm_slug=body.confirm_room_id,
                                       expected_sha256=body.expected_sha256)
        except (RoomError, OSError):
            raise HTTPException(409,
                "Restore not completed: verify room ID, schema and rollback status") from None

    @app.post("/api/rooms/{room_id}/restore-recover",
              dependencies=[Depends(authorize), Depends(check_origin)])
    def recover_interrupted_restore(room_id: str):
        from .room_restore import recover_room_restore
        try:
            return recover_room_restore(manager, room_id, runtime)
        except (RoomError, OSError):
            raise HTTPException(409,
                "Room recovery requires administrator inspection") from None

    @app.get("/api/rooms/{room_id}/backups/{name}/download",
             dependencies=[Depends(authorize)])
    def download_full_room_backup(room_id: str, name: str):
        from .room_archive import backup_file
        try:
            path = backup_file(manager, room_id, name)
        except RoomError:
            raise HTTPException(404, "Room backup not found") from None
        return FileResponse(path, media_type="application/gzip",
                            filename="serein-" + manager.get(room_id)["room_slug"] + "-" + name,
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/rooms/{room_id}/mcp-tools",
             dependencies=[Depends(authorize)])
    def mcp_permissions(room_id: str):
        try:
            return manager.mcp_permissions(room_id)
        except RoomError:
            raise HTTPException(404, "Unknown room or invalid MCP configuration") from None

    @app.post("/api/rooms/{room_id}/mcp-tools",
              dependencies=[Depends(authorize), Depends(check_origin)])
    def update_mcp_permissions(room_id: str, body: McpPermissionChange):
        if body.mode == "original" and body.tools:
            raise HTTPException(422, "Original mode cannot include custom tools")
        tools = body.tools if body.mode == "custom" else None
        try:
            return manager.update_mcp_permissions(room_id, tools, runtime)
        except RoomError:
            raise HTTPException(409, "MCP tool selection invalid or room restart failed") from None

    @app.post("/api/rooms/{room_id}/start",
              dependencies=[Depends(authorize), Depends(check_origin)])
    def start_room(room_id: str):
        try:
            return snapshot_room(manager.start(room_id, runtime))
        except RoomError:
            raise HTTPException(409, "Room could not start; check local Docker status") from None

    @app.post("/api/rooms/{room_id}/stop",
              dependencies=[Depends(authorize), Depends(check_origin)])
    def stop_room(room_id: str):
        try:
            return snapshot_room(manager.stop(room_id, runtime))
        except RoomError:
            raise HTTPException(409, "Room could not stop; check local Docker status") from None

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve private room administration UI")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18220)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Invalid administrator UI port")
    manager = RoomManager(args.root)
    token = read_admin_key(manager)
    import uvicorn
    # Intentionally never bind 0.0.0.0 until a TLS/authenticated reverse-proxy
    # deployment has been explicitly designed and reviewed.
    uvicorn.run(create_app(manager, admin_key=token), host="127.0.0.1",
                port=args.port, access_log=False)


if __name__ == "__main__":
    main()
