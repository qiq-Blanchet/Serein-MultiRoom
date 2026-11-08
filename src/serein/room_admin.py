"""M3 (phase 1): opt-in, loopback-only administrator UI and room lifecycle API.

This is a *new* control-plane service, separate from the original Serein web
gateway. It does not mount or proxy any room memory, diary, mood or dream data.
It shares the M1 RoomManager business logic with the VPS CLI.
"""
from __future__ import annotations

import argparse
import hmac
import os
from importlib.resources import files
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .room_docker import DockerRuntime
from .room_manager import RoomError, RoomManager, _private_dir, _secret_file


class CreateRoom(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=80)
    source: str = Field(default="first", min_length=1, max_length=40)
    reuse_provider_keys: bool = False


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
                body.name, source=body.source,
                reuse_provider_keys=body.reuse_provider_keys,
            )
        except (RoomError, ValueError):
            raise HTTPException(400, "Invalid room name or configuration source") from None
        # Gateway key is shown exactly once. It is never part of list/show API.
        return {"room": created["room"], "gateway_key": created["gateway_key"]}

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
